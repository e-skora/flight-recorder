"""AC-07 (integrity half): every failure is explicit, named, and before evaluation.

These exercise the same pure helpers `reconstruct` calls, so a case here is a
case in the real path. Verification stops at the first failing check, so each
test keeps every earlier check consistent and breaks exactly one thing. That
matters most for identity fields: changing an artifact's `evaluator_version`
changes its content hash, so the hash is recomputed and every reference to it
realigned first -- otherwise the test would only be re-proving the hash check.
"""

import copy
from dataclasses import replace

import pytest

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.logic.evaluator import evaluate
from flight_recorder.replay.reconstruct import (
    ArtifactMissing,
    ArtifactRow,
    ConsumedInputRow,
    DecisionRow,
    IntegrityFailure,
    ReconstructionMismatch,
    compare_with_recorded,
    verify_artifact,
)
from tests.conftest import (
    DECISION_EVENT_ID,
    canonical_boundary,
    canonical_consumed_rows,
    canonical_context,
    logic_artifact,
)


def stored_content(**overrides) -> dict:
    """The canonical `v3.2` artifact content, as the ledger stores it."""
    content = copy.deepcopy(logic_artifact("v3.2"))
    content.update(overrides)
    return content


def consistent(content: dict) -> tuple[ArtifactRow, DecisionRow]:
    """An artifact row and a decision row that agree with `content` in every field.

    Every case below starts here and breaks one thing, so the check it reaches
    is the check it names.
    """
    digest = canonical_hash(content)
    artifact_row = ArtifactRow(
        artifact_hash=digest,
        artifact_id=content["artifact_id"],
        artifact_schema_version=content["artifact_schema_version"],
        logic_version=content["logic_version"],
        decision_class=content["decision_class"],
        evaluator_version=content["evaluator_version"],
        artifact_json=canonical_text(content),
    )
    decision_row = DecisionRow(
        decision_event_id=DECISION_EVENT_ID,
        account_ref="novasignal-ai",
        decision_class=content["decision_class"],
        decision_boundary="2026-04-17T10:05:02.000000Z",
        artifact_hash=digest,
        logic_version=content["logic_version"],
        evaluator_version=content["evaluator_version"],
        score=86,
        threshold=75,
        output="PRIORITIZE",
    )
    return artifact_row, decision_row


def canonical_rows() -> tuple[ArtifactRow, DecisionRow]:
    return consistent(stored_content())


def test_the_baseline_rows_verify_so_each_case_below_isolates_one_check():
    artifact_row, decision_row = canonical_rows()
    verified = verify_artifact(artifact_row, decision_row)

    assert verified.recomputed_artifact_hash == artifact_row.artifact_hash
    assert verified.artifact.logic_version == "v3.2"
    assert verified.runtime_evaluator_version == "evaluator-v1"


def test_a_missing_artifact_is_an_explicit_failure_not_a_best_effort_replay():
    _, decision_row = canonical_rows()
    with pytest.raises(ArtifactMissing) as caught:
        verify_artifact(None, decision_row)
    assert caught.value.artifact_hash == decision_row.artifact_hash


def test_malformed_artifact_json_names_artifact_json():
    artifact_row, decision_row = canonical_rows()
    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(replace(artifact_row, artifact_json="{not json"), decision_row)
    assert caught.value.field == "artifact_json"


def test_a_stored_hash_that_disagrees_with_the_content_names_artifact_hash():
    artifact_row, decision_row = canonical_rows()
    tampered = "0" * 64
    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(
            replace(artifact_row, artifact_hash=tampered),
            replace(decision_row, artifact_hash=tampered),
        )
    assert caught.value.field == "artifact_hash"
    assert caught.value.stored == tampered
    assert caught.value.recomputed == artifact_row.artifact_hash


def test_content_that_hashes_correctly_but_fails_strict_validation_names_artifact_schema():
    content = stored_content()
    content["factors"] = copy.deepcopy(content["factors"])
    content["factors"][0]["weight"] = 25.0  # schema v1 has no float fields
    artifact_row, decision_row = consistent(content)

    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(artifact_row, decision_row)
    assert caught.value.field == "artifact_schema"


def test_a_row_column_that_disagrees_with_its_own_content_names_that_field():
    artifact_row, decision_row = canonical_rows()
    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(replace(artifact_row, logic_version="v3.2-relabeled"), decision_row)
    assert caught.value.field == "logic_version"
    assert (caught.value.stored, caught.value.recomputed) == ("v3.2-relabeled", "v3.2")


def test_a_decision_label_that_disagrees_with_the_verified_artifact_names_that_field():
    """AC-07: changing a logic label does not silently alter results."""
    artifact_row, decision_row = canonical_rows()
    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(artifact_row, replace(decision_row, logic_version="v3.3"))
    assert caught.value.field == "logic_version"
    assert (caught.value.stored, caught.value.recomputed) == ("v3.3", "v3.2")


def test_an_artifact_written_for_another_evaluator_names_evaluator_version():
    """Consistent all the way down -- including the recomputed hash -- and still
    refused, because this runtime cannot claim to replay `evaluator-v2` exactly."""
    artifact_row, decision_row = consistent(stored_content(evaluator_version="evaluator-v2"))

    verify_artifact(artifact_row, decision_row, runtime_evaluator_version="evaluator-v2")

    with pytest.raises(IntegrityFailure) as caught:
        verify_artifact(artifact_row, decision_row)
    assert caught.value.field == "evaluator_version"
    assert (caught.value.stored, caught.value.recomputed) == ("evaluator-v2", "evaluator-v1")


# --- Result divergence (step 11) -------------------------------------------


def canonical_result():
    artifact_row, decision_row = canonical_rows()
    verified = verify_artifact(artifact_row, decision_row)
    return (
        verified,
        decision_row,
        evaluate(verified.artifact, canonical_context(), canonical_boundary()),
    )


def test_a_stored_score_that_differs_from_the_reconstruction_is_a_mismatch():
    _, decision_row, result = canonical_result()

    compare_with_recorded(result, decision_row, canonical_consumed_rows())

    with pytest.raises(ReconstructionMismatch) as caught:
        compare_with_recorded(result, replace(decision_row, score=85), canonical_consumed_rows())
    assert caught.value.field == "score"
    assert (caught.value.recorded, caught.value.reconstructed) == (85, 86)


def mutated_consumed(**mutation) -> tuple[ConsumedInputRow, ...]:
    rows = list(canonical_consumed_rows())
    if "drop" in mutation:
        return tuple(row for row in rows if row.input_key != mutation["drop"])
    if "add" in mutation:
        return (*rows, mutation["add"])
    index = next(i for i, row in enumerate(rows) if row.input_key == mutation["key"])
    rows[index] = replace(rows[index], **mutation["change"])
    return tuple(rows)


@pytest.mark.parametrize(
    ("label", "rows"),
    [
        ("a missing consumed row", lambda: mutated_consumed(drop="industry")),
        (
            "an extra consumed row",
            lambda: mutated_consumed(
                add=ConsumedInputRow(
                    input_key="verified_integration_pressure",
                    evidence_version_id="ev-novasignal-verified-integration-pressure-v1",
                    contribution=0,
                )
            ),
        ),
        (
            "a wrong evidence version",
            lambda: mutated_consumed(
                key="employee_count",
                change={"evidence_version_id": "ev-novasignal-employee-count-v2"},
            ),
        ),
        (
            "a wrong contribution",
            lambda: mutated_consumed(key="industry", change={"contribution": 21}),
        ),
    ],
)
def test_a_consumed_input_that_differs_from_the_reconstruction_is_a_mismatch(label, rows):
    _, decision_row, result = canonical_result()

    with pytest.raises(ReconstructionMismatch) as caught:
        compare_with_recorded(result, decision_row, rows())
    assert caught.value.field == "consumed_inputs", label
    assert caught.value.recorded != caught.value.reconstructed
