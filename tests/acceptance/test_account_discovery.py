"""Account rules: repeated discovery is append-only; identity conflicts fail."""

import copy

from tests.conftest import canonical_by_type, canonical_raw


def test_repeated_discovery_with_matching_identity_is_stored(harness):
    assert harness.post_raw(canonical_raw(0)).status_code == 201
    accounts_before = harness.account_rows()
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["event_id"] = "evt-novasignal-01b-rediscovered"
    env["source"] = "apollo-sim-refresh"
    env["occurred_at"] = "2026-04-18T09:00:00Z"
    env["recorded_at"] = "2026-04-18T09:00:00Z"
    response = harness.post(env)
    assert response.status_code == 201
    assert harness.event_count() == 2
    assert harness.account_rows() == accounts_before


def test_repeated_discovery_with_conflicting_name_or_domain_fails(harness):
    assert harness.post_raw(canonical_raw(0)).status_code == 201
    before = harness.snapshot()
    for field, value in (("name", "NovaSignal Robotics"), ("domain", "novasignal.io")):
        env = copy.deepcopy(canonical_by_type("account.discovered"))
        env["event_id"] = f"evt-conflict-{field}"
        env["payload"][field] = value
        response = harness.post(env)
        assert response.status_code == 409
        assert response.json()["reason"] == "account_identity_mismatch"
        assert harness.snapshot() == before
