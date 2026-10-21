"""Frozen v3 originals enter the real Collector; delivery retries keep exact bytes."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.crm_schema import create_crm_schema
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import create_schema
from tests.adapters.crm.cases import make_case
from tests.adapters.crm.test_capture import freeze, read
from tests.adapters.crm.test_capture_store import target


def feature(module, name):
    try:
        return getattr(importlib.import_module("flight_recorder.adapters.crm." + module), name)
    except (ModuleNotFoundError, AttributeError):
        pytest.fail("missing frozen CRM Collector submission boundary", pytrace=False)


def setup(tmp_path, *, observation=None, selected_artifact=None):
    path = tmp_path / "ops.sqlite"
    try:
        store = CRMStore(path, format_version=3)
    except (TypeError, CRMRefused):
        pytest.fail("missing new-only operational format3 submissions", pytrace=False)
    owner = store.acquire(target(), "a" * 64)
    cfg = None
    if selected_artifact is not None:
        from tests.adapters.crm.test_oauth import configuration

        artifact = selected_artifact["payload"]["artifact"]
        cfg = configuration(
            artifact_hash=canonical_hash(artifact), evaluator_version=artifact["evaluator_version"]
        )
    cap = freeze(store, owner, observation, cfg=cfg).capture
    engine = make_engine(tmp_path / "domain.sqlite")
    create_schema(engine)
    create_crm_schema(engine)
    collector = Collector(engine)
    case = make_case()
    account = case["account"]
    account["payload"]["name"] = cap.document["properties"]["Name"]
    prerequisites = feature("submission_contracts", "CRMPrerequisites")(
        canonical_bytes(account),
        canonical_bytes(selected_artifact or case["artifact"]),
        cap.document["capture_boundary"],
        b"{}",
    )
    return store, owner, cap, engine, collector, prerequisites


def prepare(store, collector, owner, prerequisites):
    return feature("submit", "freeze_submission")(
        store, collector, owner, prerequisites=prerequisites
    )


def resume(store, collector, owner):
    return feature("submit", "resume_submission")(store, collector, owner)


def rows(engine, table):
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"SELECT * FROM {table}").fetchall()


def test_frozen_plan_precedes_all_domain_calls_and_contains_exact_v3_original(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    assert rows(engine, "events") == []
    d = frozen.document
    assert d["unit_id"] == cap.unit_id and d["capture_hash"] == cap.capture_hash
    assert [stage["stage"] for stage in d["stages"]] == [
        "account",
        "artifact",
        "evidence",
        "decision",
    ]
    assert d["domain_path"] == str(Path(engine.url.database).resolve())
    for stage in d["stages"]:
        assert stage["canonical_hash"] == canonical_hash(stage["envelope"])
    for stage in d["stages"][2:]:
        assert stage["envelope"]["schema_version"] == "3"
        assert stage["envelope"]["payload"]["capture"] == cap.document
    mutable = frozen.document
    mutable["stages"][-1]["envelope"]["payload"]["result"]["score"] = 999
    assert frozen.document != mutable
    assert store.load_submission(owner) == frozen
    store.close()
    engine.dispose()


def test_real_collector_complete_original_is_verified_and_repeat_is_idempotent(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    progress = resume(store, collector, owner)
    assert progress.state == "complete"
    assert progress.decision_event_id == "fr-crm-decision-v1-" + cap.unit_id
    assert len(rows(engine, "crm_decisions")) == 1
    assert len(rows(engine, "events")) == 4
    before = rows(engine, "events")
    assert resume(store, collector, owner) == progress
    assert rows(engine, "events") == before
    assert prepare(store, collector, owner, prerequisites) == frozen
    store.close()
    engine.dispose()


@pytest.mark.parametrize("stage", ["account", "artifact", "evidence", "decision"])
def test_lost_return_after_commit_resumes_identical_bytes_without_source_access(tmp_path, stage):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    original = collector.ingest_json
    sent = []
    lost = False
    wanted = next(s["event_id"] for s in frozen.document["stages"] if s["stage"] == stage)

    def lose_return(body):
        nonlocal lost
        sent.append(body)
        result = original(body)
        if not lost and result.event_id == wanted:
            lost = True
            raise RuntimeError("fictional-source-value must not leak")
        return result

    collector.ingest_json = lose_return
    with pytest.raises(CRMRefused):
        resume(store, collector, owner)
    store.close()
    store = CRMStore(tmp_path / "ops.sqlite", format_version=3)
    collector.ingest_json = original
    assert resume(store, collector, owner).state == "complete"
    assert len(rows(engine, "events")) == 4 and len(rows(engine, "crm_decisions")) == 1
    expected = {s["event_id"]: canonical_bytes(s["envelope"]) for s in frozen.document["stages"]}
    assert all(body == expected[json.loads(body)["event_id"]] for body in sent)
    assert store.load_capture(cap.unit_id) == cap
    store.close()
    engine.dispose()


@pytest.mark.parametrize("change", ["event_id", "canonical_hash", "http_status", "status"])
def test_forged_collector_ack_cannot_advance_next_stage(tmp_path, change):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    calls = []

    def invalid(body):
        calls.append(body)
        env = json.loads(body)
        result = dict(
            status="created",
            http_status=201,
            event_id=env["event_id"],
            canonical_hash=canonical_hash(env),
        )
        result[change] = {
            "event_id": "foreign",
            "canonical_hash": "0" * 64,
            "http_status": True,
            "status": "maybe",
        }[change]
        return SimpleNamespace(**result)

    collector.ingest_json = invalid
    progress = resume(store, collector, owner)
    assert progress.state == "failed" and len(calls) == 1
    assert rows(engine, "events") == []
    assert resume(store, collector, owner).state == "failed" and len(calls) == 1
    store.close()
    engine.dispose()


def test_matching_ack_without_actual_domain_commit_is_not_acceptance(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)

    def fake_ack(body):
        env = json.loads(body)
        return SimpleNamespace(
            status="created",
            http_status=201,
            event_id=env["event_id"],
            canonical_hash=canonical_hash(env),
        )

    collector.ingest_json = fake_ack
    with pytest.raises(CRMRefused):
        resume(store, collector, owner)
    assert rows(engine, "crm_decisions") == []
    store.close()
    engine.dispose()


def test_changed_prerequisite_time_refuses_without_rewriting_frozen_plan(tmp_path):
    from dataclasses import replace

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    with pytest.raises(CRMRefused, match="submission_prerequisite_conflict"):
        prepare(store, collector, owner, replace(prerequisites, recorded_at="2026-10-21T23:00:00Z"))
    assert store.load_submission(owner) == frozen and rows(engine, "events") == []
    store.close()
    engine.dispose()


def test_foreign_domain_and_stale_owner_do_not_deliver(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    foreign = make_engine(tmp_path / "foreign.sqlite")
    create_schema(foreign)
    create_crm_schema(foreign)
    with pytest.raises(CRMRefused):
        resume(store, Collector(foreign), owner)
    assert rows(foreign, "events") == []
    store.abandon(owner)
    store.acquire(target(), "b" * 64)
    with pytest.raises(CRMRefused, match="stale_ownership"):
        resume(store, collector, owner)
    assert rows(engine, "events") == []
    store.close()
    engine.dispose()
    foreign.dispose()


def test_delivery_requires_the_concrete_in_process_sql_collector(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    calls = []
    proxy = SimpleNamespace(engine=engine, ingest_json=lambda body: calls.append(body))
    with pytest.raises(CRMRefused):
        prepare(store, proxy, owner, prerequisites)
    assert calls == [] and store.load_submission(owner) is None
    store.close()
    engine.dispose()


def test_missing_domain_schema_is_a_named_hold_without_hidden_repair(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE crm_decision_context")
    with pytest.raises(CRMRefused):
        resume(store, collector, owner)
    with engine.connect() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='crm_decision_context'"
            ).fetchall()
            == []
        )
    assert rows(engine, "events") == []
    store.close()
    engine.dispose()


def test_operational_fence_stays_held_across_local_delivery_and_ack(tmp_path):
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    rival = CRMStore(tmp_path / "ops.sqlite", format_version=3)
    connect = rival._connect

    def no_wait():
        conn = connect()
        conn.execute("PRAGMA busy_timeout=0")
        return conn

    rival._connect = no_wait
    original = collector.ingest_json
    calls = []

    def deliver(body):
        with sqlite3.connect(store.path) as conn:
            assert (
                conn.execute("SELECT count(*) FROM crm_submission_attempts").fetchone()[0]
                == len(calls) + 1
            )
        with pytest.raises(CRMRefused, match="operational_store_contended"):
            rival.abandon(owner)
        assert rival.owner(target()) == owner
        calls.append(body)
        result = original(body)
        with pytest.raises(CRMRefused, match="operational_store_contended"):
            rival.abandon(owner)
        return result

    collector.ingest_json = deliver
    assert resume(store, collector, owner).state == "complete" and len(calls) == 4
    rival.abandon(owner)
    rival.acquire(target(), "b" * 64)
    with pytest.raises(CRMRefused, match="stale_ownership"):
        resume(store, collector, owner)
    assert len(rows(engine, "events")) == 4
    store.close()
    rival.close()
    engine.dispose()


@pytest.mark.parametrize("stage", ["account", "artifact", "evidence", "decision"])
def test_ack_persistence_fault_rolls_back_ack_but_keeps_attempt_and_domain_commit(tmp_path, stage):
    import sqlite3
    import traceback

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    original = store.observe_submission
    raised = False

    def fail_after_insert(conn, owner_, frozen, attempt, disposition, details):
        nonlocal raised
        original(conn, owner_, frozen, attempt, disposition, details)
        if not raised and store._submission_attempt(conn, frozen, attempt)["stage"] == stage:
            raised = True
            raise OSError("fictional-private-source-value")

    store.observe_submission = fail_after_insert
    with pytest.raises(CRMRefused) as caught:
        resume(store, collector, owner)
    assert "fictional-private-source-value" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    index = ["account", "artifact", "evidence", "decision"].index(stage) + 1
    assert len(rows(engine, "events")) == index
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM crm_submission_attempts").fetchone()[0] == index
        assert (
            conn.execute("SELECT count(*) FROM crm_submission_observations").fetchone()[0]
            == index - 1
        )
    store.observe_submission = original
    assert resume(store, collector, owner).state == "complete"
    assert len(rows(engine, "events")) == 4 and len(rows(engine, "crm_decisions")) == 1
    store.close()
    engine.dispose()


def test_no_usable_inputs_cannot_freeze_or_submit_a_decision(tmp_path):
    import sqlite3

    from flight_recorder.adapters.crm.contracts import ObservedRead

    d = read().document
    d["properties"] = {
        k: v
        for k, v in d["properties"].items()
        if k in ["Id", "Name", "Website", "LastModifiedDate"]
    }
    d["response_body_sha256"] = canonical_hash(d["properties"])
    store, owner, cap, engine, collector, prerequisites = setup(
        tmp_path, observation=ObservedRead.from_document(d)
    )
    with pytest.raises(CRMRefused, match="no_usable_inputs"):
        prepare(store, collector, owner, prerequisites)
    assert store.load_submission(owner) is None and rows(engine, "events") == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM crm_submission_domain").fetchone()[0] == 0
    store.close()
    engine.dispose()


@pytest.mark.parametrize(
    "change", ["account", "artifact", "supersession_key", "supersession_missing"]
)
def test_prerequisite_identity_or_lineage_failure_leaves_no_plan_or_domain_changes(
    tmp_path, change
):
    from dataclasses import replace

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    if change == "account":
        value = json.loads(prerequisites.account_envelope)
        value["account_ref"] = "foreign"
        prerequisites = replace(prerequisites, account_envelope=canonical_bytes(value))
    elif change == "artifact":
        value = json.loads(prerequisites.artifact_envelope)
        value["payload"]["artifact"]["logic_version"] = "unselected-version"
        prerequisites = replace(prerequisites, artifact_envelope=canonical_bytes(value))
    elif change == "supersession_key":
        prerequisites = replace(prerequisites, supersessions=b'{"unknown":null}')
    else:
        prerequisites = replace(
            prerequisites,
            supersessions=canonical_bytes(
                {"employee_count": "fr-crm-evidence-version-v1-" + "0" * 64}
            ),
        )
    with pytest.raises(CRMRefused):
        prepare(store, collector, owner, prerequisites)
    assert store.load_submission(owner) is None and rows(engine, "events") == []
    store.close()
    engine.dispose()


def imported_evidence(tmp_path):
    from dataclasses import replace

    from tests.adapters.crm.cases import envelopes

    store, owner, first, engine, collector, prerequisites = setup(
        tmp_path, observation=read(count=183)
    )
    store.release(owner)
    owner = store.acquire(target(), "b" * 64)
    cap = freeze(store, owner, read(seconds=10)).capture
    prerequisites = replace(prerequisites, recorded_at=cap.document["capture_boundary"])
    old = make_case()
    for key in ["account", "artifact", "evidence"]:
        collector.ingest_json(canonical_bytes(old[key]))
    current = envelopes(cap.document)
    collector.ingest_json(canonical_bytes(current["evidence"]))
    return store, owner, cap, engine, collector, prerequisites, old


def test_all_exact_imported_evidence_is_verified_and_not_reminted(tmp_path):
    store, owner, cap, engine, collector, prerequisites, _ = imported_evidence(tmp_path)
    before = rows(engine, "crm_evidence_versions")
    frozen = prepare(store, collector, owner, prerequisites)
    assert [s["stage"] for s in frozen.document["stages"]] == ["account", "artifact", "decision"]
    assert len(frozen.document["reused_evidence"]) == 7
    assert resume(store, collector, owner).state == "complete"
    assert rows(engine, "crm_evidence_versions") == before
    assert len(rows(engine, "crm_decisions")) == 1
    store.close()
    engine.dispose()


def test_mixed_imported_and_new_bound_versions_refuse_before_plan_or_ingest(tmp_path):
    from dataclasses import replace

    store, owner, cap, engine, collector, prerequisites, old = imported_evidence(tmp_path)
    predecessor = next(
        x["evidence_version_id"]
        for x in old["evidence"]["payload"]["items"]
        if x["evidence_type"] == "employee_count"
    )
    prerequisites = replace(
        prerequisites, supersessions=canonical_bytes({"employee_count": predecessor})
    )
    before = rows(engine, "events")
    with pytest.raises(CRMRefused, match="mixed_evidence_reuse_unsupported"):
        prepare(store, collector, owner, prerequisites)
    assert store.load_submission(owner) is None and rows(engine, "events") == before
    store.close()
    engine.dispose()


@pytest.mark.parametrize(
    "table",
    [
        "crm_submission_domain",
        "crm_frozen_submissions",
        "crm_submission_attempts",
        "crm_submission_observations",
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_submission_rows_are_immutable_through_raw_sql_with_recursive_triggers_off(
    tmp_path, table, operation
):
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    resume(store, collector, owner)
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        row = conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        with pytest.raises(sqlite3.IntegrityError):
            if operation == "UPDATE":
                conn.execute(f"UPDATE {table} SET {columns[0]}={columns[0]}")
            elif operation == "DELETE":
                conn.execute(f"DELETE FROM {table}")
            else:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in row)})", row
                )
        assert conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone() == row
        for alias in ["rowid", "_rowid_", "oid"]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"SELECT {alias} FROM {table}")
    assert resume(store, collector, owner).state == "complete"
    store.close()
    engine.dispose()


@pytest.mark.parametrize("table", ["crm_submission_domain", "crm_frozen_submissions"])
def test_failed_plan_persistence_leaves_no_partial_binding_or_domain_event(tmp_path, table):
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    with store.transaction() as conn:
        conn.execute(
            f"CREATE TRIGGER plan_fault AFTER INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fictional-private-source-value'); END"
        )
    with pytest.raises(CRMRefused):
        prepare(store, collector, owner, prerequisites)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM crm_submission_domain").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM crm_frozen_submissions").fetchone()[0] == 0
    assert rows(engine, "events") == []
    store.close()
    engine.dispose()


@pytest.mark.parametrize("created,opened", [(1, 3), (2, 3), (3, 1), (3, 2)])
def test_new_format3_never_migrates_a_retained_operational_file(tmp_path, created, opened):
    path = tmp_path / "ops.sqlite"
    CRMStore(path, format_version=created).close()
    before = path.read_bytes()
    with pytest.raises(CRMRefused, match="operational_store_unrecognized"):
        CRMStore(path, format_version=opened)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["result", "context", "consumed", "capture", "stage_hash", "stage_order", "extra"]
)
def test_frozen_contract_rejects_forged_original_or_partial_bindings(tmp_path, change):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    d = frozen.document
    stage = d["stages"][-1]
    p = stage["envelope"]["payload"]
    if change == "result":
        p["result"]["score"] += 1
    elif change == "context":
        p["historical_context"][0]["value"] = "forged-value"
    elif change == "consumed":
        p["consumed_inputs"][0]["contribution"] += 1
    elif change == "capture":
        p["capture_hash"] = "0" * 64
    elif change == "stage_hash":
        stage["canonical_hash"] = "0" * 64
    elif change == "stage_order":
        d["stages"][0], d["stages"][1] = d["stages"][1], d["stages"][0]
    else:
        d["caller_supplied_score"] = 999
    if change != "stage_hash":
        stage["canonical_hash"] = canonical_hash(stage["envelope"])
    with pytest.raises(CRMRefused):
        feature("submission_contracts", "CRMSubmission").from_document(d)
    assert store.load_submission(owner) == frozen and rows(engine, "events") == []
    store.close()
    engine.dispose()


def test_matching_document_cannot_claim_another_owners_plan(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    d = frozen.document
    d["claim_id"] = "b" * 64
    candidate = feature("submission_contracts", "CRMSubmission").from_document(d)
    with pytest.raises(CRMRefused, match="stale_ownership"):
        store.freeze_submission(owner, candidate)
    assert store.load_submission(owner) == frozen
    store.close()
    engine.dispose()


def test_acknowledged_context_projection_loss_is_a_hold_not_a_repair(tmp_path):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    resume(store, collector, owner)
    with engine.begin() as conn:
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='crm_decision_context_no_delete'"
        ).scalar_one()
        conn.exec_driver_sql("DROP TRIGGER crm_decision_context_no_delete")
        conn.exec_driver_sql("DELETE FROM crm_decision_context")
        conn.exec_driver_sql(ddl)
    before = rows(engine, "events")
    with pytest.raises(CRMRefused, match="acknowledged_context_integrity"):
        resume(store, collector, owner)
    assert rows(engine, "events") == before and rows(engine, "crm_decision_context") == []
    store.close()
    engine.dispose()


def test_tampered_accepted_observation_cannot_remain_accepted(tmp_path):
    import hashlib
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    resume(store, collector, owner)
    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT attempt_id,body FROM crm_submission_observations "
            "WHERE disposition='accepted_created' LIMIT 1"
        ).fetchone()
        body = json.loads(row[1])
        body["details"]["http_status"] = True
        encoded = canonical_bytes(body)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_submission_observations_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER crm_submission_observations_no_update")
        conn.execute(
            "UPDATE crm_submission_observations SET body=?,body_sha256=? WHERE attempt_id=?",
            (encoded, hashlib.sha256(encoded).hexdigest(), row[0]),
        )
        conn.execute(ddl)
    with pytest.raises(CRMRefused, match="submission_observation_integrity"):
        resume(store, collector, owner)
    assert len(rows(engine, "events")) == 4
    store.close()
    engine.dispose()


def test_concurrent_local_delivery_has_one_original_and_identical_event_bytes(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    rival = CRMStore(tmp_path / "ops.sqlite", format_version=3)
    barrier = threading.Barrier(2)

    def call(s):
        barrier.wait(timeout=5)
        return resume(s, collector, owner)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(call, [store, rival]))
    assert all(result.state == "complete" for result in results)
    assert len(rows(engine, "events")) == 4 and len(rows(engine, "crm_decisions")) == 1
    assert store.load_submission(owner) == frozen
    store.close()
    rival.close()
    engine.dispose()


def test_unregistered_selected_artifact_cannot_claim_an_unsupported_runtime_evaluator(tmp_path):
    artifact = make_case()["artifact"]
    artifact["payload"]["artifact"]["evaluator_version"] = "unknown-evaluator"
    store, owner, cap, engine, collector, prerequisites = setup(
        tmp_path, selected_artifact=artifact
    )
    with pytest.raises(CRMRefused):
        prepare(store, collector, owner, prerequisites)
    assert store.load_submission(owner) is None and rows(engine, "events") == []
    store.close()
    engine.dispose()


@pytest.mark.parametrize("key", ["plan_hash", "plan_owner", "attempt_tuple"])
def test_alternate_unique_submission_keys_cannot_replace_a_different_primary_key(tmp_path, key):
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    prepare(store, collector, owner, prerequisites)
    resume(store, collector, owner)
    table = "crm_submission_attempts" if key == "attempt_tuple" else "crm_frozen_submissions"
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        row = dict(
            zip(columns, conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone(), strict=True)
        )
        changed = dict(row)
        if key == "attempt_tuple":
            changed["attempt_id"] = "f" * 64
        else:
            changed["unit_id"] = "f" * 64
            if key == "plan_hash":
                changed.update(target_key="foreign", claim_id="e" * 64, generation=2)
            else:
                changed["submission_hash"] = "d" * 64
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in columns)})",
                tuple(changed[c] for c in columns),
            )
    assert resume(store, collector, owner).state == "complete"
    store.close()
    engine.dispose()


def test_frozen_resume_has_no_provider_or_token_access(tmp_path, monkeypatch):
    from flight_recorder.adapters.crm.salesforce import SalesforceClient
    from flight_recorder.adapters.crm.secrets import TokenStore

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)

    def forbidden(*args, **kwargs):
        pytest.fail("frozen delivery consulted current provider or token state")

    monkeypatch.setattr(SalesforceClient, "read_selected", forbidden)
    monkeypatch.setattr(SalesforceClient, "verify_connection", forbidden)
    monkeypatch.setattr(TokenStore, "read_ready", forbidden)
    assert resume(store, collector, owner).state == "complete"
    assert store.load_submission(owner) == frozen
    store.close()
    engine.dispose()


def test_frozen_plan_load_names_a_runtime_rule_failure_without_exposing_rule_data(
    tmp_path, monkeypatch
):
    from flight_recorder.adapters.crm import submission_contracts
    from flight_recorder.logic.rules import UnsupportedRule

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)

    def unsupported(*args, **kwargs):
        raise UnsupportedRule("employee_count", "fictional-private-rule-value")

    monkeypatch.setattr(submission_contracts, "evaluate", unsupported)
    with pytest.raises(CRMRefused) as caught:
        submission_contracts.CRMSubmission.from_document(frozen.document)
    assert "fictional-private-rule-value" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert rows(engine, "events") == []
    store.close()
    engine.dispose()


@pytest.mark.parametrize(
    "part",
    [
        "artifact_id",
        "logic_version",
        "prerequisite_target",
        "prerequisite_source",
        "prerequisite_artifact",
    ],
)
def test_forged_logic_or_original_prerequisite_refuses_before_any_persistence(tmp_path, part):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    persist = store.freeze_submission
    store.freeze_submission = lambda owner_, submission: submission
    frozen = prepare(store, collector, owner, prerequisites)
    store.freeze_submission = persist
    d = _forge_submission_identity(frozen.document, part)
    with pytest.raises(CRMRefused):
        persist(owner, feature("submission_contracts", "CRMSubmission").from_document(d))
    with store._read() as conn:
        for table in [
            "crm_submission_domain",
            "crm_frozen_submissions",
            "crm_submission_attempts",
            "crm_submission_observations",
        ]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert rows(engine, "events") == [] and store.capture_for_owner(owner) == cap
    store.close()
    engine.dispose()


def _forge_submission_identity(d, part):
    if part in ("artifact_id", "logic_version"):
        stage = d["stages"][-1]
        stage["envelope"]["payload"]["logic_artifact"][part] = "foreign-frozen-label"
        stage["canonical_hash"] = canonical_hash(stage["envelope"])
    else:
        pri = d["prerequisite_binding"]
        if part == "prerequisite_target":
            pri["account"]["account_ref"] = "fr-crm-account-v1-" + "b" * 64
        elif part == "prerequisite_source":
            pri["account"]["source"] = "foreign-prerequisite-source"
        else:
            pri["artifact"]["payload"]["artifact"]["artifact_id"] = "foreign-prerequisite-artifact"
        d["prerequisite_hash"] = canonical_hash(pri)
    return d


@pytest.mark.parametrize(
    "part",
    [
        "artifact_id",
        "logic_version",
        "prerequisite_target",
        "prerequisite_source",
        "prerequisite_artifact",
    ],
)
def test_loading_self_consistent_forged_plan_is_a_named_integrity_hold(tmp_path, part):
    import sqlite3

    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    d = _forge_submission_identity(frozen.document, part)
    with sqlite3.connect(store.path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_frozen_submissions_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER crm_frozen_submissions_no_update")
        conn.execute(
            "UPDATE crm_frozen_submissions SET body=?,submission_hash=?",
            (canonical_bytes(d), canonical_hash(d)),
        )
        conn.execute(ddl)
    with pytest.raises(CRMRefused):
        store.load_submission(owner)
    assert rows(engine, "events") == []
    store.close()
    engine.dispose()


@pytest.mark.parametrize("damage", ["schema", "accepted_context"])
def test_repeated_freeze_rechecks_bound_domain_without_repair_or_new_attempt(tmp_path, damage):
    store, owner, cap, engine, collector, prerequisites = setup(tmp_path)
    frozen = prepare(store, collector, owner, prerequisites)
    if damage == "accepted_context":
        assert resume(store, collector, owner).state == "complete"
        with engine.begin() as conn:
            ddl = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='crm_decision_context_no_delete'"
            ).scalar_one()
            conn.exec_driver_sql("DROP TRIGGER crm_decision_context_no_delete")
            conn.exec_driver_sql("DELETE FROM crm_decision_context")
            conn.exec_driver_sql(ddl)
    else:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE crm_decision_context")
    before = rows(engine, "events")
    with store._read() as conn:
        attempts = conn.execute("SELECT count(*) FROM crm_submission_attempts").fetchone()[0]
    with pytest.raises(CRMRefused):
        prepare(store, collector, owner, prerequisites)
    assert rows(engine, "events") == before
    assert store.load_submission(owner) == frozen
    with store._read() as conn:
        assert (
            conn.execute("SELECT count(*) FROM crm_submission_attempts").fetchone()[0] == attempts
        )
    store.close()
    engine.dispose()
