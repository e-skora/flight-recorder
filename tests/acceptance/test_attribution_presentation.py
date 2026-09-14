"""D-013 Q3 and Q4, INV-08, INV-09: the persisted attribution, fully inspectable.

Below the outcomes table, every outcome version gets a detail block: its
standing as a version, its window, each observation qualified by what a
recorded `no` means for that version, the source-provided claim or recorded
reference, and -- when a result exists -- the attribution status, the heuristic
label, the policy-resolved references and whether the resolved decision is the
one on the page, each under its own label. Every persisted result for the
version is listed in an attribution history with its own stored values and its
standing, so a replacement that changed only the resolved action or only the
reason is visible as a change.

The table's Attribution cell stays a summary whose leading text is one of the
five prefixes the eight conditions are read from. The outcome section is read
at one display cutoff, so nothing appended after it leaks into a page load or
shows up as a failure. The trace's attribution rows link to the credited
decision at the outcome's block, and an unresolved result links nowhere.

Every id, value and stored field is read from the fixtures, the conftest
helpers or the ledger; states are asserted as text, never by class alone.
"""

import re
from html.parser import HTMLParser

from flight_recorder.attribution import policy
from flight_recorder.attribution.policy import ledger_maximum
from flight_recorder.web.decision_view import _outcome_rows
from tests.acceptance.test_decision_detail_page import element, has_element, rows
from tests.conftest import (
    ACCOUNT_REF,
    ACTION_EVENT_ID,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    FixedClock,
    Harness,
    action_envelope,
    attribute_ledger,
    attribution_rows,
    canonical_by_type,
    decision_copy_envelope,
    insert_ambiguous_attribution,
    outcome_row,
    outcome_v2_envelope,
    post_created,
    seed_all,
    seed_and_attribute,
    seed_through_decision,
)

OPENED = canonical_by_type("action.recorded")["occurred_at"]
CLOSES = canonical_by_type("outcome.evaluated")["occurred_at"]
CANONICAL_WINDOW_DAYS = canonical_by_type("outcome.evaluated")["payload"]["window_days"]
ACCOUNT_NAME = canonical_by_type("account.discovered")["payload"]["name"]
TRACE_URL = f"/accounts/{ACCOUNT_REF}"

FIRST_ACTION = "evt-test-action-first"
LATER_ACTION = "evt-test-action-later"
CLAIMED_ACTION = "evt-test-action-claimed"
WATCHED = "evt-test-o-watched"
CLOSING = "evt-test-o-closing"
SECOND_DECISION = "evt-test-decision-second"

THIS_PAGE = "(the decision on this page)"
NOT_THIS_PAGE = "(not the decision on this page)"
NOT_YET_EVALUATED = "Attribution not yet evaluated."
COULD_NOT_BE_ESTABLISHED = "Attribution could not be established"
HEURISTIC_STATUS = f"{policy.STATUS_INFERRED} heuristic"
OBSERVATIONS = ("reply", "meeting", "opportunity")
ALL_FALSE = {"reply": False, "meeting": False, "opportunity": False}
REPLIED = {"reply": True, "meeting": False, "opportunity": False}

#: The columns of an attribution history table, by position.
EVENT, STATUS, ACTION, DECISION, REASON, CUTOFF, ATTRIBUTED_AT, STANDING, MORE = range(9)


# --- Assertion helpers ------------------------------------------------------------------


class _Fields(HTMLParser):
    """The `<dt>` / `<dd>` pairs of a region's top-level `<dl>`, as visible text.

    A `<dl>` nested inside a `<dd>` (the history table's `<details>`) is part
    of that `<dd>`'s text and contributes no pairs of its own.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.fields: dict[str, list[str]] = {}
        self.label: str | None = None
        self.open_tag: str | None = None
        self.pieces: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "dl":
            self.depth += 1
        elif self.depth == 1 and tag in ("dt", "dd"):
            self.open_tag, self.pieces = tag, []

    def handle_endtag(self, tag):
        if tag == "dl":
            self.depth -= 1
        elif self.depth == 1 and tag == self.open_tag:
            text = " ".join("".join(self.pieces).split())
            if tag == "dt":
                assert text not in self.fields, f"duplicate label {text!r}"
                self.label = text
                self.fields[text] = []
            else:
                self.fields[self.label].append(text)
            self.open_tag = None

    def handle_data(self, data):
        if self.open_tag is not None:
            self.pieces.append(data)


def _pairs(fragment: str) -> dict[str, list[str]]:
    parser = _Fields()
    parser.feed(fragment)
    parser.close()
    return parser.fields


def fields(html: str, block_id: str) -> dict[str, list[str]]:
    """Each `<dt>` label of `#block_id` mapped to its `<dd>` texts, in order."""
    return _pairs(element(html, block_id))


def history_id(outcome_event_id: str) -> str:
    return f"attribution-history-{outcome_event_id}"


def history_more(html: str, outcome_event_id: str) -> list[dict[str, list[str]]]:
    """The fields inside each history row's `<details>`, in row order."""
    table = element(html, history_id(outcome_event_id))
    return [
        _pairs(body)
        for body in re.findall(r'<details class="history-more">(.*?)</details>', table, re.S)
    ]


def caption(html: str, table_id: str) -> str:
    (text,) = re.findall(r"<caption>(.*?)</caption>", element(html, table_id), re.S)
    return " ".join(text.split())


def text_of(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def decision_page(harness: Harness, decision_event_id: str = DECISION_EVENT_ID) -> str:
    response = harness.client.get(f"/accounts/{ACCOUNT_REF}/decisions/{decision_event_id}")
    assert response.status_code == 200, response.status_code
    return response.text


def attribution_trace_rows(harness: Harness) -> list[str]:
    """The inner HTML of every `ATTRIBUTION` row of the account trace."""
    response = harness.client.get(TRACE_URL)
    assert response.status_code == 200
    return re.findall(r'<tr class="kind-attribution">(.*?)</tr>', response.text, re.S)


def cell_row(html: str, pick) -> list[str]:
    (row,) = [row for row in rows(html, "outcomes-table") if pick(row)]
    return row


def is_v1(row: list[str]) -> bool:
    return not row[0].startswith("window ")


def is_open(row: list[str]) -> bool:
    return row[0].startswith("window open")


def stored_for(harness: Harness, outcome_event_id: str) -> list:
    return [row for row in attribution_rows(harness) if row.outcome_event_id == outcome_event_id]


# --- Local constructions -----------------------------------------------------------------


def open_observation(event_id: str, observed_at: str = "2026-05-01T00:00:00Z", **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at=observed_at,
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state="open",
        **payload,
    )


def watched_observation(**claims) -> dict:
    return outcome_v2_envelope(
        WATCHED,
        observed_at="2026-05-01T00:00:00Z",
        window_opened_at="2026-04-17T10:07:00Z",
        window_closes_at="2026-07-16T10:07:00Z",
        evaluation_state="open",
        **claims,
    )


def watched_ledger(harness: Harness, **claims) -> FixedClock:
    """One eligible action and one observation, attributed once."""
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FIRST_ACTION, occurred_at="2026-04-18T09:00:00Z"),
        watched_observation(**claims),
    )
    clock = FixedClock()
    run = attribute_ledger(harness, clock=clock)
    assert [s.http_status for s in run.submissions] == [201]
    return clock


def replace_with_later_action(harness: Harness, clock: FixedClock) -> None:
    """A later eligible action, then a reevaluation: an action-only replacement."""
    post_created(
        harness,
        action_envelope(
            LATER_ACTION, occurred_at="2026-04-25T09:00:00Z", recorded_at="2026-09-01T00:00:00Z"
        ),
    )
    clock.advance(days=1)
    run = attribute_ledger(harness, reevaluate=True, clock=clock)
    assert [s.http_status for s in run.submissions] == [201]


def closing_version() -> dict:
    return outcome_v2_envelope(
        CLOSING,
        observed_at=CLOSES,
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state="closed",
        supersedes_outcome_event_id=OUTCOME_EVENT_ID,
        **ALL_FALSE,
    )


def three_outcomes(harness: Harness) -> None:
    """The canonical outcome, an attributed open observation, and an unattributed one."""
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    post_created(harness, open_observation("evt-test-o-inferred", meeting=False))
    attribute_ledger(harness)
    post_created(
        harness,
        open_observation(
            "evt-test-o-unattributed",
            observed_at="2026-05-02T00:00:00Z",
            source_decision_event_id=DECISION_EVENT_ID,
        ),
    )


def selection_failure(harness: Harness) -> None:
    """A second effective result for the canonical outcome, inserted around the collector."""
    seed_and_attribute(harness)
    post_created(
        harness,
        open_observation("evt-test-o-other", observed_at="2026-06-01T00:00:00Z", **REPLIED),
    )
    attribute_ledger(harness)
    (other,) = stored_for(harness, "evt-test-o-other")
    insert_ambiguous_attribution(harness, other.attribution_event_id)


# --- The canonical outcome ------------------------------------------------------------------


def test_the_canonical_outcome_detail_block_names_every_field(harness):
    seed_and_attribute(harness)
    (stored,) = attribution_rows(harness)
    html = decision_page(harness)

    assert has_element(html, f"outcome-{OUTCOME_EVENT_ID}")
    block = fields(html, f"outcome-{OUTCOME_EVENT_ID}")
    assert list(block) == [
        "Outcome version",
        "Evaluation window",
        "Observations",
        "Recorded reference",
        "Attribution status",
        "Heuristic",
        "Policy-resolved references",
        "Policy",
        "Method",
        "Attribution window",
        "Reason",
        "Attributed at",
        "Ledger cutoff",
        "Attribution event",
        "Attribution history",
    ]
    assert block["Outcome version"] == ["effective version"]
    (window,) = block["Evaluation window"]
    assert window.startswith(f"{CANONICAL_WINDOW_DAYS} days, recorded as a length only")
    assert len(block["Observations"]) == 3
    for word, line in zip(OBSERVATIONS, block["Observations"], strict=True):
        assert line.startswith(f"{word}: no")
        assert line.endswith("(recorded negative observation)")
    reference = block["Recorded reference"]
    assert any(ACTION_EVENT_ID in line for line in reference[:-1])
    assert reference[-1] == "a recorded reference validated at ingest, not an attribution"

    assert block["Attribution status"] == [stored.status] == [policy.STATUS_DIRECT]
    assert block["Heuristic"] == ["no"]
    assert block["Policy-resolved references"] == [
        f"action {ACTION_EVENT_ID}",
        f"decision {DECISION_EVENT_ID} {THIS_PAGE}",
    ]
    assert (stored.resolved_action_event_id, stored.resolved_decision_event_id) == (
        ACTION_EVENT_ID,
        DECISION_EVENT_ID,
    )
    assert block["Policy"] == [stored.policy_version] == [policy.POLICY_VERSION]
    assert block["Method"] == [stored.method] == [policy.METHOD_EXPLICIT_REFERENCE]
    assert block["Attribution window"] == [f"{stored.window_days} days"]
    assert stored.window_days == policy.LOOKBACK_DAYS
    assert block["Reason"] == [stored.reason] == [policy.VALID_SOURCE_ACTION]
    assert block["Attributed at"] == [stored.attributed_at]
    assert block["Ledger cutoff"] == [str(stored.ingest_cutoff)]
    assert block["Attribution event"] == [stored.attribution_event_id]

    (row,) = rows(html, "outcomes-table")
    assert row[5].startswith(policy.STATUS_DIRECT)
    assert f"policy-resolved decision {DECISION_EVENT_ID} {THIS_PAGE}" in row[5]
    for moved in ("policy ", "method ", "attributed at"):
        assert moved not in row[5], moved
    assert f'<tr id="outcome-row-{OUTCOME_EVENT_ID}">' in element(html, "outcomes-table")


def test_the_history_of_a_single_result_is_one_effective_row(harness):
    seed_and_attribute(harness)
    (stored,) = attribution_rows(harness)
    html = decision_page(harness)

    (row,) = rows(html, history_id(OUTCOME_EVENT_ID))
    assert row[EVENT] == stored.attribution_event_id
    assert row[STATUS] == stored.status == policy.STATUS_DIRECT
    assert row[ACTION] == ACTION_EVENT_ID
    assert row[DECISION] == f"{DECISION_EVENT_ID} {THIS_PAGE}"
    assert row[REASON] == stored.reason
    assert row[CUTOFF] == str(stored.ingest_cutoff)
    assert row[ATTRIBUTED_AT] == stored.attributed_at
    assert row[STANDING] == "effective"
    assert row[MORE].startswith("policy, method, window")

    (more,) = history_more(html, OUTCOME_EVENT_ID)
    assert more == {
        "Policy": [policy.POLICY_VERSION],
        "Method": [policy.METHOD_EXPLICIT_REFERENCE],
        "Attribution window": [f"{stored.window_days} days"],
        "Supersedes": ["none"],
    }


# --- Attribution history: replacements ------------------------------------------------------


def test_a_superseded_attribution_result_is_listed_as_history_and_the_effective_one_is_marked(
    harness,
):
    clock = watched_ledger(harness)
    replace_with_later_action(harness, clock)
    first, second = attribution_rows(harness)
    assert second.supersedes_attribution_event_id == first.attribution_event_id
    html = decision_page(harness)

    earlier, later = rows(html, history_id(WATCHED))
    assert earlier[:MORE] == [
        first.attribution_event_id,
        HEURISTIC_STATUS,
        FIRST_ACTION,
        f"{DECISION_EVENT_ID} {THIS_PAGE}",
        first.reason,
        str(first.ingest_cutoff),
        first.attributed_at,
        f"superseded by {second.attribution_event_id}",
    ]
    assert later[:MORE] == [
        second.attribution_event_id,
        HEURISTIC_STATUS,
        LATER_ACTION,
        f"{DECISION_EVENT_ID} {THIS_PAGE}",
        second.reason,
        str(second.ingest_cutoff),
        second.attributed_at,
        "effective",
    ]
    assert (first.resolved_action_event_id, second.resolved_action_event_id) == (
        FIRST_ACTION,
        LATER_ACTION,
    )
    assert second.ingest_cutoff > first.ingest_cutoff
    more_earlier, more_later = history_more(html, WATCHED)
    assert more_earlier["Supersedes"] == ["none"]
    assert more_later["Supersedes"] == [first.attribution_event_id]
    assert "Earlier results are retained as recorded" in caption(html, history_id(WATCHED))

    assert cell_row(html, is_open)[5].startswith(HEURISTIC_STATUS)
    block = fields(html, f"outcome-{WATCHED}")
    assert block["Policy-resolved references"] == [
        f"action {LATER_ACTION}",
        f"decision {DECISION_EVENT_ID} {THIS_PAGE}",
    ]
    assert block["Attribution event"] == [second.attribution_event_id]

    link = f'<a href="/accounts/{ACCOUNT_REF}/decisions/{DECISION_EVENT_ID}#outcome-{WATCHED}">'
    trace_rows = attribution_trace_rows(harness)
    assert len(trace_rows) == 2
    for trace_row in trace_rows:
        assert link in trace_row


def test_a_reason_only_replacement_shows_both_reasons_in_history(harness):
    clock = watched_ledger(harness, source_action_event_id=CLAIMED_ACTION)
    post_created(
        harness,
        action_envelope(CLAIMED_ACTION, occurred_at="2026-04-20T09:00:00Z", status="failed"),
    )
    clock.advance(hours=6)
    run = attribute_ledger(harness, reevaluate=True, clock=clock)
    assert [s.http_status for s in run.submissions] == [201]

    first, second = attribution_rows(harness)
    assert first.reason == (
        f"{policy.SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF}{policy.SEGMENT_SEPARATOR}"
        f"{policy.MOST_RECENT_ELIGIBLE_ACTION}"
    )
    assert second.reason == (
        f"{policy.SOURCE_ACTION_FAILED}{policy.SEGMENT_SEPARATOR}{policy.MOST_RECENT_ELIGIBLE_ACTION}"
    )
    assert (first.status, first.resolved_action_event_id, first.resolved_decision_event_id) == (
        second.status,
        second.resolved_action_event_id,
        second.resolved_decision_event_id,
    )
    html = decision_page(harness)

    earlier, later = rows(html, history_id(WATCHED))
    assert earlier[STATUS] == later[STATUS] == HEURISTIC_STATUS
    assert earlier[ACTION] == later[ACTION] == first.resolved_action_event_id
    assert earlier[DECISION] == later[DECISION] == f"{DECISION_EVENT_ID} {THIS_PAGE}"
    assert (earlier[REASON], later[REASON]) == (first.reason, second.reason)
    assert earlier[STANDING] == f"superseded by {second.attribution_event_id}"
    assert later[STANDING] == "effective"

    block = fields(html, f"outcome-{WATCHED}")
    claim = block["Source-provided claim"]
    assert any(CLAIMED_ACTION in line for line in claim[:-1])
    assert claim[-1] == "a claim to evaluate, not credit"
    assert block["Reason"] == [second.reason]


# --- One display cutoff ----------------------------------------------------------------------


def test_history_and_standing_are_read_at_the_page_cutoff(harness, tmp_path):
    clock = watched_ledger(harness)
    with harness.engine.connect() as conn:
        first_cutoff = ledger_maximum(conn)
    replace_with_later_action(harness, clock)
    first, second = attribution_rows(harness)

    with harness.engine.connect() as conn:
        bounded = _outcome_rows(conn, ACCOUNT_REF, DECISION_EVENT_ID, cutoff=first_cutoff)
        current = _outcome_rows(conn, ACCOUNT_REF, DECISION_EVENT_ID)

    (watched,) = bounded
    (only,) = watched.attribution_history
    assert only.attribution_event_id == first.attribution_event_id
    assert only.effective is True
    assert only.superseded_by_attribution_event_id is None
    assert watched.attribution.attribution_event_id == first.attribution_event_id
    assert second.attribution_event_id not in repr(bounded)

    (watched_now,) = current
    earlier, later = watched_now.attribution_history
    assert (earlier.attribution_event_id, later.attribution_event_id) == (
        first.attribution_event_id,
        second.attribution_event_id,
    )
    assert (earlier.effective, later.effective) == (False, True)
    assert earlier.superseded_by_attribution_event_id == second.attribution_event_id
    assert later.superseded_by_attribution_event_id is None
    assert watched_now.attribution.attribution_event_id == second.attribution_event_id

    # A later outcome version is absent from a read bounded before it, not a failure.
    fresh = Harness(tmp_path)
    seed_and_attribute(fresh)
    with fresh.engine.connect() as conn:
        canonical_cutoff = ledger_maximum(conn)
    post_created(fresh, closing_version())

    with fresh.engine.connect() as conn:
        bounded = _outcome_rows(conn, ACCOUNT_REF, DECISION_EVENT_ID, cutoff=canonical_cutoff)
        current = _outcome_rows(conn, ACCOUNT_REF, DECISION_EVENT_ID)

    (canonical,) = bounded
    assert canonical.outcome_event_id == OUTCOME_EVENT_ID
    assert canonical.effective_outcome_event_id == OUTCOME_EVENT_ID
    assert canonical.superseded_by_outcome_event_id is None
    assert canonical.attribution_failure is None
    assert CLOSING not in repr(bounded)

    by_id = {row.outcome_event_id: row for row in current}
    assert set(by_id) == {OUTCOME_EVENT_ID, CLOSING}
    assert by_id[OUTCOME_EVENT_ID].effective_outcome_event_id == CLOSING
    assert by_id[OUTCOME_EVENT_ID].superseded_by_outcome_event_id == CLOSING
    assert by_id[CLOSING].attribution_failure is None


# --- States under their own labels -----------------------------------------------------------


def test_an_unresolved_result_links_nowhere_and_resolves_nothing(harness):
    seed_through_decision(harness)
    post_created(harness, open_observation("evt-test-o-early", observed_at="2026-04-17T10:07:00Z"))
    attribute_ledger(harness)
    (stored,) = attribution_rows(harness)
    assert stored.status == policy.STATUS_UNRESOLVED
    assert stored.reason == f"{policy.NO_SOURCE_REFERENCE}{policy.SEGMENT_SEPARATOR}" + (
        policy.NO_ELIGIBLE_ACTION
    )
    html = decision_page(harness)

    block = fields(html, "outcome-evt-test-o-early")
    assert block["Attribution status"] == [policy.STATUS_UNRESOLVED]
    assert block["Heuristic"] == ["no"]
    assert block["Policy-resolved references"] == ["none resolved"]
    assert block["Reason"] == [stored.reason]

    (row,) = rows(html, history_id("evt-test-o-early"))
    assert row[EVENT] == stored.attribution_event_id
    assert row[ACTION] == "none"
    assert row[DECISION] == "none"
    assert row[STANDING] == "effective"

    (trace_row,) = attribution_trace_rows(harness)
    assert f"Outcome attribution: {policy.STATUS_UNRESOLVED}" in trace_row
    assert "<a " not in trace_row


def test_open_window_observations_are_qualified_in_the_block_and_plain_in_the_table(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    post_created(harness, open_observation("evt-test-o-open", meeting=False))
    attribute_ledger(harness)
    observed_at = outcome_row(harness, "evt-test-o-open").observed_at
    html = decision_page(harness)

    assert cell_row(html, is_open)[1] == "reply: unknown meeting: no opportunity: unknown"
    block = fields(html, "outcome-evt-test-o-open")
    assert block["Observations"] == [
        "reply: unknown (the observation itself was not recorded)",
        f"meeting: no (nothing recorded as of {observed_at}; the window is open)",
        "opportunity: unknown (the observation itself was not recorded)",
    ]
    (window,) = block["Evaluation window"]
    assert window.startswith("open, ")


def test_a_closed_window_observation_reads_as_a_recorded_negative(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        outcome_v2_envelope(
            "evt-test-o-closed",
            observed_at=CLOSES,
            window_opened_at=OPENED,
            window_closes_at=CLOSES,
            evaluation_state="closed",
            **ALL_FALSE,
        ),
    )
    html = decision_page(harness)

    block = fields(html, "outcome-evt-test-o-closed")
    assert len(block["Observations"]) == 3
    for line in block["Observations"]:
        assert line.endswith("(recorded negative observation)"), line
    (window,) = block["Evaluation window"]
    assert window.startswith("closed, ")
    assert block["Attribution status"] == ["not yet evaluated"]
    for absent in ("Heuristic", "Policy", "Policy-resolved references"):
        assert absent not in block, absent
    assert block["Attribution history"] == [
        "No attribution result has been recorded for this outcome version."
    ]
    assert not has_element(html, history_id("evt-test-o-closed"))


def test_a_superseded_outcome_version_names_its_successor_and_the_successor_names_it(harness):
    seed_and_attribute(harness)
    (stored,) = attribution_rows(harness)
    post_created(harness, closing_version())
    html = decision_page(harness)

    original = fields(html, f"outcome-{OUTCOME_EVENT_ID}")
    assert original["Outcome version"] == [
        f"superseded by {CLOSING}; the effective version is {CLOSING}"
    ]
    assert original["Attribution status"] == [policy.STATUS_DIRECT]
    (retained,) = rows(html, history_id(OUTCOME_EVENT_ID))
    assert retained[EVENT] == stored.attribution_event_id
    assert retained[STANDING] == "effective"

    closing = fields(html, f"outcome-{CLOSING}")
    assert closing["Outcome version"] == ["effective version", f"supersedes {OUTCOME_EVENT_ID}"]
    assert closing["Attribution status"] == ["not yet evaluated"]
    assert not has_element(html, history_id(CLOSING))


def test_source_provided_claims_and_policy_resolved_references_are_separate_fields(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    post_created(
        harness, open_observation("evt-test-o-claim", source_decision_event_id=DECISION_EVENT_ID)
    )
    attribute_ledger(harness)
    (stored,) = stored_for(harness, "evt-test-o-claim")
    assert stored.resolved_action_event_id is None
    html = decision_page(harness)

    block = fields(html, "outcome-evt-test-o-claim")
    assert "Source-provided claim" in block and "Policy-resolved references" in block
    assert "Recorded reference" not in block
    claim = block["Source-provided claim"]
    assert any(DECISION_EVENT_ID in line for line in claim[:-1])
    assert claim[-1] == "a claim to evaluate, not credit"
    assert block["Policy-resolved references"] == [f"decision {DECISION_EVENT_ID} {THIS_PAGE}"]
    assert block["Reason"] == [stored.reason] == [policy.VALID_SOURCE_DECISION]


def test_a_result_for_another_decision_is_marked_in_both_the_cell_and_the_block(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    post_created(
        harness,
        decision_copy_envelope(SECOND_DECISION, boundary="2026-04-18T00:00:00Z"),
        action_envelope(
            "evt-test-action-second",
            occurred_at="2026-04-18T00:01:00Z",
            decision_event_id=SECOND_DECISION,
        ),
        open_observation("evt-test-o-second"),
    )
    attribute_ledger(harness)
    (stored,) = stored_for(harness, "evt-test-o-second")
    assert stored.resolved_decision_event_id == SECOND_DECISION

    for page_decision, marker in ((DECISION_EVENT_ID, NOT_THIS_PAGE), (SECOND_DECISION, THIS_PAGE)):
        html = decision_page(harness, page_decision)
        cell = cell_row(html, is_open)[5]
        assert f"policy-resolved decision {SECOND_DECISION} {marker}" in cell, page_decision
        block = fields(html, "outcome-evt-test-o-second")
        assert f"decision {SECOND_DECISION} {marker}" in block["Policy-resolved references"]
        (row,) = rows(html, history_id("evt-test-o-second"))
        assert row[DECISION] == f"{SECOND_DECISION} {marker}"


# --- Failures keep what is known -------------------------------------------------------------


def test_an_attribution_selection_failure_keeps_the_known_outcome_standing(harness):
    selection_failure(harness)
    stored = stored_for(harness, OUTCOME_EVENT_ID)
    assert len(stored) == 2
    html = decision_page(harness)

    reason = policy.AmbiguousSelection.reason
    cell = cell_row(html, is_v1)[5]
    assert cell.startswith(f"{COULD_NOT_BE_ESTABLISHED}: {reason}.")
    assert NOT_YET_EVALUATED not in cell
    assert policy.STATUS_UNRESOLVED not in cell

    block = fields(html, f"outcome-{OUTCOME_EVENT_ID}")
    assert block["Outcome version"] == ["effective version"]
    assert block["Attribution status"] == [f"could not be established ({reason})"]
    assert "Heuristic" not in block

    history = rows(html, history_id(OUTCOME_EVENT_ID))
    assert [row[EVENT] for row in history] == [row.attribution_event_id for row in stored]
    for row in history:
        assert row[STANDING] == "standing could not be established"


def test_an_outcome_selection_failure_is_named_as_such(harness, monkeypatch):
    seed_and_attribute(harness)
    (stored,) = attribution_rows(harness)

    def raising(conn, outcome_chain, *, cutoff):
        raise policy.AmbiguousSelection("outcome version", [OUTCOME_EVENT_ID, "evt-test-o-other"])

    monkeypatch.setattr("flight_recorder.web.decision_view.effective_outcome_version", raising)
    html = decision_page(harness)

    block = fields(html, f"outcome-{OUTCOME_EVENT_ID}")
    assert block["Outcome version"] == ["standing could not be established"]
    (row,) = rows(html, "outcomes-table")
    assert row[5].startswith(f"{COULD_NOT_BE_ESTABLISHED}: ")
    (history,) = rows(html, history_id(OUTCOME_EVENT_ID))
    assert history[EVENT] == stored.attribution_event_id
    assert history[STANDING] == "standing could not be established"


# --- No aggregates, keyboard, the five prefixes ----------------------------------------------


def test_the_section_renders_no_counts(harness):
    three_outcomes(harness)
    html = decision_page(harness)
    region = element(html, "outcomes")
    assert len(rows(html, "outcomes-table")) == 3

    labels = [text_of(inner) for _, inner in re.findall(r"<(dt|h2|h3)\b[^>]*>(.*?)</\1>", region)]
    assert labels
    for label in labels:
        # Whole words: the section's heading reads "account", which is not a count.
        assert re.search(r"\b(total|count|rate)\b", label, re.IGNORECASE) is None, label
        assert "%" not in label and "of 3" not in label, label
    assert re.search(r"\b[0-9]+ outcomes?\b", text_of(region)) is None
    assert caption(html, "outcomes-table") == (
        f"Every outcome recorded for {ACCOUNT_NAME}, with its evaluation window and the "
        "observations recorded inside it."
    )


def test_every_row_has_a_block_and_the_page_stays_keyboard_usable(harness):
    three_outcomes(harness)
    html = decision_page(harness)
    region = element(html, "outcomes")

    row_ids = re.findall(r'<tr id="outcome-row-([^"]+)">', element(html, "outcomes-table"))
    block_ids = re.findall(r'<div class="outcome-detail" id="outcome-([^"]+)">', region)
    assert len(row_ids) == len(rows(html, "outcomes-table")) == 3
    assert row_ids == block_ids

    assert "onclick" not in html
    assert 'role="button"' not in html
    assert html.count('tabindex="') == 1
    assert '<main id="main" tabindex="-1">' in html
    assert "<script" not in html

    tables = re.findall(r"<table\b.*?</table>", region, re.S)
    assert len(tables) == 3  # the outcomes table and two attribution histories
    for table in tables:
        assert "<caption>" in table
        assert '<th scope="col">' in table
    details = re.findall(r"<details\b.*?</details>", region, re.S)
    assert details
    for body in details:
        assert "<summary>" in body

    selector = element(html, "current-logic-selector")
    assert '<form method="get"' in selector
    assert '<label for="current-artifact">' in selector
    assert '<select name="current" id="current-artifact" required>' in selector
    assert '<button type="submit">' in selector


def test_the_eight_conditions_still_read_distinctly_from_the_slimmed_cell(tmp_path_factory):
    def direct(harness):
        seed_and_attribute(harness)
        return is_v1

    def inferred(harness):
        for response in seed_all(harness):
            assert response.status_code == 201, response.json()
        post_created(harness, open_observation("evt-test-o-inferred"))
        attribute_ledger(harness)
        return is_open

    def unresolved(harness):
        seed_through_decision(harness)
        post_created(
            harness, open_observation("evt-test-o-early", observed_at="2026-04-17T10:07:00Z")
        )
        attribute_ledger(harness)
        return is_open

    def not_yet_evaluated(harness):
        for response in seed_all(harness):
            assert response.status_code == 201, response.json()
        return is_v1

    def failure(harness):
        selection_failure(harness)
        return is_v1

    expected = {
        direct: policy.STATUS_DIRECT,
        inferred: HEURISTIC_STATUS,
        unresolved: policy.STATUS_UNRESOLVED,
        not_yet_evaluated: NOT_YET_EVALUATED,
        failure: COULD_NOT_BE_ESTABLISHED,
    }
    for build, prefix in expected.items():
        harness = Harness(tmp_path_factory.mktemp(build.__name__))
        pick = build(harness)
        cell = cell_row(decision_page(harness), pick)[5]
        assert cell.startswith(prefix), (build.__name__, cell)
        for other in set(expected.values()) - {prefix}:
            assert not cell.startswith(other), (build.__name__, other)
