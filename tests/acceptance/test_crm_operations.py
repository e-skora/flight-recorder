"""Original-bound CRM operation receipts; no provider clients or writes."""

import copy
import importlib
import importlib.util
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.crm_schema import create_crm_schema
from tests.adapters.crm.cases import envelopes, make_case, seal
from tests.invariants.test_inv_crm_collector import snapshot


def api():
    name = "flight_recorder.collector.crm_operations"
    assert importlib.util.find_spec(name) is not None, (
        "original-bound CRM operation contract missing"
    )
    return importlib.import_module(name)


def prepare(harness, case=None):
    api()
    schema = importlib.import_module("flight_recorder.ledger.crm_operations")
    create_crm_schema(harness.engine)
    schema.create_crm_operation_schema(harness.engine)
    case = case or make_case()
    for key in ("account", "artifact", "evidence", "decision"):
        assert harness.post(case[key]).status_code == 201
    return case


def time_at(case, seconds):
    return format_utc(
        datetime.fromisoformat(case["capture"]["capture_boundary"]) + timedelta(seconds=seconds)
    )


def intent(case, revision=1, **changes):
    cap = case["capture"]
    p = case["decision"]["payload"]
    d = dict(
        format_version=1,
        target=cap["target"],
        connection_id=cap["connection_id"],
        decision_event_id=case["decision"]["event_id"],
        capture_hash=p["capture_hash"],
        capture_sequence=cap["capture_sequence"],
        artifact_hash=cap["rule_hash"],
        evaluator_version=cap["evaluator_version"],
        mapping_hash=cap["mapping_hash"],
        selection_identity=cap["selection_identity"],
        policy_generation=1,
        revision=revision,
        source_precondition=cap["precondition"],
        created_at=time_at(case, 1),
        operation_id="0" * 64,
        intended=dict(
            fr_score=p["result"]["score"],
            fr_output=p["result"]["output"],
            fr_logic_version=p["logic_artifact"]["logic_version"],
            fr_decision_ref=case["decision"]["event_id"],
            fr_op_id="0" * 64,
            fr_op_rev=revision,
        ),
    )
    d.update(changes)
    pre = copy.deepcopy(d)
    pre.pop("operation_id")
    pre["intended"].pop("fr_op_id")
    digest = canonical_hash(pre)
    d["operation_id"] = digest
    d["intended"]["fr_op_id"] = digest
    return d


def observation(case, i, seq=1, phase="prepared", state="prepared", at=None, **changes):
    at = at or time_at(case, seq + 1)
    p = dict(
        data_origin="fictional_records_real_crm",
        observation_origin=case["capture"]["observation_origin"],
        intent=copy.deepcopy(i),
        observation_sequence=seq,
        phase=phase,
        state=state,
        observed_at=at,
        response=None,
        output_read=None,
        reason=None,
    )
    p.update(changes)
    return dict(
        schema_version="2",
        event_type="external_operation.observed",
        source="salesforce-test-account",
        event_id=f"fr-crm-write-observation-v1-{i['operation_id']}-{seq}",
        account_ref=case["decision"]["account_ref"],
        occurred_at=at,
        recorded_at=at,
        payload=p,
    )


def receipt(case, i, status=204, complete=True):
    return dict(
        request_hash=api().request_digest(i),
        started_at=time_at(case, 3),
        finished_at=time_at(case, 4),
        status=status,
        complete=complete,
        body_sha256=canonical_hash("redacted fictional body") if complete else None,
    )


def output_read(case, i, **changes):
    d = dict(
        target=case["capture"]["target"],
        fields=sorted(i["intended"]),
        values=copy.deepcopy(i["intended"]),
        started_at=time_at(case, 5),
        finished_at=time_at(case, 6),
        body_sha256=canonical_hash("fictional outputs"),
    )
    d.update(changes)
    return d


def post_ok(harness, env):
    response = harness.post(env)
    assert response.status_code == 201, response.text
    return response


def sent(harness, case, i):
    post_ok(harness, observation(case, i))
    post_ok(harness, observation(case, i, 2, "send_started", "unknown"))


def test_original_complete_response_and_matching_outputs_are_terminal(harness):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    post_ok(
        harness,
        observation(case, i, 3, "response", "awaiting_observation", response=receipt(case, i)),
    )
    env = observation(
        case,
        i,
        4,
        "output_observed",
        "observed_applied_at_t",
        at=time_at(case, 7),
        output_read=output_read(case, i),
    )
    post_ok(harness, env)
    before = snapshot(harness.engine)
    assert harness.post(env).status_code == 200
    assert snapshot(harness.engine) == before
    with harness.engine.connect() as conn:
        rows = api().operation_history(conn, case["decision"]["account_ref"])
    assert len(rows) == 4 and rows[-1]["payload"]["state"] == "observed_applied_at_t"


@pytest.mark.parametrize("status", [None, 401, 429, 500, 503, 204, 412])
def test_incomplete_or_lost_response_stays_unknown_after_matching_read(harness, status):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    post_ok(
        harness,
        observation(case, i, 3, "response", "unknown", response=receipt(case, i, status, False)),
    )
    post_ok(
        harness,
        observation(
            case,
            i,
            4,
            "output_observed",
            "unknown",
            at=time_at(case, 7),
            output_read=output_read(case, i),
        ),
    )
    before = snapshot(harness.engine)
    env = observation(case, i, 5, "send_started", "unknown", at=time_at(case, 8))
    assert harness.post(env).status_code in (409, 422)
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "status,state",
    [
        (204, "awaiting_observation"),
        (200, "awaiting_observation"),
        (412, "refused_no_effect"),
        (401, "unknown"),
        (429, "unknown"),
        (500, "unknown"),
    ],
)
def test_response_status_disposition(harness, status, state):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    post_ok(harness, observation(case, i, 3, "response", state, response=receipt(case, i, status)))


@pytest.mark.parametrize(
    "change",
    [
        "score",
        "output",
        "logic",
        "decision",
        "artifact",
        "capture",
        "target",
        "connection",
        "selection",
        "mapping",
        "etag",
        "revision",
        "policy",
        "counterfactual",
        "created_before_original",
    ],
)
def test_forged_original_or_binding_is_atomic_refusal(harness, change):
    case = prepare(harness)
    i = intent(case)
    if change == "score":
        i["intended"]["fr_score"] += 1
    elif change == "output":
        i["intended"]["fr_output"] = "DO_NOT_PRIORITIZE"
    elif change == "logic":
        i["intended"]["fr_logic_version"] = "invented"
    elif change == "decision":
        i["intended"]["fr_decision_ref"] = "foreign"
    elif change in ("artifact", "capture", "mapping"):
        i[change + "_hash"] = "f" * 64
    elif change == "target":
        i["target"]["record_id"] = "001000000000002AAA"
    elif change == "connection":
        i["connection_id"] = "foreign"
    elif change == "selection":
        i["selection_identity"] = "foreign"
    elif change == "etag":
        i["source_precondition"] = '"newer-token"'
    elif change == "revision":
        i["revision"] = 2
        i["intended"]["fr_op_rev"] = 2
    elif change == "policy":
        i["policy_generation"] = True
    elif change == "counterfactual":
        i["decision_event_id"] = "counterfactual-only"
    else:
        i["created_at"] = time_at(case, -1)
    # Rehash a forged binding so rejection cannot rely just on stale operation hash.
    pre = copy.deepcopy(i)
    pre.pop("operation_id")
    pre["intended"].pop("fr_op_id")
    i["operation_id"] = canonical_hash(pre)
    i["intended"]["fr_op_id"] = i["operation_id"]
    before = snapshot(harness.engine)
    assert harness.post(observation(case, i)).status_code in (409, 422)
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize("etag", [None, 'W/"weak"', "unquoted", '""', '"bad"tag"'])
def test_no_usable_captured_etag_refuses_operation_not_history(harness, etag):
    # All are valid captured strings; none is a usable strong write precondition.
    case = prepare(harness, make_case(precondition=etag))
    i = intent(case)
    before = snapshot(harness.engine)
    assert harness.post(observation(case, i)).status_code == 422
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "wrong_score",
        "foreign_target",
        "before_response",
        "future_read",
        "wrong_request",
        "before_send",
        "incomplete_response",
    ],
)
def test_false_terminal_proof_is_refused_atomically(harness, change):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    rec = receipt(case, i)
    if change == "wrong_request":
        rec["request_hash"] = "f" * 64
    elif change == "before_send":
        rec["started_at"] = time_at(case, 2)
    elif change == "incomplete_response":
        rec["complete"] = False
        rec["body_sha256"] = None
    if change in ("wrong_request", "before_send", "incomplete_response"):
        env = observation(case, i, 3, "response", "awaiting_observation", response=rec)
    else:
        post_ok(harness, observation(case, i, 3, "response", "awaiting_observation", response=rec))
        read = output_read(case, i)
        if change == "partial":
            read["values"].pop("fr_decision_ref")
        elif change == "wrong_score":
            read["values"]["fr_score"] += 1
        elif change == "foreign_target":
            read["target"] = {**read["target"], "record_id": "001000000000002AAA"}
        elif change == "before_response":
            read["started_at"] = time_at(case, 3)
        else:
            read["finished_at"] = time_at(case, 8)
        env = observation(
            case,
            i,
            4,
            "output_observed",
            "observed_applied_at_t",
            at=time_at(case, 7),
            output_read=read,
        )
    before = snapshot(harness.engine)
    assert harness.post(env).status_code in (409, 422)
    assert snapshot(harness.engine) == before


def test_append_only_projection_and_atomic_fault(harness):
    case = prepare(harness)
    i = intent(case)
    before = snapshot(harness.engine)

    def fail(_):
        raise RuntimeError("injected before commit")

    harness.collector.before_commit = fail
    with pytest.raises(RuntimeError, match="injected"):
        harness.post(observation(case, i))
    assert snapshot(harness.engine) == before
    harness.collector.before_commit = None
    post_ok(harness, observation(case, i))
    for sql in [
        "DELETE FROM crm_operation_observations",
        "UPDATE crm_operation_observations SET state=state",
        "INSERT OR REPLACE INTO crm_operation_observations "
        "SELECT * FROM crm_operation_observations",
    ]:
        with pytest.raises(Exception, match="append-only"), harness.engine.begin() as conn:
            conn.exec_driver_sql(sql)


def test_legacy_database_refuses_operation_without_creating_tables(harness):
    api()
    case = make_case()
    before = set(inspect(harness.engine).get_table_names())
    response = harness.post(observation(case, intent(case)))
    assert response.status_code == 422
    assert response.json()["reason"] == "crm_operation_schema_uninitialized"
    assert set(inspect(harness.engine).get_table_names()) == before


def newer(harness, case):
    cap = copy.deepcopy(case["capture"])
    cap["capture_sequence"] += 1
    cap["reprocesses"] = cap["unit_id"]
    cap["capture_boundary"] = time_at(case, 10)
    cap["adapter_version"] = "crm-adapter-v1"
    cap["selection_identity"] += "-new"
    new = envelopes(seal(cap), case["artifact"])
    # Same observation and evidence are reused by explicit reprocessing.
    post_ok(harness, new["decision"])
    return new


def test_new_capture_after_send_does_not_rewrite_pending_history(harness):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    newer(harness, case)
    post_ok(
        harness,
        observation(
            case,
            i,
            3,
            "response",
            "awaiting_observation",
            at=time_at(case, 11),
            response=receipt(case, i),
        ),
    )
    post_ok(
        harness,
        observation(
            case,
            i,
            4,
            "output_observed",
            "observed_applied_at_t",
            at=time_at(case, 12),
            output_read=output_read(case, i),
        ),
    )
    with harness.engine.connect() as conn:
        assert len(api().operation_history(conn, case["decision"]["account_ref"])) == 4


def test_new_capture_blocks_unsent_old_original(harness):
    case = prepare(harness)
    i = intent(case)
    post_ok(harness, observation(case, i))
    newer(harness, case)
    before = snapshot(harness.engine)
    assert harness.post(
        observation(case, i, 2, "send_started", "unknown", at=time_at(case, 11))
    ).status_code in (409, 422)
    assert snapshot(harness.engine) == before


def test_terminal_old_operation_allows_only_new_original_next_revision(harness):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    later = newer(harness, case)
    j = intent(later, revision=2)
    before = snapshot(harness.engine)
    assert harness.post(observation(later, j)).status_code == 422
    assert snapshot(harness.engine) == before
    post_ok(
        harness,
        observation(
            case,
            i,
            3,
            "response",
            "awaiting_observation",
            at=time_at(case, 11),
            response=receipt(case, i),
        ),
    )
    post_ok(
        harness,
        observation(
            case,
            i,
            4,
            "output_observed",
            "observed_applied_at_t",
            at=time_at(case, 12),
            output_read=output_read(case, i),
        ),
    )
    post_ok(harness, observation(later, j))
    with harness.engine.connect() as conn:
        assert len(api().operation_history(conn, case["decision"]["account_ref"])) == 5


@pytest.mark.parametrize(
    "change",
    [
        "intent_extra",
        "response_extra",
        "origin",
        "event_id",
        "schema",
        "source",
        "sequence",
        "occurred",
        "future_receipt",
        "future_read",
        "skip_send",
        "second_send",
        "response_after_read",
        "terminal_followup",
        "fake_proof",
    ],
)
def test_closed_wire_and_order_refuse_without_partial_rows(harness, change):
    case = prepare(harness)
    i = intent(case)
    env = observation(case, i)
    if change == "intent_extra":
        env["payload"]["intent"]["operator_score"] = 100
    elif change == "origin":
        env["payload"]["observation_origin"] = "provider_http"
    elif change == "event_id":
        env["event_id"] += "foreign"
    elif change == "schema":
        env["schema_version"] = "3"
    elif change == "source":
        env["source"] = "hubspot-synthetic-writer"
    elif change == "sequence":
        env["payload"]["observation_sequence"] = 2
    elif change == "occurred":
        env["occurred_at"] = time_at(case, 1)
    elif change == "fake_proof":
        env["payload"]["settled_operation_ids"] = [i["operation_id"]]
    else:
        sent(harness, case, i)
        rec = receipt(case, i)
        env = observation(case, i, 3, "response", "awaiting_observation", response=rec)
        if change == "response_extra":
            rec["access_token"] = "fictional-never-a-token"
        elif change == "future_receipt":
            rec["finished_at"] = time_at(case, 10)
        elif change == "skip_send":
            env["payload"]["phase"] = "prepared"
            env["payload"]["state"] = "prepared"
        elif change == "second_send":
            env = observation(case, i, 3, "send_started", "unknown")
        elif change == "future_read":
            env = observation(
                case, i, 3, "output_observed", "unknown", output_read=output_read(case, i)
            )
        elif change == "response_after_read":
            post_ok(
                harness,
                observation(
                    case,
                    i,
                    3,
                    "output_observed",
                    "unknown",
                    at=time_at(case, 7),
                    output_read=output_read(case, i),
                ),
            )
            env = observation(
                case, i, 4, "response", "awaiting_observation", at=time_at(case, 8), response=rec
            )
        elif change == "terminal_followup":
            post_ok(
                harness,
                observation(
                    case, i, 3, "response", "refused_no_effect", response=receipt(case, i, 412)
                ),
            )
            env = observation(
                case,
                i,
                4,
                "output_observed",
                "unknown",
                at=time_at(case, 7),
                output_read=output_read(case, i),
            )
    before = snapshot(harness.engine)
    assert harness.post(env).status_code in (409, 422)
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "column,value", [("state", "observed_applied_at_t"), ("intent_json", "{}"), ("revision", 99)]
)
def test_corrupt_projection_is_refused_without_repair(harness, column, value):
    case = prepare(harness)
    i = intent(case)
    post_ok(harness, observation(case, i))
    raw = harness.engine.raw_connection()
    try:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_operation_observations_no_update'"
        ).fetchone()[0]
        raw.execute("DROP TRIGGER crm_operation_observations_no_update")
        raw.execute("UPDATE crm_operation_observations SET " + column + "=?", (value,))
        raw.execute(trigger)
        raw.commit()
    finally:
        raw.close()
    before = snapshot(harness.engine)
    with harness.engine.connect() as conn, pytest.raises(CollectorError):
        api().operation_history(conn, case["decision"]["account_ref"])
    assert snapshot(harness.engine) == before


def test_retained_domain_cannot_be_migrated_implicitly_or_explicitly(harness):
    prepare(harness)
    before = snapshot(harness.engine)
    schema = importlib.import_module("flight_recorder.ledger.crm_operations")
    with pytest.raises(ValueError, match="initialization_refused"):
        schema.create_crm_operation_schema(harness.engine)
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize("format_version", [True, 1.0])
def test_operation_format_requires_exact_integer(format_version):
    value = intent(make_case())
    value["format_version"] = format_version
    with pytest.raises(ValueError):
        api().CRMWriteIntent.model_validate_json(json.dumps(value), strict=True)


def test_malformed_stored_event_is_named_refusal_not_uncaught_parser_error(harness):
    case = prepare(harness)
    i = intent(case)
    post_ok(harness, observation(case, i))
    raw = harness.engine.raw_connection()
    try:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='events_no_update'"
        ).fetchone()[0]
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute(
            "UPDATE events SET payload='{' WHERE event_id=?", (observation(case, i)["event_id"],)
        )
        raw.execute(trigger)
        raw.commit()
    finally:
        raw.close()
    before = snapshot(harness.engine)
    with harness.engine.connect() as conn, pytest.raises(CollectorError):
        api().operation_history(conn, case["decision"]["account_ref"])
    assert snapshot(harness.engine) == before


def test_missing_input_history_is_readable_but_not_writable(harness):
    case = make_case()
    cap = copy.deepcopy(case["capture"])
    cap["properties"].pop("NumberOfEmployees")
    mapping = importlib.import_module("flight_recorder.adapters.crm.mapping")
    cap["inputs"] = mapping.map_inputs(cap["properties"], [])
    case = prepare(harness, envelopes(seal(cap), case["artifact"]))
    before = snapshot(harness.engine)
    assert harness.post(observation(case, intent(case))).status_code == 422
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("crm_decisions", "score", 999),
        ("crm_captures", "body_json", "{}"),
        ("crm_decision_context", "value_text", '"tampered"'),
        ("crm_decision_consumed_inputs", "contribution", 999),
        ("logic_artifacts", "artifact_json", "{}"),
    ],
)
def test_original_corruption_cannot_enter_write_history(harness, table, column, value):
    case = prepare(harness)
    i = intent(case)
    raw = harness.engine.raw_connection()
    try:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table + "_no_update",)
        ).fetchone()[0]
        raw.execute("DROP TRIGGER " + table + "_no_update")
        raw.execute("UPDATE " + table + " SET " + column + "=?", (value,))
        raw.execute(trigger)
        raw.commit()
    finally:
        raw.close()
    before = snapshot(harness.engine)
    assert harness.post(observation(case, i)).status_code == 422
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "state,reason", [("refused", "policy_disabled"), ("refused", "authorization_unavailable")]
)
def test_unsent_refusal_cannot_have_a_later_send(harness, state, reason):
    case = prepare(harness)
    i = intent(case)
    post_ok(harness, observation(case, i))
    post_ok(harness, observation(case, i, 2, "refused", state, reason=reason))
    before = snapshot(harness.engine)
    assert harness.post(observation(case, i, 3, "send_started", "unknown")).status_code == 422
    assert snapshot(harness.engine) == before


def test_old_original_cannot_be_prepared_after_new_capture(harness):
    case = prepare(harness)
    newer(harness, case)
    before = snapshot(harness.engine)
    assert harness.post(observation(case, intent(case), at=time_at(case, 12))).status_code == 422
    assert snapshot(harness.engine) == before


def test_output_subset_keeps_completed_request_awaiting_observation(harness):
    case = prepare(harness)
    i = intent(case)
    sent(harness, case, i)
    post_ok(
        harness,
        observation(case, i, 3, "response", "awaiting_observation", response=receipt(case, i)),
    )
    read = output_read(case, i)
    read["values"].pop("fr_op_id")
    post_ok(
        harness,
        observation(
            case,
            i,
            4,
            "output_observed",
            "awaiting_observation",
            at=time_at(case, 7),
            output_read=read,
        ),
    )
    post_ok(
        harness,
        observation(
            case,
            i,
            5,
            "output_observed",
            "observed_applied_at_t",
            at=time_at(case, 8),
            output_read=output_read(case, i),
        ),
    )
