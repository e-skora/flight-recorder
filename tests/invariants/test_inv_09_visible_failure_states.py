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

The second half of this module covers the same responsibility for artifacts the
*collector accepts* and the *evaluator cannot interpret*. Schema v1 stores a
factor's rule as free prose and `missing_value_behavior` as any non-empty
string, so the closed grammar of `logic/rules.py` and the behaviors
`logic/evaluator.py` implements are narrower than what the ledger admits. Those
failures are `RuleError`s and `EvaluationError`s -- siblings of
`ReconstructionError`, not subclasses of it -- and each must reach the page as a
named, inspectable failure rather than a crash. A failure raised while
reproducing the decision's own recorded logic is a different fact from one
raised while evaluating the selected current artifact, and the page states
which, so a recorded decision whose own logic cannot be interpreted stays
viewable and is not blamed on the reader's selection.
"""

import copy

import pytest

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF, events, logic_artifacts
from flight_recorder.logic import evaluator as evaluator_module
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.reconstruct import DecisionNotFound
from flight_recorder.web.decision_view import (
    ARTIFACT_UNREADABLE,
    CONTEXT_WITHOUT_ARTIFACT,
    ORIGIN_RECORDED_LOGIC,
    ORIGIN_SELECTED_ARTIFACT,
    failure_view,
)
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


# --- Collector-accepted artifacts the evaluator cannot interpret ---------------
#
# The three shapes below pass strict schema validation and register with 201:
# schema v1 stores a factor's rule as free prose and `missing_value_behavior`
# as any non-empty string, so the closed grammar of `logic/rules.py` and the
# behaviors `logic/evaluator.py` implements are narrower than what the ledger
# accepts. Each is a `RuleError` or an `EvaluationError`, which are siblings of
# `ReconstructionError` rather than subclasses of it, and each must reach the
# page as a named, inspectable failure rather than a crash (INV-09).


def mutated_artifact(harness, artifact_id: str, logic_version: str, event_id: str, mutate) -> str:
    """Register a derived artifact after `mutate` has changed its content.

    Its own `logic_version` keeps the default resolution by `v5.1`
    unambiguous, so a page requested without a `current` parameter still
    resolves exactly one default artifact.
    """
    envelope = derived_artifact_envelope(
        artifact_id,
        logic_version,
        logic_artifact("v5.1")["factors"],
        event_id=event_id,
    )
    mutate(envelope["payload"]["artifact"])
    response = harness.post(envelope)
    assert response.status_code == 201, response.json()
    return canonical_hash(envelope["payload"]["artifact"])


UNSUPPORTED_RULE_TEXT = "employee_count is quite large"


def register_unsupported_rule_artifact(harness) -> str:
    def mutate(artifact: dict) -> None:
        assert artifact["factors"][0]["key"] == "employee_count"
        artifact["factors"][0]["rule"] = UNSUPPORTED_RULE_TEXT

    return mutated_artifact(
        harness,
        "test-unsupported-rule",
        "v5.1-test-unsupported-rule",
        "evt-system-logic-artifact-test-unsupported-rule",
        mutate,
    )


def assert_recorded_sections_intact(html: str) -> None:
    """The canonical decision's own record, unaffected by a replay failure."""
    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    context = {row[0]: row for row in rows(html, "context-table")}
    consumed = canonical_by_type("decision.recorded")["payload"]["consumed_inputs"]
    assert len(consumed) == 5
    for used in consumed:
        assert context[used["input_key"]][2] == "consumed", used["input_key"]
        assert context[used["input_key"]][3] == str(used["contribution"]), used["input_key"]
    assert canonical_by_type("decision.recorded")["payload"]["explanation"] in element(
        html, "explanation"
    )
    assert "1.42 USD" in element(html, "actions")
    assert "90 days" in element(html, "outcomes")


def test_an_unsupported_rule_in_the_selected_artifact_renders_a_named_failure(seeded):
    artifact_hash = register_unsupported_rule_artifact(seeded)

    html = page(seeded, query=f"?current={artifact_hash}")
    region = element(html, "replay-integrity-failure")
    assert "UnsupportedRule" in region

    fields = failure_fields(html)
    assert fields["key"] == "employee_count"
    assert fields["text"] == UNSUPPORTED_RULE_TEXT

    assert ORIGIN_SELECTED_ARTIFACT in region
    assert ORIGIN_RECORDED_LOGIC not in region
    assert_no_counterfactual_result(html)
    assert_recorded_sections_intact(html)


def test_an_unsupported_missing_value_behavior_renders_a_named_failure(seeded):
    artifact_hash = mutated_artifact(
        seeded,
        "test-bad-missing-value",
        "v5.1-test-bad-missing-value",
        "evt-system-logic-artifact-test-bad-missing-value",
        lambda artifact: artifact.update(missing_value_behavior="guess_the_value"),
    )

    html = page(seeded, query=f"?current={artifact_hash}")
    region = element(html, "replay-integrity-failure")
    assert "UnsupportedMissingValueBehavior" in region
    assert failure_fields(html)["behavior"] == "guess_the_value"

    assert ORIGIN_SELECTED_ARTIFACT in region
    assert_no_counterfactual_result(html)
    assert_recorded_sections_intact(html)


def test_a_rule_that_does_not_fit_the_preserved_value_renders_a_named_failure(seeded):
    def mutate(artifact: dict) -> None:
        industry = next(f for f in artifact["factors"] if f["key"] == "industry")
        industry["rule"] = "industry at least 10"

    artifact_hash = mutated_artifact(
        seeded,
        "test-rule-type-error",
        "v5.1-test-rule-type-error",
        "evt-system-logic-artifact-test-rule-type-error",
        mutate,
    )

    html = page(seeded, query=f"?current={artifact_hash}")
    region = element(html, "replay-integrity-failure")
    assert "RuleTypeError" in region

    fields = failure_fields(html)
    assert fields["key"] == "industry"
    assert fields["detail"].strip()

    assert ORIGIN_SELECTED_ARTIFACT in region
    assert_no_counterfactual_result(html)
    assert_recorded_sections_intact(html)


# --- A decision whose own recorded logic cannot be interpreted -----------------


HISTORICAL_BAD_RULE_ID = "evt-test-historical-bad-rule"


def test_a_decision_whose_own_logic_cannot_be_interpreted_still_shows_its_record(seeded):
    """The recorded decision stays viewable with no `current` parameter at all.

    The failure is raised while reproducing the original, so it belongs to the
    decision's own preserved logic rather than to any selection, and the page
    says so instead of blaming the artifact the reader chose.
    """
    artifact_hash = register_unsupported_rule_artifact(seeded)

    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = HISTORICAL_BAD_RULE_ID
    envelope["payload"]["logic_artifact"] = {
        "logic_version": "v5.1-test-unsupported-rule",
        "artifact_id": "test-unsupported-rule",
        "artifact_hash": artifact_hash,
        "evaluator_version": "evaluator-v1",
    }
    assert seeded.post(envelope).status_code == 201, envelope["event_id"]

    html = page(seeded, HISTORICAL_BAD_RULE_ID)
    region = element(html, "replay-integrity-failure")
    assert "UnsupportedRule" in region

    fields = failure_fields(html)
    assert fields["key"] == "employee_count"
    assert fields["text"] == UNSUPPORTED_RULE_TEXT

    assert ORIGIN_RECORDED_LOGIC in region
    assert ORIGIN_SELECTED_ARTIFACT not in region
    assert_no_counterfactual_result(html)

    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    context = {row[0]: row for row in rows(html, "context-table")}
    for entry in envelope["payload"]["historical_context"]:
        assert entry["input_key"] in context
    assert envelope["payload"]["explanation"] in element(html, "explanation")


# --- A recorded artifact whose stored text is not decodable -------------------


MALFORMED_HASH = "b" * 64
MALFORMED_ID = "logic-account-prioritization-malformed"
MALFORMED_VERSION = "v5.1-malformed"
MALFORMED_DECISION_ID = "evt-test-decision-malformed-artifact"


def insert_malformed_artifact(harness) -> None:
    """A `logic_artifacts` row with coherent columns and undecodable content.

    `INSERT` is permitted; `UPDATE` and `DELETE` are not, and no protected row
    is touched. Foreign keys are on, so the row needs a real `_system` event to
    point at (the `test_inv_05_evaluator_integrity.py` pattern).
    """
    source_event_id = "evt-system-logic-artifact-malformed"
    with harness.engine.begin() as conn:
        conn.execute(
            events.insert().values(
                event_id=source_event_id,
                schema_version="1",
                event_type="logic_artifact.registered",
                source="direct-insert-for-inv-09",
                account_ref=SYSTEM_ACCOUNT_REF,
                occurred_at="2026-05-05T09:00:01.000000Z",
                recorded_at="2026-05-05T09:00:01.000000Z",
                canonical_hash=canonical_hash({"malformed": source_event_id}),
                payload=canonical_text({"malformed": source_event_id}),
            )
        )
        conn.execute(
            logic_artifacts.insert().values(
                artifact_hash=MALFORMED_HASH,
                artifact_id=MALFORMED_ID,
                logic_version=MALFORMED_VERSION,
                decision_class="account_prioritization",
                artifact_schema_version="1",
                evaluator_version="evaluator-v1",
                artifact_json="{this is not decodable JSON",
                source_event_id=source_event_id,
            )
        )


def test_a_malformed_recorded_artifact_degrades_the_artifact_dependent_sections(seeded):
    """The artifact-dependent sections say what is unavailable and why.

    The ruleset cannot be listed and the `absent` rows cannot be derived, but
    the recorded summary, the preserved context and the failure region all
    still render (INV-09, `PRODUCT.md` §5 "Replay honesty").
    """
    insert_malformed_artifact(seeded)

    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = MALFORMED_DECISION_ID
    envelope["payload"]["logic_artifact"] = {
        "logic_version": MALFORMED_VERSION,
        "artifact_id": MALFORMED_ID,
        "artifact_hash": MALFORMED_HASH,
        "evaluator_version": "evaluator-v1",
    }
    assert seeded.post(envelope).status_code == 201, envelope["event_id"]

    html = page(seeded, MALFORMED_DECISION_ID)
    assert ARTIFACT_UNREADABLE in element(html, "ruleset")
    assert not has_element(html, "ruleset-table")
    assert CONTEXT_WITHOUT_ARTIFACT in element(html, "evidence-context")
    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"

    assert element(html, "replay-integrity-failure")
    assert_no_counterfactual_result(html)


# --- The failure view's own fallback ------------------------------------------


def test_an_unmapped_rule_family_member_still_names_itself_and_its_message():
    class UnmappedRuleFailure(RuleError):
        pass

    view = failure_view(UnmappedRuleFailure("a rule family member with no field map"))

    assert view.class_name == "UnmappedRuleFailure"
    assert view.fields, "an unmapped family member must not render an empty field table"
    rendered = {field.name: field.lines for field in view.fields}
    assert "UnmappedRuleFailure" in str(rendered)
    assert "a rule family member with no field map" in str(rendered)
