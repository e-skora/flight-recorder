"""INV-04: evidence corrections append; the earlier decision keeps its version.

A correction is a new `evidence_versions` row carrying an explicit supersession
link. Nothing on the superseded row changes, and the decision that consumed it
still resolves to the original identifier, value, and provenance. Establishing
this representation is Phase 2A's obligation; AC-05's full proof is 2C.
"""

import copy

import pytest
from sqlalchemy import select

from flight_recorder.ledger.schema import (
    decision_consumed_inputs,
    decision_context,
    evidence_versions,
)
from tests.conftest import Harness, local_fixture, seed_all

pytestmark = pytest.mark.invariant

ORIGINAL_ID = "ev-novasignal-employee-count-v1"
CORRECTION_ID = "ev-novasignal-employee-count-v2"
DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"


def correction() -> dict:
    return copy.deepcopy(local_fixture("evidence-correction-employee-count.json"))


def _evidence(harness: Harness, version_id: str):
    with harness.engine.connect() as conn:
        return conn.execute(
            select(evidence_versions).where(evidence_versions.c.evidence_version_id == version_id)
        ).first()


def _seeded_and_corrected(harness: Harness):
    seed_all(harness)
    before = _evidence(harness, ORIGINAL_ID)
    assert harness.post(correction()).status_code == 201
    return before


def test_a_correction_appends_a_new_version_and_retains_the_original(harness):
    before = _seeded_and_corrected(harness)

    original = _evidence(harness, ORIGINAL_ID)
    assert tuple(original) == tuple(before), "the superseded row must be untouched"
    assert original.value_json == '{"value":184}'
    assert original.source == "clay-sim"
    assert original.supersedes_evidence_version_id is None

    corrected = _evidence(harness, CORRECTION_ID)
    assert corrected.value_json == '{"value":191}'
    assert corrected.supersedes_evidence_version_id == ORIGINAL_ID
    assert corrected.account_ref == original.account_ref
    assert corrected.evidence_type == original.evidence_type
    assert corrected.available_at > original.available_at
    assert corrected.source_event_id == correction()["event_id"]


def test_the_earlier_decision_still_resolves_to_the_original_version(harness):
    _seeded_and_corrected(harness)
    with harness.engine.connect() as conn:
        context = conn.execute(
            select(decision_context).where(
                decision_context.c.decision_event_id == DECISION_EVENT_ID,
                decision_context.c.input_key == "employee_count",
            )
        ).one()
        consumed = conn.execute(
            select(decision_consumed_inputs).where(
                decision_consumed_inputs.c.decision_event_id == DECISION_EVENT_ID,
                decision_consumed_inputs.c.input_key == "employee_count",
            )
        ).one()
    assert context.evidence_version_id == ORIGINAL_ID
    assert context.value_text == "184"
    assert consumed.evidence_version_id == ORIGINAL_ID
    assert consumed.value_text == "184"
    assert _evidence(harness, ORIGINAL_ID).source == "clay-sim"


def test_no_current_value_view_collapses_the_two_versions(harness):
    _seeded_and_corrected(harness)
    with harness.engine.connect() as conn:
        rows = conn.execute(
            select(evidence_versions.c.evidence_version_id).where(
                evidence_versions.c.evidence_type == "employee_count"
            )
        ).all()
    assert {r.evidence_version_id for r in rows} == {ORIGINAL_ID, CORRECTION_ID}


# --- What a correction may not do ------------------------------------------


def test_superseding_a_version_minted_in_the_same_envelope_is_rejected(harness):
    """Even listed earlier in `items`, a same-envelope version is not yet stored."""
    seed_all(harness)
    before = harness.snapshot()
    env = correction()
    env["event_id"] = "evt-test-same-envelope-supersession"
    env["payload"]["items"] = [
        {
            "evidence_version_id": "ev-novasignal-employee-count-v3",
            "evidence_type": "employee_count",
            "value": 200,
        },
        {
            "evidence_version_id": "ev-novasignal-employee-count-v4",
            "evidence_type": "employee_count",
            "value": 201,
            "supersedes_evidence_version_id": "ev-novasignal-employee-count-v3",
        },
    ]
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["reason"] == "unknown_superseded_evidence_version"
    assert harness.snapshot() == before


def test_superseding_an_unknown_version_is_rejected(harness):
    seed_all(harness)
    before = harness.snapshot()
    env = correction()
    env["event_id"] = "evt-test-unknown-supersession"
    env["payload"]["items"][0]["supersedes_evidence_version_id"] = "ev-no-such-version"
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["reason"] == "unknown_superseded_evidence_version"
    assert harness.snapshot() == before


def test_superseding_a_version_of_another_evidence_type_is_rejected(harness):
    seed_all(harness)
    before = harness.snapshot()
    env = correction()
    env["event_id"] = "evt-test-cross-type-supersession"
    env["payload"]["items"][0]["supersedes_evidence_version_id"] = "ev-novasignal-industry-v1"
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["reason"] == "superseded_evidence_has_a_different_type"
    assert harness.snapshot() == before


def test_superseding_a_version_of_another_account_is_rejected(harness):
    seed_all(harness)
    other = {
        "schema_version": "1",
        "event_id": "evt-other-discovered",
        "event_type": "account.discovered",
        "source": "apollo-sim",
        "account_ref": "other-account",
        "occurred_at": "2026-04-17T10:04:00Z",
        "recorded_at": "2026-04-17T10:04:00Z",
        "payload": {"name": "Other Co", "domain": "other.example"},
    }
    assert harness.post(other).status_code == 201
    before = harness.snapshot()
    env = correction()
    env["event_id"] = "evt-test-cross-account-supersession"
    env["account_ref"] = "other-account"
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["reason"] == "superseded_evidence_belongs_to_another_account"
    assert harness.snapshot() == before


def test_superseding_a_version_that_became_available_later_is_rejected(harness):
    seed_all(harness)
    before = harness.snapshot()
    env = correction()
    env["event_id"] = "evt-test-backdated-correction"
    # Earlier than the version it claims to correct.
    env["occurred_at"] = "2026-04-17T10:04:36Z"
    env["recorded_at"] = "2026-04-17T10:04:36Z"
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["reason"] == "superseded_evidence_is_later"
    assert harness.snapshot() == before


def test_the_supersession_link_is_itself_append_only(harness):
    """INV-01 covers the correction row like every other projected record."""
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    _seeded_and_corrected(harness)
    before = harness.projection_rows()
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(
            update(evidence_versions)
            .where(evidence_versions.c.evidence_version_id == CORRECTION_ID)
            .values(supersedes_evidence_version_id=None)
        )
    assert harness.projection_rows() == before
