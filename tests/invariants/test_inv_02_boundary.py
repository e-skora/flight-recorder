"""INV-02 at the invariant level: `available_at(e) > T(d) => e not in H(d)`.

Two proofs:

1. Generatively (AC-18): evidence recorded at a random instant around `T(d)`,
   in a random UTC offset, is admitted into a decision at `T(d)` if and only if
   it is not after `T(d)`; when admitted it reconstructs exactly, and the
   canonical decision is never touched.
2. At the database: a preserved context reference whose stored `available_at`
   is after the decision boundary is an explicit `IntegrityFailure` at the
   context read, before any evaluation, for a consumed input and for a
   historically available but ignored input alike; a reference available
   exactly at the boundary is admitted and reconstructs. The rows are inserted
   directly (INSERT is not blocked by the append-only triggers) in the
   normalized storage format, so the only thing wrong with them is the
   availability time.
"""

from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import (
    decision_consumed_inputs,
    decision_context,
    decisions,
    events,
    evidence_versions,
)
from flight_recorder.logic.evaluator import InputState
from flight_recorder.replay import reconstruct as reconstruct_module
from flight_recorder.replay.reconstruct import IntegrityFailure, load_context, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    assert_same_reconstruction,
    canonical_boundary,
    consumed_versions,
    decision_envelope_with,
    evidence_envelope,
    factor,
    seed_all,
    stored_form,
)
from tests.invariants.test_inv_05_evaluator_integrity import refuse_to_evaluate

pytestmark = pytest.mark.invariant

ACCOUNT_REF = "novasignal-ai"
EMPLOYEE_COUNT_V1 = "ev-novasignal-employee-count-v1"
EMPLOYEE_COUNT_V2 = "ev-novasignal-employee-count-v2"
BOUNDARY_DECISION_ID = "evt-test-decision-boundary"

#: Every consumed factor of the canonical decision resolves to its `-v1` id.
PRESERVED = {
    "employee_count": "ev-novasignal-employee-count-v1",
    "industry": "ev-novasignal-industry-v1",
    "funding_event": "ev-novasignal-funding-event-v1",
    "open_platform_engineering_roles": "ev-novasignal-open-platform-engineering-roles-v1",
    "headquarters_country": "ev-novasignal-headquarters-country-v1",
}


# --- Rendering an instant in a UTC offset ----------------------------------

OFFSETS = ["Z", "+00:00", "-07:00", "+05:30", "+14:00", "-12:00"]


def render(instant: datetime, offset: str) -> str:
    """`instant` as ISO-8601 text in `offset`, at microsecond precision.

    `Z` is rendered as the letter; every other offset as `+HH:MM` / `-HH:MM`.
    The rendering is checked against the instant it came from, so the test
    cannot silently drift from the instant it claims to exercise.
    """
    if offset == "Z":
        text = format_utc(instant)
    else:
        zone = datetime.fromisoformat(f"2000-01-01T00:00:00{offset}").tzinfo
        text = instant.astimezone(zone).isoformat(timespec="microseconds")
        assert text.endswith(offset)
    assert datetime.fromisoformat(text) == instant
    assert stored_form(text) == format_utc(instant)
    return text


def expected_contribution(employee_count: int) -> int:
    return 25 if 50 <= employee_count <= 500 else 0


# --- The generative case (AC-18) ----------------------------------------------


@given(
    delta_ms=st.one_of(
        st.sampled_from([-1, 0, 1]),
        st.integers(min_value=-25_000, max_value=86_400_000),
    ),
    offset=st.sampled_from(OFFSETS),
    employee_count=st.integers(min_value=1, max_value=200_000),
    supersedes=st.booleans(),
)
def test_evidence_at_a_random_instant_around_the_boundary_is_admitted_iff_not_after_it(
    tmp_path_factory, delta_ms, offset, employee_count, supersedes
):
    harness = Harness(tmp_path_factory.mktemp("inv02-boundary"))
    for response in seed_all(harness):
        assert response.status_code == 201
    with harness.engine.connect() as conn:
        before = reconstruct(conn, DECISION_EVENT_ID)

    boundary = canonical_boundary()
    instant = boundary + timedelta(milliseconds=delta_ms)
    item = {
        "evidence_version_id": EMPLOYEE_COUNT_V2,
        "evidence_type": "employee_count",
        "value": employee_count,
    }
    if supersedes:
        # Legal at every drawn instant: `-v1` became available 25 seconds
        # before `T(d)`, and a correction may not precede what it supersedes.
        item["supersedes_evidence_version_id"] = EMPLOYEE_COUNT_V1
    response = harness.post(
        evidence_envelope("evt-test-boundary-evidence", [item], occurred_at=render(instant, offset))
    )
    assert response.status_code == 201, response.json()

    contribution = expected_contribution(employee_count)
    score = 61 + contribution
    output = "PRIORITIZE" if score >= 75 else "DO_NOT_PRIORITIZE"
    response = harness.post(
        decision_envelope_with(
            BOUNDARY_DECISION_ID,
            input_key="employee_count",
            value=employee_count,
            evidence_version_id=EMPLOYEE_COUNT_V2,
            contribution=contribution,
            score=score,
            output=output,
        )
    )

    if delta_ms <= 0:
        assert response.status_code == 201, (delta_ms, offset, response.json())
        with harness.engine.connect() as conn:
            reconstructed = reconstruct(conn, BOUNDARY_DECISION_ID)
        assert (reconstructed.result.score, reconstructed.result.output) == (score, output)
        resolved = factor(reconstructed.result, "employee_count")
        assert resolved.input_state is InputState.CONSUMED
        assert resolved.evidence_version_id == EMPLOYEE_COUNT_V2
        assert resolved.contribution == contribution
        with harness.engine.connect() as conn:
            context = load_context(conn, BOUNDARY_DECISION_ID, format_utc(boundary))
        entry = next(e for e in context if e.key == "employee_count")
        assert (entry.value, entry.evidence_version_id) == (employee_count, EMPLOYEE_COUNT_V2)
    else:
        assert response.status_code == 422, (delta_ms, offset, response.json())
        assert response.json()["reason"] == "evidence_version_available_after_the_boundary"

    with harness.engine.connect() as conn:
        after = reconstruct(conn, DECISION_EVENT_ID)
    assert_same_reconstruction(after, before)
    assert consumed_versions(after.result) == PRESERVED


# --- Database-level: a late preserved reference fails at the context read -----

LATE_EVIDENCE_EVENT_ID = "evt-direct-late-evidence"
DIRECT_DECISION_ID = "evt-direct-decision"
DIRECT_SOURCE = "direct-insert-for-inv-02"

LATE_IDS = {
    "employee_count": "ev-novasignal-employee-count-late",
    "verified_integration_pressure": "ev-novasignal-verified-integration-pressure-late",
}


def _insert_event(conn, event_id: str, event_type: str, occurred_at: str) -> int:
    """An `events` row in the normalized storage format; returns its sequence."""
    conn.execute(
        events.insert().values(
            event_id=event_id,
            schema_version="1",
            event_type=event_type,
            source=DIRECT_SOURCE,
            account_ref=ACCOUNT_REF,
            occurred_at=occurred_at,
            recorded_at=occurred_at,
            canonical_hash=canonical_hash({"direct": event_id}),
            payload=canonical_text({"direct": event_id}),
        )
    )
    return conn.execute(
        select(events.c.ingest_sequence).where(events.c.event_id == event_id)
    ).scalar_one()


def insert_late_reference(harness: Harness, input_key: str, available_at: str) -> str:
    """Seed the nine, then insert a copy of the canonical decision whose
    preserved reference for `input_key` points at a directly inserted evidence
    version available at `available_at`.

    Every value is copied from the seeded ledger; the only thing different
    about the late version is its availability time, its id, and its source.
    Returns the canonical decision's stored boundary text.
    """
    for response in seed_all(harness):
        assert response.status_code == 201
    late_id = LATE_IDS[input_key]

    with harness.engine.begin() as conn:
        original = conn.execute(
            select(evidence_versions).where(
                evidence_versions.c.account_ref == ACCOUNT_REF,
                evidence_versions.c.evidence_type == input_key,
            )
        ).one()
        decision = conn.execute(
            select(decisions).where(decisions.c.decision_event_id == DECISION_EVENT_ID)
        ).one()
        context_rows = conn.execute(
            select(decision_context).where(
                decision_context.c.decision_event_id == DECISION_EVENT_ID
            )
        ).all()
        consumed_rows = conn.execute(
            select(decision_consumed_inputs).where(
                decision_consumed_inputs.c.decision_event_id == DECISION_EVENT_ID
            )
        ).all()
        assert len(context_rows) == 8 and len(consumed_rows) == 5

        _insert_event(conn, LATE_EVIDENCE_EVENT_ID, "evidence.recorded", available_at)
        conn.execute(
            evidence_versions.insert().values(
                evidence_version_id=late_id,
                account_ref=original.account_ref,
                evidence_type=original.evidence_type,
                value_json=original.value_json,
                source=DIRECT_SOURCE,
                observed_at=original.observed_at,
                available_at=available_at,
                source_event_id=LATE_EVIDENCE_EVENT_ID,
                supersedes_evidence_version_id=None,
            )
        )

        sequence = _insert_event(
            conn, DIRECT_DECISION_ID, "decision.recorded", decision.decision_boundary
        )
        conn.execute(
            decisions.insert().values(
                {
                    **decision._mapping,
                    "decision_event_id": DIRECT_DECISION_ID,
                    "ingest_sequence": sequence,
                }
            )
        )
        conn.execute(
            decision_context.insert(),
            [
                {
                    **row._mapping,
                    "decision_event_id": DIRECT_DECISION_ID,
                    "evidence_version_id": (
                        late_id if row.input_key == input_key else row.evidence_version_id
                    ),
                }
                for row in context_rows
            ],
        )
        conn.execute(
            decision_consumed_inputs.insert(),
            [
                {
                    **row._mapping,
                    "decision_event_id": DIRECT_DECISION_ID,
                    "evidence_version_id": (
                        late_id if row.input_key == input_key else row.evidence_version_id
                    ),
                }
                for row in consumed_rows
            ],
        )

    # The late id appears wherever the input appears: always in the context,
    # and in the consumed inputs only when `v3.2` consumed the input.
    with harness.engine.connect() as conn:
        in_context = conn.execute(
            select(decision_context.c.evidence_version_id).where(
                decision_context.c.decision_event_id == DIRECT_DECISION_ID,
                decision_context.c.input_key == input_key,
            )
        ).scalar_one()
        in_consumed = conn.execute(
            select(decision_consumed_inputs.c.evidence_version_id).where(
                decision_consumed_inputs.c.decision_event_id == DIRECT_DECISION_ID,
                decision_consumed_inputs.c.input_key == input_key,
            )
        ).scalar_one_or_none()
    assert in_context == late_id
    assert in_consumed == (late_id if input_key == "employee_count" else None)
    return decision.decision_boundary


def assert_canonical_still_reconstructs(harness: Harness) -> None:
    """With the real evaluator back in place, the canonical decision is intact."""
    assert reconstruct_module.evaluate is not refuse_to_evaluate
    with harness.engine.connect() as conn:
        original = reconstruct(conn, DECISION_EVENT_ID)
    assert original.result.score == 86
    assert consumed_versions(original.result) == PRESERVED


def assert_late_reference_fails_before_evaluation(harness, monkeypatch, input_key: str) -> None:
    """The stub raises `AssertionError` if reached, so the `IntegrityFailure`
    itself proves evaluation never ran."""
    late_available_at = "2026-04-17T10:05:02.001000Z"
    boundary = insert_late_reference(harness, input_key, late_available_at)
    assert boundary == "2026-04-17T10:05:02.000000Z"

    monkeypatch.setattr(reconstruct_module, "evaluate", refuse_to_evaluate)
    with harness.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        reconstruct(conn, DIRECT_DECISION_ID)

    assert caught.value.field == "available_at"
    assert caught.value.stored == late_available_at
    assert caught.value.recomputed == boundary
    assert LATE_IDS[input_key] in caught.value.detail

    monkeypatch.undo()
    assert_canonical_still_reconstructs(harness)


def test_a_consumed_reference_available_after_the_boundary_fails_before_evaluation(
    harness, monkeypatch
):
    """`employee_count` is consumed by `v3.2`: the late id appears in both
    `decision_context` and `decision_consumed_inputs`."""
    assert_late_reference_fails_before_evaluation(harness, monkeypatch, "employee_count")


def test_an_ignored_reference_available_after_the_boundary_fails_before_evaluation(
    harness, monkeypatch
):
    """`verified_integration_pressure` is available in `H(d)` but referenced by
    no `v3.2` factor, so it appears in `decision_context` only. The check covers
    every preserved reference; a builder who checks availability only for
    consumed inputs fails this test."""
    assert_late_reference_fails_before_evaluation(
        harness, monkeypatch, "verified_integration_pressure"
    )


def test_a_reference_available_exactly_at_the_boundary_is_admitted_and_reconstructs(
    harness, monkeypatch
):
    boundary = insert_late_reference(harness, "employee_count", "2026-04-17T10:05:02.000000Z")
    late_id = LATE_IDS["employee_count"]

    calls: list[tuple] = []
    real_evaluate = reconstruct_module.evaluate

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(reconstruct_module, "evaluate", spy)
    with harness.engine.connect() as conn:
        reconstructed = reconstruct(conn, DIRECT_DECISION_ID)
        context = load_context(conn, DIRECT_DECISION_ID, boundary)

    assert len(calls) == 1
    result = reconstructed.result
    assert (result.score, result.threshold, result.output) == (86, 75, "PRIORITIZE")
    employee_count = factor(result, "employee_count")
    assert employee_count.evidence_version_id == late_id
    assert employee_count.contribution == 25
    entry = next(e for e in context if e.key == "employee_count")
    assert (entry.value, entry.evidence_version_id) == (184, late_id)
    assert consumed_versions(result) == {**PRESERVED, "employee_count": late_id}

    monkeypatch.undo()
    assert_canonical_still_reconstructs(harness)
