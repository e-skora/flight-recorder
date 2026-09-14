"""Outcome schema v1 identity is pinned independently; v2 is a separate version.

The baselines below were captured with the validator at `be37208`, before any
Phase 4A change, as `canonical_hash(envelope.model_dump(mode="json"))`. They
are literals on purpose: recomputing them with the changed validator, or
hashing the raw fixture bytes, would prove nothing (validation normalizes
timestamps, and identity is the validated dump).
"""

import copy
import json

import pytest

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import (
    OutcomeEvaluatedEnvelope,
    OutcomeEvaluatedV2Envelope,
    validate_envelope_json,
)
from flight_recorder.fixtures import canonical_envelope_paths, load_json
from tests.conftest import (
    OUTCOME_EVENT_ID,
    canonical_by_type,
    event_hash,
    outcome_row,
    outcome_v2_envelope,
    reformatted,
    seed_all,
)

#: `events.canonical_hash` of every canonical envelope, captured at `be37208`.
BASELINE_CANONICAL_HASHES = {
    "00a-logic-artifact-v3.2.json": (
        "bfb2ad92007b511fe196c0ee400ba5793ce9b6946e458ecc9471d78930c7aa29"
    ),
    "00b-logic-artifact-v5.1.json": (
        "569142d25e6ac9768b653147ae5faaf48c3d182d5c23e099f9eb22338e80d5a6"
    ),
    "01-account-discovered.json": (
        "60df3c9ccb161d7f63e6beb420873136b930a0399084e4528a405921918501e8"
    ),
    "02-evidence-recorded-enrichment.json": (
        "a2daa528754cd34ab88caa4aac50e0ab678cf9ac7173b8ef4fe9507e82053417"
    ),
    "03-evidence-recorded-integration-pressure.json": (
        "0eecf8535bcf00359dda091f7994158dbd7628b9d60413f672377ae394763d4b"
    ),
    "04-decision-recorded.json": (
        "c6036673cf0888e09ab25a0001b8019767b9cf8b9b31b8cef08def9c51859f34"
    ),
    "05-persona-selected.json": (
        "bc0cc67280cffc7931c4378f38c6c302067de695a587456da02bbde998669779"
    ),
    "06-action-recorded.json": ("87b79ca4f5ae368992431b2a6a24888ff2afd04977d5bb26db384ad8be0fbe0a"),
    "07-outcome-evaluated.json": (
        "0d72dfdc0a37854930a9229d50fef19aba6c6e5d1e00c83e943505d861396429"
    ),
}

V2_ONLY_FIELDS = {
    "window_opened_at": "2026-04-17T10:07:00Z",
    "window_closes_at": "2026-07-16T10:07:00Z",
    "evaluation_state": "closed",
    "observed_at": "2026-07-16T10:07:00Z",
    "source_action_event_id": "evt-novasignal-06-action-recorded",
    "source_decision_event_id": "evt-novasignal-04-decision-recorded",
    "supersedes_outcome_event_id": "evt-some-earlier-outcome",
}

OPENED = "2026-04-17T10:07:00Z"
CLOSES = "2026-07-16T10:07:00Z"


def v2(event_id: str = "evt-test-o-v2", **overrides) -> dict:
    envelope = outcome_v2_envelope(
        event_id,
        observed_at="2026-05-01T00:00:00Z",
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state="open",
    )
    envelope["payload"].update(overrides)
    return envelope


def validation_messages(body: dict) -> str:
    return json.dumps(body["errors"])


# --- v1 identity ------------------------------------------------------------------


def test_every_canonical_envelope_keeps_its_pinned_baseline_hash(harness):
    assert set(BASELINE_CANONICAL_HASHES) == {p.name for p in canonical_envelope_paths()}
    for response in seed_all(harness):
        assert response.status_code == 201
    for path in canonical_envelope_paths():
        event_id = load_json(path)["event_id"]
        assert event_hash(harness, event_id) == BASELINE_CANONICAL_HASHES[path.name], path.name


def test_the_canonical_outcome_still_validates_as_the_unchanged_v1_model():
    envelope = validate_envelope_json(json.dumps(canonical_by_type("outcome.evaluated")))
    assert type(envelope) is OutcomeEvaluatedEnvelope
    assert (
        canonical_hash(envelope.model_dump(mode="json"))
        == (BASELINE_CANONICAL_HASHES["07-outcome-evaluated.json"])
    )
    assert set(envelope.payload.model_dump()) == {
        "window_days",
        "reply",
        "meeting",
        "opportunity",
        "action_event_id",
    }


def test_a_reordered_key_retry_of_the_v1_outcome_is_still_a_duplicate(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    before = harness.snapshot()
    response = harness.post_raw(reformatted(canonical_by_type("outcome.evaluated")))
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert (
        response.json()["canonical_hash"] == BASELINE_CANONICAL_HASHES["07-outcome-evaluated.json"]
    )
    assert harness.snapshot() == before


def test_a_v1_outcome_row_writes_exactly_what_it_wrote_before(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = canonical_by_type("outcome.evaluated")
    row = outcome_row(harness, OUTCOME_EVENT_ID)
    payload = envelope["payload"]
    assert row.schema_version == "1"
    assert row.account_ref == envelope["account_ref"]
    assert row.action_event_id == payload["action_event_id"]
    assert row.window_days == payload["window_days"]
    assert (row.reply, row.meeting, row.opportunity) == (
        payload["reply"],
        payload["meeting"],
        payload["opportunity"],
    )
    for column in (
        "window_opened_at",
        "window_closes_at",
        "evaluation_state",
        "observed_at",
        "source_action_event_id",
        "source_action_unusable_reason",
        "source_decision_event_id",
        "source_decision_unusable_reason",
        "supersedes_outcome_event_id",
    ):
        assert getattr(row, column) is None, column


@pytest.mark.parametrize("field", sorted(V2_ONLY_FIELDS))
def test_v1_rejects_every_v2_only_field(harness, field):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    envelope["event_id"] = "evt-test-v1-with-v2-field"
    envelope["payload"][field] = V2_ONLY_FIELDS[field]
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422
    body = response.json()
    assert body["reason"] == "invalid_envelope"
    assert "extra_forbidden" in validation_messages(body)
    assert field in validation_messages(body)
    assert harness.snapshot() == before


def test_v1_still_requires_true_or_false_observations(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    envelope["event_id"] = "evt-test-v1-null-observation"
    envelope["payload"]["reply"] = None
    before = harness.snapshot()
    assert harness.post(envelope).status_code == 422
    assert harness.snapshot() == before


def test_v2_rejects_a_v1_shaped_payload(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    envelope["event_id"] = "evt-test-v2-v1-shaped"
    envelope["schema_version"] = "2"
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422
    messages = validation_messages(response.json())
    for missing in ("window_opened_at", "window_closes_at", "evaluation_state", "observed_at"):
        assert missing in messages, missing
    for extra in ("window_days", "action_event_id"):
        assert extra in messages, extra
    assert harness.snapshot() == before


@pytest.mark.parametrize(
    "event_type,schema_version",
    [
        ("outcome.evaluated", "3"),
        ("action.recorded", "2"),
        ("decision.recorded", "2"),
        ("account.discovered", "2"),
    ],
)
def test_only_outcome_evaluated_accepts_schema_version_2(harness, event_type, schema_version):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = copy.deepcopy(canonical_by_type(event_type))
    envelope["event_id"] = "evt-test-unsupported-version"
    envelope["schema_version"] = schema_version
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422
    assert "schema_version" in validation_messages(response.json())
    assert harness.snapshot() == before


# --- v2 ----------------------------------------------------------------------------


def test_an_omitted_and_an_explicit_null_observation_share_one_canonical_hash(harness):
    omitted = v2()
    explicit = v2(reply=None, meeting=None, opportunity=None)
    assert "reply" not in omitted["payload"] and explicit["payload"]["reply"] is None

    dumps = [
        validate_envelope_json(json.dumps(e)).model_dump(mode="json") for e in (omitted, explicit)
    ]
    assert dumps[0] == dumps[1]
    assert dumps[0]["payload"]["reply"] is None  # present in the dump, as null
    assert canonical_hash(dumps[0]) == canonical_hash(dumps[1])

    for response in seed_all(harness):
        assert response.status_code == 201
    first = harness.post(omitted)
    assert first.status_code == 201, first.json()
    retry = harness.post(explicit)
    assert retry.status_code == 200
    assert retry.json()["status"] == "duplicate"
    assert retry.json()["canonical_hash"] == first.json()["canonical_hash"]
    row = outcome_row(harness, "evt-test-o-v2")
    assert (row.reply, row.meeting, row.opportunity) == (None, None, None)


def test_a_v2_outcome_validates_as_its_own_model_and_normalizes_its_instants():
    envelope = v2(reply=False)
    envelope["payload"]["window_opened_at"] = "2026-04-17T12:07:00+02:00"
    envelope["occurred_at"] = envelope["payload"]["observed_at"] = "2026-04-30T19:00:00-05:00"
    envelope["recorded_at"] = "2026-05-01T00:00:00Z"
    validated = validate_envelope_json(json.dumps(envelope))
    assert type(validated) is OutcomeEvaluatedV2Envelope
    dumped = validated.model_dump(mode="json")["payload"]
    assert dumped["window_opened_at"] == "2026-04-17T10:07:00.000000Z"
    assert dumped["observed_at"] == "2026-05-01T00:00:00.000000Z"
    assert dumped["reply"] is False


@pytest.mark.parametrize(
    "mutate,message",
    [
        pytest.param(
            lambda e: e["payload"].update(window_closes_at=OPENED),
            "must be strictly before window_closes_at",
            id="empty-period",
        ),
        pytest.param(
            lambda e: e["payload"].update(window_opened_at=CLOSES, window_closes_at=OPENED),
            "must be strictly before window_closes_at",
            id="reversed-period",
        ),
        pytest.param(
            lambda e: (
                e.update(occurred_at="2026-04-17T10:06:59Z", recorded_at="2026-05-01T00:00:00Z")
                or e["payload"].update(observed_at="2026-04-17T10:06:59Z")
            ),
            "must not be earlier than window_opened_at",
            id="observed-before-the-period",
        ),
        pytest.param(
            lambda e: e["payload"].update(evaluation_state="closed"),
            "evaluation_state 'closed' requires observed_at",
            id="closed-before-the-period-ends",
        ),
        pytest.param(
            lambda e: (
                e.update(occurred_at=CLOSES, recorded_at=CLOSES)
                or e["payload"].update(observed_at=CLOSES)
            ),
            "evaluation_state 'open' requires observed_at",
            id="open-at-the-period-end",
        ),
        pytest.param(
            lambda e: e["payload"].update(observed_at="2026-05-01T00:00:01Z"),
            "must be the same instant as occurred_at",
            id="observed-at-is-not-occurred-at",
        ),
        pytest.param(
            lambda e: e.update(recorded_at="2026-04-30T23:59:59Z"),
            "recorded_at must not be earlier than occurred_at",
            id="as-of-in-the-future-of-recording",
        ),
        pytest.param(
            lambda e: e["payload"].update(evaluation_state="pending"),
            "evaluation_state",
            id="unknown-state",
        ),
        pytest.param(
            lambda e: e["payload"].pop("evaluation_state"),
            "evaluation_state",
            id="state-never-inferred",
        ),
        pytest.param(
            lambda e: e["payload"].update(window_closes_at="2026-07-16T10:07:00"),
            "timezone",
            id="naive-bound",
        ),
        pytest.param(
            lambda e: e["payload"].update(source_action_event_id=""),
            "source_action_event_id",
            id="empty-claim",
        ),
    ],
)
def test_a_structurally_invalid_v2_outcome_is_rejected_without_writes(harness, mutate, message):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = v2()
    mutate(envelope)
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422, response.json()
    body = response.json()
    assert body["reason"] == "invalid_envelope"
    assert message in validation_messages(body)
    assert harness.snapshot() == before


def test_closed_exactly_at_the_period_end_is_valid(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = outcome_v2_envelope(
        "evt-test-o-closed",
        observed_at=CLOSES,
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state="closed",
        reply=False,
    )
    assert harness.post(envelope).status_code == 201
