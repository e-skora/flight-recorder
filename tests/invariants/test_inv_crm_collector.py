"""Atomic admission attacks against real SQLite and the unchanged evaluator."""

import copy
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import inspect, text

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.schema import format_utc
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import create_schema
from tests.adapters.crm.cases import envelopes, feature, make_case, seal

pytestmark = pytest.mark.invariant

TABLES = {
    "crm_captures",
    "crm_evidence_versions",
    "crm_decisions",
    "crm_decision_context",
    "crm_decision_consumed_inputs",
}


@pytest.fixture
def domain(tmp_path):
    engine = make_engine(tmp_path / "domain.sqlite")
    create_schema(engine)
    collector = Collector(engine)
    yield engine, collector
    engine.dispose()


def submit(collector, envelope):
    return collector.ingest_json(json.dumps(envelope))


def prepare(domain, case):
    if not TABLES & set(inspect(domain[0]).get_table_names()):
        feature("ledger.crm_schema").create_crm_schema(domain[0])
    for key in ("account", "artifact"):
        submit(domain[1], case[key])


def snapshot(engine):
    with engine.connect() as conn:
        return {
            name: conn.exec_driver_sql('SELECT * FROM "' + name + '"').fetchall()
            for name in inspect(conn).get_table_names()
        }


def test_complete_capture_is_admitted_and_duplicate_is_idempotent(domain):
    case = make_case()
    prepare(domain, case)
    for name in ("evidence", "decision"):
        assert submit(domain[1], case[name]).status == "created"
        before = snapshot(domain[0])
        assert submit(domain[1], case[name]).status == "duplicate"
        assert snapshot(domain[0]) == before
    with domain[0].connect() as conn:
        row = conn.exec_driver_sql("SELECT score,output FROM crm_decisions").one()
    assert row == (case["decision"]["payload"]["result"]["score"], "PRIORITIZE")


@pytest.mark.parametrize("precondition", [None, 'W/"weak-observed-token"'])
def test_read_history_with_no_strong_write_token_is_still_admissible(domain, precondition):
    case = make_case(precondition=precondition)
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    assert submit(domain[1], case["decision"]).status == "created"
    assert case["capture"]["precondition"] == precondition


@pytest.mark.parametrize(
    "change",
    [
        "score",
        "contribution",
        "context_value",
        "omitted_available",
        "missing_evidence",
        "foreign_artifact",
    ],
)
def test_forged_decision_is_atomic_refusal(domain, change):
    case = make_case()
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    env = copy.deepcopy(case["decision"])
    p = env["payload"]
    if change == "score":
        p["result"]["score"] += 1
    elif change == "contribution":
        p["consumed_inputs"][0]["contribution"] += 1
    elif change == "context_value":
        p["historical_context"][0]["value"] = "forged"
    elif change == "omitted_available":
        p["historical_context"].pop()
    elif change == "missing_evidence":
        p["historical_context"][0]["evidence_version_id"] = "missing"
    else:
        p["logic_artifact"]["artifact_id"] = "foreign"
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], env)
    assert snapshot(domain[0]) == before


def test_fault_after_projection_rolls_back_every_new_row(domain):
    case = make_case()
    prepare(domain, case)
    before = snapshot(domain[0])

    def crash(envelope):
        raise RuntimeError("fault after projection")

    domain[1].before_commit = crash
    with pytest.raises(RuntimeError, match="fault after projection"):
        submit(domain[1], case["evidence"])
    assert snapshot(domain[0]) == before


@pytest.mark.parametrize("table", sorted(TABLES))
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {t} SET {col}={col}",
        "DELETE FROM {t}",
        "INSERT OR REPLACE INTO {t} SELECT * FROM {t}",
    ],
)
def test_domain_projections_are_append_only_even_for_replacement(domain, table, statement):
    case = make_case()
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    submit(domain[1], case["decision"])
    before = snapshot(domain[0])
    column = inspect(domain[0]).get_columns(table)[0]["name"]
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError), domain[0].begin() as conn:
        conn.execute(text(statement.format(t=table, col=column)))
    assert snapshot(domain[0]) == before


def test_default_database_has_no_live_tables_and_refuses_without_migration(tmp_path):
    feature("ledger.crm_schema")
    engine = make_engine(tmp_path / "legacy.sqlite")
    create_schema(engine)
    assert not TABLES & set(inspect(engine).get_table_names())
    case = make_case()
    collector = Collector(engine)
    for key in ("account", "artifact"):
        submit(collector, case[key])
    before = snapshot(engine)
    with pytest.raises(CollectorError) as exc:
        submit(collector, case["evidence"])
    assert exc.value.body["reason"] == "crm_schema_uninitialized"
    assert snapshot(engine) == before
    engine.dispose()


def test_reprocessing_reuses_only_identical_full_observation(domain):
    case = make_case()
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    submit(domain[1], case["decision"])
    cap = copy.deepcopy(case["capture"])
    cap.update(
        capture_sequence=2,
        ownership_generation=2,
        reprocesses=cap["unit_id"],
        selection_identity="different-reviewed-selection",
    )
    later = envelopes(seal(cap))
    assert later["capture"]["observation_id"] == case["capture"]["observation_id"]
    assert submit(domain[1], later["decision"]).status == "created"
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], later["evidence"])
    assert snapshot(domain[0]) == before


def test_unit_cannot_be_rebound_to_another_capture_sequence(domain):
    case = make_case()
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    cap = copy.deepcopy(case["capture"])
    cap["capture_sequence"] += 1
    bad = envelopes(seal(cap))
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], bad["decision"])
    assert snapshot(domain[0]) == before


def test_any_forged_score_is_refused_without_domain_change(domain):
    case = make_case()
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    before = snapshot(domain[0])

    @given(st.integers(min_value=-100000, max_value=100000).filter(lambda x: x != 0))
    def attack(delta):
        env = copy.deepcopy(case["decision"])
        env["payload"]["result"]["score"] += delta
        with pytest.raises(CollectorError):
            submit(domain[1], env)
        assert snapshot(domain[0]) == before

    attack()


def next_capture(case):
    cap = copy.deepcopy(case["capture"])
    for key in ["read_started_at", "read_finished_at", "capture_boundary"]:
        cap[key] = format_utc(datetime.fromisoformat(cap[key]) + timedelta(seconds=10))
    cap["capture_sequence"] += 1
    cap["ownership_generation"] += 1
    return seal(cap)


def accept_case(domain, case):
    prepare(domain, case)
    submit(domain[1], case["evidence"])
    submit(domain[1], case["decision"])


def test_input_states_and_ignored_availability_remain_distinct(domain):
    case = make_case()
    cap = case["capture"]
    cap["properties"]["NumberOfEmployees"] = None
    cap["properties"].pop("FR_Head_Of_Platform_Start__c")
    cap["inputs"] = feature("adapters.crm.mapping").map_inputs(cap["properties"], [])
    case = envelopes(seal(cap))
    accept_case(domain, case)
    with domain[0].connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT input_key,availability,evidence_version_id FROM crm_decision_context"
        ).all()
        used = {
            row[0]
            for row in conn.exec_driver_sql("SELECT input_key FROM crm_decision_consumed_inputs")
        }
    by_key = {row[0]: row for row in rows}
    assert by_key["employee_count"][1:] == ("unavailable", None)
    assert "head_of_platform_start_date" not in by_key
    assert by_key["verified_integration_pressure"][1] == "available"
    assert "verified_integration_pressure" not in used
    assert "employee_count" not in used


def test_equal_semantic_value_from_another_read_cannot_borrow_old_evidence(domain):
    case = make_case()
    accept_case(domain, case)
    cap = next_capture(case)
    # Fixture value is loaded from the canonical source, rather than hard-coded in identities.
    count = case["capture"]["inputs"]["open_platform_engineering_roles"]["value"]
    cap["properties"]["FR_Open_Platform_Roles__c"] = {"json_number": str(count) + ".0"}
    cap["response_body_sha256"] = canonical_hash(cap["properties"])
    later = envelopes(seal(cap))
    originals = {
        x["input_key"]: x["evidence_version_id"]
        for x in case["decision"]["payload"]["historical_context"]
    }
    for key in ("historical_context", "consumed_inputs"):
        for entry in later["decision"]["payload"][key]:
            entry["evidence_version_id"] = originals[entry["input_key"]]
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], later["decision"])
    assert snapshot(domain[0]) == before


@pytest.mark.parametrize("foreign", [False, True])
def test_reprocessing_requires_explicit_same_target_lineage(domain, foreign):
    case = make_case()
    accept_case(domain, case)
    cap = copy.deepcopy(case["capture"])
    cap.update(capture_sequence=2, selection_identity="reprocessed-selection")
    if foreign:
        cap["target"]["instance_id"] = "00D000000000002EAA"
        cap["reprocesses"] = case["capture"]["unit_id"]
    later = envelopes(seal(cap))
    if foreign:
        submit(domain[1], later["account"])
        submit(domain[1], later["evidence"])
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], later["decision"])
    assert snapshot(domain[0]) == before


@pytest.mark.parametrize("valid_type", [True, False])
def test_supersession_appends_only_earlier_same_target_type(domain, valid_type):
    case = make_case()
    accept_case(domain, case)
    original = {
        x["evidence_type"]: x["evidence_version_id"] for x in case["evidence"]["payload"]["items"]
    }
    cap = next_capture(case)
    predecessor = original["employee_count" if valid_type else "industry"]
    later = envelopes(cap, supersessions={"employee_count": predecessor})
    before = snapshot(domain[0])
    if valid_type:
        submit(domain[1], later["evidence"])
        submit(domain[1], later["decision"])
        with domain[0].connect() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM crm_decisions").scalar_one() == 2
        assert all(
            row in snapshot(domain[0])["crm_evidence_versions"]
            for row in before["crm_evidence_versions"]
        )
    else:
        with pytest.raises(CollectorError):
            submit(domain[1], later["evidence"])
        assert snapshot(domain[0]) == before


@pytest.mark.parametrize("corrupt", ["semantic", "observation", "minting_event"])
def test_reused_evidence_verifies_projection_and_minting_event(domain, corrupt):
    case = make_case()
    accept_case(domain, case)
    cap = copy.deepcopy(case["capture"])
    cap.update(
        capture_sequence=2, reprocesses=cap["unit_id"], selection_identity="reprocess-selection"
    )
    later = envelopes(seal(cap))
    table = "events" if corrupt == "minting_event" else "crm_evidence_versions"
    trigger = table + "_no_update"
    with domain[0].begin() as conn:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
        ).scalar_one()
        conn.exec_driver_sql("DROP TRIGGER " + trigger)
        if corrupt == "minting_event":
            conn.exec_driver_sql(
                "UPDATE events SET recorded_at='2999-01-01T00:00:00.000000Z' WHERE event_id=?",
                (case["evidence"]["event_id"],),
            )
        else:
            column = "value_json" if corrupt == "semantic" else "observation_json"
            conn.exec_driver_sql(
                "UPDATE crm_evidence_versions SET "
                + column
                + "='{}' WHERE evidence_type='employee_count'"
            )
        conn.exec_driver_sql(sql)
    before = snapshot(domain[0])
    with pytest.raises(CollectorError):
        submit(domain[1], later["decision"])
    assert snapshot(domain[0]) == before


def test_damaged_live_schema_refuses_even_an_idempotent_duplicate(domain):
    case = make_case()
    accept_case(domain, case)
    with domain[0].begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER crm_decisions_no_replace")
    before = snapshot(domain[0])
    with pytest.raises(CollectorError) as exc:
        submit(domain[1], case["evidence"])
    assert exc.value.body["reason"] == "crm_schema_uninitialized"
    assert snapshot(domain[0]) == before


def test_explicit_initializer_refuses_reinitialization_without_changes(domain):
    feature("ledger.crm_schema").create_crm_schema(domain[0])
    before = snapshot(domain[0])
    with pytest.raises(ValueError, match="crm_schema_initialization_refused"):
        feature("ledger.crm_schema").create_crm_schema(domain[0])
    assert snapshot(domain[0]) == before


def test_live_schema_is_never_added_to_a_nonempty_legacy_ledger(tmp_path):
    engine = make_engine(tmp_path / "retained.sqlite")
    create_schema(engine)
    case = make_case()
    submit(Collector(engine), case["account"])
    before = snapshot(engine)
    with pytest.raises(ValueError, match="crm_schema_initialization_refused"):
        feature("ledger.crm_schema").create_crm_schema(engine)
    assert snapshot(engine) == before
    assert not TABLES & set(inspect(engine).get_table_names())
    engine.dispose()


def test_two_collectors_race_without_partial_or_duplicate_domain_rows(domain):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    case = make_case()
    prepare(domain, case)
    barrier = Barrier(2)

    def caller():
        collector = Collector(domain[0])
        barrier.wait(timeout=5)
        try:
            return submit(collector, case["evidence"]).status
        except CollectorError as exc:
            return exc.body["reason"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(caller), pool.submit(caller)]
        results = [future.result(timeout=15) for future in futures]
    assert results.count("created") == 1
    assert all(
        value in ("created", "duplicate", "crm_identity_conflict", "crm_admission_contended")
        for value in results
    )
    with domain[0].connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM crm_captures").scalar_one() == 1
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM crm_evidence_versions"
        ).scalar_one() == len(case["evidence"]["payload"]["items"])


@pytest.mark.parametrize("collision", ["capture_unit", "capture_sequence", "decision_unit"])
def test_replace_cannot_delete_history_through_a_secondary_unique_key(domain, collision):
    case = make_case()
    accept_case(domain, case)
    table = "crm_decisions" if collision == "decision_unit" else "crm_captures"
    before = snapshot(domain[0])
    connection = sqlite3.connect(domain[0].url.database)
    connection.row_factory = sqlite3.Row
    try:
        values = dict(connection.execute("SELECT * FROM " + table).fetchone())
        if collision == "decision_unit":
            values["decision_event_id"] = case["evidence"]["event_id"]
        else:
            values["capture_hash"] = "0" * 64
            if collision == "capture_unit":
                values["capture_sequence"] += 1
            else:
                values["unit_id"] = "1" * 64
        statement = (
            "INSERT OR REPLACE INTO "
            + table
            + " ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("?" for _ in values)
            + ")"
        )
        with connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, tuple(values.values()))
    finally:
        connection.close()
    assert snapshot(domain[0]) == before
