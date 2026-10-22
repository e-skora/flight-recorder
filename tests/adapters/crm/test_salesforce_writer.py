"""Durable original-only writer exercised through injected clients and a real Collector."""

import copy
import importlib
import importlib.util
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from flight_recorder.adapters.crm.contracts import ConfirmedConnection, CRMRefused
from flight_recorder.adapters.crm.mapping import OUTPUTS
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.crm_operations import request_digest
from flight_recorder.collector.schema import format_utc
from tests.adapters.crm.test_poll import Provider, config, dispatch


def feature(module="writer"):
    name = "flight_recorder.adapters.crm." + module
    assert importlib.util.find_spec(name) is not None, "guarded Salesforce writer missing"
    return importlib.import_module(name)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 4, 17, 10, 10, tzinfo=UTC)

    def __call__(self):
        now = self.value
        self.value += timedelta(milliseconds=1)
        return now


class WriteProvider(Provider):
    observation_origin = "injected_transport"

    def __init__(self, cfg, clock):
        super().__init__()
        self.cfg, self.clock = cfg, clock
        self.sent = []
        self.output_reads = 0
        self.outputs = {}
        self.mode = "normal"
        self.generation = 1

    def verify_connection(self):
        self.verifications += 1
        c = self.cfg.connection
        return ConfirmedConnection(
            c.connection_id,
            c.provider,
            c.expected_instance_id,
            c.expected_user_id,
            c.api_version,
            self.clock(),
            self.observation_origin,
        )

    def write_generation(self):
        return self.generation

    def send_once(self, intent, admission):
        admission.consume(intent, self, credential_generation=self.generation)
        d = intent.document
        self.sent.append(copy.deepcopy(d))
        started = self.clock()
        if self.mode != "412":
            self.outputs = copy.deepcopy(d["intended"])
        if self.mode == "lost":
            raise TimeoutError("fictional lost response")
        status = int(self.mode) if self.mode.isdecimal() else 204
        return feature("writer_contracts").ProviderResponse.from_document(
            dict(
                request_hash=request_digest(d),
                started_at=format_utc(started),
                finished_at=format_utc(self.clock()),
                status=status,
                complete=True,
                body_sha256=canonical_hash("fictional response"),
            ),
            observation_origin=self.observation_origin,
        )

    def read_outputs(self, intent):
        self.output_reads += 1
        started = self.clock()
        values = copy.deepcopy(self.outputs)
        if self.mode == "partial":
            values.pop("fr_op_id", None)
        return feature("writer_contracts").OutputObservation.from_document(
            dict(
                target=intent.document["target"],
                fields=sorted(OUTPUTS),
                values=values,
                started_at=format_utc(started),
                finished_at=format_utc(self.clock()),
                body_sha256=canonical_hash(values),
            ),
            observation_origin=self.observation_origin,
        )


def setup(tmp_path):
    feature()
    cfg = config(tmp_path)
    clock = Clock()
    provider = WriteProvider(cfg, clock)
    feature("cli").initialize(cfg, writer_capable=True)
    runtime = feature("runtime").CRMRuntime(
        cfg, writer_capable=True, client_factory=lambda: provider, clock=clock
    )
    runtime.start(background=False)
    dispatch(runtime, "original")
    runtime.run_pending()
    decision = runtime.poll_report("original").members[0].decision_event_id
    assert decision is not None
    return runtime, provider, decision


def policy(runtime, *, generation=1, enabled=True, **updates):
    cfg = runtime.config
    d = dict(
        enabled=enabled,
        connection_id=cfg.connection.connection_id,
        expected_instance_id=cfg.connection.expected_instance_id,
        target_keys=tuple(sorted(t.key for t in cfg.targets)),
        output_fields=tuple(sorted(OUTPUTS.values())),
        mapping_hash=cfg.connection.mapping_hash,
        selection_identity=cfg.document["selection_identity"],
        generation=generation,
    )
    d.update(updates)
    return feature("writer_contracts").CRMWritePolicy(**d)


def enable(runtime):
    return feature().set_write_policy(runtime, policy(runtime), clock=runtime.clock)


def prepare(runtime, decision):
    return feature().prepare_write(runtime, decision, clock=runtime.clock)


def send(runtime, intent):
    return feature().send_write_once(runtime, intent.operation_id, clock=runtime.clock)


def test_default_off_has_no_credential_or_provider_work(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        before = (provider.calls, provider.verifications)
        with pytest.raises(CRMRefused, match="policy_disabled"):
            prepare(runtime, decision)
        assert (provider.calls, provider.verifications) == before
        assert provider.sent == [] and provider.output_reads == 0
    finally:
        runtime.close()


def test_one_original_is_durable_idempotent_and_writes_exact_tuple_once(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        assert prepare(runtime, decision).canonical_bytes == intent.canonical_bytes
        result = send(runtime, intent)
        assert result.state == "observed_applied_at_t" and not result.held
        assert len(provider.sent) == 1
        assert provider.sent[0]["source_precondition"] == '"fictional-read-1"'
        assert provider.sent[0]["intended"]["fr_decision_ref"] == decision
        assert set(provider.sent[0]["intended"]) == set(OUTPUTS)
        assert send(runtime, intent) == result
        assert len(provider.sent) == 1
        with runtime.collector.engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM crm_operation_observations")).scalar_one()
                == 4
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("lost", "unknown"),
        ("401", "unknown"),
        ("429", "unknown"),
        ("500", "unknown"),
        ("412", "refused_no_effect"),
        ("partial", "awaiting_observation"),
    ],
)
def test_ambiguous_results_hold_and_never_resend_even_after_matching_read(tmp_path, mode, expected):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        provider.mode = mode
        result = send(runtime, intent)
        assert result.state == expected
        assert result.held == (expected != "refused_no_effect")
        if expected == "unknown":
            provider.mode = "normal"
            observed = feature().observe_write(runtime, intent.operation_id, clock=runtime.clock)
            assert observed.state == "unknown" and observed.held
        send(runtime, intent)
        assert len(provider.sent) == 1
    finally:
        runtime.close()


def test_restart_after_unknown_never_calls_client_to_send(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    enable(runtime)
    intent = prepare(runtime, decision)
    provider.mode = "lost"
    assert send(runtime, intent).state == "unknown"
    runtime.close()

    def forbidden():
        pytest.fail("restart attempted to construct a provider client")

    resumed = feature("runtime").CRMRuntime(
        cfg, writer_capable=True, client_factory=forbidden, clock=clock
    )
    resumed.start(background=False)
    try:
        assert send(resumed, intent).state == "unknown"
        assert len(provider.sent) == 1
    finally:
        resumed.close()


def test_new_capture_blocks_unsent_original_without_patch(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        provider.count = 700
        dispatch(runtime, "changed")
        runtime.run_pending()
        result = send(runtime, intent)
        assert result.state == "refused" and not result.held
        assert provider.sent == []
    finally:
        runtime.close()


def test_disabling_policy_never_clears_a_lost_request(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        provider.mode = "lost"
        assert send(runtime, intent).state == "unknown"
        feature().set_write_policy(
            runtime, policy(runtime, enabled=False, generation=2), clock=runtime.clock
        )
        assert send(runtime, intent).state == "unknown"
        assert len(provider.sent) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "change",
    ["foreign_connection", "foreign_org", "extra_field", "selection", "mapping", "bool_generation"],
)
def test_policy_binding_refuses_before_provider_access(tmp_path, change):
    runtime, provider, _ = setup(tmp_path)
    try:
        before = (provider.calls, provider.verifications)
        updates = {
            "foreign_connection": {"connection_id": "foreign"},
            "foreign_org": {"expected_instance_id": "00D000000000002EAA"},
            "extra_field": {"output_fields": tuple(sorted((*OUTPUTS.values(), "Name")))},
            "selection": {"selection_identity": "foreign"},
            "mapping": {"mapping_hash": "f" * 64},
            "bool_generation": {"generation": True},
        }[change]
        with pytest.raises((CRMRefused, ValueError)):
            feature().set_write_policy(runtime, policy(runtime, **updates), clock=runtime.clock)
        assert (provider.calls, provider.verifications) == before
        assert provider.sent == []
    finally:
        runtime.close()


def test_writer5_and_default4_share_polling_without_implicit_migration(tmp_path):
    (tmp_path / "writer").mkdir()
    (tmp_path / "reader").mkdir()
    runtime, _, _ = setup(tmp_path / "writer")
    try:
        assert runtime.store._format_version == 5
        assert runtime.poll_report("original").disposition == "complete"
    finally:
        runtime.close()
    from tests.adapters.crm.test_poll import harness

    old, provider = harness(tmp_path / "reader")
    try:
        assert old.store._format_version == 4
        dispatch(old, "old")
        old.run_pending()
        assert provider.calls == 1
        with old.store._read() as conn:
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'crm_write_%'"
            ).fetchall()
    finally:
        old.close()


@pytest.mark.parametrize("phase", ["prepared", "send_started", "response", "output_observed"])
@pytest.mark.parametrize("after", [False, True])
def test_exact_outbox_recovers_each_collector_crash_without_a_second_send(
    tmp_path, monkeypatch, phase, after
):
    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    enable(runtime)
    ingest = runtime.collector.ingest_json
    fired = []

    def fail_once(body):
        document = json.loads(body)
        if document["payload"].get("phase") == phase and not fired:
            fired.append(body)
            if after:
                ingest(body)
            raise RuntimeError("fictional crash at Collector boundary")
        return ingest(body)

    intent = None if phase == "prepared" else prepare(runtime, decision)
    monkeypatch.setattr(runtime.collector, "ingest_json", fail_once)
    with pytest.raises(RuntimeError, match="fictional crash"):
        prepare(runtime, decision) if intent is None else send(runtime, intent)
    with runtime.store._read() as conn:
        from flight_recorder.adapters.crm.writer_contracts import CRMWriteIntent

        frozen = CRMWriteIntent(conn.execute("SELECT body FROM crm_write_intents").fetchone()[0])
    runtime.close()
    resumed = feature("runtime").CRMRuntime(
        cfg, writer_capable=True, client_factory=lambda: provider, clock=clock
    )
    resumed.start(background=False)
    try:
        assert prepare(resumed, decision).canonical_bytes == frozen.canonical_bytes
        result = send(resumed, frozen)
        assert len(provider.sent) == (0 if phase == "send_started" else 1)
        expected = (
            "unknown"
            if phase == "send_started"
            else "awaiting_observation"
            if phase == "response"
            else "observed_applied_at_t"
        )
        assert result.state == expected
        if phase == "response":
            assert (
                feature().observe_write(resumed, frozen.operation_id, clock=clock).state
                == "observed_applied_at_t"
            )
        with resumed.store._read() as conn:
            assert (
                conn.execute(
                    "SELECT body FROM crm_write_outbox WHERE event_id=?",
                    (json.loads(fired[0])["event_id"],),
                ).fetchone()[0]
                == fired[0]
            )
    finally:
        resumed.close()


@pytest.mark.parametrize("fault", ["duplicate", "bad_hash", "bad_id", "bad_status"])
def test_marker_ack_never_creates_grant_unless_fresh_created_and_exact(
    tmp_path, monkeypatch, fault
):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        ingest = runtime.collector.ingest_json

        def broken_ack(body):
            result = ingest(body)
            if json.loads(body)["payload"].get("phase") == "send_started":
                d = dict(
                    event_id=result.event_id,
                    canonical_hash=result.canonical_hash,
                    status=result.status,
                    http_status=result.http_status,
                )
                if fault == "duplicate":
                    d.update(status="duplicate", http_status=200)
                elif fault == "bad_hash":
                    d["canonical_hash"] = "f" * 64
                elif fault == "bad_id":
                    d["event_id"] = "foreign"
                else:
                    d["http_status"] = True
                return SimpleNamespace(**d)
            return result

        monkeypatch.setattr(runtime.collector, "ingest_json", broken_ack)
        if fault == "duplicate":
            assert send(runtime, intent).state == "unknown"
        else:
            with pytest.raises(CRMRefused, match="invalid_write_collector_ack"):
                send(runtime, intent)
        monkeypatch.setattr(runtime.collector, "ingest_json", ingest)
        assert send(runtime, intent).state == "unknown"
        assert provider.sent == []
    finally:
        runtime.close()


def test_known_unadmitted_preparation_preserves_reason_without_consuming_revision(
    tmp_path, monkeypatch
):
    from flight_recorder.collector.errors import RejectedError

    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        ingest = runtime.collector.ingest_json

        def reject_prepared(body):
            if json.loads(body)["payload"].get("phase") == "prepared":
                raise RejectedError(dict(status="rejected", reason="crm_operation_newer_capture"))
            return ingest(body)

        monkeypatch.setattr(runtime.collector, "ingest_json", reject_prepared)
        abandoned = prepare(runtime, decision)
        assert send(runtime, abandoned).state == "refused"
        with runtime.store._read() as conn:
            local = json.loads(
                conn.execute("SELECT body FROM crm_write_local_refusals").fetchone()[0]
            )
            assert local["collector_reason"] == "crm_operation_newer_capture"
        assert provider.sent == []
        monkeypatch.setattr(runtime.collector, "ingest_json", ingest)
        provider.count = 700
        dispatch(runtime, "new")
        runtime.run_pending()
        new_decision = runtime.poll_report("new").members[0].decision_event_id
        fresh = prepare(runtime, new_decision)
        assert abandoned.document["revision"] == fresh.document["revision"] == 1
        assert send(runtime, fresh).state == "observed_applied_at_t"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "terminal_progress",
        "deleted_progress",
        "released_owner",
        "foreign_generation",
        "orphan_attempt",
        "missing_outbox",
        "changed_intent",
    ],
)
def test_immutable_unknown_cannot_be_hidden_from_any_entry(tmp_path, corruption):
    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    enable(runtime)
    intent = prepare(runtime, decision)
    provider.mode = "lost"
    assert send(runtime, intent).state == "unknown"
    with sqlite3.connect(cfg.operational_path) as conn:
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        if corruption == "terminal_progress":
            conn.execute(
                "UPDATE crm_write_progress SET state='observed_applied_at_t',"
                "attempted=0,generation=NULL"
            )
        elif corruption == "deleted_progress":
            conn.execute("DELETE FROM crm_write_progress")
        elif corruption == "released_owner":
            conn.execute("UPDATE crm_target_ownership SET state='released'")
        elif corruption == "foreign_generation":
            conn.execute("UPDATE crm_target_ownership SET generation=generation+1")
        elif corruption == "orphan_attempt":
            conn.execute("DROP TRIGGER crm_write_attempts_no_update")
            conn.execute("UPDATE crm_write_attempts SET operation_id=?", ("f" * 64,))
            conn.execute(triggers["crm_write_attempts_no_update"])
        elif corruption == "missing_outbox":
            conn.execute("DROP TRIGGER crm_write_outbox_no_delete")
            conn.execute("DELETE FROM crm_write_outbox WHERE observation_sequence=2")
            conn.execute(triggers["crm_write_outbox_no_delete"])
        else:
            conn.execute("DROP TRIGGER crm_write_intents_no_update")
            conn.execute("UPDATE crm_write_intents SET body=?", (b"{}",))
            conn.execute(triggers["crm_write_intents_no_update"])
    before = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
    for action in [
        lambda: send(runtime, intent),
        lambda: prepare(runtime, decision),
        lambda: feature().observe_write(runtime, intent.operation_id, clock=clock),
        lambda: feature().set_write_policy(runtime, policy(runtime, generation=2), clock=clock),
        lambda: dispatch(runtime, "blocked"),
        lambda: runtime.run_pending(),
        lambda: feature("poll").capture_and_submit(
            runtime, cfg.targets[0], "original", clock=clock
        ),
    ]:
        with pytest.raises(CRMRefused):
            action()
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == before
    runtime.close()
    resumed = feature("runtime").CRMRuntime(
        cfg,
        writer_capable=True,
        client_factory=lambda: pytest.fail("provider constructed on damaged restart"),
        clock=clock,
    )
    try:
        with pytest.raises(CRMRefused):
            resumed.start(background=False)
    finally:
        resumed.close()


def test_auth_failure_is_before_marker_and_can_only_refuse_unsent(tmp_path, monkeypatch):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        monkeypatch.setattr(
            provider,
            "verify_connection",
            lambda: (_ for _ in ()).throw(CRMRefused("token_expired")),
        )
        assert send(runtime, intent).state == "refused"
        with runtime.store._read() as conn:
            assert conn.execute("SELECT count(*) FROM crm_write_attempts").fetchone() == (0,)
        assert provider.sent == []
    finally:
        runtime.close()


def test_no_sql_transaction_over_auth_patch_or_output_get(tmp_path, monkeypatch):
    runtime, provider, decision = setup(tmp_path)
    active = []
    original = runtime.store.transaction

    @contextmanager
    def transaction():
        with original() as conn:
            active.append(conn)
            try:
                yield conn
            finally:
                active.remove(conn)

    monkeypatch.setattr(runtime.store, "transaction", transaction)
    for name in ["verify_connection", "send_once", "read_outputs"]:
        method = getattr(provider, name)

        def guarded(*args, method=method, **kwargs):
            assert not active
            with sqlite3.connect(runtime.config.domain_path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            with sqlite3.connect(runtime.config.operational_path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return method(*args, **kwargs)

        monkeypatch.setattr(provider, name, guarded)
    try:
        enable(runtime)
        assert send(runtime, prepare(runtime, decision)).state == "observed_applied_at_t"
    finally:
        runtime.close()


def test_parallel_send_and_shutdown_cannot_create_another_grant(tmp_path, monkeypatch):
    runtime, provider, decision = setup(tmp_path)
    enable(runtime)
    intent = prepare(runtime, decision)
    entered, release = threading.Event(), threading.Event()
    method = provider.send_once
    results = []

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return method(*args)

    monkeypatch.setattr(provider, "send_once", blocked)

    def worker():
        try:
            results.append(send(runtime, intent))
        except BaseException as exc:
            results.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(CRMRefused, match="writer_busy"):
            send(runtime, intent)
        with pytest.raises(CRMRefused, match="runtime_shutdown_pending"):
            runtime.close()
    finally:
        release.set()
        thread.join(20)
        runtime.close()
    assert not thread.is_alive()
    assert len(results) == 1 and not isinstance(results[0], BaseException)
    assert len(provider.sent) == 1


def test_complete_history_audit_includes_terminal_facts_and_process_rival(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    rival = feature("runtime").CRMRuntime(
        runtime.config,
        writer_capable=True,
        client_factory=lambda: pytest.fail("rival provider"),
        clock=runtime.clock,
    )
    try:
        with pytest.raises(CRMRefused):
            rival.start(background=False)
        enable(runtime)
        assert send(runtime, prepare(runtime, decision)).state == "observed_applied_at_t"
        with runtime.store.transaction() as conn:
            conn.execute("DELETE FROM crm_write_progress")
        before = provider.calls
        with pytest.raises(CRMRefused):
            dispatch(runtime, "damaged-completed")
        assert provider.calls == before
    finally:
        rival.close()
        runtime.close()


def test_fresh_process_cannot_take_writer_lock_even_after_lost_request(tmp_path):
    import os
    import subprocess
    import sys

    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        provider.mode = "lost"
        assert send(runtime, intent).state == "unknown"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-s",
                "-c",
                """
import sys
from pathlib import Path
from flight_recorder.adapters.crm.runtime import ProcessAuthority
from flight_recorder.adapters.crm.contracts import CRMRefused
authority = ProcessAuthority(Path(sys.argv[1]))
try:
    authority.acquire()
    raise AssertionError('rival acquired writer process')
except CRMRefused as exc:
    assert exc.reason == 'runtime_already_owned'
    print(exc.reason)
finally:
    authority.close()
""",
                str(runtime.config.operational_path),
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "runtime_already_owned"
        assert len(provider.sent) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("precondition", [None, 'W/"weak-read"'])
def test_missing_or_weak_captured_etag_stays_replay_only(tmp_path, monkeypatch, precondition):
    cfg, clock = config(tmp_path), Clock()
    provider = WriteProvider(cfg, clock)
    read_selected = provider.read_selected

    def without_strong_tag(target):
        from flight_recorder.adapters.crm.contracts import ObservedRead

        d = read_selected(target).document
        d["precondition"] = precondition
        return ObservedRead.from_document(d)

    monkeypatch.setattr(provider, "read_selected", without_strong_tag)
    feature("cli").initialize(cfg, writer_capable=True)
    runtime = feature("runtime").CRMRuntime(
        cfg, writer_capable=True, client_factory=lambda: provider, clock=clock
    )
    runtime.start(background=False)
    try:
        dispatch(runtime, "original")
        runtime.run_pending()
        decision = runtime.poll_report("original").members[0].decision_event_id
        enable(runtime)
        before = provider.verifications
        with pytest.raises(CRMRefused, match="write_precondition_unavailable"):
            prepare(runtime, decision)
        assert provider.verifications == before and provider.sent == []
    finally:
        runtime.close()


def test_counterfactual_or_operator_score_is_not_an_original_write_api(tmp_path):
    runtime, provider, _ = setup(tmp_path)
    try:
        enable(runtime)
        with pytest.raises(CRMRefused):
            prepare(runtime, "counterfactual-score-999")
        with pytest.raises(TypeError):
            feature().prepare_write(runtime, "counterfactual", score=999, clock=runtime.clock)
        assert provider.sent == []
    finally:
        runtime.close()


def test_domain_path_cannot_be_swapped_for_a_copy_of_history(tmp_path):
    import shutil

    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    enable(runtime)
    intent = prepare(runtime, decision)
    provider.mode = "lost"
    assert send(runtime, intent).state == "unknown"
    runtime.close()
    copy = tmp_path / "other-domain.sqlite"
    shutil.copyfile(cfg.domain_path, copy)
    d = cfg.document
    d["domain_path"] = str(copy)
    changed = feature("runtime_config").CRMRuntimeConfig.from_document(d)
    resumed = None
    try:
        with pytest.raises(CRMRefused, match="poll_config_conflict"):
            resumed = feature("runtime").CRMRuntime(
                changed,
                writer_capable=True,
                client_factory=lambda: pytest.fail("provider accessed for wrong domain"),
                clock=clock,
            )
            resumed.start(background=False)
    finally:
        if resumed is not None:
            resumed.close()


@pytest.mark.parametrize("boundary", ["before_response_persist", "after_terminal_insert"])
def test_crash_after_http_and_during_release_cannot_invent_completion(
    tmp_path, monkeypatch, boundary
):
    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    enable(runtime)
    intent = prepare(runtime, decision)
    api = feature()
    append, put = runtime._writer_store.append, api.put

    def interrupted_append(conn, document):
        if document["payload"]["phase"] == "response":
            raise RuntimeError("fictional crash before response commit")
        return append(conn, document)

    def interrupted_put(conn, table, *args, **kwargs):
        result = put(conn, table, *args, **kwargs)
        if table == "crm_write_terminals":
            raise RuntimeError("fictional crash during release")
        return result

    with monkeypatch.context() as patch:
        if boundary == "before_response_persist":
            patch.setattr(runtime._writer_store, "append", interrupted_append)
        else:
            patch.setattr(api, "put", interrupted_put)
        with pytest.raises(RuntimeError, match="fictional crash"):
            send(runtime, intent)
    runtime.close()
    resumed = feature("runtime").CRMRuntime(
        cfg, writer_capable=True, client_factory=lambda: provider, clock=clock
    )
    resumed.start(background=False)
    try:
        result = send(resumed, intent)
        assert len(provider.sent) == 1
        if boundary == "before_response_persist":
            assert result.state == "unknown" and result.held
            assert (
                feature().observe_write(resumed, intent.operation_id, clock=clock).state
                == "unknown"
            )
        else:
            assert result.state == "observed_applied_at_t" and not result.held
    finally:
        resumed.close()


def test_newer_capture_between_local_freeze_and_domain_admission_has_no_write_revision_gap(
    tmp_path, monkeypatch
):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        ingest = runtime.collector.ingest_json
        raced = []

        def race_domain_admission(body):
            doc = json.loads(body)
            if doc["payload"].get("phase") == "prepared" and not raced:
                raced.append(True)
                # Fault injection models a competing Collector capture at this cross-store boundary.
                provider.count = 700
                dispatch(runtime, "competing-capture")
                feature("poll")._capture_and_submit_owned(
                    runtime, runtime.config.targets[0], "competing-capture", clock=runtime.clock
                )
            return ingest(body)

        monkeypatch.setattr(runtime.collector, "ingest_json", race_domain_admission)
        stale = prepare(runtime, decision)
        assert send(runtime, stale).state == "refused" and provider.sent == []
        new_decision = runtime.poll_report("competing-capture").members[0].decision_event_id
        fresh = prepare(runtime, new_decision)
        assert fresh.document["revision"] == 1
        assert send(runtime, fresh).state == "observed_applied_at_t"
    finally:
        runtime.close()


def test_output_feedback_does_not_create_a_new_decision_or_write(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        assert send(runtime, intent).state == "observed_applied_at_t"
        dispatch(runtime, "output-only")
        runtime.run_pending()
        assert runtime.poll_report("output-only").members[0].state == "scoring_inputs_unchanged"
        with runtime.store._read() as conn:
            assert conn.execute("SELECT count(*) FROM crm_write_intents").fetchone() == (1,)
        assert len(provider.sent) == 1
    finally:
        runtime.close()


def test_412_requires_a_new_original_and_keeps_each_captured_etag(tmp_path):
    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        stale = prepare(runtime, decision)
        provider.mode = "412"
        assert send(runtime, stale).state == "refused_no_effect"
        assert prepare(runtime, decision) == stale
        assert send(runtime, stale).state == "refused_no_effect"
        provider.count, provider.mode = 700, "normal"
        dispatch(runtime, "changed-after-conflict")
        runtime.run_pending()
        next_decision = runtime.poll_report("changed-after-conflict").members[0].decision_event_id
        fresh = prepare(runtime, next_decision)
        assert fresh.document["revision"] == 2
        assert send(runtime, fresh).state == "observed_applied_at_t"
        assert [d["source_precondition"] for d in provider.sent] == [
            '"fictional-read-1"',
            '"fictional-read-2"',
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize("fault", ["identity", "origin", "time", "generation"])
def test_malformed_identity_confirmation_cannot_reach_send_marker(tmp_path, monkeypatch, fault):
    from dataclasses import replace

    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        intent = prepare(runtime, decision)
        verify = provider.verify_connection

        def wrong_confirmation():
            confirmed = verify()
            if fault == "identity":
                return replace(confirmed, instance_id="00D000000000002EAA")
            if fault == "origin":
                return replace(confirmed, observation_origin="provider_http")
            if fault == "time":
                return replace(confirmed, confirmed_at=datetime(2026, 4, 17))
            return confirmed

        monkeypatch.setattr(provider, "verify_connection", wrong_confirmation)
        if fault == "generation":
            provider.generation = True
        assert send(runtime, intent).state == "refused"
        assert provider.sent == []
        with runtime.store._read() as conn:
            assert conn.execute("SELECT count(*) FROM crm_write_attempts").fetchone() == (0,)
    finally:
        runtime.close()
