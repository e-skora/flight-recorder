"""Durable bounded polls over injected reads and the real local Collector."""

import importlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from flight_recorder.adapters.crm.contracts import CRMRefused, ObservedRead
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from tests.adapters.crm.cases import make_case
from tests.adapters.crm.test_capture import read
from tests.adapters.crm.test_oauth import configuration


def feature(module, name):
    try:
        return getattr(importlib.import_module("flight_recorder.adapters.crm." + module), name)
    except (ModuleNotFoundError, AttributeError):
        pytest.fail("missing durable Salesforce polling runtime", pytrace=False)


def config(tmp_path, **updates):
    case = make_case()
    artifact = case["artifact"]["payload"]["artifact"]
    connection = configuration(artifact_hash=canonical_hash(artifact))
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    secret = directory / "ingress"
    if not secret.exists():
        secret.write_bytes(b"fictional-ingress-secret-for-local-tests-only")
        secret.chmod(0o600)
    case["account"]["payload"]["name"] = read().properties["Name"]
    d = dict(
        format_version=1,
        connection=connection.document,
        domain_path=str(tmp_path / "domain.sqlite"),
        operational_path=str(tmp_path / "ops.sqlite"),
        credential_directory=str(directory),
        ingress_secret_file=str(secret),
        ingress_origin="http://localhost:8768",
        selection_identity="test-selection-v1",
        discoveries={connection.record_ids[0]: case["account"]},
        artifact_registration=case["artifact"],
    )
    d.update(updates)
    return feature("runtime_config", "CRMRuntimeConfig").from_document(d)


class Provider:
    def __init__(self):
        self.calls = 0
        self.verifications = 0
        self.count = 184
        self.missing = False

    def verify_connection(self):
        self.verifications += 1

    def read_selected(self, target):
        self.calls += 1
        d = read(seconds=self.calls * 2, count=self.count).document
        if self.missing:
            d["properties"] = {
                k: v
                for k, v in d["properties"].items()
                if k in ["Id", "Name", "Website", "LastModifiedDate"]
            }
        d["response_body_sha256"] = canonical_hash(d["properties"])
        d["precondition"] = f'"fictional-read-{self.calls}"'
        return ObservedRead.from_document(d)


def harness(tmp_path):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    provider = Provider()
    runtime = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    runtime.start(background=False)
    return runtime, provider


def dispatch(runtime, tick):
    return feature("poll", "dispatch_poll")(
        runtime, runtime.config.connection.connection_id, tick, clock=runtime.clock
    )


def count(runtime, table):
    with runtime.collector.engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_tick_is_durable_before_return_and_duplicate_never_rereads(tmp_path):
    runtime, provider = harness(tmp_path)
    try:
        first = dispatch(runtime, "tick-1")
        assert first.disposition == "active" and provider.calls == 0
        assert runtime.store.receipt_count() == 1
        runtime.run_pending()
        report = dispatch(runtime, "tick-1")
        assert report.disposition == "complete" and provider.calls == 1
        assert report.members[0].decision_event_id and count(runtime, "crm_decisions") == 1
        runtime.run_pending()
        assert provider.calls == 1 and count(runtime, "events") == 4
    finally:
        runtime.close()


def test_queue_has_one_active_one_waiting_and_honest_permanent_coalescing(tmp_path):
    runtime, provider = harness(tmp_path)
    try:
        assert dispatch(runtime, "t1").disposition == "active"
        assert dispatch(runtime, "t2").disposition == "waiting"
        excess = dispatch(runtime, "t3")
        assert excess.disposition == "coalesced" and excess.members == ()
        runtime.run_pending()
        assert provider.calls == 2 and count(runtime, "crm_decisions") == 1
        assert dispatch(runtime, "t3") == excess
        runtime.run_pending()
        assert provider.calls == 2
    finally:
        runtime.close()


def test_observed_a_b_a_has_three_originals_while_unchanged_does_not(tmp_path):
    runtime, provider = harness(tmp_path)
    try:
        for i, employees in enumerate([184, 700, 184, 184]):
            provider.count = employees
            dispatch(runtime, f"t{i}")
            runtime.run_pending()
        assert provider.calls == 4 and count(runtime, "crm_decisions") == 3
        assert dispatch(runtime, "t3").members[0].reason == "scoring_inputs_unchanged"
    finally:
        runtime.close()


def test_missing_scoring_inputs_keeps_capture_and_terminal_refusal(tmp_path):
    runtime, provider = harness(tmp_path)
    try:
        provider.missing = True
        dispatch(runtime, "missing")
        runtime.run_pending()
        result = dispatch(runtime, "missing").members[0]
        assert result.state == "refused" and result.reason == "no_usable_inputs"
        assert result.unit_id and count(runtime, "events") == 0
        assert runtime.store.owner(runtime.config.targets[0]) is None
    finally:
        runtime.close()


def test_restart_drains_durable_queue_without_a_new_request(tmp_path):
    runtime, provider = harness(tmp_path)
    dispatch(runtime, "saved")
    cfg = runtime.config
    runtime.close()
    restarted = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    restarted.start(background=False)
    try:
        restarted.run_pending()
        assert restarted.poll_report("saved").disposition == "complete"
        assert provider.calls == 1 and count(restarted, "crm_decisions") == 1
    finally:
        restarted.close()


def test_restart_after_capture_freeze_never_reads_provider_or_clock_again(tmp_path, monkeypatch):
    runtime, provider = harness(tmp_path)
    poll = importlib.import_module("flight_recorder.adapters.crm.poll")
    original = poll.freeze_submission

    class Crash(BaseException):
        pass

    monkeypatch.setattr(poll, "freeze_submission", lambda *a, **k: (_ for _ in ()).throw(Crash()))
    dispatch(runtime, "saved")
    with pytest.raises(Crash):
        runtime.run_pending()
    assert provider.calls == 1 and count(runtime, "events") == 0
    cfg = runtime.config
    runtime.close()
    monkeypatch.setattr(poll, "freeze_submission", original)

    def forbidden(*a, **k):
        pytest.fail("frozen recovery fetched mutable provider, credentials or clock")

    restarted = feature("runtime", "CRMRuntime")(cfg, client_factory=forbidden, clock=forbidden)
    restarted.start(background=False)
    try:
        restarted.run_pending()
        assert restarted.poll_report("saved").disposition == "complete"
        assert count(restarted, "crm_decisions") == 1 and provider.calls == 1
    finally:
        restarted.close()


def test_process_lock_refuses_second_runtime_and_never_expires(tmp_path):
    runtime, provider = harness(tmp_path)
    rival = feature("runtime", "CRMRuntime")(
        runtime.config, client_factory=lambda: provider, clock=runtime.clock
    )
    try:
        with pytest.raises(CRMRefused, match="runtime_already_owned"):
            rival.start(background=False)
        assert provider.calls == 0
    finally:
        rival.close()
        runtime.close()


@pytest.mark.parametrize("invalid", ["", "x" * 513, None, True])
def test_invalid_tick_and_foreign_connection_have_no_receipt(tmp_path, invalid):
    runtime, provider = harness(tmp_path)
    try:
        with pytest.raises(CRMRefused):
            dispatch(runtime, invalid)
        with pytest.raises(CRMRefused):
            feature("poll", "dispatch_poll")(runtime, "foreign", "tick", clock=runtime.clock)
        assert runtime.store.receipt_count() == 0 and provider.calls == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("forgery", ["decision", "submission", "refusal"])
def test_saved_terminal_must_match_the_actual_complete_frozen_submission(tmp_path, forgery):
    import hashlib
    import sqlite3

    runtime, provider = harness(tmp_path)
    try:
        dispatch(runtime, "done")
        runtime.run_pending()
        with sqlite3.connect(runtime.store.path) as conn:
            old, body = conn.execute("SELECT terminal_id,body FROM crm_poll_terminals").fetchone()
            d = json.loads(body)
            if forgery == "decision":
                d["decision_event_id"] = "fr-crm-decision-v1-" + "b" * 64
            elif forgery == "submission":
                d["submission_hash"] = "b" * 64
            else:
                d["state"], d["reason"] = "refused", "no_usable_inputs"
                d["decision_event_id"] = d["submission_hash"] = None
            encoded = canonical_bytes(d)
            identity = canonical_hash(d)
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='crm_poll_terminals_no_update'"
            ).fetchone()[0]
            conn.execute("DROP TRIGGER crm_poll_terminals_no_update")
            conn.execute(
                "UPDATE crm_poll_terminals SET terminal_id=?,body=?,body_sha256=?",
                (identity, encoded, hashlib.sha256(encoded).hexdigest()),
            )
            conn.execute(
                "UPDATE crm_poll_members SET terminal_id=? WHERE terminal_id=?", (identity, old)
            )
            conn.execute(ddl)
        with pytest.raises(CRMRefused):
            runtime.poll_report("done")
        assert provider.calls == 1 and count(runtime, "crm_decisions") == 1
    finally:
        runtime.close()


def test_mutable_queue_state_cannot_hide_older_unfinished_tick(tmp_path):
    import sqlite3

    runtime, provider = harness(tmp_path)
    try:
        dispatch(runtime, "first")
        dispatch(runtime, "second")
        with sqlite3.connect(runtime.store.path) as conn:
            conn.execute("UPDATE crm_poll_progress SET state='complete' WHERE tick_id='first'")
            conn.execute("UPDATE crm_poll_progress SET state='active' WHERE tick_id='second'")
        with pytest.raises(CRMRefused):
            runtime.run_pending()
        assert provider.calls == 0 and count(runtime, "events") == 0
    finally:
        runtime.close()


def test_admission_refuses_corrupt_completed_progress_before_any_new_receipt(tmp_path):
    import sqlite3

    runtime, provider = harness(tmp_path)
    try:
        dispatch(runtime, "first")
        with sqlite3.connect(runtime.store.path) as conn:
            conn.execute("UPDATE crm_poll_progress SET state='complete' WHERE tick_id='first'")
        with pytest.raises(CRMRefused):
            dispatch(runtime, "second")
        assert runtime.store.receipt_count() == 1 and provider.calls == 0
    finally:
        runtime.close()


def test_lost_read_before_freeze_is_explicitly_recovered_with_new_generation(tmp_path):
    runtime, provider = harness(tmp_path)
    original = provider.read_selected

    class Crash(BaseException):
        pass

    def lost(target):
        original(target)
        raise Crash()

    provider.read_selected = lost
    dispatch(runtime, "lost")
    with pytest.raises(Crash):
        runtime.run_pending()
    old = runtime.store.owner(runtime.config.targets[0])
    cfg = runtime.config
    assert old.generation == 1 and runtime.store.observation_for_owner(old) is None
    runtime.close()
    provider.read_selected = original
    provider.count = 700
    restarted = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    restarted.start(background=False)
    try:
        restarted.run_pending()
        result = restarted.poll_report("lost")
        assert (
            result.disposition == "complete"
            and provider.calls == 2
            and count(restarted, "crm_decisions") == 1
        )
        with restarted.store._read() as conn:
            recovery = json.loads(
                conn.execute("SELECT body FROM crm_poll_recoveries").fetchone()[0]
            )
            assert (
                recovery["generation"] == 1
                and recovery["next_generation"] == 2
                and recovery["reason"] == "read_lost_before_freeze"
            )
            assert conn.execute("SELECT generation FROM crm_target_ownership").fetchone()[0] == 2
    finally:
        restarted.close()


def test_failed_terminal_insert_cannot_release_owner_or_forget_complete_original(tmp_path):
    runtime, provider = harness(tmp_path)
    try:
        dispatch(runtime, "terminal-fault")
        with runtime.store.transaction() as conn:
            conn.execute(
                "CREATE TRIGGER terminal_fault AFTER INSERT ON crm_poll_terminals "
                "BEGIN SELECT RAISE(ABORT,'fictional-terminal-fault'); END"
            )
        runtime.run_pending()
        report = runtime.poll_report("terminal-fault")
        assert report.disposition == "active" and report.members[0].state == "blocked"
        assert runtime.store.owner(runtime.config.targets[0]) is not None
        assert (
            provider.calls == 1
            and count(runtime, "events") == 4
            and count(runtime, "crm_decisions") == 1
        )
        with runtime.store.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM crm_poll_terminals").fetchone()[0] == 0
            conn.execute("DROP TRIGGER terminal_fault")
        runtime.run_pending()
        assert runtime.poll_report("terminal-fault").disposition == "complete"
        assert (
            runtime.store.owner(runtime.config.targets[0]) is None
            and provider.calls == 1
            and count(runtime, "events") == 4
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("stage", range(4))
def test_restart_after_local_collector_return_loss_reuses_saved_read_and_bytes(tmp_path, stage):
    runtime, provider = harness(tmp_path)
    original = runtime.collector.ingest_json
    calls = []

    def lose(body):
        calls.append(body)
        result = original(body)
        if len(calls) == stage + 1:
            raise OSError("fictional-lost-local-return")
        return result

    runtime.collector.ingest_json = lose
    dispatch(runtime, "return-loss")
    runtime.run_pending()
    assert runtime.poll_report("return-loss").disposition == "active" and provider.calls == 1
    assert count(runtime, "events") == stage + 1
    cfg = runtime.config
    runtime.close()

    def forbidden(*args, **kwargs):
        pytest.fail("frozen resume fetched provider, token or clock")

    restarted = feature("runtime", "CRMRuntime")(cfg, client_factory=forbidden, clock=forbidden)
    restarted.start(background=False)
    try:
        resent = []
        send = restarted.collector.ingest_json

        def tracked(body):
            resent.append(body)
            return send(body)

        restarted.collector.ingest_json = tracked
        restarted.run_pending()
        assert restarted.poll_report("return-loss").disposition == "complete"
        assert (
            resent[0] == calls[-1]
            and count(restarted, "events") == 4
            and count(restarted, "crm_decisions") == 1
            and provider.calls == 1
        )
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "table", ["crm_receipts", "crm_poll_ticks", "crm_poll_progress", "crm_poll_members"]
)
def test_admission_fault_is_named_and_rolls_back_the_entire_receipt_and_work(tmp_path, table):
    import traceback

    runtime, provider = harness(tmp_path)
    try:
        with runtime.store.transaction() as conn:
            conn.execute(
                f"CREATE TRIGGER intake_fault AFTER INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT,'fictional-private-fault-detail'); END"
            )
        with pytest.raises(CRMRefused) as caught:
            dispatch(runtime, "intake-fault")
        assert "fictional-private-fault-detail" not in "".join(
            traceback.format_exception(caught.value)
        )
        with runtime.store._read() as conn:
            for name in ["crm_receipts", "crm_poll_ticks", "crm_poll_progress", "crm_poll_members"]:
                assert conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
        assert provider.calls == 0 and count(runtime, "events") == 0
    finally:
        runtime.close()


def _populate_poll_history(runtime, provider):
    original = provider.read_selected
    raised = False

    def fail_once(target):
        nonlocal raised
        if not raised:
            raised = True
            raise CRMRefused("source_read_temporarily_refused")
        return original(target)

    provider.read_selected = fail_once
    dispatch(runtime, "immutable")
    runtime.run_pending()
    runtime.run_pending()
    assert runtime.poll_report("immutable").disposition == "complete"


@pytest.mark.parametrize(
    "table", ["crm_runtime_binding", "crm_poll_ticks", "crm_poll_terminals", "crm_poll_recoveries"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE", "UPSERT"])
def test_all_immutable_poll_facts_refuse_sql_mutations_and_hidden_rowids(
    tmp_path, table, operation
):
    import sqlite3

    runtime, provider = harness(tmp_path)
    try:
        _populate_poll_history(runtime, provider)
        with sqlite3.connect(runtime.store.path) as conn:
            conn.execute("PRAGMA recursive_triggers=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            row = conn.execute(f"SELECT * FROM {table}").fetchone()
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert row is not None
            with pytest.raises(sqlite3.IntegrityError, match="immutable CRM poll fact"):
                if operation == "UPDATE":
                    conn.execute(f"UPDATE {table} SET {columns[0]}={columns[0]}")
                elif operation == "DELETE":
                    conn.execute(f"DELETE FROM {table}")
                elif operation == "REPLACE":
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in row)})", row
                    )
                else:
                    conn.execute(
                        f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)}) "
                        f"ON CONFLICT DO UPDATE SET {columns[0]}=excluded.{columns[0]}",
                        row,
                    )
            assert conn.execute(f"SELECT * FROM {table}").fetchone() == row
            for alias in ["rowid", "_rowid_", "oid"]:
                with pytest.raises(sqlite3.DatabaseError):
                    conn.execute(f"SELECT {alias} FROM {table}")
    finally:
        runtime.close()


@pytest.mark.parametrize("table", ["crm_poll_ticks", "crm_poll_terminals", "crm_poll_recoveries"])
def test_alternate_unique_key_cannot_replace_poll_history_with_a_new_primary_id(tmp_path, table):
    import sqlite3

    runtime, provider = harness(tmp_path)
    try:
        _populate_poll_history(runtime, provider)
        with sqlite3.connect(runtime.store.path) as conn:
            conn.execute("PRAGMA recursive_triggers=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            row = list(conn.execute(f"SELECT * FROM {table}").fetchone())
            old = tuple(row)
            if table == "crm_poll_ticks":
                row[1] = "different-primary-tick"
                body, digest = conn.execute("SELECT body,body_sha256 FROM crm_receipts").fetchone()
                conn.execute(
                    "INSERT INTO crm_receipts VALUES (?,?,?,?)", (row[0], row[1], body, digest)
                )
            else:
                row[0] = "b" * 64
            with pytest.raises(sqlite3.IntegrityError, match="immutable CRM poll fact"):
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in row)})", row
                )
            assert old in conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        runtime.close()


def test_concurrent_duplicate_ticks_have_one_receipt_and_config_conflict_is_a_hold(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    runtime, provider = harness(tmp_path)
    try:
        with ThreadPoolExecutor(4) as workers:
            reports = list(workers.map(lambda _: dispatch(runtime, "same"), range(4)))
        assert (
            all(r == reports[0] for r in reports)
            and runtime.store.receipt_count() == 1
            and provider.calls == 0
        )
        runtime.run_pending()
        d = runtime.config.document
        d["selection_identity"] = "changed-selection"
        cfg = feature("runtime_config", "CRMRuntimeConfig").from_document(d)
        runtime.close()
        changed = feature("runtime", "CRMRuntime")(
            cfg,
            client_factory=lambda: provider,
            clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
        )
        try:
            with pytest.raises(CRMRefused, match="poll_config_conflict"):
                changed.start(background=False)
            assert provider.calls == 1
        finally:
            changed.close()
    finally:
        runtime.close()


def test_shutdown_does_not_release_process_lock_while_read_is_in_flight(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    runtime, provider = harness(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = provider.read_selected

    def paused(target):
        entered.set()
        assert release.wait(10)
        return original(target)

    provider.read_selected = paused
    dispatch(runtime, "shutdown")
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(runtime.run_pending)
        try:
            assert entered.wait(5)
            with pytest.raises(CRMRefused, match="runtime_shutdown_pending"):
                runtime.close()
            rival = feature("runtime", "CRMRuntime")(
                runtime.config, client_factory=lambda: provider, clock=runtime.clock
            )
            try:
                with pytest.raises(CRMRefused, match="runtime_already_owned"):
                    rival.start(background=False)
            finally:
                rival.close()
        finally:
            release.set()
            pending.result(timeout=5)
            runtime.close()
    assert provider.calls == 1


def test_exact_three_record_allowlist_yields_two_originals_and_one_missing_input_trace(tmp_path):
    import copy

    from flight_recorder.adapters.crm.contracts import CRMTarget

    cfg = config(tmp_path)
    d = cfg.document
    records = [f"00100000000000{i}AAA" for i in [1, 2, 3]]
    d["connection"]["record_ids"] = records
    discovery = next(iter(d["discoveries"].values()))
    d["discoveries"] = {}
    names = {record: f"Fictional Company {index}" for index, record in enumerate(records, 1)}
    for record in records:
        target = CRMTarget("salesforce", d["connection"]["expected_instance_id"], "Account", record)
        envelope = copy.deepcopy(discovery)
        envelope["account_ref"] = target.account_ref
        envelope["event_id"] += "-" + record
        envelope["payload"]["name"] = names[record]
        d["discoveries"][record] = envelope
    cfg = feature("runtime_config", "CRMRuntimeConfig").from_document(d)
    feature("cli", "initialize")(cfg)
    provider = Provider()
    original = provider.read_selected
    seen = []

    def selected(target):
        seen.append(target.record_id)
        provider.count = 184 if target.record_id == records[0] else 700
        provider.missing = target.record_id == records[2]
        value = original(target).document
        value["target"] = target.document
        value["properties"]["Id"] = target.record_id
        value["properties"]["Name"] = names[target.record_id]
        value["response_body_sha256"] = canonical_hash(value["properties"])
        return ObservedRead.from_document(value)

    provider.read_selected = selected
    runtime = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    runtime.start(background=False)
    try:
        dispatch(runtime, "three-1")
        runtime.run_pending()
        first = runtime.poll_report("three-1")
        assert first.disposition == "complete" and [m.state for m in first.members] == [
            "complete",
            "complete",
            "refused",
        ]
        assert (
            first.members[2].reason == "no_usable_inputs" and count(runtime, "crm_decisions") == 2
        )
        dispatch(runtime, "three-2")
        runtime.run_pending()
        assert (
            seen == records + records
            and provider.calls == 6
            and count(runtime, "crm_decisions") == 2
        )
        assert all(
            m.reason == "scoring_inputs_unchanged" for m in runtime.poll_report("three-2").members
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("first_entry", ["runner", "direct"])
def test_direct_capture_shares_runner_and_shutdown_exclusion(tmp_path, first_entry):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    runtime, provider = harness(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = provider.read_selected

    def paused(target):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(target)

    provider.read_selected = paused
    dispatch(runtime, "direct-work")
    work = feature("poll", "capture_and_submit")
    with ThreadPoolExecutor(2) as pool:
        first = (
            pool.submit(runtime.run_pending)
            if first_entry == "runner"
            else pool.submit(
                work, runtime, runtime.config.targets[0], "direct-work", clock=runtime.clock
            )
        )
        assert entered.wait(5)
        second = pool.submit(
            work, runtime, runtime.config.targets[0], "direct-work", clock=runtime.clock
        )
        try:
            with pytest.raises(CRMRefused, match="poll_runner_busy"):
                second.result(timeout=2)
            with pytest.raises(CRMRefused, match="runtime_shutdown_pending"):
                runtime.close()
            assert runtime.authority._fd is not None
            from tests.adapters.crm.test_crm_runtime import _other_process

            assert _other_process(runtime.config, tmp_path) == ("runtime_already_owned", False)
        finally:
            release.set()
            try:
                first.result(timeout=10)
            except CRMRefused:
                pass
            runtime.close()
    assert provider.calls == 1
