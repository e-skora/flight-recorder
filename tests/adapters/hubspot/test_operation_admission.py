"""Task4: real-Collector binding, one-send grants and truthful fake observations."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import events
from tests.adapters.hubspot.test_submission_recovery import setup_case
from tests.public_demo.conftest import full_state

SOURCE = "hubspot-synthetic-writer"
EVENT = "external_operation.observed"


def accepted_case(tmp_path, harness, **kwargs):
    submit, _, store, case, frozen, _ = setup_case(tmp_path, harness, **kwargs)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    return {
        "store": store,
        "case": case,
        "frozen": frozen,
        "decision": frozen.document["stages"][-1]["envelope"],
    }


def rehash_intent(intent):
    binding = copy.deepcopy(intent)
    binding.pop("operation_id", None)
    binding["intended"].pop("fr_op_id", None)
    binding["intended"].pop("fr_op_rev", None)
    op = canonical_hash(binding)
    intent["operation_id"] = op
    intent["intended"].update(fr_op_id=op, fr_op_rev=intent["revision"])
    return intent


def make_intent(context, *, revision=1, shift=0):
    d = context["decision"]
    payload = d["payload"]
    at = datetime.fromisoformat(d["recorded_at"]) + timedelta(seconds=1 + shift)
    return rehash_intent(
        {
            "format_version": 1,
            "company_key": context["case"]["unit"].company_key,
            "unit_id": payload["capture"]["unit_id"],
            "capture_hash": payload["capture_hash"],
            "capture_sequence": payload["capture"]["capture_sequence"],
            "decision_event_id": d["event_id"],
            "artifact_hash": payload["logic_artifact"]["artifact_hash"],
            "evaluator_version": payload["logic_artifact"]["evaluator_version"],
            "revision": revision,
            "created_at": format_utc(at),
            "intended": {
                "fr_score": payload["result"]["score"],
                "fr_output": payload["result"]["output"],
                "fr_logic_version": payload["logic_artifact"]["logic_version"],
                "fr_decision_ref": d["event_id"],
            },
        }
    )


def observation(
    context,
    intent=None,
    *,
    sequence=1,
    state="prepared",
    reason="intent_prepared",
    properties=None,
    settled=(),
    pending=(),
    not_applied=(),
):
    intent = copy.deepcopy(intent or make_intent(context))
    at = format_utc(datetime.fromisoformat(intent["created_at"]) + timedelta(seconds=sequence - 1))
    return {
        "schema_version": "1",
        "event_id": f"fr-write-observation-v1-{intent['operation_id']}-{sequence}",
        "event_type": EVENT,
        "source": SOURCE,
        "account_ref": context["decision"]["account_ref"],
        "occurred_at": at,
        "recorded_at": at,
        "payload": {
            "intent": intent,
            "observation_sequence": sequence,
            "observed_at": at,
            "state": state,
            "reason": reason,
            "observed_properties": properties,
            "settled_operation_ids": sorted(set(settled)),
            "pending_operation_ids": sorted(set(pending)),
            "not_applied_operation_ids": sorted(set(not_applied)),
        },
    }


def ingest(harness, envelope):
    return harness.collector.ingest_json(canonical_bytes(envelope))


def started(harness, context, intent=None):
    intent = intent or make_intent(context)
    ingest(harness, observation(context, intent))
    ingest(
        harness, observation(context, intent, sequence=2, state="unknown", reason="send_started")
    )
    return intent


def test_prepared_operation_is_accepted_atomically_and_idempotently(tmp_path, harness):
    context = accepted_case(tmp_path, harness)
    env = observation(context)
    before = harness.event_count()
    assert ingest(harness, env).status == "created"
    assert harness.event_count() == before + 1
    assert (
        harness.collector.ingest_json(json.dumps(env, indent=4, sort_keys=True)).status
        == "duplicate"
    )
    changed = copy.deepcopy(env)
    changed["payload"]["reason"] = "different"
    with pytest.raises(CollectorError) as error:
        ingest(harness, changed)
    assert error.value.status_code == 409
    with harness.engine.connect() as conn:
        row = conn.execute(select(events).where(events.c.event_id == env["event_id"])).one()
        assert row.canonical_hash == canonical_hash(env)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", True),
        ("revision", True),
        ("capture_sequence", 0),
        ("extra", "hidden"),
        ("unit_id", "bad"),
        ("capture_hash", "0" * 64),
        ("decision_event_id", "counterfactual"),
        ("company_key", '["71",74]'),
        ("artifact_hash", "1" * 64),
        ("evaluator_version", "other"),
    ],
)
def test_intent_shape_and_original_binding_fail_without_writes(tmp_path, harness, field, value):
    context = accepted_case(tmp_path, harness)
    intent = make_intent(context)
    intent[field] = value
    rehash_intent(intent)
    env = observation(context, intent)
    before = full_state(harness.db_path)
    with pytest.raises(CollectorError):
        ingest(harness, env)
    assert full_state(harness.db_path) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("fr_score", True),
        ("fr_score", 999),
        ("fr_output", "PRIORITIZE"),
        ("fr_decision_ref", "foreign"),
        ("fr_logic_version", "relabelled"),
        ("other_property", "not allowed"),
    ],
)
def test_full_intended_tuple_is_derived_from_original(tmp_path, harness, field, value):
    context = accepted_case(tmp_path, harness)
    intent = make_intent(context)
    intent["intended"][field] = value
    rehash_intent(intent)
    with pytest.raises(CollectorError):
        ingest(harness, observation(context, intent))


def test_one_original_cannot_bind_a_second_intent_after_completion(tmp_path, harness):
    context = accepted_case(tmp_path, harness)
    intent = started(harness, context)
    ingest(
        harness,
        observation(
            context,
            intent,
            sequence=3,
            state="observed_applied_at_t",
            reason="full_tuple_observed",
            properties=intent["intended"],
            settled=[intent["operation_id"]],
        ),
    )
    other = make_intent(context, revision=2, shift=100)
    with pytest.raises(CollectorError):
        ingest(harness, observation(context, other))


def test_send_started_marker_cannot_be_rearmed_at_later_sequence(tmp_path, harness):
    context = accepted_case(tmp_path, harness)
    intent = started(harness, context)
    ingest(
        harness,
        observation(context, intent, sequence=3, state="unknown", reason="observation_pending"),
    )
    with pytest.raises(CollectorError):
        ingest(
            harness,
            observation(context, intent, sequence=4, state="unknown", reason="send_started"),
        )


@pytest.mark.parametrize(
    "state,proof", [("observed_applied_at_t", "match"), ("observed_not_applied_at_t", "negative")]
)
def test_positive_and_negative_terminal_observations_are_distinct(tmp_path, harness, state, proof):
    context = accepted_case(tmp_path, harness)
    intent = started(harness, context)
    op = intent["operation_id"]
    env = observation(
        context,
        intent,
        sequence=3,
        state=state,
        reason="definite_fake_rejection" if proof == "negative" else "full_tuple_observed",
        properties=None if proof == "negative" else intent["intended"],
        settled=[op],
        not_applied=[op] if proof == "negative" else [],
    )
    assert ingest(harness, env).status == "created"
    assert ingest(harness, env).status == "duplicate"
    with pytest.raises(CollectorError):
        ingest(
            harness,
            observation(context, intent, sequence=4, state="unknown", reason="send_started"),
        )


@pytest.mark.parametrize(
    "fault",
    [
        "score_only",
        "foreign_operation",
        "pending_earlier",
        "not_settled",
        "negative_missing_proof",
        "applied_with_negative_proof",
    ],
)
def test_weak_or_contradictory_disposition_never_becomes_terminal(tmp_path, harness, fault):
    context = accepted_case(tmp_path, harness)
    intent = started(harness, context)
    op = intent["operation_id"]
    env = observation(
        context,
        intent,
        sequence=3,
        state="observed_applied_at_t",
        reason="full_tuple_observed",
        properties=intent["intended"],
        settled=[op],
    )
    p = env["payload"]
    if fault == "score_only":
        p["observed_properties"] = {"fr_score": intent["intended"]["fr_score"]}
    if fault == "foreign_operation":
        p["observed_properties"]["fr_op_id"] = "f" * 64
    if fault == "pending_earlier":
        p["pending_operation_ids"] = ["e" * 64]
    if fault == "not_settled":
        p["settled_operation_ids"] = []
    if fault == "negative_missing_proof":
        p.update(state="observed_not_applied_at_t", observed_properties=None)
    if fault == "applied_with_negative_proof":
        p["not_applied_operation_ids"] = [op]
    with pytest.raises(CollectorError):
        ingest(harness, env)


def test_new_event_rollback_and_trace_label(tmp_path, harness):
    context = accepted_case(tmp_path, harness)
    env = observation(context)
    before = full_state(harness.db_path)

    def fail(_):
        raise RuntimeError("injected before commit")

    harness.collector.before_commit = fail
    with pytest.raises(RuntimeError, match="injected"):
        ingest(harness, env)
    assert full_state(harness.db_path) == before
    harness.collector.before_commit = None
    ingest(harness, env)
    response = harness.client.get("/accounts/" + env["account_ref"])
    assert response.status_code == 200
    assert "OPERATION" in response.text and "Simulated" in response.text
    assert "observed_applied" not in response.text


def test_two_store_initial_preparations_have_one_collector_binding(tmp_path, harness):
    context = accepted_case(tmp_path, harness)
    barrier = Barrier(2)

    def prepare(shift):
        barrier.wait()
        try:
            return ingest(harness, observation(context, make_intent(context, shift=shift))).status
        except CollectorError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(prepare, [0, 10]))
    assert outcomes.count("created") == 1
    assert all(x == "created" or x == 409 for x in outcomes)
    with harness.engine.connect() as conn:
        assert len(conn.execute(select(events).where(events.c.event_type == EVENT)).all()) == 1


@pytest.mark.parametrize("fault", ["hash", "payload", "missing_marker"])
def test_corrupt_or_missing_prior_observation_is_not_repaired(tmp_path, harness, fault):
    import sqlite3

    context = accepted_case(tmp_path, harness)
    intent = started(harness, context)
    marker = observation(context, intent, sequence=2, state="unknown", reason="send_started")
    with sqlite3.connect(harness.db_path) as conn:
        if fault == "missing_marker":
            conn.execute("DROP TRIGGER events_no_delete")
            conn.execute("DELETE FROM events WHERE event_id=?", (marker["event_id"],))
        else:
            conn.execute("DROP TRIGGER events_no_update")
            if fault == "hash":
                conn.execute(
                    "UPDATE events SET canonical_hash=? WHERE event_id=?",
                    ("f" * 64, marker["event_id"]),
                )
            else:
                conn.execute(
                    "UPDATE events SET payload=? WHERE event_id=?",
                    ('{"malformed":true}', marker["event_id"]),
                )
    before = full_state(harness.db_path)
    with pytest.raises(CollectorError):
        ingest(
            harness,
            observation(context, intent, sequence=3, state="unknown", reason="observation_pending"),
        )
    assert full_state(harness.db_path) == before
