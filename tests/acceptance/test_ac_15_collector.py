"""AC-15, Phase 1 subset: canonical ingestion, idempotency, conflicts, rejection.

Full AC-15 stays open until Phase 2 projects domain records from accepted events.
"""

import copy
import json

import pytest

from tests.conftest import Harness, canonical_by_type, canonical_raw, reformatted


def _discover(harness: Harness):
    response = harness.post_raw(canonical_raw(0))
    assert response.status_code == 201
    return response


def _evidence_with_employee_count(value):
    env = copy.deepcopy(canonical_by_type("evidence.recorded"))
    env["payload"]["items"] = [
        {
            "evidence_version_id": "ev-test-employee-count-v1",
            "evidence_type": "employee_count",
            "value": value,
        }
    ]
    return env


def test_valid_canonical_event_creates_one_row(harness):
    response = _discover(harness)
    assert response.json()["status"] == "created"
    assert harness.event_count() == 1
    assert len(harness.account_rows()) == 1


def test_reordered_keys_and_whitespace_retry_is_duplicate(harness):
    _discover(harness)
    raw = reformatted(canonical_by_type("account.discovered"))
    assert raw != canonical_raw(0)
    response = harness.post_raw(raw)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert harness.event_count() == 1


def test_same_event_id_with_changed_valid_field_conflicts(harness):
    _discover(harness)
    before = harness.snapshot()
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["source"] = "apollo-sim-2"
    response = harness.post(env)
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "conflict"
    assert body["stored_hash"] != body["submitted_hash"]
    assert harness.snapshot() == before


def test_same_instant_different_offset_is_duplicate(harness):
    _discover(harness)
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    assert env["occurred_at"].endswith("Z")
    env["occurred_at"] = env["occurred_at"].replace("T10:", "T12:").replace("Z", "+02:00")
    env["recorded_at"] = env["recorded_at"].replace("T10:", "T05:").replace("Z", "-05:00")
    response = harness.post(env)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert harness.event_count() == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["payload"].pop("domain"), id="malformed-payload-missing-field"),
        pytest.param(lambda e: e.update(schema_version="2"), id="unsupported-schema-version"),
        pytest.param(lambda e: e.update(unexpected="x"), id="unknown-envelope-field"),
        pytest.param(lambda e: e["payload"].update(extra="x"), id="unknown-payload-field"),
        pytest.param(lambda e: e.update(event_type="account.renamed"), id="unknown-event-type"),
        pytest.param(lambda e: e.update(occurred_at="2026-04-17T10:04:00"), id="naive-timestamp"),
        pytest.param(
            lambda e: e.update(recorded_at="2026-04-17T10:03:59Z"), id="recorded-before-occurred"
        ),
    ],
)
def test_invalid_envelopes_are_rejected_without_writes(harness, mutate):
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["event_id"] = "evt-test-rejected"
    mutate(env)
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert harness.snapshot() == (0, [])


def test_non_discovery_event_for_unknown_account_is_rejected(harness):
    response = harness.post_raw(canonical_raw(1))
    assert response.status_code == 422
    assert response.json()["reason"] == "unknown_account_ref"
    assert harness.snapshot() == (0, [])


@pytest.mark.parametrize("bad_value", ["184", 184.0], ids=["string-integer", "float-integer"])
def test_string_or_float_employee_count_is_rejected(harness, bad_value):
    _discover(harness)
    before = harness.snapshot()
    response = harness.post(_evidence_with_employee_count(bad_value))
    assert response.status_code == 422
    assert harness.snapshot() == before


def test_integer_employee_count_is_accepted(harness):
    _discover(harness)
    response = harness.post(_evidence_with_employee_count(184))
    assert response.status_code == 201


def test_non_json_body_is_rejected(harness):
    response = harness.post_raw(b"not json")
    assert response.status_code == 422
    assert harness.snapshot() == (0, [])


def test_openapi_publishes_envelope_schema(client):
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/v1/decision-events"]["post"]
    assert "application/json" in post["requestBody"]["content"]
    assert "DecisionRecordedEnvelope" in schema["components"]["schemas"]
    assert json.dumps(schema)  # serializable


def test_full_canonical_seed_and_reseed(harness):
    from tests.conftest import seed_all

    first = seed_all(harness)
    assert [r.status_code for r in first] == [201] * 7
    second = seed_all(harness)
    assert [r.json()["status"] for r in second] == ["duplicate"] * 7
    assert harness.event_count() == 7
