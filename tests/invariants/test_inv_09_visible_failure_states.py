"""INV-09 / AC-07 on screen: replay that cannot be established fails visibly.

Four genuine failures reach the page here -- an unregistered current artifact,
a current artifact written for another evaluator, a recorded score the
preserved logic does not reproduce, and a recorded consumed-input set the
preserved logic does not reproduce -- and each renders a named failure state
carrying *that class's own fields under their own names*. A
`ReconstructionMismatch` reports `recorded` and `reconstructed`, never
`stored` and `recomputed`: the record against the reproduction is a different
claim from the ledger against a recomputation.

In every case no comparison and no counterfactual result is rendered, nothing
falls back to another artifact or a cached value, and the recorded sections of
the page stay readable: a failure to replay does not erase the record
(`PRODUCT.md` §5 "Replay honesty").

The two mismatch decisions are built from the canonical envelope through the
collector, which validates evidence references and artifact identity but does
not re-evaluate the logic, so a decision whose recorded result disagrees with
its own artifact is ingestible and is exactly the case INV-09 requires the UI
to surface.
"""

import copy

import pytest

from flight_recorder.logic import evaluator as evaluator_module
from flight_recorder.replay.reconstruct import DecisionNotFound
from flight_recorder.web.decision_view import failure_view
from tests.acceptance.test_decision_detail_page import (
    decision_url,
    element,
    has_element,
    page,
    rows,
)
from tests.conftest import (
    DECISION_EVENT_ID,
    canonical_by_type,
    canonical_evidence_ids,
    derived_artifact_envelope,
    logic_artifact,
    register_derived_artifact,
    seed_all,
)

pytestmark = pytest.mark.invariant

SCORE_MISMATCH_ID = "evt-test-decision-score-mismatch"
CONSUMED_MISMATCH_ID = "evt-test-decision-consumed-mismatch"
MISMATCH_KEY = "headquarters_country"


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def failure_fields(html: str) -> dict[str, str]:
    return {row[0]: row[1] for row in rows(html, "failure-fields-table")}


def assert_no_counterfactual_result(html: str) -> None:
    """No comparison table, no counterfactual score, output, or delta field."""
    assert not has_element(html, "replay-comparison")
    assert not has_element(html, "contributions-table")
    for field_id in (
        "counterfactual-score",
        "counterfactual-output",
        "score-delta",
        "output-changed",
        "missing-inputs-table",
    ):
        assert not has_element(html, field_id), field_id


def mismatch_envelope(event_id: str, *, reduce_score: bool) -> dict:
    """The canonical decision with one recorded contribution one point lower.

    With `reduce_score`, the recorded `result.score` drops by the same point,
    so the score comparison is the first thing that disagrees. Without it, the
    score still matches and the consumed-input comparison is what disagrees.
    Both values are derived from the fixture.
    """
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = event_id
    payload = envelope["payload"]
    used = next(u for u in payload["consumed_inputs"] if u["input_key"] == MISMATCH_KEY)
    used["contribution"] -= 1
    if reduce_score:
        payload["result"] = {**payload["result"], "score": payload["result"]["score"] - 1}
    return envelope


# --- A current artifact that cannot be verified -------------------------------


def test_an_unregistered_current_artifact_renders_a_named_failure(seeded):
    unregistered = "0" * 64
    html = page(seeded, query=f"?current={unregistered}")

    region = element(html, "replay-integrity-failure")
    assert "ArtifactMissing" in region
    assert failure_fields(html) == {"artifact_hash": unregistered}
    assert_no_counterfactual_result(html)

    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    assert element(html, "decision-output") == "PRIORITIZE"


def test_a_current_artifact_for_another_evaluator_renders_the_named_integrity_failure(seeded):
    other_hash = register_derived_artifact(
        seeded,
        derived_artifact_envelope(
            "logic-account-prioritization-v5.1-other-evaluator",
            "v5.1-other-evaluator",
            logic_artifact("v5.1")["factors"],
            event_id="evt-system-logic-artifact-v5.1-other-evaluator",
            evaluator_version="evaluator-v2",
        ),
    )
    html = page(seeded, query=f"?current={other_hash}")

    assert "IntegrityFailure" in element(html, "replay-integrity-failure")
    fields = failure_fields(html)
    assert fields["field"] == "evaluator_version"
    assert fields["stored"] == "evaluator-v2"
    assert fields["recomputed"] == "evaluator-v1"
    assert "detail" in fields
    assert_no_counterfactual_result(html)


# --- A record the preserved logic does not reproduce ---------------------------


def test_a_genuine_score_mismatch_renders_recorded_and_reconstructed(seeded):
    envelope = mismatch_envelope(SCORE_MISMATCH_ID, reduce_score=True)
    assert seeded.post(envelope).status_code == 201
    recorded_score = envelope["payload"]["result"]["score"]
    assert recorded_score == 85

    html = page(seeded, SCORE_MISMATCH_ID)
    region = element(html, "replay-integrity-failure")
    assert "ReconstructionMismatch" in region

    fields = failure_fields(html)
    assert fields["field"] == "score"
    assert fields["recorded"] == str(recorded_score) == "85"
    assert fields["reconstructed"] == "86"
    assert "stored" not in fields and "recomputed" not in fields
    assert_no_counterfactual_result(html)

    assert element(html, "decision-score-threshold") == f"score {recorded_score} / threshold 75"


def test_a_consumed_input_mismatch_renders_both_lists_readably(seeded):
    envelope = mismatch_envelope(CONSUMED_MISMATCH_ID, reduce_score=False)
    assert seeded.post(envelope).status_code == 201
    reduced = next(
        u["contribution"]
        for u in envelope["payload"]["consumed_inputs"]
        if u["input_key"] == MISMATCH_KEY
    )
    original = next(
        u["contribution"]
        for u in canonical_by_type("decision.recorded")["payload"]["consumed_inputs"]
        if u["input_key"] == MISMATCH_KEY
    )
    assert (reduced, original) == (7, 8)

    html = page(seeded, CONSUMED_MISMATCH_ID)
    region = element(html, "replay-integrity-failure")
    assert "ReconstructionMismatch" in region

    fields = failure_fields(html)
    assert fields["field"] == "consumed_inputs"
    assert "stored" not in fields and "recomputed" not in fields

    evidence_version_id = canonical_evidence_ids()[MISMATCH_KEY]
    triple = f"{MISMATCH_KEY} · evidence version {evidence_version_id} · contribution "
    assert triple + str(reduced) in fields["recorded"]
    assert triple + str(original) in fields["reconstructed"]

    # One line per triple: five recorded, five reconstructed, plus `field`.
    assert region.count('<span class="failure-line">') == 11
    assert_no_counterfactual_result(html)


def test_a_failure_reproducing_the_original_leaves_the_recorded_sections_readable(
    seeded, monkeypatch
):
    monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSION", "evaluator-v2")
    html = page(seeded)

    fields = failure_fields(html)
    assert "IntegrityFailure" in element(html, "replay-integrity-failure")
    assert fields["field"] == "evaluator_version"
    assert_no_counterfactual_result(html)

    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    context = {row[0]: row for row in rows(html, "context-table")}
    for used in canonical_by_type("decision.recorded")["payload"]["consumed_inputs"]:
        assert context[used["input_key"]][3] == str(used["contribution"]), used["input_key"]
        assert context[used["input_key"]][2] == "consumed", used["input_key"]
    assert canonical_by_type("decision.recorded")["payload"]["explanation"] in element(
        html, "explanation"
    )
    assert "90 days" in element(html, "outcomes")
    assert "1.42 USD" in element(html, "actions")


# --- A request that is not answerable, and the failure view itself -------------


def test_a_query_parameter_that_is_not_an_artifact_hash_is_rejected(seeded):
    for bad in ("not-a-hash", "ABC" + "0" * 61, "0" * 63, "0" * 65, ""):
        response = seeded.client.get(decision_url() + f"?current={bad}")
        assert response.status_code == 400, bad
        assert "replay-panel" not in response.text


def test_the_failure_view_exposes_decision_not_found():
    view = failure_view(DecisionNotFound(DECISION_EVENT_ID))

    assert view.class_name == "DecisionNotFound"
    assert [field.name for field in view.fields] == ["decision_event_id"]
    assert view.fields[0].lines == (DECISION_EVENT_ID,)
    assert DECISION_EVENT_ID in view.message
