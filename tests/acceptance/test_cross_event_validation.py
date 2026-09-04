"""Cross-event references are validated at ingest (INV-02, INV-04, INV-05).

An envelope may only point at records that already exist, belong to the same
account, and are not later than the event that references them. Each case posts
a raw body against a seeded ledger and asserts the failure status, a readable
reason naming the offending reference, and an unchanged database.
"""

import copy
import json

import pytest

from tests.conftest import (
    Harness,
    canonical_by_type,
    canonical_raw,
    register_artifacts,
    seed_all,
)

DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"
ACTION_EVENT_ID = "evt-novasignal-06-action-recorded"
EMPLOYEE_COUNT_ID = "ev-novasignal-employee-count-v1"
BOUNDARY = "2026-04-17T10:05:02Z"


def _seed_through(harness: Harness, count: int) -> None:
    """Both registrations plus the first `count` account envelopes."""
    register_artifacts(harness)
    for index in range(count):
        assert harness.post_raw(canonical_raw(index)).status_code == 201


def _decision() -> dict:
    env = copy.deepcopy(canonical_by_type("decision.recorded"))
    env["event_id"] = "evt-test-decision"
    return env


def _context(env: dict, input_key: str) -> dict:
    return next(e for e in env["payload"]["historical_context"] if e["input_key"] == input_key)


def _consumed(env: dict, input_key: str) -> dict:
    return next(c for c in env["payload"]["consumed_inputs"] if c["input_key"] == input_key)


def _assert_rejected(harness: Harness, envelope: dict, reason: str, mentions: str):
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422, response.json()
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reason"] == reason
    assert mentions in json.dumps(body)
    assert harness.snapshot() == before
    return body


# --- Decision → evidence version (INV-02, INV-04) ---------------------------


def test_a_decision_referencing_an_unknown_evidence_version_is_rejected(harness):
    _seed_through(harness, 3)
    env = _decision()
    _context(env, "industry")["evidence_version_id"] = "ev-invented-v1"
    _consumed(env, "industry")["evidence_version_id"] = "ev-invented-v1"
    _assert_rejected(harness, env, "unknown_evidence_version", "ev-invented-v1")


def test_a_decision_referencing_another_accounts_evidence_is_rejected(harness):
    _seed_through(harness, 3)
    # A second account mints its own employee_count version.
    other = copy.deepcopy(canonical_by_type("account.discovered"))
    other.update(
        event_id="evt-other-discovered",
        account_ref="other-account",
        payload={"name": "Other Co", "domain": "other.example"},
    )
    assert harness.post(other).status_code == 201
    evidence = copy.deepcopy(canonical_by_type("evidence.recorded"))
    evidence.update(event_id="evt-other-evidence", account_ref="other-account")
    evidence["payload"]["items"] = [
        {
            "evidence_version_id": "ev-other-employee-count-v1",
            "evidence_type": "employee_count",
            "value": 184,
        }
    ]
    assert harness.post(evidence).status_code == 201

    env = _decision()
    _context(env, "employee_count")["evidence_version_id"] = "ev-other-employee-count-v1"
    _consumed(env, "employee_count")["evidence_version_id"] = "ev-other-employee-count-v1"
    _assert_rejected(
        harness,
        env,
        "evidence_version_belongs_to_another_account",
        "ev-other-employee-count-v1",
    )


def _late_evidence(recorded_at: str) -> dict:
    envelope = copy.deepcopy(canonical_by_type("evidence.recorded"))
    envelope.update(
        event_id="evt-late-evidence",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
    )
    envelope["payload"]["items"] = [
        {
            "evidence_version_id": "ev-novasignal-website-intent-v1",
            "evidence_type": "industry",
            "value": "B2B AI Software",
        }
    ]
    return envelope


def _decision_using_late_evidence() -> dict:
    env = _decision()
    _context(env, "industry")["evidence_version_id"] = "ev-novasignal-website-intent-v1"
    _consumed(env, "industry")["evidence_version_id"] = "ev-novasignal-website-intent-v1"
    return env


def test_evidence_available_one_millisecond_after_the_boundary_is_rejected(harness):
    _seed_through(harness, 3)
    assert harness.post(_late_evidence("2026-04-17T10:05:02.001000Z")).status_code == 201
    body = _assert_rejected(
        harness,
        _decision_using_late_evidence(),
        "evidence_version_available_after_the_boundary",
        "ev-novasignal-website-intent-v1",
    )
    assert "after the decision boundary" in body["detail"]


def test_evidence_available_exactly_at_the_boundary_is_admitted(harness):
    _seed_through(harness, 3)
    assert harness.post(_late_evidence(BOUNDARY)).status_code == 201
    response = harness.post(_decision_using_late_evidence())
    assert response.status_code == 201, response.json()


def test_evidence_available_one_millisecond_before_the_boundary_is_admitted(harness):
    _seed_through(harness, 3)
    assert harness.post(_late_evidence("2026-04-17T10:05:01.999000Z")).status_code == 201
    assert harness.post(_decision_using_late_evidence()).status_code == 201


def test_an_input_key_that_is_not_the_evidence_type_is_rejected(harness):
    _seed_through(harness, 3)
    env = _decision()
    # Point the industry entry at the employee_count version.
    _context(env, "industry")["evidence_version_id"] = EMPLOYEE_COUNT_ID
    _consumed(env, "industry")["evidence_version_id"] = EMPLOYEE_COUNT_ID
    _assert_rejected(harness, env, "evidence_type_does_not_match_input_key", EMPLOYEE_COUNT_ID)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(184.0, id="float"),
        pytest.param("184", id="string"),
        pytest.param(185, id="int"),
    ],
)
def test_a_preserved_value_that_is_not_the_stored_value_is_rejected(harness, value):
    """Type-exact comparison: `184` is not `184.0` and not `"184"` (§2a)."""
    _seed_through(harness, 3)
    env = _decision()
    _context(env, "employee_count")["value"] = value
    _consumed(env, "employee_count")["value"] = value
    before = harness.snapshot()
    response = harness.post(env)
    assert response.status_code == 422
    if value == 184.0:
        # A float never reaches the ledger: schema v1 has no float fields.
        assert response.json()["reason"] == "invalid_envelope"
    else:
        assert response.json()["reason"] == "preserved_value_does_not_match_the_evidence_version"
    assert harness.snapshot() == before


def test_a_preserved_date_string_that_is_not_the_stored_value_is_rejected(harness):
    _seed_through(harness, 3)
    env = _decision()
    _context(env, "head_of_platform_start_date")["value"] = "2026-03-06"
    _assert_rejected(
        harness,
        env,
        "preserved_value_does_not_match_the_evidence_version",
        "ev-novasignal-head-of-platform-start-date-v1",
    )


def test_only_the_value_member_is_compared_for_evidence_carrying_extra_fields(harness):
    """`verified_integration_pressure` stores `basis` alongside `value`; the
    decision preserves the scalar only, and the comparison must not see the
    whole stored object (§2a)."""
    _seed_through(harness, 3)
    entry = _context(_decision(), "verified_integration_pressure")
    assert entry["value"] == "LOW"
    assert harness.post(_decision()).status_code == 201

    env = _decision()
    env["event_id"] = "evt-test-decision-pressure"
    _context(env, "verified_integration_pressure")["value"] = "HIGH"
    _assert_rejected(
        harness,
        env,
        "preserved_value_does_not_match_the_evidence_version",
        "ev-novasignal-verified-integration-pressure-v1",
    )


# --- Decision → logic artifact (INV-05) ------------------------------------


def test_a_decision_referencing_an_unregistered_artifact_is_rejected(harness):
    _seed_through(harness, 3)
    env = _decision()
    env["payload"]["logic_artifact"]["artifact_hash"] = "0" * 64
    _assert_rejected(harness, env, "unregistered_logic_artifact", "0" * 64)


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", "logic-account-prioritization-v9.9"),
        ("logic_version", "v9.9"),
        ("evaluator_version", "evaluator-v2"),
    ],
)
def test_a_decision_whose_logic_identity_contradicts_the_artifact_is_rejected(
    harness, field, value
):
    """INV-05: a label is metadata; the registered artifact decides identity."""
    _seed_through(harness, 3)
    env = _decision()
    env["payload"]["logic_artifact"][field] = value
    _assert_rejected(harness, env, "logic_artifact_identity_mismatch", field)


# --- Evidence versions are minted exactly once -----------------------------


def test_a_duplicate_evidence_version_id_within_one_envelope_is_rejected(harness):
    _seed_through(harness, 1)
    env = copy.deepcopy(canonical_by_type("evidence.recorded"))
    env["event_id"] = "evt-test-duplicate-within-envelope"
    env["payload"]["items"].append(copy.deepcopy(env["payload"]["items"][0]))
    before = harness.snapshot()
    response = harness.post(env)
    assert response.status_code == 422
    assert "items has more than one entry for evidence_version_id" in json.dumps(response.json())
    assert harness.snapshot() == before


def test_reminting_the_same_evidence_version_under_a_new_event_id_is_rejected(harness):
    _seed_through(harness, 2)
    env = copy.deepcopy(canonical_by_type("evidence.recorded"))
    env["event_id"] = "evt-test-reminted"
    _assert_rejected(harness, env, "evidence_version_already_minted", EMPLOYEE_COUNT_ID)


def test_reusing_an_evidence_version_id_with_different_content_conflicts(harness):
    _seed_through(harness, 2)
    env = copy.deepcopy(canonical_by_type("evidence.recorded"))
    env["event_id"] = "evt-test-changed-evidence"
    env["payload"]["items"][0]["value"] = 999
    before = harness.snapshot()
    response = harness.post(env)
    assert response.status_code == 409
    assert response.json()["reason"] == "evidence_version_id_reused_with_different_content"
    assert EMPLOYEE_COUNT_ID in json.dumps(response.json())
    assert harness.snapshot() == before


# --- Persona and action → decision -----------------------------------------


@pytest.mark.parametrize("event_type", ["persona.selected", "action.recorded"])
def test_referencing_an_unknown_decision_is_rejected(harness, event_type):
    _seed_through(harness, 4)
    env = copy.deepcopy(canonical_by_type(event_type))
    env["event_id"] = "evt-test-dangling"
    env["payload"]["decision_event_id"] = "evt-no-such-decision"
    _assert_rejected(harness, env, "unknown_decision_event_id", "evt-no-such-decision")


@pytest.mark.parametrize("event_type", ["persona.selected", "action.recorded"])
def test_an_event_earlier_than_its_decision_is_rejected(harness, event_type):
    _seed_through(harness, 4)
    env = copy.deepcopy(canonical_by_type(event_type))
    env["event_id"] = "evt-test-before-decision"
    env["occurred_at"] = "2026-04-17T10:05:01.999000Z"
    env["recorded_at"] = "2026-04-17T10:05:01.999000Z"
    _assert_rejected(harness, env, "decision_is_later_than_the_event", DECISION_EVENT_ID)


def test_an_event_at_exactly_the_decision_boundary_is_admitted(harness):
    _seed_through(harness, 4)
    env = copy.deepcopy(canonical_by_type("persona.selected"))
    env["event_id"] = "evt-test-at-boundary"
    env["occurred_at"] = BOUNDARY
    env["recorded_at"] = BOUNDARY
    assert harness.post(env).status_code == 201


def test_referencing_another_accounts_decision_is_rejected(harness):
    seed_all(harness)
    other = copy.deepcopy(canonical_by_type("account.discovered"))
    other.update(
        event_id="evt-other-discovered",
        account_ref="other-account",
        payload={"name": "Other Co", "domain": "other.example"},
    )
    assert harness.post(other).status_code == 201
    env = copy.deepcopy(canonical_by_type("persona.selected"))
    env["event_id"] = "evt-test-cross-account-persona"
    env["account_ref"] = "other-account"
    _assert_rejected(harness, env, "decision_belongs_to_another_account", DECISION_EVENT_ID)


# --- Outcome → action (INV-08) ---------------------------------------------


def test_an_outcome_referencing_an_unknown_action_is_rejected(harness):
    _seed_through(harness, 6)
    env = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    env["event_id"] = "evt-test-dangling-outcome"
    env["payload"]["action_event_id"] = "evt-no-such-action"
    _assert_rejected(harness, env, "unknown_action_event_id", "evt-no-such-action")


def test_an_outcome_earlier_than_its_action_is_rejected(harness):
    _seed_through(harness, 6)
    env = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    env["event_id"] = "evt-test-early-outcome"
    env["occurred_at"] = "2026-04-17T10:06:59.999000Z"
    env["recorded_at"] = "2026-04-17T10:06:59.999000Z"
    _assert_rejected(harness, env, "action_is_later_than_the_outcome", ACTION_EVENT_ID)


def test_an_outcome_at_exactly_the_action_instant_is_admitted(harness):
    _seed_through(harness, 6)
    env = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    env["event_id"] = "evt-test-outcome-at-action"
    env["occurred_at"] = "2026-04-17T10:07:00Z"
    env["recorded_at"] = "2026-04-17T10:07:00Z"
    assert harness.post(env).status_code == 201


def test_an_outcome_referencing_another_accounts_action_is_rejected(harness):
    seed_all(harness)
    other = copy.deepcopy(canonical_by_type("account.discovered"))
    other.update(
        event_id="evt-other-discovered",
        account_ref="other-account",
        payload={"name": "Other Co", "domain": "other.example"},
    )
    assert harness.post(other).status_code == 201
    env = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    env["event_id"] = "evt-test-cross-account-outcome"
    env["account_ref"] = "other-account"
    _assert_rejected(harness, env, "action_belongs_to_another_account", ACTION_EVENT_ID)


# --- Idempotency still wins ------------------------------------------------


def test_an_identical_retry_is_a_duplicate_before_any_reference_check(harness):
    seed_all(harness)
    before = harness.snapshot()
    for index in range(7):
        response = harness.post_raw(canonical_raw(index))
        assert response.status_code == 200
        assert response.json()["status"] == "duplicate"
    assert harness.snapshot() == before
