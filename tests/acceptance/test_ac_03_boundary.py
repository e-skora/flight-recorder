"""AC-03 (reconstruction half) / INV-02: the decision boundary, at replay.

`T(d)` of the canonical decision is stored as `2026-04-17T10:05:02.000000Z`.
Three instants matter: one millisecond before, exactly at, and one millisecond
after. Each is posted in at least two spellings -- `Z` and a UTC offset that
denotes the same instant -- so what these tests exercise is the collector's
normalization (`Timestamp`: aware, converted to UTC) and the reconstruction
that reads the normalized text, not any one spelling.

The counterfactual half of AC-03 (a counterfactual run under `v5.1`) is Phase 3.
"""

from datetime import UTC, datetime

import pytest

from flight_recorder.logic.evaluator import InputState
from flight_recorder.replay.reconstruct import load_context, load_decision_row, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    assert_same_reconstruction,
    canonical_boundary,
    canonical_by_type,
    canonical_raw,
    decision_envelope_with,
    decision_rows,
    evidence_envelope,
    evidence_version_row,
    factor,
    register_artifacts,
    seed_all,
    stored_form,
)

EMPLOYEE_COUNT_V1 = "ev-novasignal-employee-count-v1"
EMPLOYEE_COUNT_V2 = "ev-novasignal-employee-count-v2"
BOUNDARY_DECISION_ID = "evt-test-decision-boundary"

#: The stored form of `T(d)`.
T = "2026-04-17T10:05:02.000000Z"

#: instant label -> (stored `Z` form, every spelling posted for it).
SPELLINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "T-1ms": (
        "2026-04-17T10:05:01.999000Z",
        ("2026-04-17T10:05:01.999Z", "2026-04-17T03:05:01.999-07:00"),
    ),
    "T": (
        T,
        ("2026-04-17T10:05:02Z", "2026-04-17T03:05:02-07:00", "2026-04-17T15:35:02+05:30"),
    ),
    "T+1ms": (
        "2026-04-17T10:05:02.001000Z",
        ("2026-04-17T10:05:02.001Z", "2026-04-17T03:05:02.001-07:00"),
    ),
}


def spellings(*labels: str) -> list:
    """Parametrization over (instant, spelling) for the named instants.

    Every spelling is checked against its `Z` form here, so a test cannot drift
    from the instant it claims to exercise.
    """
    params = []
    for label in labels:
        z_form, forms = SPELLINGS[label]
        assert stored_form(z_form) == z_form
        for spelling in forms:
            assert datetime.fromisoformat(spelling) == datetime.fromisoformat(z_form), spelling
            assert stored_form(spelling) == z_form
            params.append(pytest.param(label, spelling, id=f"{label}:{spelling}"))
    return params


def test_the_instants_bracket_the_stored_boundary():
    assert T == stored_form(canonical_by_type("decision.recorded")["payload"]["decision_boundary"])
    assert canonical_boundary() == datetime.fromisoformat(T)
    before, at, after = (SPELLINGS[label][0] for label in ("T-1ms", "T", "T+1ms"))
    assert before < at < after
    assert (datetime.fromisoformat(at) - datetime.fromisoformat(before)).total_seconds() == 0.001
    assert (datetime.fromisoformat(after) - datetime.fromisoformat(at)).total_seconds() == 0.001


# --- Helpers ---------------------------------------------------------------


def evidence_at(instant: str, value: int = 40) -> dict:
    """`-v2` employee count, superseding `-v1`, recorded at `instant`.

    A supersession is legal at all three instants: `-v1` became available at
    `10:04:37`, before any of them.
    """
    return evidence_envelope(
        "evt-test-boundary-evidence",
        [
            {
                "evidence_version_id": EMPLOYEE_COUNT_V2,
                "evidence_type": "employee_count",
                "value": value,
                "supersedes_evidence_version_id": EMPLOYEE_COUNT_V1,
            }
        ],
        occurred_at=instant,
    )


def decision_using_v2() -> dict:
    """A decision at `T` preserving `-v2` / 40: outside the range, so 61."""
    return decision_envelope_with(
        BOUNDARY_DECISION_ID,
        input_key="employee_count",
        value=40,
        evidence_version_id=EMPLOYEE_COUNT_V2,
        contribution=0,
        score=61,
        output="DO_NOT_PRIORITIZE",
    )


def seed_through(harness: Harness, count: int) -> None:
    """Both registrations plus the first `count` account envelopes."""
    register_artifacts(harness)
    for index in range(count):
        assert harness.post_raw(canonical_raw(index)).status_code == 201


def reconstruction(harness, decision_event_id: str = DECISION_EVENT_ID):
    with harness.engine.connect() as conn:
        return reconstruct(conn, decision_event_id)


def employee_count_entry(harness, decision_event_id: str, boundary: str):
    with harness.engine.connect() as conn:
        context = load_context(conn, decision_event_id, boundary)
    return next(entry for entry in context if entry.key == "employee_count")


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


# --- Unreferenced evidence around the boundary --------------------------------


@pytest.mark.parametrize("label,spelling", spellings("T-1ms", "T", "T+1ms"))
def test_unreferenced_evidence_at_any_instant_around_the_boundary_leaves_the_sealed_context_alone(
    seeded, label, spelling
):
    """Membership in `H(d)` is sealed-context membership, not a time-window
    recomputation at replay: evidence recorded before, at, or after `T(d)` but
    never preserved by the decision changes nothing (§5 "Decision-time
    boundary")."""
    before = reconstruction(seeded)
    rows_before = decision_rows(seeded)

    response = seeded.post(evidence_at(spelling))
    assert response.status_code == 201, response.json()

    after = reconstruction(seeded)
    assert_same_reconstruction(after, before)
    employee_count = factor(after.result, "employee_count")
    assert employee_count.evidence_version_id == EMPLOYEE_COUNT_V1
    assert employee_count.contribution == 25
    assert employee_count_entry(seeded, DECISION_EVENT_ID, T).value == 184
    assert decision_rows(seeded) == rows_before

    stored = evidence_version_row(seeded, EMPLOYEE_COUNT_V2)
    assert stored.available_at == SPELLINGS[label][0] == stored_form(spelling)
    assert stored.value_json == '{"value":40}'


# --- A decision referencing evidence at or before its boundary ---------------


@pytest.mark.parametrize("label,spelling", spellings("T-1ms", "T"))
def test_a_decision_referencing_evidence_at_or_before_its_boundary_reconstructs_with_it(
    harness, label, spelling
):
    seed_through(harness, 3)
    assert harness.post(evidence_at(spelling)).status_code == 201

    response = harness.post(decision_using_v2())
    assert response.status_code == 201, response.json()

    reconstructed = reconstruction(harness, BOUNDARY_DECISION_ID)
    result = reconstructed.result
    assert (result.score, result.threshold, result.output) == (61, 75, "DO_NOT_PRIORITIZE")

    employee_count = factor(result, "employee_count")
    assert employee_count.input_state is InputState.CONSUMED
    assert employee_count.matched is False
    assert employee_count.contribution == 0
    assert employee_count.evidence_version_id == EMPLOYEE_COUNT_V2
    assert employee_count_entry(harness, BOUNDARY_DECISION_ID, T).value == 40

    assert reconstructed.decision_boundary == canonical_boundary()
    assert reconstructed.decision_boundary.tzinfo is not None
    assert reconstructed.decision_boundary.utcoffset().total_seconds() == 0
    assert reconstructed.decision_boundary == datetime.fromisoformat(T).astimezone(UTC)


# --- A reference after the boundary, in every spelling -------------------------


@pytest.mark.parametrize("label,spelling", spellings("T+1ms"))
def test_a_decision_referencing_evidence_after_its_boundary_is_rejected_in_every_spelling(
    harness, label, spelling
):
    seed_through(harness, 3)
    assert harness.post(evidence_at(spelling)).status_code == 201
    snapshot = harness.snapshot()

    response = harness.post(decision_using_v2())
    assert response.status_code == 422, response.json()
    body = response.json()
    assert body["reason"] == "evidence_version_available_after_the_boundary"
    assert EMPLOYEE_COUNT_V2 in body["detail"]
    assert body["evidence_version_id"] == EMPLOYEE_COUNT_V2
    assert harness.snapshot() == snapshot


# --- The boundary itself, spelled with an offset -------------------------------


OFFSET_BOUNDARY = "2026-04-17T03:05:02-07:00"


def test_a_decision_boundary_spelled_with_an_offset_is_the_same_instant(harness):
    assert datetime.fromisoformat(OFFSET_BOUNDARY) == canonical_boundary()
    seed_through(harness, 3)

    envelope = canonical_by_type("decision.recorded")
    envelope["occurred_at"] = OFFSET_BOUNDARY
    envelope["recorded_at"] = OFFSET_BOUNDARY
    envelope["payload"]["decision_boundary"] = OFFSET_BOUNDARY
    response = harness.post(envelope)
    assert response.status_code == 201, response.json()

    stored = decision_rows(harness)["decisions"]
    assert len(stored) == 1
    with harness.engine.connect() as conn:
        assert load_decision_row(conn, DECISION_EVENT_ID).decision_boundary == T

    reconstructed = reconstruction(harness)
    assert reconstructed.result.score == 86
    assert reconstructed.decision_boundary == canonical_boundary()


# --- A naive timestamp never enters the ledger ---------------------------------


def test_a_naive_timestamp_never_enters_the_ledger(seeded):
    before = reconstruction(seeded)
    snapshot = seeded.snapshot()

    response = seeded.post(evidence_at("2026-04-17T10:05:01.999"))
    assert response.status_code == 422, response.json()
    assert response.json()["reason"] == "invalid_envelope"

    assert seeded.snapshot() == snapshot
    assert evidence_version_row(seeded, EMPLOYEE_COUNT_V2) is None
    assert_same_reconstruction(reconstruction(seeded), before)
