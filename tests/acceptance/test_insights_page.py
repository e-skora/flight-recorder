"""The `/insights` page renders `analytics.insights` and computes nothing (D-014 Q1, Q3).

Evidence executed: every rendered number and rate compared to `Insights.as_dict()`
computed on a test connection at the cutoff the response renders (AC-14, D-014
Q1 item 11); the AC-16 engine-to-page mapping, unresolved and awaiting
observations moving coverage only (INV-08, INV-10); a reconstruction failure
listed with its diagnostic intact and kept out of rates, an ambiguous selection
and an empty ledger as named states (INV-09); the display cutoff captured once
per request (Q3); the language block and the authored-copy vocabulary (AC-11,
AC-12, INV-10); replay changing no rendered number and page loads writing
nothing (INV-06, AC-11); the markup's keyboard and text-state structure (§11).

Constructions shared with other test modules are duplicated here, not imported.
"""

import re
from datetime import datetime, timedelta
from fractions import Fraction
from html import unescape
from html.parser import HTMLParser

import pytest
from markupsafe import escape
from sqlalchemy import select

import flight_recorder.web.insights_view as insights_view
from flight_recorder.analytics.insights import (
    KNOWN_FALSE,
    KNOWN_TRUE,
    NOT_APPLICABLE,
    NOT_AVAILABLE_NOTE,
    QUALIFYING_PERIOD_DAYS,
    WORKFLOW_UNDER_COMPARISON,
    Comparison,
    Rate,
    insights,
)
from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    STATUS_DIRECT,
    STATUS_UNRESOLVED,
    effective_attribution,
    effective_outcome_versions,
    ledger_maximum,
)
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import format_utc
from flight_recorder.fixtures import dataset_comparison_workflow_version, dataset_signals
from flight_recorder.ledger.schema import actions, decisions
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import ReconstructionMismatch, reconstruct
from flight_recorder.web.insights_view import difference_text, rate_line
from tests.acceptance.test_decision_detail_page import element, has_element, rows
from tests.conftest import (
    ACCOUNT_REF,
    OUTCOME_EVENT_ID,
    Harness,
    append_unrelated,
    attribute_ledger,
    attribution_rows,
    canonical_by_type,
    captured_statements,
    decision_copy_envelope,
    insert_ambiguous_attribution,
    logic_artifact,
    outcome_v2_envelope,
    post_created,
    seed_and_attribute,
    seed_dataset,
    seed_through_decision,
    small_dataset_config,
    submit_attribution,
)

SIGNALS = dataset_signals()
FUNDED = next(signal for signal in SIGNALS if signal.kind == "rule")
PRESSURE = next(signal for signal in SIGNALS if signal.kind == "context_value")

READY_SECTIONS = (
    "insights-summary",
    "overall-rate",
    "coverage",
    "reconstruction-failures",
    "signals",
    "workflows",
)

SUMMARY_IDS = {
    "insights-cutoff": "cutoff",
    "insights-population": "population",
    "insights-reconstructed": "reconstructed",
}
OVERALL_IDS = {
    "overall-cohort-total": "cohort_total",
    "overall-eligible": "eligible",
    "overall-positives": "positives",
    "overall-excluded-other-period": "excluded_other_period_only",
    "overall-excluded-reconstruction-failed": "excluded_reconstruction_failed",
    "overall-excluded-not-evaluated": "excluded_not_evaluated",
}
COVERAGE_IDS = {
    "coverage-awaiting-attribution": "awaiting_attribution",
    "coverage-unresolved": "unresolved",
    "coverage-direct": "direct",
    "coverage-inferred": "inferred",
    "coverage-open": "open",
    "coverage-closed-known": "closed_known",
    "coverage-closed-unknown": "closed_unknown",
    "coverage-other-period": "other_period",
    "coverage-qualifying": "qualifying_90_day",
    "coverage-total": "total",
}
STANDINGS_IDS = {
    "standings-evaluated": "evaluated",
    "standings-unknown": "unknown",
    "standings-open": "open",
    "standings-unattributed": "unattributed",
    "standings-total": "total",
}
NUMERIC_IDS = (
    *SUMMARY_IDS,
    "insights-reconstruction-failures-count",
    *OVERALL_IDS,
    *COVERAGE_IDS,
    *STANDINGS_IDS,
    "standings-eligible",
)

BANNED_WORDS = (
    "lift",
    "causes",
    "caused",
    "proves",
    "proven",
    "significant",
    "expected",
    "passed",
    "bound",
    "bounds",
    "threshold",
)

#: The elements a `%` may appear in: the rate values and rate rows named in the task's §3.
PERCENT_HOLDERS = re.compile(
    r"overall-rate-value|workflow-comparison-(present|absent)-rate"
    r"|signal-.+-(present|absent)|workflow-row-.+"
)


# --- Assertion helpers --------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []

    def handle_data(self, data):
        self.pieces.append(data)


def visible(fragment: str) -> str:
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    return " ".join("".join(parser.pieces).split())


def text(html: str, element_id: str) -> str:
    """The visible text of `#element_id`, whitespace-normalized."""
    return visible(element(html, element_id))


def number(html: str, element_id: str) -> int:
    value = text(html, element_id)
    if not re.fullmatch(r"\d+", value):
        raise AssertionError(f"#{element_id} holds {value!r}, not an integer")
    return int(value)


class _Fields(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.current: str | None = None
        self.tag: str | None = None
        self.pieces: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("dt", "dd"):
            self.tag, self.pieces = tag, []

    def handle_endtag(self, tag):
        if tag != self.tag:
            return
        content = " ".join("".join(self.pieces).split())
        if tag == "dt":
            self.current = content
            self.fields.setdefault(content, [])
        elif self.current is not None:
            self.fields[self.current].append(content)
        self.tag = None

    def handle_data(self, data):
        if self.tag is not None:
            self.pieces.append(data)


def fields(html: str, block_id: str) -> dict[str, list[str]]:
    """Each `<dt>` label inside `#block_id` mapped to its `<dd>` texts."""
    parser = _Fields()
    parser.feed(element(html, block_id))
    parser.close()
    return parser.fields


class _Authored(HTMLParser):
    """Visible page text with every `<code>` element's content removed, and every
    piece of text holding a `%` outside the named rate holders."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.code_depth = 0
        self.holders: list[bool] = []
        self.pieces: list[str] = []
        self.stray_percent: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "code":
            self.code_depth += 1
        if tag in ("meta", "link", "input", "br"):
            return
        self.holders.append(bool(PERCENT_HOLDERS.fullmatch(dict(attrs).get("id") or "")))

    def handle_endtag(self, tag):
        if tag == "code":
            self.code_depth -= 1
        if self.holders:
            self.holders.pop()

    def handle_data(self, data):
        if "%" in data and not any(self.holders):
            self.stray_percent.append(data)
        if self.code_depth == 0:
            self.pieces.append(data)


def authored(html: str) -> _Authored:
    parser = _Authored()
    parser.feed(html)
    parser.close()
    return parser


def authored_text(html: str) -> str:
    """The page's visible text with every `<code>` element's content removed."""
    return " ".join("".join(authored(html).pieces).split())


def sections(html: str) -> list[str]:
    return re.findall(r'<section id="([^"]+)"', html)


def assert_language(html: str) -> None:
    assert sections(html)[0] == "insights-language"
    language = text(html, "insights-language")
    for phrase in (
        "synthetic",
        "observed",
        "descriptive",
        "not causal",
        "never recorded",
        "no simulation view",
        "demonstration checks",
    ):
        assert phrase in language, phrase
    words = authored_text(html)
    for banned in BANNED_WORDS:
        assert not re.search(rf"\b{banned}\b", words, re.IGNORECASE), banned
    assert authored(html).stray_percent == []
    assert "D-0" not in words
    assert "Phase " not in words
    assert not re.search(r"\bphase\s*\d", words, re.IGNORECASE)


# --- Fixtures and reads ------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("insights-page-shipped"))
    schedule, report = seed_dataset(harness)
    assert report.fresh
    harness.schedule = schedule
    return harness


@pytest.fixture
def small(harness):
    seed_dataset(harness, config=small_dataset_config())
    return harness


def page(harness: Harness) -> str:
    response = harness.client.get("/insights")
    assert response.status_code == 200, response.status_code
    return response.text


def engine_result(harness: Harness, cutoff: int) -> dict:
    with harness.engine.connect() as conn:
        return insights(
            conn,
            cutoff,
            signals=dataset_signals(),
            comparison_workflow_version=dataset_comparison_workflow_version(),
        ).as_dict()


def fraction(stored: dict | None) -> Fraction | None:
    return None if stored is None else Fraction(stored["numerator"], stored["denominator"])


def rate_from(stored: dict) -> Rate:
    """The `Rate` rebuilt from its `as_dict()` integers, its value checked against the dict."""
    rate = Rate(
        cohort_total=stored["cohort_total"],
        eligible=stored["eligible"],
        positives=stored["positives"],
        excluded_other_period_only=stored["excluded_other_period_only"],
        excluded_reconstruction_failed=stored["excluded_reconstruction_failed"],
        excluded_not_evaluated=stored["excluded_not_evaluated"],
    )
    assert rate.value == fraction(stored["value"])
    return rate


def difference_from(comparison: dict, present_name: str, absent_name: str) -> str:
    rebuilt = Comparison(
        present=rate_from(comparison["present"]), absent=rate_from(comparison["absent"])
    )
    assert rebuilt.difference_points == fraction(comparison["difference_points"])
    return difference_text(rebuilt, present_name, absent_name)


def arm_cells(arm: dict) -> list[str]:
    return [
        str(arm["cohort_total"]),
        str(arm["eligible"]),
        str(arm["positives"]),
        rate_line(rate_from(arm)),
        str(arm["excluded_other_period_only"]),
        str(arm["excluded_reconstruction_failed"]),
        str(arm["excluded_not_evaluated"]),
    ]


def comparison_rate_text(arm: dict) -> str:
    line = rate_line(rate_from(arm))
    if arm["cohort_total"] == 0:
        return f"{line} (no decisions recorded under this workflow version)"
    return line


def assert_page_is_engine(html: str, result: dict) -> None:
    """Every number, rate line and difference on the ready page equals `result`."""
    for element_id, key in SUMMARY_IDS.items():
        assert number(html, element_id) == result[key], element_id
    failures = number(html, "insights-reconstruction-failures-count")
    assert failures == len(result["reconstruction_failures"])
    assert failures == result["population"] - result["reconstructed"]
    assert text(html, "insights-window") == f"{QUALIFYING_PERIOD_DAYS} days"
    assert text(html, "insights-comparison-workflow") == dataset_comparison_workflow_version()
    assert (
        text(html, "insights-comparison-workflow")
        == result["workflow_comparison"]["comparison_workflow_version"]
    )

    overall = result["overall"]
    for element_id, key in OVERALL_IDS.items():
        assert number(html, element_id) == overall[key], element_id
    assert text(html, "overall-rate-value") == rate_line(rate_from(overall))
    for element_id, key in COVERAGE_IDS.items():
        assert number(html, element_id) == result["observations"][key], element_id
    for element_id, key in STANDINGS_IDS.items():
        assert number(html, element_id) == result["standings"][key], element_id
    assert number(html, "standings-eligible") == overall["eligible"]

    signal_rows = rows(html, "signals-table")
    assert len(signal_rows) == len(result["signals"])
    for cells, signal in zip(signal_rows, result["signals"], strict=True):
        assert has_element(html, f"signal-row-{signal['id']}")
        assert cells == [
            signal["id"],
            signal["predicate"],
            *(
                str(signal[key])
                for key in (
                    "known_true",
                    "known_false",
                    "unavailable",
                    "absent",
                    "not_applicable",
                    "reconstruction_failed",
                    "input_available",
                    "input_consumed",
                )
            ),
        ], signal["id"]
        assert rows(html, f"signal-{signal['id']}-rules") == [
            [
                match["logic_version"],
                match["artifact_hash"],
                NOT_APPLICABLE if match["rule"] is None else match["rule"],
                str(match["decisions"]),
                str(match["matched"]),
            ]
            for match in signal["historical_rule_matched"]
        ], signal["id"]
        comparison = signal["comparison"]
        assert rows(html, f"signal-{signal['id']}-comparison") == [
            [KNOWN_TRUE, *arm_cells(comparison["present"])],
            [KNOWN_FALSE, *arm_cells(comparison["absent"])],
        ], signal["id"]
        assert text(html, f"signal-{signal['id']}-present").startswith(KNOWN_TRUE)
        assert text(html, f"signal-{signal['id']}-absent").startswith(KNOWN_FALSE)
        assert text(html, f"signal-{signal['id']}-difference") == difference_from(
            comparison, KNOWN_TRUE, KNOWN_FALSE
        )

    workflow_rows = rows(html, "workflows-table")
    assert len(workflow_rows) == len(result["workflows"])
    for cells, workflow in zip(workflow_rows, result["workflows"], strict=True):
        assert has_element(html, f"workflow-row-{workflow['workflow_version']}")
        standings, rate = workflow["standings"], workflow["rate"]
        assert cells == [
            workflow["workflow_version"],
            str(workflow["decisions"]),
            str(standings["evaluated"]),
            str(standings["unknown"]),
            str(standings["open"]),
            str(standings["unattributed"]),
            str(rate["eligible"]),
            str(rate["positives"]),
            rate_line(rate_from(rate)),
            str(rate["excluded_other_period_only"]),
            str(rate["excluded_reconstruction_failed"]),
            str(rate["excluded_not_evaluated"]),
        ], workflow["workflow_version"]

    compared = result["workflow_comparison"]
    assert text(html, "workflow-comparison-version") == compared["workflow_version"]
    assert text(html, "workflow-comparison-against") == compared["comparison_workflow_version"]
    assert text(html, "workflow-comparison-present-rate") == comparison_rate_text(
        compared["comparison"]["present"]
    )
    assert text(html, "workflow-comparison-absent-rate") == comparison_rate_text(
        compared["comparison"]["absent"]
    )
    assert text(html, "workflow-comparison-difference") == difference_from(
        compared["comparison"],
        compared["workflow_version"],
        compared["comparison_workflow_version"],
    )


def rendered(html: str) -> dict:
    """Every rendered number, rate line and difference, keyed by where it sits."""
    snapshot: dict = {element_id: number(html, element_id) for element_id in NUMERIC_IDS}
    snapshot["overall-rate-value"] = text(html, "overall-rate-value")
    for signal in SIGNALS:
        for suffix in ("rules", "comparison"):
            snapshot[f"signal-{signal.id}-{suffix}"] = rows(html, f"signal-{signal.id}-{suffix}")
        snapshot[f"signal-{signal.id}-difference"] = text(html, f"signal-{signal.id}-difference")
    snapshot["signals-table"] = rows(html, "signals-table")
    snapshot["workflows-table"] = rows(html, "workflows-table")
    snapshot["workflow-comparison"] = fields(html, "workflow-comparison")
    return snapshot


# --- Constructions (duplicated from the engine tests) --------------------------------------


def observation(
    event_id, *, opened, days, state="closed", account_ref=ACCOUNT_REF, age_days=10, **payload
):
    opened_at = datetime.fromisoformat(opened)
    closes = opened_at + timedelta(days=days)
    observed = closes if state == "closed" else opened_at + timedelta(days=age_days)
    return outcome_v2_envelope(
        event_id,
        observed_at=format_utc(observed),
        window_opened_at=format_utc(opened_at),
        window_closes_at=format_utc(closes),
        evaluation_state=state,
        account_ref=account_ref,
        **payload,
    )


def build_ambiguous_selection(harness: Harness) -> None:
    """The construction of `test_ambiguous_selection_fails_the_whole_read`."""
    seed_and_attribute(harness)
    opened = canonical_by_type("action.recorded")["occurred_at"]
    post_created(
        harness,
        observation("evt-test-o-other", opened=opened, days=90, state="open", reply=True),
    )
    attribute_ledger(harness)
    other = attribution_rows(harness)[1]
    insert_ambiguous_attribution(harness, other.attribution_event_id)


def mismatched_decision(harness: Harness, event_id: str, **result_fields) -> str:
    """The canonical decision re-recorded with its recorded result altered."""
    envelope = decision_copy_envelope(event_id, boundary="2026-04-18T00:00:00Z")
    envelope["payload"]["result"].update(result_fields)
    post_created(harness, envelope)
    return event_id


def replay_every_decision(harness: Harness) -> None:
    hashes = {version: canonical_hash(logic_artifact(version)) for version in ("v3.2", "v5.1")}
    other = {
        logic_artifact("v3.2")["logic_version"]: hashes["v5.1"],
        logic_artifact("v5.1")["logic_version"]: hashes["v3.2"],
    }
    with harness.engine.connect() as conn:
        found = conn.execute(select(decisions.c.decision_event_id, decisions.c.logic_version)).all()
        assert found
        for row in found:
            replay(conn, row.decision_event_id, other[row.logic_version])  # raises on failure


# --- The shipped seed ----------------------------------------------------------------------


def test_every_rendered_number_is_an_engine_field(seeded):
    html = page(seeded)
    cutoff = number(html, "insights-cutoff")
    with seeded.engine.connect() as conn:
        assert cutoff == ledger_maximum(conn)
    assert_page_is_engine(html, engine_result(seeded, cutoff))


def test_the_shipped_page_shows_every_state_and_the_two_awaiting_outcomes(seeded):
    html = page(seeded)
    result = engine_result(seeded, number(html, "insights-cutoff"))

    awaiting_count = number(html, "coverage-awaiting-attribution")
    assert awaiting_count == result["observations"]["awaiting_attribution"] == 2
    with seeded.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        awaiting = {
            outcome
            for outcome in effective_outcome_versions(conn, cutoff=cutoff)
            if effective_attribution(conn, outcome, POLICY_VERSION, cutoff=cutoff) is None
        }
    assert awaiting == {envelope["event_id"] for envelope in seeded.schedule.stage_2}

    for element_id in (
        "coverage-unresolved",
        "coverage-direct",
        "coverage-inferred",
        "coverage-open",
        "coverage-closed-known",
        "coverage-closed-unknown",
        "coverage-other-period",
    ):
        assert number(html, element_id) >= 1, element_id
    assert has_element(html, "reconstruction-failures-none")
    assert not has_element(html, "reconstruction-failures-table")

    earlier, later = logic_artifact("v3.2"), logic_artifact("v5.1")

    def rule_on(artifact: dict, key: str) -> str | None:
        return next((f["rule"] for f in artifact["factors"] if f["key"] == key), None)

    assert rule_on(earlier, PRESSURE.input_key) is None
    pressure = {cells[0]: cells for cells in rows(html, f"signal-{PRESSURE.id}-rules")}
    assert set(pressure) == {earlier["logic_version"], later["logic_version"]}
    assert pressure[earlier["logic_version"]][2] == NOT_APPLICABLE
    assert pressure[earlier["logic_version"]][4] == "0"
    assert pressure[later["logic_version"]][2] == rule_on(later, PRESSURE.input_key)
    pressure_rules = element(html, f"signal-{PRESSURE.id}-rules")
    assert f'<span class="state-word">{NOT_APPLICABLE}</span>' in pressure_rules

    funded = {cells[0]: cells for cells in rows(html, f"signal-{FUNDED.id}-rules")}
    for artifact in (earlier, later):
        assert funded[artifact["logic_version"]][2] == rule_on(artifact, FUNDED.input_key)

    for signal in (PRESSURE, FUNDED):
        assert "<a " not in element(html, f"signal-{signal.id}-rules")
        for cells in rows(html, f"signal-{signal.id}-rules"):
            assert "HIGH" not in cells[2], cells


def test_the_language_block_precedes_every_number_and_says_what_d014_requires(seeded):
    html = page(seeded)
    assert_language(html)
    assert html.index('id="insights-language"') < html.index('id="insights-cutoff"')


def test_the_shipped_seed_facts_the_page_relies_on(seeded, small):
    """The coordinator's executed probe values at `328b4aa` (2026-09-15)."""
    html = page(seeded)
    assert number(html, "insights-population") == 289
    assert number(html, "coverage-awaiting-attribution") == 2
    assert text(seeded.client.get("/").text, "account-count") == "Showing 241 of 241 accounts"

    html = page(small)
    assert number(html, "insights-population") == 20
    assert number(html, "coverage-awaiting-attribution") == 2
    assert number(html, "coverage-unresolved") == 3
    assert text(small.client.get("/").text, "account-count") == "Showing 17 of 17 accounts"


def test_the_page_is_keyboard_usable_and_states_are_text(seeded):
    html = page(seeded)

    assert "onclick" not in html
    assert 'role="button"' not in html
    assert "<script" not in html
    assert html.count('tabindex="') == 1
    assert 'tabindex="-1"' in html

    assert html.count("<table") == html.count("<caption>")
    headers = re.findall(r"<th(?=[\s>])[^>]*>", html)
    assert headers
    for header in headers:
        assert 'scope="col"' in header, header

    nav = re.search(r'<nav class="site-nav" aria-label="Site">(.*?)</nav>', html, re.DOTALL)
    assert nav is not None
    assert 'href="/"' in nav.group(1) and 'href="/insights"' in nav.group(1)

    for signal in SIGNALS:
        comparison = element(html, f"signal-{signal.id}-comparison")
        for word in (KNOWN_TRUE, KNOWN_FALSE):
            assert f'<span class="state-word">{word}</span>' in comparison, word
        assert [cells[0] for cells in rows(html, f"signal-{signal.id}-comparison")] == [
            KNOWN_TRUE,
            KNOWN_FALSE,
        ]
        rules_table = element(html, f"signal-{signal.id}-rules")
        assert rules_table.count(NOT_APPLICABLE) == rules_table.count(
            f'<span class="state-word">{NOT_APPLICABLE}</span>'
        )

    assert sections(html) == ["insights-language", *READY_SECTIONS]
    for section_id in sections(html):
        assert "<section" not in element(html, section_id), section_id


# --- AC-16 on the page (INV-08, INV-10) ---------------------------------------------------------


def test_unresolved_and_awaiting_observations_change_coverage_only_on_the_page(small):
    """The page half of AC-16. Engine half, on `main` at `b75da53`:
    `test_insights_engine.py::test_unresolved_positive_observations_never_enter_decision_metrics`."""
    harness = small
    with harness.engine.connect() as conn:
        found = conn.execute(
            select(actions.c.account_ref, actions.c.occurred_at).order_by(actions.c.account_ref)
        ).all()
    by_account: dict[str, list[str]] = {}
    for row in found:
        by_account.setdefault(row.account_ref, []).append(row.occurred_at)
    singles = [(account, times[0]) for account, times in by_account.items() if len(times) == 1][:3]
    assert len(singles) == 3

    before = rendered(page(harness))

    # Each window opens 100 days after the account's only action, so the observation
    # instant is 190 days after it: outside the lookback, with no reference.
    posted = []
    for position, (account, acted) in enumerate(singles):
        outcome = f"evt-test-o-late-{position}"
        opened = format_utc(datetime.fromisoformat(acted) + timedelta(days=100))
        post_created(
            harness,
            observation(outcome, opened=opened, days=90, account_ref=account, opportunity=True),
        )
        submit_attribution(harness, outcome)
        posted.append(outcome)
    stored = {row.outcome_event_id: row.status for row in attribution_rows(harness)}
    assert [stored[outcome] for outcome in posted] == [STATUS_UNRESOLVED] * 3

    after_html = page(harness)
    assert_page_is_engine(after_html, engine_result(harness, number(after_html, "insights-cutoff")))
    after = rendered(after_html)
    assert after["coverage-unresolved"] == before["coverage-unresolved"] + 3
    assert after["coverage-total"] == before["coverage-total"] + 3
    assert after["coverage-closed-known"] == before["coverage-closed-known"] + 3
    assert after["insights-cutoff"] > before["insights-cutoff"]
    moved = {"coverage-unresolved", "coverage-total", "coverage-closed-known", "insights-cutoff"}
    assert {k: v for k, v in after.items() if k not in moved} == {
        k: v for k, v in before.items() if k not in moved
    }

    account, acted = singles[0]
    opened = format_utc(datetime.fromisoformat(acted) + timedelta(days=110))
    post_created(
        harness,
        observation(
            "evt-test-o-late-awaiting",
            opened=opened,
            days=90,
            account_ref=account,
            opportunity=True,
        ),
    )
    awaiting = rendered(page(harness))
    assert awaiting["coverage-awaiting-attribution"] == after["coverage-awaiting-attribution"] + 1
    assert awaiting["coverage-total"] == after["coverage-total"] + 1
    assert awaiting["coverage-closed-known"] == after["coverage-closed-known"] + 1
    assert awaiting["insights-cutoff"] > after["insights-cutoff"]
    moved = {
        "coverage-awaiting-attribution",
        "coverage-total",
        "coverage-closed-known",
        "insights-cutoff",
    }
    assert {k: v for k, v in awaiting.items() if k not in moved} == {
        k: v for k, v in after.items() if k not in moved
    }


# --- Failure, unavailable and empty states (INV-09) --------------------------------------------


def test_a_diagnostic_containing_a_banned_word_is_shown_intact(harness):
    seed_through_decision(harness)
    failing = mismatched_decision(harness, "evt-test-d-threshold", threshold=74)
    with harness.engine.connect() as conn:
        with pytest.raises(ReconstructionMismatch) as raised:
            reconstruct(conn, failing)
    detail = str(raised.value)
    assert re.search(r"\bthreshold\b", detail)

    html = page(harness)
    assert rows(html, "reconstruction-failures-table") == [
        [failing, ReconstructionMismatch.__name__, detail]
    ]
    table = element(html, "reconstruction-failures-table")
    assert f'<code class="diagnostic">{escape(detail)}</code>' in table
    assert f'<code class="diagnostic">{ReconstructionMismatch.__name__}</code>' in table
    assert number(html, "insights-reconstruction-failures-count") == 1
    assert number(html, "overall-eligible") == 0
    assert text(html, "overall-rate-value") == NOT_AVAILABLE_NOTE
    assert_language(html)


def test_a_failed_reconstruction_is_listed_and_kept_out_of_rates(harness):
    seed_through_decision(harness)
    failing = mismatched_decision(harness, "evt-test-d-failing", score=85)
    post_created(
        harness,
        observation(
            "evt-test-o-failing",
            opened="2026-04-18T00:00:00Z",
            days=90,
            opportunity=True,
            source_decision_event_id=failing,
        ),
    )
    submit_attribution(harness, "evt-test-o-failing")
    assert {row.outcome_event_id: row.status for row in attribution_rows(harness)} == {
        "evt-test-o-failing": STATUS_DIRECT
    }

    html = page(harness)
    assert_page_is_engine(html, engine_result(harness, number(html, "insights-cutoff")))
    assert number(html, "insights-population") == 2
    assert number(html, "insights-reconstructed") == 1
    assert number(html, "insights-reconstruction-failures-count") == 1
    ((decision_cell, error_cell, _),) = rows(html, "reconstruction-failures-table")
    assert decision_cell == failing
    assert error_cell == ReconstructionMismatch.__name__
    assert (
        f'<a href="/accounts/{ACCOUNT_REF}/decisions/{failing}"><code>{failing}</code></a>'
        in element(html, "reconstruction-failures-table")
    )
    assert number(html, "standings-evaluated") == 1
    assert number(html, "overall-eligible") == 0
    assert text(html, "overall-rate-value") == NOT_AVAILABLE_NOTE
    assert number(html, "overall-excluded-reconstruction-failed") == 1
    for cells in rows(html, "signals-table"):
        assert cells[7] == "1", cells[0]


def test_an_unavailable_rate_and_a_missing_comparison_cohort_render_without_numbers(harness):
    seed_through_decision(harness)
    html = page(harness)
    result = engine_result(harness, number(html, "insights-cutoff"))
    assert_page_is_engine(html, result)
    against = dataset_comparison_workflow_version()
    recorded_workflow = canonical_by_type("decision.recorded")["payload"]["workflow_version"]
    assert recorded_workflow == WORKFLOW_UNDER_COMPARISON

    (workflow_cells,) = rows(html, "workflows-table")
    assert workflow_cells[0] == WORKFLOW_UNDER_COMPARISON
    assert workflow_cells[8] == NOT_AVAILABLE_NOTE
    assert (workflow_cells[6], workflow_cells[7]) == ("0", "0")
    assert text(html, "workflow-comparison-present-rate") == NOT_AVAILABLE_NOTE
    assert text(html, "workflow-comparison-absent-rate") == (
        f"{NOT_AVAILABLE_NOTE} (no decisions recorded under this workflow version)"
    )
    assert result["workflow_comparison"]["comparison"]["absent"]["cohort_total"] == 0
    assert text(html, "workflow-comparison-difference") == (
        f"no comparison: {WORKFLOW_UNDER_COMPARISON} and {against} have 0 eligible decisions"
    )

    for signal in result["signals"]:
        assert text(html, f"signal-{signal['id']}-difference") == (
            f"no comparison: {KNOWN_TRUE} and {KNOWN_FALSE} have 0 eligible decisions"
        )
        arms = (signal["comparison"]["present"], signal["comparison"]["absent"])
        for cells, arm in zip(rows(html, f"signal-{signal['id']}-comparison"), arms, strict=True):
            assert arm["eligible"] == 0
            assert cells[4] == NOT_AVAILABLE_NOTE
        for region in (f"signal-{signal['id']}-comparison", f"signal-{signal['id']}-difference"):
            assert "%" not in text(html, region), region
            assert "percentage points" not in text(html, region), region
    assert "%" not in text(html, "workflows")
    assert "percentage points" not in text(html, "workflows")
    assert text(html, "overall-rate-value") == NOT_AVAILABLE_NOTE


def test_an_ambiguous_selection_renders_a_named_failure_and_no_number(harness):
    build_ambiguous_selection(harness)
    html = page(harness)
    with harness.engine.connect() as conn:
        maximum = ledger_maximum(conn)

    failure = element(html, "insights-failure")
    assert f"Insights could not be computed at ledger cutoff {maximum}:" in visible(failure)
    codes = [unescape(code) for code in re.findall(r"<code[^>]*>(.*?)</code>", failure)]
    assert any(OUTCOME_EVENT_ID in code for code in codes)
    assert sections(html) == ["insights-language", "insights-failure"]
    for section_id in READY_SECTIONS:
        assert not has_element(html, section_id), section_id
    assert not has_element(html, "insights-empty")
    assert_language(html)


def test_an_empty_ledger_renders_the_empty_state(harness):
    html = page(harness)
    assert "seed-dataset" in text(html, "insights-empty")
    assert sections(html) == ["insights-language", "insights-empty"]
    for section_id in READY_SECTIONS:
        assert not has_element(html, section_id), section_id
    for element_id in NUMERIC_IDS:
        assert not has_element(html, element_id), element_id
    assert_language(html)


# --- The cutoff (D-014 Q3) ----------------------------------------------------------------------


def test_separate_requests_refresh_the_cutoff(small):
    first = page(small)
    with small.engine.connect() as conn:
        assert number(first, "insights-cutoff") == ledger_maximum(conn)
    append_unrelated(small, 2)
    second = page(small)
    assert number(second, "insights-cutoff") == number(first, "insights-cutoff") + 2
    assert number(second, "insights-population") == number(first, "insights-population")


def test_the_display_cutoff_is_captured_once_per_request(small, monkeypatch):
    captures: list[tuple[object, int | None]] = []
    engine_calls: list[tuple[object, int]] = []
    real_maximum = insights_view.ledger_maximum
    real_insights = insights_view.insights

    def maximum_spy(conn):
        value = real_maximum(conn)
        captures.append((conn, value))
        return value

    def insights_spy(conn, cutoff, **kwargs):
        engine_calls.append((conn, cutoff))
        return real_insights(conn, cutoff, **kwargs)

    monkeypatch.setattr(insights_view, "ledger_maximum", maximum_spy)
    monkeypatch.setattr(insights_view, "insights", insights_spy)

    html = page(small)
    assert len(captures) == 1
    assert len(engine_calls) == 1
    (captured_conn, captured), (engine_conn, engine_cutoff) = captures[0], engine_calls[0]
    assert engine_cutoff == captured
    assert engine_conn is captured_conn
    assert number(html, "insights-cutoff") == captured
    assert_page_is_engine(html, engine_result(small, captured))


# --- Read-only (INV-06, AC-11) ----------------------------------------------------------------


def test_replaying_every_decision_changes_no_rendered_number(small):
    before = rendered(page(small))
    replay_every_decision(small)
    assert rendered(page(small)) == before


def test_the_page_writes_nothing(small, tmp_path):
    def assert_read_only(harness: Harness, url: str) -> None:
        before = harness.snapshot()
        with captured_statements(harness.app.state.engine) as statements:
            assert harness.client.get(url).status_code == 200
        # Every connection opens with the engine's explicit `BEGIN` (`ledger/database.py`).
        assert [s for s in statements if s.strip().upper().startswith("SELECT")]
        for statement in statements:
            assert statement.strip().upper().startswith(("SELECT", "BEGIN")), statement
        assert harness.snapshot() == before

    assert_read_only(small, "/insights")
    assert_read_only(small, "/?q=nova")

    failing = Harness(tmp_path)
    build_ambiguous_selection(failing)
    assert has_element(page(failing), "insights-failure")
    assert_read_only(failing, "/insights")
