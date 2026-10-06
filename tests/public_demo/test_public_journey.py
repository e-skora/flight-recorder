"""P-03 to P-07: public reading order and native result anchors.

INV-02/03/05/06/09: preserve the one real replay, labels, missing inputs and
named failures. Existing engine and local presentation tests stay unchanged.
"""

import re
from collections import Counter
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.logic.evaluator import InputState
from flight_recorder.public_demo import create_public_demo
from flight_recorder.replay.counterfactual import MissingInput
from flight_recorder.replay.reconstruct import ArtifactMissing, IntegrityFailure
from flight_recorder.web import routes
from tests.acceptance.test_readme import element, has_element, visible
from tests.public_demo.conftest import DECISION_URL, V5_1_HASH, V5_2_HASH, owned_copy
from tests.public_demo.test_public_branding import Tags


@pytest.fixture(scope="module")
def snapshot(built_snapshot, tmp_path_factory):
    return owned_copy(built_snapshot, tmp_path_factory.mktemp("journey"))


@pytest.fixture(scope="module")
def public(snapshot):
    with TestClient(create_public_demo(snapshot)) as client:
        yield client


def assert_result(html, state):
    target = element(html, "replay-result")
    assert has_element(target, state)
    ids = [attrs["id"] for _, attrs in Tags(html).tags if "id" in attrs]
    assert max(Counter(ids).values()) == 1
    positions = [
        html.index(f'id="{name}"')
        for name in (
            "decision-summary",
            "decision-navigation",
            "replay-panel",
            "evidence-context",
            "ruleset",
            "explanation",
            "actions",
            "outcomes",
            "logic-identity",
        )
    ]
    assert positions == sorted(positions)
    button = next(a for t, a in Tags(element(html, "current-logic-selector")).tags if t == "button")
    assert button["formaction"] == DECISION_URL + "#replay-result"


@pytest.mark.parametrize("query", ["", f"?current={V5_1_HASH}", f"?current={V5_2_HASH}"])
def test_recorded_summary_then_one_replay_then_evidence(public, query):
    html = public.get(DECISION_URL + query).text
    assert_result(html, "replay-comparison")
    assert "Logic version (label)" in visible(element(html, "decision-summary"))
    assert "Original, recorded" in visible(element(html, "replay-result"))
    assert "Counterfactual, computed now, not recorded" in visible(element(html, "replay-result"))


def test_same_artifact_and_unregistered_artifact_land_in_the_same_result_region(public):
    original = visible(element(public.get(DECISION_URL).text, "original-artifact-hash"))
    same = public.get(f"{DECISION_URL}?current={original}").text
    assert_result(same, "replay-comparison")
    assert visible(element(same, "original-score")) == visible(
        element(same, "counterfactual-score")
    )
    failure = public.get(f"{DECISION_URL}?current={'0' * 64}").text
    assert_result(failure, "replay-integrity-failure")
    assert "ArtifactMissing" in visible(element(failure, "replay-result"))
    assert not has_element(failure, "replay-summary")


@pytest.mark.parametrize(
    "error",
    [
        ArtifactMissing("0" * 64),
        IntegrityFailure("artifact_hash", "mismatch", stored="old", recomputed="new"),
    ],
)
def test_named_failures_stay_visible_in_the_result_region(public, monkeypatch, error):
    def fail(*args):
        raise error

    monkeypatch.setattr(routes, "replay", fail)
    html = public.get(DECISION_URL).text
    assert_result(html, "replay-integrity-failure")
    result = element(html, "replay-result")
    assert "<details" not in result
    assert type(error).__name__ in visible(result)
    assert not has_element(result, "replay-summary")


@pytest.mark.parametrize(
    "message",
    [
        "Default replay logic is not registered.",
        "More than one registered artifact carries the default version.",
    ],
)
def test_no_selection_has_a_visible_destination(public, monkeypatch, message):
    monkeypatch.setattr(routes, "_default_current_artifact", lambda *args: (None, message))
    html = public.get(DECISION_URL).text
    assert_result(html, "replay-no-selection")
    assert message in visible(element(html, "replay-result"))
    assert "<details" not in element(html, "replay-result")


def test_warnings_missing_inputs_and_old_deep_links_are_not_in_closed_details(public):
    html = public.get(f"{DECISION_URL}?current={V5_1_HASH}").text
    outside = re.sub(r"<details\b(?![^>]*\bopen\b).*?</details>", "", html, flags=re.S)
    for target in (
        "in-effect-positive-weight-bound",
        "artifact-positive-weight-bounds",
        "missing-inputs-table",
        "context-table",
        "ruleset-table",
        "replay-comparison",
        "logic-identity",
        "original-artifact-hash",
    ):
        assert has_element(outside, target)
    assert "None." in visible(element(outside, "missing-inputs-table"))
    assert "Scroll sideways to see all columns." in html


def test_reordering_does_not_repeat_replay(public, monkeypatch):
    real = routes.replay
    calls = []

    def observe(*args):
        calls.append(args[1:])
        return real(*args)

    monkeypatch.setattr(routes, "replay", observe)
    public.get(DECISION_URL)
    assert len(calls) == 1


@pytest.mark.parametrize("query", ["nova", "no-matching-company"])
def test_filter_has_result_and_clear_destinations(public, query):
    html = public.get("/demo", params={"q": query}).text
    assert has_element(element(html, "account-results"), "account-count")
    assert 'formaction="/demo#account-results"' in element(html, "account-filter")
    assert '<a href="/demo#account-results">Clear filter</a>' in html
    assert "Clear filter" not in public.get("/demo").text


def test_trace_summary_precedes_source_and_times_for_each_event(public):
    for account in ("novasignal-ai", "ds-0053"):
        html = public.get(f"/accounts/{account}").text
        assert 'role="table"' in html
        rows = re.findall(r'<tr class="kind-[^"]*"[^>]*>(.*?)</tr>', html, re.S)
        assert rows
        decision_links = 0
        for row in rows:
            tags = Tags(row).tags
            cells = [a.get("class") for t, a in tags if t == "td"]
            assert cells[:3] == ["trace-index", "trace-kind", "trace-summary"]
            assert "Occurred at:" in visible(row) and "Recorded at:" in visible(row)
            decision_links += sum(t == "a" and "/decisions/" in a["href"] for t, a in tags)
        assert decision_links >= 1


def test_local_form_tags_and_order_stay_unchanged(snapshot, tmp_path):
    with TestClient(create_app(owned_copy(snapshot, tmp_path))) as client:
        html = client.get(DECISION_URL).text
        assert "formaction=" not in html and "decision-navigation" not in html
        assert html.index('id="outcomes"') < html.index('id="replay-panel"')
        assert "account-results" not in client.get("/").text
        assert "trace-summary" not in client.get("/accounts/novasignal-ai").text


def test_missing_input_notices_remain_visible_for_a_supplied_comparison(public, monkeypatch):
    real = routes.compare

    def with_missing_inputs(value):
        return replace(
            real(value),
            missing_inputs=(
                MissingInput("website_intent", InputState.UNAVAILABLE),
                MissingInput("partner_referral", InputState.ABSENT),
            ),
        )

    monkeypatch.setattr(routes, "compare", with_missing_inputs)
    html = public.get(DECISION_URL).text
    outside = re.sub(r"<details\b.*?</details>", "", html, flags=re.S)
    missing = visible(element(outside, "missing-inputs-table"))
    rows = re.findall(r"<tr>(.*?)</tr>", element(outside, "missing-inputs-table"), re.S)
    cells = [[visible(cell) for cell in re.findall(r"<td>(.*?)</td>", row)] for row in rows]
    assert ["website_intent", "unavailable", "No present-day value was substituted."] in cells
    assert ["partner_referral", "absent", "No present-day value was substituted."] in cells
    assert "No present-day value was substituted." in missing
