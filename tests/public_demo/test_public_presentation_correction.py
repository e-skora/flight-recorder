"""D-018 piece 1, revision 4 implementation-review correction: the public demo
puts its first action, its replay result and its Insights result first.

1. The replay comparison opens, in public mode, with a summary of the recorded
   original and the computed counterfactual (score, threshold, logic version,
   output) and whether the output changes. Every value comes from the same
   `compare(replay(...))` result as the detailed list; it is checked against the
   engine for every registered artifact, so a hard-coded or stale value fails.
   A same-artifact comparison claims no change; a failed or absent comparison
   shows no summary (INV-06: the counterfactual is labelled computed now, not
   recorded, and is never stored; INV-09: failure stays visible).
2. The account list states its purpose and offers its one primary action before
   the longer explanation, and the condensed disclosure keeps "public read-only
   demo", "synthetic" and "fictional" visible without opening anything.
3. Insights opens, in public mode, with the existing overall observed rate as
   rendered by `rate_line`, with its cohort total and cutoff; zero eligible
   decisions show the engine's own note, and the empty and selection-failure
   states show no headline (INV-10: nothing is invented).

Local pages carry none of it (their rendering is unchanged).
"""

import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

from flight_recorder.analytics.insights import NOT_AVAILABLE_NOTE, Rate, SelectionFailure
from flight_recorder.app import create_app
from flight_recorder.public_demo import create_public_demo, open_read_only
from flight_recorder.replay.counterfactual import compare, replay
from flight_recorder.web.insights_view import (
    PAGE_EMPTY,
    PAGE_SELECTION_FAILURE,
    InsightsPage,
)
from tests.acceptance.test_readme import element, has_element, visible
from tests.public_demo.conftest import (
    DECISION_EVENT_ID,
    DECISION_URL,
    V5_1_HASH,
    V5_2_HASH,
    owned_copy,
)

SUMMARY = "replay-summary"


@pytest.fixture(scope="module")
def snapshot(built_snapshot, tmp_path_factory):
    return owned_copy(built_snapshot, tmp_path_factory.mktemp("presentation"))


@pytest.fixture(scope="module")
def app(snapshot):
    return create_public_demo(snapshot)


@pytest.fixture(scope="module")
def public(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def local(snapshot, tmp_path_factory):
    path = owned_copy(snapshot, tmp_path_factory.mktemp("presentation-local"), "local.db")
    with TestClient(create_app(path)) as client:
        yield client


def text_of(html: str, element_id: str) -> str:
    return visible(element(html, element_id))


def registered_hashes(snapshot) -> list[str]:
    engine = open_read_only(snapshot)
    try:
        with engine.connect() as conn:
            return [
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT artifact_hash FROM logic_artifacts "
                    "WHERE decision_class = 'account_prioritization' ORDER BY artifact_hash"
                )
            ]
    finally:
        engine.dispose()


# --- 1. The replay result summary ---------------------------------------------------


def test_the_summary_leads_the_comparison_and_is_labelled(public):
    html = public.get(DECISION_URL).text
    comparison = element(html, "replay-comparison")
    assert comparison.index('id="replay-summary"') < comparison.index('id="original-logic-version"')
    summary = text_of(html, SUMMARY)
    assert "Original, recorded" in summary
    assert "Counterfactual, computed now, not recorded" in summary
    assert "Same preserved context, evaluated on demand; nothing is stored." in summary
    for element_id in (
        SUMMARY,
        "summary-original-score",
        "summary-counterfactual-score",
        "summary-output-change",
        "original-score",
        "counterfactual-score",
        "output-changed",
    ):
        assert html.count(f'id="{element_id}"') == 1, element_id


def test_default_and_explicit_v5_1_summaries_show_the_actual_results(public):
    default = public.get(DECISION_URL).text
    assert text_of(default, "summary-original-score") == "86"
    assert text_of(default, "summary-original-logic") == "v3.2"
    assert text_of(default, "summary-counterfactual-score") == "72"
    assert text_of(default, "summary-counterfactual-logic") == "v5.2"
    assert text_of(default, "summary-counterfactual-output") == "DO_NOT_PRIORITIZE"
    assert text_of(default, "summary-output-change").startswith(
        "The output changes from PRIORITIZE to DO_NOT_PRIORITIZE; score 86 to 72 (-14)."
    )
    explicit = public.get(f"{DECISION_URL}?current={V5_1_HASH}").text
    assert text_of(explicit, "summary-counterfactual-score") == "51"
    assert text_of(explicit, "summary-counterfactual-logic") == "v5.1"


def test_every_summary_equals_the_engine_for_every_registered_artifact(public, snapshot):
    hashes = registered_hashes(snapshot)
    assert {V5_1_HASH, V5_2_HASH} <= set(hashes) and len(hashes) >= 3
    engine = open_read_only(snapshot)
    try:
        for artifact_hash in hashes:
            with engine.connect() as conn:
                expected = compare(replay(conn, DECISION_EVENT_ID, artifact_hash))
            html = public.get(f"{DECISION_URL}?current={artifact_hash}").text
            got = {
                key: text_of(html, f"summary-{key}")
                for key in (
                    "original-score",
                    "original-logic",
                    "original-output",
                    "counterfactual-score",
                    "counterfactual-logic",
                    "counterfactual-output",
                )
            }
            assert got == {
                "original-score": str(expected.original_score),
                "original-logic": expected.historical_logic.logic_version,
                "original-output": str(expected.original_output),
                "counterfactual-score": str(expected.counterfactual_score),
                "counterfactual-logic": expected.current_logic.logic_version,
                "counterfactual-output": str(expected.counterfactual_output),
            }, artifact_hash
            summary = text_of(html, SUMMARY)
            assert f"against threshold {expected.counterfactual_threshold}" in summary
            change = text_of(html, "summary-output-change")
            assert change.startswith(
                "The output changes" if expected.output_changed else "The output does not change"
            ), artifact_hash
    finally:
        engine.dispose()


def test_a_same_artifact_comparison_claims_no_change(public):
    recorded = text_of(public.get(DECISION_URL).text, "original-artifact-hash")
    assert re.fullmatch(r"[0-9a-f]{64}", recorded)
    html = public.get(f"{DECISION_URL}?current={recorded}").text
    change = text_of(html, "summary-output-change")
    assert change.startswith("The output does not change: both are PRIORITIZE")
    assert "changes from" not in change
    assert text_of(html, "summary-original-score") == text_of(html, "summary-counterfactual-score")


def test_no_summary_without_a_successful_comparison(public, local):
    unregistered = public.get(f"{DECISION_URL}?current={'0' * 64}").text
    assert not has_element(unregistered, "replay-comparison")
    assert not has_element(unregistered, SUMMARY)
    assert has_element(unregistered, "replay-integrity-failure")
    assert not has_element(local.get(DECISION_URL).text, SUMMARY)


# --- 2. The entry page ---------------------------------------------------------------


def test_the_start_block_puts_purpose_and_the_primary_action_before_the_explanation(public):
    block = element(public.get("/demo").text, "start-here")
    heading = block.index('id="start-here-heading"')
    purpose = block.index('class="start-purpose"')
    primary = block.index('id="start-canonical-decision"')
    explanation = block.index("Replay recomputes a past decision")
    assert heading < purpose < primary < explanation
    assert block.count("button-primary") == 1


def test_the_condensed_disclosure_states_the_essentials_without_opening(public, local):
    for url in ("/", "/demo", DECISION_URL, "/insights"):
        html = public.get(url).text
        notice = element(html, "public-demo-notice")
        lead = visible(notice[: notice.index("<details")])
        assert "Public read-only demo." in lead
        assert "synthetic" in lead and "fictional" in lead
        details = notice[notice.index("<details") :]
        assert "Nothing on this page is connected to a live customer system." in visible(details)
        assert "never stored" in visible(details) and "source repository" in visible(details)
        assert 'class="synthetic-banner"' not in html
    local_html = local.get("/").text
    assert 'class="synthetic-banner"' in local_html
    assert not has_element(local_html, "public-demo-notice")


# --- 3. The Insights headline --------------------------------------------------------


def test_the_headline_repeats_the_existing_rate_with_its_denominators(public, local):
    html = public.get("/insights").text
    assert html.index('id="insights-headline"') < html.index('id="insights-language"')
    assert text_of(html, "headline-rate") == text_of(html, "overall-rate-value")
    assert text_of(html, "headline-cohort-total") == text_of(html, "overall-cohort-total")
    assert text_of(html, "headline-cutoff") == text_of(html, "insights-cutoff")
    context = text_of(html, "insights-headline")
    assert "descriptive, not causal, and not a conversion probability" in context
    assert "synthetic" in context
    assert not has_element(local.get("/insights").text, "insights-headline")


@pytest.fixture
def swapped(app):
    """Render the public page over another page model, then restore the stored one."""
    stored = app.state.public_insights_page

    def render(page) -> str:
        app.state.public_insights_page = page
        with TestClient(app) as client:
            return client.get("/insights").text

    yield stored, render
    app.state.public_insights_page = stored


def test_zero_eligible_decisions_show_the_engines_note_not_a_number(swapped):
    stored, render = swapped
    overall = stored.result.overall
    none_eligible = Rate(
        cohort_total=overall.cohort_total,
        eligible=0,
        positives=0,
        excluded_other_period_only=overall.excluded_other_period_only,
        excluded_reconstruction_failed=overall.excluded_reconstruction_failed,
        excluded_not_evaluated=overall.cohort_total,
    )
    page = dataclasses.replace(
        stored, result=dataclasses.replace(stored.result, overall=none_eligible)
    )
    html = render(page)
    assert text_of(html, "headline-rate") == NOT_AVAILABLE_NOTE
    assert text_of(html, "headline-rate") == text_of(html, "overall-rate-value")
    assert "%" not in text_of(html, "headline-rate")


def test_empty_and_selection_failure_states_show_no_headline(swapped):
    _, render = swapped
    empty = render(InsightsPage(state=PAGE_EMPTY))
    assert has_element(empty, "insights-empty")
    assert not has_element(empty, "insights-headline")
    failure = render(
        InsightsPage(
            state=PAGE_SELECTION_FAILURE,
            cutoff=1597,
            failure=SelectionFailure("evt-example-outcome", ["evt-a", "evt-b"]),
        )
    )
    assert has_element(failure, "insights-failure")
    assert not has_element(failure, "insights-headline")
    assert "%" not in visible(element(failure, "main"))
