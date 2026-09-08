"""AC-05 / INV-04 / INV-01 at the reconstruction level: corrections append.

A correction to historical evidence creates a new evidence version with its own
provenance. The earlier decision still reconstructs, through the projection
tables, to the original value, the original evidence version, and the original
provenance; its own rows stay byte-identical. A later decision may use the
correction (`PRODUCT.md` §5 "Corrections append": it MAY affect later
decisions).

Every correction here arrives through the collector, after the canonical
boundary, in its own envelope. The literal availability instants below are the
subject of the tests; every canonical value is derived from the fixtures or
read back from the seeded ledger.
"""

import copy
from datetime import datetime

import pytest

from flight_recorder.collector.schema import format_utc
from flight_recorder.logic.evaluator import InputState
from flight_recorder.replay.reconstruct import load_context, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    RECONSTRUCTION_FIELDS,
    assert_same_reconstruction,
    canonical_by_type,
    canonical_observed_at,
    consumed_versions,
    decision_envelope_with,
    decision_rows,
    evidence_envelope,
    evidence_version_row,
    factor,
    local_fixture,
    seed_all,
    stored_form,
)

EMPLOYEE_COUNT_V1 = "ev-novasignal-employee-count-v1"
EMPLOYEE_COUNT_V2 = "ev-novasignal-employee-count-v2"
EMPLOYEE_COUNT_V3 = "ev-novasignal-employee-count-v3"
FUNDING_V1 = "ev-novasignal-funding-event-v1"
FUNDING_V2 = "ev-novasignal-funding-event-v2"
PRESSURE_V1 = "ev-novasignal-verified-integration-pressure-v1"
PRESSURE_V2 = "ev-novasignal-verified-integration-pressure-v2"

LATER_DECISION_ID = "evt-test-later-decision"
LATER_BOUNDARY = "2026-04-21T09:00:00Z"


def canonical_correction() -> dict:
    """`tests/fixtures/evidence-correction-employee-count.json`, unchanged."""
    return copy.deepcopy(local_fixture("evidence-correction-employee-count.json"))


def observation_date_correction() -> dict:
    """A funding observation moved outside the 90-day window of the boundary."""
    return evidence_envelope(
        "evt-test-funding-observation-corrected",
        [
            {
                "evidence_version_id": FUNDING_V2,
                "evidence_type": "funding_event",
                "value": "Series B",
                "observed_at": "2025-12-01",
                "supersedes_evidence_version_id": FUNDING_V1,
            }
        ],
        occurred_at="2026-04-21T09:00:00Z",
        source="clay-sim-correction",
    )


def ignored_input_correction() -> dict:
    return evidence_envelope(
        "evt-test-integration-pressure-corrected",
        [
            {
                "evidence_version_id": PRESSURE_V2,
                "evidence_type": "verified_integration_pressure",
                "value": "HIGH",
                "basis": ["three documented production integrations", "public API launched"],
                "supersedes_evidence_version_id": PRESSURE_V1,
            }
        ],
        occurred_at="2026-04-21T09:30:00Z",
        source="relaybridge-research-sim-correction",
    )


def chain_correction() -> dict:
    """A second correction of the canonical correction (`-v2` -> `-v3`)."""
    return evidence_envelope(
        "evt-test-employee-count-corrected-again",
        [
            {
                "evidence_version_id": EMPLOYEE_COUNT_V3,
                "evidence_type": "employee_count",
                "value": 205,
                "supersedes_evidence_version_id": EMPLOYEE_COUNT_V2,
            }
        ],
        occurred_at="2026-04-22T09:00:00Z",
        source="clay-sim-correction",
    )


# --- Setup ----------------------------------------------------------------


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


@pytest.fixture
def before(seeded):
    """The canonical reconstruction, the decision's rows, and the `-v1` rows,
    all taken before any correction arrives."""
    return {
        "reconstruction": reconstruction(seeded),
        "rows": decision_rows(seeded),
        "evidence": {
            version_id: tuple(evidence_version_row(seeded, version_id))
            for version_id in (EMPLOYEE_COUNT_V1, FUNDING_V1, PRESSURE_V1)
        },
    }


def reconstruction(harness, decision_event_id: str = DECISION_EVENT_ID):
    with harness.engine.connect() as conn:
        return reconstruct(conn, decision_event_id)


def context_entry(harness, key: str, decision_event_id: str = DECISION_EVENT_ID):
    with harness.engine.connect() as conn:
        boundary = reconstruct(conn, decision_event_id).decision_boundary
        context = load_context(conn, decision_event_id, format_utc(boundary))
    return next(entry for entry in context if entry.key == key)


def post_corrections(harness, *envelopes: dict) -> None:
    for envelope in envelopes:
        response = harness.post(envelope)
        assert response.status_code == 201, (envelope["event_id"], response.json())


def assert_original_untouched(harness, before: dict) -> None:
    """The canonical decision still reconstructs to `-v1` / 184, field by
    field, and its rows are byte-identical."""
    after = reconstruction(harness)
    assert_same_reconstruction(after, before["reconstruction"])

    employee_count = factor(after.result, "employee_count")
    assert employee_count.input_state is InputState.CONSUMED
    assert employee_count.matched is True
    assert employee_count.contribution == 25
    assert employee_count.evidence_version_id == EMPLOYEE_COUNT_V1
    entry = context_entry(harness, "employee_count")
    assert (entry.value, entry.evidence_version_id) == (184, EMPLOYEE_COUNT_V1)

    assert decision_rows(harness) == before["rows"]


# --- The canonical correction -------------------------------------------------


def test_the_canonical_correction_leaves_the_original_decision_on_the_original_value(
    seeded, before
):
    post_corrections(seeded, canonical_correction())

    assert_original_untouched(seeded, before)

    # The `-v1` row is untouched and reads exactly what the canonical
    # enrichment event recorded: its own source, availability, and provenance.
    enrichment = canonical_by_type("evidence.recorded")
    original = evidence_version_row(seeded, EMPLOYEE_COUNT_V1)
    assert tuple(original) == before["evidence"][EMPLOYEE_COUNT_V1]
    assert original.source == enrichment["source"] == "clay-sim"
    assert original.available_at == stored_form(enrichment["recorded_at"])
    assert original.available_at == "2026-04-17T10:04:37.000000Z"
    assert original.observed_at is None
    assert original.supersedes_evidence_version_id is None
    assert original.source_event_id == enrichment["event_id"]
    assert original.value_json == '{"value":184}'

    # The `-v2` row exists alongside it, with its own provenance.
    correction = canonical_correction()
    corrected = evidence_version_row(seeded, EMPLOYEE_COUNT_V2)
    assert corrected.value_json == '{"value":191}'
    assert corrected.source == correction["source"] == "clay-sim-correction"
    assert corrected.available_at == stored_form(correction["recorded_at"])
    assert corrected.available_at == "2026-04-20T09:00:00.000000Z"
    assert corrected.supersedes_evidence_version_id == EMPLOYEE_COUNT_V1
    assert corrected.source_event_id == correction["event_id"]


def test_a_correction_to_the_observation_date_does_not_reach_the_temporal_rule(seeded, before):
    """The temporal rule reads `observed_at` through the single
    `evidence_versions` read, by the preserved `-v1` id; a superseding version
    with a different date is never consulted."""
    post_corrections(seeded, observation_date_correction())

    after = reconstruction(seeded)
    assert_same_reconstruction(after, before["reconstruction"])

    funding = factor(after.result, "funding_event")
    assert funding.input_state is InputState.CONSUMED
    assert funding.matched is True
    assert funding.contribution == 18
    assert funding.evidence_version_id == FUNDING_V1

    entry = context_entry(seeded, "funding_event")
    assert entry.evidence_version_id == FUNDING_V1
    assert entry.observed_at == canonical_observed_at()[FUNDING_V1]
    assert entry.observed_at.isoformat() == "2026-03-30"

    assert tuple(evidence_version_row(seeded, FUNDING_V1)) == before["evidence"][FUNDING_V1]
    assert evidence_version_row(seeded, FUNDING_V2).observed_at == "2025-12-01"
    assert decision_rows(seeded) == before["rows"]


def test_a_correction_to_an_ignored_input_stays_ignored(seeded, before):
    post_corrections(seeded, ignored_input_correction())

    after = reconstruction(seeded)
    assert_same_reconstruction(after, before["reconstruction"])
    assert after.result.context_states["verified_integration_pressure"] is InputState.IGNORED
    assert after.result.ignored_inputs == before["reconstruction"].result.ignored_inputs
    assert "verified_integration_pressure" in after.result.ignored_inputs

    entry = context_entry(seeded, "verified_integration_pressure")
    assert (entry.value, entry.evidence_version_id) == ("LOW", PRESSURE_V1)
    assert tuple(evidence_version_row(seeded, PRESSURE_V1)) == before["evidence"][PRESSURE_V1]
    assert evidence_version_row(seeded, PRESSURE_V2).supersedes_evidence_version_id == PRESSURE_V1
    assert decision_rows(seeded) == before["rows"]


def test_a_correction_chain_never_moves_the_original_reference(seeded, before):
    post_corrections(seeded, canonical_correction(), chain_correction())

    rows = {
        version_id: evidence_version_row(seeded, version_id)
        for version_id in (EMPLOYEE_COUNT_V1, EMPLOYEE_COUNT_V2, EMPLOYEE_COUNT_V3)
    }
    assert all(row is not None for row in rows.values())
    assert [row.value_json for row in rows.values()] == [
        '{"value":184}',
        '{"value":191}',
        '{"value":205}',
    ]
    assert [row.supersedes_evidence_version_id for row in rows.values()] == [
        None,
        EMPLOYEE_COUNT_V1,
        EMPLOYEE_COUNT_V2,
    ]
    assert tuple(rows[EMPLOYEE_COUNT_V1]) == before["evidence"][EMPLOYEE_COUNT_V1]

    assert_original_untouched(seeded, before)


# --- A later decision may use the correction ---------------------------------


def later_decision() -> dict:
    """A second NovaSignal AI decision, after the correction, preserving `-v2`.

    191 is inside the employee range, and the funding observation is 22 days
    before this boundary, so `v3.2` still scores 86 and prioritizes.
    """
    return decision_envelope_with(
        LATER_DECISION_ID,
        input_key="employee_count",
        value=191,
        evidence_version_id=EMPLOYEE_COUNT_V2,
        contribution=25,
        score=86,
        output="PRIORITIZE",
        boundary=LATER_BOUNDARY,
    )


def test_a_later_decision_may_use_the_correction(seeded, before):
    post_corrections(seeded, canonical_correction())

    envelope = later_decision()
    assert envelope["payload"]["workflow_version"] == "v4.2"
    assert (
        envelope["payload"]["logic_artifact"]
        == (canonical_by_type("decision.recorded")["payload"]["logic_artifact"])
    )
    website_intent = next(
        e for e in envelope["payload"]["historical_context"] if e["input_key"] == "website_intent"
    )
    assert website_intent["availability"] == "unavailable"
    response = seeded.post(envelope)
    assert response.status_code == 201, response.json()

    later = reconstruction(seeded, LATER_DECISION_ID)
    assert (later.result.score, later.result.threshold, later.result.output) == (
        86,
        75,
        "PRIORITIZE",
    )
    later_employee_count = factor(later.result, "employee_count")
    assert later_employee_count.evidence_version_id == EMPLOYEE_COUNT_V2
    assert later_employee_count.contribution == 25
    later_entry = context_entry(seeded, "employee_count", LATER_DECISION_ID)
    assert (later_entry.value, later_entry.evidence_version_id) == (191, EMPLOYEE_COUNT_V2)

    # The original is exactly what it was.
    assert_original_untouched(seeded, before)
    original = reconstruction(seeded)

    # The two reconstructions differ in the decision id, the boundary, and that
    # one factor's evidence version, and in nothing else.
    for field in RECONSTRUCTION_FIELDS:
        if field in ("decision_event_id", "decision_boundary"):
            assert getattr(later, field) != getattr(original, field), field
        else:
            assert getattr(later, field) == getattr(original, field), field
    assert later.decision_event_id == LATER_DECISION_ID
    assert later.decision_boundary == datetime.fromisoformat(LATER_BOUNDARY)
    assert format_utc(later.decision_boundary) == stored_form(LATER_BOUNDARY)
    for field in ("score", "threshold", "output", "ignored_inputs"):
        assert getattr(later.result, field) == getattr(original.result, field), field
    assert dict(later.result.context_states) == dict(original.result.context_states)
    for later_factor, original_factor in zip(
        later.result.factors, original.result.factors, strict=True
    ):
        if later_factor.key == "employee_count":
            assert later_factor.evidence_version_id == EMPLOYEE_COUNT_V2
            assert original_factor.evidence_version_id == EMPLOYEE_COUNT_V1
            for attribute in ("key", "rule", "weight", "input_state", "matched", "contribution"):
                assert getattr(later_factor, attribute) == getattr(original_factor, attribute)
        else:
            assert later_factor == original_factor, later_factor.key

    assert consumed_versions(original.result)["employee_count"] == EMPLOYEE_COUNT_V1
    assert consumed_versions(later.result)["employee_count"] == EMPLOYEE_COUNT_V2
