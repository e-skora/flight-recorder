"""D-017 on screen: the demo default, the comparison it renders, and the bound note.

Three places present an artifact, and each reports that artifact's own bound
from that artifact's own registered content: the recorded ruleset section, each
selector entry, and the in-effect panel. The note is scoped per artifact, so
`v5.1`'s note is shown while `v5.2` is selected, and no note is attached to
`v3.2` or to `v5.2` (AC-02, AC-07, INV-03, INV-05).

The note is read-side only. It changes no score, no output and no stored row,
it is never styled or worded as a failure, and it never replaces one: an
artifact that fails verification still renders its named failure and no
comparison, whether or not a note sits beside it.

Tests 8 (rendered), 10, 11, 12 and 14 of the task's numbering.
"""

import copy

import pytest

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.fixtures import canonical_artifacts, current_logic_artifact
from flight_recorder.ledger.schema import logic_artifacts
from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
from flight_recorder.web.decision_view import positive_weight_bound_view
from tests.acceptance.test_decision_detail_page import (
    element,
    has_element,
    page,
    row_for,
)
from tests.conftest import (
    canonical_boundary,
    canonical_by_type,
    canonical_context,
    canonical_raw,
    derived_artifact_envelope,
    evidence_envelope,
    logic_artifact,
    register_current_logic,
    register_derived_artifact,
    replace_context,
    seed_all,
    system_raw,
    v5_1_hash,
    v5_2_hash,
)

PRESSURE = "verified_integration_pressure"

#: The exact sentence the task fixes for `v5.1`.
V5_1_NOTE = (
    "Positive weights total 72, below threshold 75; the score cannot reach this "
    "threshold under evaluator-v1."
)

ALTERNATE_DECISION_ID = "evt-test-decision-high-pressure"
HIGH_PRESSURE_EVIDENCE_ID = "ev-test-verified-integration-pressure-high"
UNREACHABLE_DECISION_ID = "evt-test-decision-under-unreachable-artifact"
MISMATCH_DECISION_ID = "evt-test-decision-unreachable-artifact-mismatch"

#: Wordings the note must never carry: each asserts a replay outcome that the
#: note itself has not established, and each contradicted a visible failure.
REPLAY_SUCCESS_CLAIMS = (
    "registered and replayable",
    "replays exactly as it was recorded",
    "a replay under it succeeds",
)


def assert_claims_no_replay_success(note: str) -> None:
    """The note describes the artifact and never asserts that a replay succeeded."""
    for phrase in REPLAY_SUCCESS_CLAIMS:
        assert phrase not in note, phrase


@pytest.fixture
def seeded(harness):
    """The canonical nine, then the `v5.2` overlay: the demo, in its own order."""
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    register_current_logic(harness)
    return harness


def v3_2_hash() -> str:
    return canonical_hash(logic_artifact("v3.2"))


def selector(html: str) -> str:
    return element(html, "current-logic-selector")


# --- Disposable test data -------------------------------------------------------


def unreachable_artifact_envelope(*, below_threshold: str | None = None, suffix: str = "") -> dict:
    """`v5.1`'s weights under their own label: positive sum 72, threshold 75.

    Never canonical, and never `v5.1` itself: the canonical artifacts are
    immutable and this module only ever adds beside them.
    """
    envelope = derived_artifact_envelope(
        f"logic-account-prioritization-test-unreachable{suffix}",
        f"test-unreachable{suffix}",
        copy.deepcopy(logic_artifact("v5.1")["factors"]),
        event_id=f"evt-system-logic-artifact-test-unreachable{suffix}",
    )
    if below_threshold is not None:
        envelope["payload"]["artifact"]["output_mapping"]["below_threshold"] = below_threshold
    return envelope


def decision_recorded_under(envelope: dict, event_id: str) -> dict:
    """A disposable decision recorded under `envelope`'s artifact.

    Its score, output and consumed contributions are computed from that
    artifact over the canonical preserved context, so the decision reconstructs
    exactly and the page's failure paths stay out of the way. The canonical
    recorded decision is not touched: this is a separate event.
    """
    artifact = LogicArtifact.model_validate(envelope["payload"]["artifact"])
    result = evaluate(artifact, canonical_context(), canonical_boundary())

    decision = copy.deepcopy(canonical_by_type("decision.recorded"))
    decision["event_id"] = event_id
    payload = decision["payload"]
    payload["logic_artifact"] = {
        "logic_version": artifact.logic_version,
        "artifact_id": artifact.artifact_id,
        "artifact_hash": canonical_hash(envelope["payload"]["artifact"]),
        "evaluator_version": artifact.evaluator_version,
    }
    values = {entry.key: entry.value for entry in canonical_context()}
    payload["consumed_inputs"] = [
        {
            "input_key": factor.key,
            "value": values[factor.key],
            "evidence_version_id": factor.evidence_version_id,
            "contribution": factor.contribution,
        }
        for factor in result.factors
        if factor.input_state is InputState.CONSUMED
    ]
    payload["result"] = {
        "score": result.score,
        "threshold": result.threshold,
        "output": result.output,
    }
    return decision


# --- 10. The three locations, scoped per artifact -------------------------------


def test_the_selector_carries_the_note_for_v5_1_while_v5_2_is_selected(seeded):
    """Test 10. The note belongs to `v5.1` and is shown even when it is not in effect."""
    html = page(seeded)

    assert V5_1_NOTE in selector(html)
    bounds = element(html, "artifact-positive-weight-bounds")
    assert v5_1_hash() in bounds
    assert v3_2_hash() not in bounds, "v3.2 reaches its threshold; it gets no note"
    assert v5_2_hash() not in bounds, "v5.2 reaches its threshold; it gets no note"
    # The selected artifact is v5.2, and it carries no in-effect note.
    assert not has_element(html, "in-effect-positive-weight-bound")


def test_the_in_effect_panel_carries_the_note_when_v5_1_is_selected_by_hash(seeded):
    """Test 10. Selecting the preserved artifact reports its own bound."""
    html = page(seeded, query=f"?current={v5_1_hash()}")

    in_effect = element(html, "in-effect-positive-weight-bound")
    assert V5_1_NOTE in in_effect
    assert_claims_no_replay_success(in_effect)
    # The comparison still runs and is unaffected by the note.
    assert element(html, "counterfactual-score") == "51"


def test_no_note_is_attached_to_v3_2_or_to_v5_2_in_the_in_effect_panel(seeded):
    """Test 10. Absence is scoped to the artifact, not to the page."""
    for artifact_hash in (v3_2_hash(), v5_2_hash()):
        html = page(seeded, query=f"?current={artifact_hash}")
        assert not has_element(html, "in-effect-positive-weight-bound"), artifact_hash
        # The selector still reports v5.1's note; only the in-effect one is absent.
        assert V5_1_NOTE in selector(html), artifact_hash


def test_the_recorded_ruleset_section_reports_the_note_for_the_artifact_that_ran(seeded):
    """Test 10. A decision actually recorded under an unreachable artifact.

    Built as disposable data beside the canonical decision, which keeps its own
    ruleset section free of any note.
    """
    envelope = unreachable_artifact_envelope()
    register_derived_artifact(seeded, envelope)
    decision = decision_recorded_under(envelope, UNREACHABLE_DECISION_ID)
    response = seeded.post(decision)
    assert response.status_code == 201, response.json()

    html = page(seeded, UNREACHABLE_DECISION_ID)
    recorded = element(html, "recorded-positive-weight-bound")
    assert V5_1_NOTE in recorded
    assert "not a failure" in recorded
    assert_claims_no_replay_success(recorded)
    assert element(html, "decision-output") == "DO_NOT_PRIORITIZE"
    assert element(html, "decision-score-threshold") == "score 51 / threshold 75"

    # The canonical decision ran under v3.2, which reaches its threshold.
    assert not has_element(page(seeded), "recorded-positive-weight-bound")


def test_the_output_consequence_is_read_from_the_artifacts_own_mapping(seeded):
    """Test 10. A below-threshold label that is not `DO_NOT_PRIORITIZE`.

    The schema admits any non-empty label, so the consequence sentence is
    derived from the artifact and never hardcoded.
    """
    envelope = unreachable_artifact_envelope(below_threshold="HOLD", suffix="-hold")
    artifact_hash = register_derived_artifact(seeded, envelope)

    note = element(
        page(seeded, query=f"?current={artifact_hash}"), "in-effect-positive-weight-bound"
    )

    assert V5_1_NOTE in note
    assert "can therefore only output HOLD" in note
    assert "DO_NOT_PRIORITIZE" not in note


def test_the_note_never_replaces_a_visible_artifact_failure(seeded):
    """The disposition's governing rule for condition 3's closure.

    A note may sit beside a failure; it must not stand in for one, create a
    successful comparison, or crash the page. This artifact carries `v5.1`'s
    below-threshold weights *and* a rule the closed grammar refuses.
    """
    envelope = unreachable_artifact_envelope(suffix="-unsupported")
    factors = envelope["payload"]["artifact"]["factors"]
    assert factors[0]["key"] == "employee_count"
    factors[0]["rule"] = "employee_count is quite large"
    artifact_hash = register_derived_artifact(seeded, envelope)

    html = page(seeded, query=f"?current={artifact_hash}")

    assert "UnsupportedRule" in element(html, "replay-integrity-failure")
    assert not has_element(html, "replay-comparison")
    # The note is still the artifact's own property, and claims nothing about the failure.
    note = element(html, "in-effect-positive-weight-bound")
    assert V5_1_NOTE in note
    assert_claims_no_replay_success(note)


def test_the_recorded_note_makes_no_success_claim_when_the_record_does_not_reproduce(seeded):
    """Finding 2's second half: a failing *recorded* decision, not a failing selection.

    The decision is recorded under an unreachable artifact and its stored score
    is one point below what that artifact reconstructs, so the page renders
    `ReconstructionMismatch` and no comparison. The recorded ruleset section
    still reports the artifact's own bound -- and must not assert, beside a
    visible reconstruction failure, that the decision replays as recorded.
    """
    envelope = unreachable_artifact_envelope(suffix="-mismatch")
    register_derived_artifact(seeded, envelope)
    decision = decision_recorded_under(envelope, MISMATCH_DECISION_ID)
    reconstructed = decision["payload"]["result"]["score"]
    decision["payload"]["result"] = {
        **decision["payload"]["result"],
        "score": reconstructed - 1,
    }
    assert seeded.post(decision).status_code == 201

    html = page(seeded, MISMATCH_DECISION_ID)

    region = element(html, "replay-integrity-failure")
    assert "ReconstructionMismatch" in region
    assert not has_element(html, "replay-comparison")
    assert not has_element(html, "counterfactual-score")

    recorded = element(html, "recorded-positive-weight-bound")
    assert V5_1_NOTE in recorded
    assert_claims_no_replay_success(recorded)


def test_an_unreadable_registered_artifact_renders_no_note_and_no_page_error(seeded):
    """Parsing the extra selector content must not introduce an unhandled error."""
    assert positive_weight_bound_view(None) is None

    with seeded.engine.begin() as conn:
        conn.execute(
            logic_artifacts.insert().values(
                artifact_hash="f" * 64,
                artifact_id="logic-account-prioritization-unreadable",
                artifact_schema_version="1",
                logic_version="v9.9-unreadable",
                decision_class="account_prioritization",
                evaluator_version="evaluator-v1",
                artifact_json="{not json",
                source_event_id="evt-system-00b-logic-artifact-v5.1",
            )
        )

    html = page(seeded)

    assert "v9.9-unreadable" in selector(html)
    assert V5_1_NOTE in selector(html)
    assert element(html, "counterfactual-score") == "72"


# --- 11. The demo default renders 86 to 72 --------------------------------------


def test_with_no_current_parameter_the_default_resolves_to_v5_2_and_compares(seeded):
    """Test 11. 86 becomes 72, delta -14, output changed, full hash named."""
    html = page(seeded)

    assert element(html, "counterfactual-logic-version") == "v5.2"
    assert v5_2_hash() in element(html, "counterfactual-artifact-hash")
    assert element(html, "original-score") == "86"
    assert element(html, "counterfactual-score") == "72"
    assert element(html, "score-delta") == "-14"
    assert element(html, "original-threshold") == "75"
    assert element(html, "counterfactual-threshold") == "75"
    assert element(html, "original-output") == "PRIORITIZE"
    assert element(html, "counterfactual-output") == "DO_NOT_PRIORITIZE"
    assert element(html, "output-changed") == "output changed: yes"
    assert "the default was resolved by logic version v5.2" in selector(html)


def test_a_different_preserved_context_renders_its_own_counterfactual_score(seeded):
    """Test 11. A varied context, so a hardcoded displayed 72 cannot pass.

    A second disposable decision preserves `HIGH` integration pressure instead
    of `LOW`. Under the same default `v5.2` it scores 87, not 72.
    """
    evidence = evidence_envelope(
        "evt-test-evidence-integration-pressure-high",
        [
            {
                "evidence_version_id": HIGH_PRESSURE_EVIDENCE_ID,
                "evidence_type": PRESSURE,
                "value": "HIGH",
                "basis": ["synthetic demonstration value"],
            }
        ],
        occurred_at="2026-04-17T10:04:51Z",
        source="relaybridge-research-sim",
    )
    response = seeded.post(evidence)
    assert response.status_code == 201, response.json()

    context = replace_context(
        canonical_context(),
        PRESSURE,
        ContextInput(
            key=PRESSURE,
            availability="available",
            value="HIGH",
            evidence_version_id=HIGH_PRESSURE_EVIDENCE_ID,
        ),
    )
    result = evaluate(canonical_artifacts()["v3.2"], context, canonical_boundary())

    decision = copy.deepcopy(canonical_by_type("decision.recorded"))
    decision["event_id"] = ALTERNATE_DECISION_ID
    payload = decision["payload"]
    entry = next(e for e in payload["historical_context"] if e["input_key"] == PRESSURE)
    entry.update(value="HIGH", evidence_version_id=HIGH_PRESSURE_EVIDENCE_ID)
    payload["result"] = {
        "score": result.score,
        "threshold": result.threshold,
        "output": result.output,
    }
    response = seeded.post(decision)
    assert response.status_code == 201, response.json()

    html = page(seeded, ALTERNATE_DECISION_ID)

    assert element(html, "counterfactual-logic-version") == "v5.2"
    assert element(html, "counterfactual-score") == "87"
    assert element(html, "counterfactual-output") == "PRIORITIZE"


# --- 12. The original comparison is exactly reproducible ------------------------


def test_selecting_v5_1_by_hash_still_renders_86_to_51_with_its_factor_detail(seeded):
    """Test 12. Delta -35, and the pressure factor still explains the whole difference."""
    html = page(seeded, query=f"?current={v5_1_hash()}")

    assert element(html, "original-score") == "86"
    assert element(html, "counterfactual-score") == "51"
    assert element(html, "score-delta") == "-35"
    assert element(html, "counterfactual-logic-version") == "v5.1"
    assert element(html, "output-changed") == "output changed: yes"

    pressure = row_for(html, "contributions-table", PRESSURE)
    assert pressure[1] == "added"
    assert pressure[4] == "consumed"
    assert pressure[5] == "-21"
    assert pressure[6] == "-21"


# --- 14. The default-dependent states, now against v5.2 -------------------------


def test_a_missing_v5_2_names_itself_and_explicit_selection_still_works(harness):
    """Test 14. No overlay registered: the named state, then recovery by hash."""
    for index in (0, 1):
        assert harness.post_raw(system_raw(index)).status_code == 201
    for index in range(4):
        assert harness.post_raw(canonical_raw(index)).status_code == 201

    html = page(harness)
    assert "Default replay logic v5.2 is not registered for this decision class." in element(
        html, "replay-no-selection"
    )
    assert not has_element(html, "replay-comparison")

    explicit = page(harness, query=f"?current={v5_1_hash()}")
    assert element(explicit, "counterfactual-score") == "51"
    assert V5_1_NOTE in element(explicit, "in-effect-positive-weight-bound")


def test_two_artifacts_labelled_v5_2_force_an_explicit_selection(seeded):
    """Test 14. Ambiguity is named, never broken by recency or activation date."""
    envelope = derived_artifact_envelope(
        "logic-account-prioritization-v5.2-duplicate",
        "v5.2",
        current_logic_artifact().model_dump(mode="json")["factors"],
        event_id="evt-system-logic-artifact-v5.2-duplicate",
    )
    duplicate_hash = register_derived_artifact(seeded, envelope)
    assert duplicate_hash != v5_2_hash()

    html = page(seeded)
    no_selection = element(html, "replay-no-selection")
    assert "More than one registered artifact carries logic version v5.2" in no_selection
    assert v5_2_hash() in no_selection and duplicate_hash in no_selection
    assert not has_element(html, "replay-comparison")

    explicit = page(seeded, query=f"?current={v5_2_hash()}")
    assert element(explicit, "counterfactual-score") == "72"


# --- 8, rendered. Three missing-evidence states, three rendered words ------------


def test_a_consumed_low_pressure_input_renders_consumed(seeded):
    """Test 8, rendered. The canonical decision preserves `LOW`: consumed, 72."""
    html = page(seeded)

    assert element(html, "counterfactual-score") == "72"
    assert row_for(html, "contributions-table", PRESSURE)[4] == "consumed"
    assert row_for(html, "contributions-table", PRESSURE)[5] == "0"


def test_an_unavailable_pressure_input_renders_unavailable_at_the_same_score(seeded):
    """Test 8, rendered. Explicitly unavailable is its own word, not `absent`."""
    decision = copy.deepcopy(canonical_by_type("decision.recorded"))
    decision["event_id"] = "evt-test-decision-pressure-unavailable"
    payload = decision["payload"]
    entry = next(e for e in payload["historical_context"] if e["input_key"] == PRESSURE)
    entry.clear()
    entry.update(input_key=PRESSURE, value=None, availability="unavailable")
    response = seeded.post(decision)
    assert response.status_code == 201, response.json()

    html = page(seeded, "evt-test-decision-pressure-unavailable")

    assert element(html, "counterfactual-score") == "72"
    assert row_for(html, "contributions-table", PRESSURE)[4] == "unavailable"
    assert row_for(html, "missing-inputs-table", PRESSURE)[1] == "unavailable"


def test_an_absent_pressure_input_renders_absent_at_the_same_score(seeded):
    """Test 8, rendered. `absent` is a third word, not a synonym for the others."""
    decision = copy.deepcopy(canonical_by_type("decision.recorded"))
    decision["event_id"] = "evt-test-decision-pressure-absent"
    payload = decision["payload"]
    payload["historical_context"] = [
        entry for entry in payload["historical_context"] if entry["input_key"] != PRESSURE
    ]
    response = seeded.post(decision)
    assert response.status_code == 201, response.json()

    html = page(seeded, "evt-test-decision-pressure-absent")

    assert element(html, "counterfactual-score") == "72"
    assert row_for(html, "contributions-table", PRESSURE)[4] == "absent"
    assert row_for(html, "missing-inputs-table", PRESSURE)[1] == "absent"
