"""Explicit local initialization and closed runtime configuration preserve retained files."""

import hashlib
import importlib
import sqlite3

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.collector.canonical import canonical_bytes
from tests.adapters.crm.test_poll import config, feature


def hashes(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def test_initialization_refuses_existing_files_without_changing_bytes(tmp_path):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    before = hashes([cfg.domain_path, cfg.operational_path])
    with pytest.raises(CRMRefused, match="crm_init_requires_new_files"):
        feature("cli", "initialize")(cfg)
    assert hashes([cfg.domain_path, cfg.operational_path]) == before


def test_runtime_construction_never_initializes_missing_files(tmp_path):
    cfg = config(tmp_path)
    with pytest.raises(CRMRefused, match="crm_runtime_uninitialized"):
        feature("runtime", "CRMRuntime")(cfg)
    assert not cfg.domain_path.exists() and not cfg.operational_path.exists()


def test_partial_initialization_is_preserved_without_cleanup_or_false_success(
    tmp_path, monkeypatch
):
    cfg = config(tmp_path)
    module = importlib.import_module("flight_recorder.adapters.crm.cli")

    def fail(*args, **kwargs):
        raise OSError("fictional-init-fault")

    monkeypatch.setattr(module, "create_crm_schema", fail)
    with pytest.raises(CRMRefused, match="crm_init_incomplete"):
        module.initialize(cfg)
    assert cfg.domain_path.is_file() and cfg.operational_path.is_file()
    before = hashes([cfg.domain_path, cfg.operational_path])
    with pytest.raises(CRMRefused):
        module.initialize(cfg)
    assert before == hashes([cfg.domain_path, cfg.operational_path])


def test_schema_damage_holds_runtime_and_does_not_repair(tmp_path):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    with sqlite3.connect(cfg.domain_path) as conn:
        conn.execute("DROP TRIGGER events_no_delete")
    before = hashes([cfg.domain_path, cfg.operational_path])
    with pytest.raises(CRMRefused, match="crm_domain_uninitialized"):
        feature("runtime", "CRMRuntime")(cfg)
    assert before == hashes([cfg.domain_path, cfg.operational_path])


@pytest.mark.parametrize("part", ["extra", "origin", "target", "artifact", "selection", "alias"])
def test_closed_config_refuses_unapproved_scope_before_creating_files(tmp_path, part):
    cfg = config(tmp_path)
    d = cfg.document
    if part == "extra":
        d["request_record_ids"] = ["foreign"]
    elif part == "origin":
        d["ingress_origin"] = "http://0.0.0.0:8768"
    elif part == "target":
        d["discoveries"][cfg.connection.record_ids[0]]["account_ref"] = "foreign-account"
    elif part == "artifact":
        d["artifact_registration"]["payload"]["artifact"]["artifact_id"] = "foreign-artifact"
    elif part == "selection":
        d["selection_identity"] = "bad\nselection"
    else:
        d["domain_path"] = d["operational_path"]
    with pytest.raises(CRMRefused):
        feature("runtime_config", "CRMRuntimeConfig").from_document(d)
    assert not cfg.domain_path.exists() and not cfg.operational_path.exists()


def test_private_companion_lock_has_an_independent_immutable_initialization_anchor(tmp_path):
    import json
    import stat

    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    lock = cfg.operational_path.with_name(cfg.operational_path.name + ".runtime-lock")
    assert lock.is_file(), "missing initialized companion process lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    body = lock.read_bytes()
    with sqlite3.connect(cfg.operational_path) as conn:
        row = conn.execute("SELECT singleton,body,body_sha256 FROM crm_runtime_binding").fetchone()
        assert row == (1, body, hashlib.sha256(body).hexdigest())
        for statement in [
            "DELETE FROM crm_runtime_binding",
            "UPDATE crm_runtime_binding SET singleton=singleton",
            "INSERT OR REPLACE INTO crm_runtime_binding SELECT * FROM crm_runtime_binding",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    d = json.loads(body)
    assert (d["operational_device"], d["operational_inode"]) == (
        cfg.operational_path.stat().st_dev,
        cfg.operational_path.stat().st_ino,
    )
    assert (d["lock_device"], d["lock_inode"]) == (lock.stat().st_dev, lock.stat().st_ino)
    before = hashes([lock])
    runtime = feature("runtime", "CRMRuntime")(cfg)
    runtime.start(background=False)
    runtime.close()
    assert hashes([lock]) == before


@pytest.mark.parametrize("damage", ["replacement", "mode", "missing", "symlink"])
def test_lock_damage_never_admits_a_runtime_or_reinitializes_files(tmp_path, damage):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    lock = cfg.operational_path.with_name(cfg.operational_path.name + ".runtime-lock")
    assert lock.is_file(), "missing initialized companion process lock"
    original = lock.read_bytes()
    if damage == "replacement":
        lock.rename(tmp_path / "retained-lock")
        lock.write_bytes(original)
        lock.chmod(0o600)
    elif damage == "mode":
        lock.chmod(0o644)
    elif damage == "missing":
        lock.rename(tmp_path / "retained-lock")
    else:
        lock.rename(tmp_path / "retained-lock")
        lock.symlink_to(tmp_path / "retained-lock")
    before = hashes([cfg.domain_path, cfg.operational_path])
    with pytest.raises(CRMRefused):
        runtime = feature("runtime", "CRMRuntime")(cfg)
        try:
            runtime.start(background=False)
        finally:
            runtime.close()
    assert before == hashes([cfg.domain_path, cfg.operational_path])


def _other_process(cfg, tmp_path):
    import os
    import subprocess
    import sys

    document = tmp_path / "fictional-config.json"
    document.write_bytes(cfg.canonical_bytes)
    marker = tmp_path / "unexpected-provider-read"
    script = """
import sys
from pathlib import Path
from flight_recorder.adapters.crm.runtime_config import load_config
from flight_recorder.adapters.crm.runtime import CRMRuntime
from flight_recorder.adapters.crm.contracts import CRMRefused
runtime = None
def forbidden():
    Path(sys.argv[2]).write_text('unexpected source acquisition')
    raise RuntimeError('unexpected source acquisition')
try:
    runtime = CRMRuntime(load_config(sys.argv[1]), client_factory=forbidden)
    runtime.start(background=False)
    runtime.run_pending()
    print('ADMITTED')
except CRMRefused as exc:
    print(exc.reason)
finally:
    if runtime is not None: runtime.close()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-s", "-c", script, str(document), str(marker)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip(), marker.exists()


def test_independent_process_cannot_overlap_and_can_start_after_orderly_close(tmp_path):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    runtime = feature("runtime", "CRMRuntime")(cfg)
    runtime.start(background=False)
    try:
        result, read = _other_process(cfg, tmp_path)
        assert result == "runtime_already_owned" and not read
    finally:
        runtime.close()
    assert _other_process(cfg, tmp_path) == ("ADMITTED", False)


@pytest.mark.parametrize("forge_contents", [False, True])
def test_second_process_refuses_replaced_lock_while_old_runner_is_inside_get(
    tmp_path, forge_contents
):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tests.adapters.crm.test_poll import dispatch, harness

    runtime, provider = harness(tmp_path)
    entered, release = threading.Event(), threading.Event()
    read_selected = provider.read_selected

    def paused(target):
        entered.set()
        assert release.wait(10)
        return read_selected(target)

    provider.read_selected = paused
    dispatch(runtime, "paused")
    lock = runtime.authority.lock_path
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(runtime.run_pending)
        try:
            assert entered.wait(5)
            with runtime.store._read() as conn:
                before = conn.execute("SELECT * FROM crm_target_ownership").fetchall()
            contents = lock.read_bytes()
            lock.rename(tmp_path / "old-pinned-lock")
            lock.write_bytes(contents)
            lock.chmod(0o600)
            if forge_contents:
                d = json.loads(contents)
                d["lock_device"], d["lock_inode"] = lock.stat().st_dev, lock.stat().st_ino
                lock.write_bytes(canonical_bytes(d))
            result, read = _other_process(runtime.config, tmp_path)
            assert result in ("runtime_lock_invalid", "runtime_lock_identity_mismatch") and not read
            with runtime.store._read() as conn:
                assert conn.execute("SELECT * FROM crm_target_ownership").fetchall() == before
                assert conn.execute("SELECT count(*) FROM crm_poll_recoveries").fetchone()[0] == 0
        finally:
            release.set()
            with pytest.raises(CRMRefused):
                pending.result(timeout=5)
            runtime.close()


def test_crm_init_cli_uses_explicit_config_and_ignores_ambient_database(
    tmp_path, monkeypatch, capsys
):
    from flight_recorder.cli import main

    cfg = config(tmp_path)
    file = tmp_path / "fictional-config.json"
    file.write_bytes(cfg.canonical_bytes)
    unrelated = tmp_path / "unrelated.sqlite"
    monkeypatch.setenv("FLIGHT_RECORDER_DB", str(unrelated))
    assert main(["--db", str(unrelated), "crm-init", "--config", str(file)]) == 0
    assert cfg.domain_path.is_file() and cfg.operational_path.is_file() and not unrelated.exists()
    assert main(["crm-init", "--config", str(file)]) == 1
    assert "crm_init_requires_new_files" in capsys.readouterr().err


def test_crm_serve_cli_has_fixed_loopback_and_no_callback_access_log(tmp_path, monkeypatch):
    import uvicorn

    from flight_recorder.cli import main

    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    file = tmp_path / "fictional-config.json"
    file.write_bytes(cfg.canonical_bytes)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    assert main(["crm-serve", "--config", str(file)]) == 0
    args, options = calls[0]
    assert (
        options["host"] == "localhost"
        and options["port"] == 8768
        and options["access_log"] is False
    )
    assert len(calls) == 1 and args[0].state.crm_runtime._thread is None
    args[0].state.crm_runtime.close()
    args[0].state.engine.dispose()
    with pytest.raises(SystemExit):
        main(["crm-serve", "--config", str(file), "--host", "0.0.0.0"])


def _workflow():
    import json
    from pathlib import Path

    path = Path(__file__).parents[3] / "integrations/salesforce/n8n-poll.json"
    assert path.is_file(), "missing inactive two-node Salesforce polling export"
    return json.loads(path.read_text())


def test_inactive_workflow_has_only_schedule_and_authenticated_local_delivery():
    d = _workflow()
    assert d["active"] is False and len(d["nodes"]) == 2
    schedule, http = d["nodes"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    assert schedule["parameters"] == {
        "rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}
    }
    assert http["type"] == "n8n-nodes-base.httpRequest"
    p = http["parameters"]
    assert p["method"] == "POST" and p["url"] == "http://localhost:8768/api/internal/crm/poll"
    assert "$workflow.id" in p["jsonBody"] and "$execution.id" in p["jsonBody"]
    assert "Date" not in p["jsonBody"] and "random" not in p["jsonBody"]
    assert p["genericAuthType"] == "httpHeaderAuth" and http["credentials"]["httpHeaderAuth"]["id"]
    assert http["retryOnFail"] is True and http["maxTries"] == 3
    assert feature("cli", "validate_n8n_export")(d) is True


@pytest.mark.parametrize(
    "change",
    ["active", "write_node", "extra_node", "external_url", "inline_secret", "unstable_tick"],
)
def test_workflow_validation_refuses_mutable_or_unreviewed_nodes(change):
    d = _workflow()
    if change == "active":
        d["active"] = True
    elif change == "write_node":
        d["nodes"][1]["type"] = "n8n-nodes-base.salesforce"
    elif change == "extra_node":
        d["nodes"].append({"type": "n8n-nodes-base.code"})
    elif change == "external_url":
        d["nodes"][1]["parameters"]["url"] = "https://external.invalid/write"
    elif change == "inline_secret":
        d["nodes"][1]["parameters"]["headerParameters"] = {
            "parameters": [{"name": "Authorization", "value": "Bearer fictional-inline-secret"}]
        }
    else:
        d["nodes"][1]["parameters"]["jsonBody"] = "={{ { tick_id: Date.now() } }}"
    with pytest.raises(CRMRefused):
        feature("cli", "validate_n8n_export")(d)


def test_repeated_process_fence_checks_do_not_accumulate_file_descriptors(tmp_path):
    import gc
    import os

    from tests.adapters.crm.test_poll import harness

    runtime, provider = harness(tmp_path)
    gc.collect()
    before = len(os.listdir("/dev/fd"))
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(40):
            runtime.authority.assert_held()
        after = len(os.listdir("/dev/fd"))
        assert after <= before + 2, "process fence leaked SQLite connections"
    finally:
        if was_enabled:
            gc.enable()
        runtime.close()
        gc.collect()


@pytest.mark.parametrize("which", ["domain", "operational"])
def test_runtime_never_recreates_a_file_removed_after_the_initial_path_check(
    tmp_path, monkeypatch, which
):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    module = importlib.import_module("flight_recorder.adapters.crm.runtime")
    if which == "domain":
        original = module.qualify_domain

        def raced(engine):
            cfg.domain_path.unlink()
            return original(engine)

        monkeypatch.setattr(module, "qualify_domain", raced)
        removed = cfg.domain_path
    else:
        original = module.CRMStore

        def raced(path, *args, **kwargs):
            cfg.operational_path.unlink()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(module, "CRMStore", raced)
        removed = cfg.operational_path
    with pytest.raises(CRMRefused):
        module.CRMRuntime(cfg)
    assert not removed.exists(), "runtime recreated a missing database during qualification"


@pytest.mark.parametrize("saved_before_start", [False, True])
def test_owned_background_runner_wakes_and_resumes_without_another_tick(
    tmp_path, saved_before_start
):
    import threading
    from datetime import UTC, datetime

    from tests.adapters.crm.test_poll import Provider, dispatch, harness

    if saved_before_start:
        previous, provider = harness(tmp_path)
        dispatch(previous, "wake")
        cfg = previous.config
        previous.close()
    else:
        cfg = config(tmp_path)
        feature("cli", "initialize")(cfg)
        provider = Provider()
    runtime = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    done = threading.Event()
    settle = runtime.queue.settle

    def observed(*args, **kwargs):
        result = settle(*args, **kwargs)
        done.set()
        return result

    runtime.queue.settle = observed
    runtime.start(background=True)
    try:
        if not saved_before_start:
            dispatch(runtime, "wake")
        assert (
            done.wait(5)
            and runtime.poll_report("wake").disposition == "complete"
            and provider.calls == 1
        )
    finally:
        runtime.close()
    assert not runtime._thread.is_alive()


@pytest.mark.parametrize("format_version", [1, 2, 3, 4])
def test_existing_only_store_open_preserves_each_exact_format_and_missing_path(
    tmp_path, format_version
):
    from flight_recorder.adapters.crm.store import CRMStore

    path = tmp_path / "operational.sqlite"
    with pytest.raises(CRMRefused):
        CRMStore(path, format_version=format_version, create=False)
    assert not path.exists()
    store = CRMStore(path, format_version=format_version)
    store.close()
    before = hashes([path])
    store = CRMStore(path, format_version=format_version, create=False)
    store.close()
    assert before == hashes([path])


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", []])
def test_store_creation_option_requires_an_actual_boolean_without_creating_files(tmp_path, invalid):
    from flight_recorder.adapters.crm.store import CRMStore

    path = tmp_path / "operational.sqlite"
    with pytest.raises(CRMRefused):
        CRMStore(path, create=invalid)
    assert not path.exists()


def test_init_refuses_foreign_operational_file_appearing_after_preflight(tmp_path, monkeypatch):
    import flight_recorder.adapters.crm.cli as cli
    from flight_recorder.adapters.crm.store import CRMStore

    cfg = config(tmp_path)
    retained = {}
    original = cli.CRMStore

    def race(path, **kwargs):
        foreign = CRMStore(path, format_version=4)
        foreign.record_receipt("foreign-owner", "foreign-receipt", b"preserved fictional bytes")
        foreign.close()
        retained.update(hashes([path]))
        return original(path, **kwargs)

    monkeypatch.setattr(cli, "CRMStore", race)
    with pytest.raises(CRMRefused, match="crm_init_incomplete"):
        cli.initialize(cfg)
    assert hashes([cfg.operational_path]) == retained
    assert not cfg.operational_path.with_name(cfg.operational_path.name + ".runtime-lock").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("direction", ["domain", "operational", "ingress", "lock"])
def test_init_rejects_overlapping_sqlite_file_families_before_creation(tmp_path, suffix, direction):
    from pathlib import Path

    cfg = config(tmp_path)
    d = cfg.document
    if direction == "domain":
        d["domain_path"] = d["operational_path"] + suffix
    elif direction == "operational":
        d["operational_path"] = d["domain_path"] + suffix
    elif direction == "ingress":
        d["domain_path"] = d["ingress_secret_file"].removesuffix(suffix)
        # Set the DB base so its actual companion is the existing ingress file.
        old = Path(d["ingress_secret_file"])
        new = old.with_name(old.name + suffix)
        old.rename(new)
        d["ingress_secret_file"] = str(new)
        d["domain_path"] = str(old)
    else:
        d["domain_path"] = d["operational_path"] + ".runtime-lock" + suffix
    before = hashes([Path(d["ingress_secret_file"])])
    with pytest.raises(CRMRefused):
        feature("runtime_config", "CRMRuntimeConfig").from_document(d)
    assert hashes([Path(d["ingress_secret_file"])]) == before
    assert not Path(d["domain_path"]).exists()
    assert not Path(d["operational_path"]).exists()


@pytest.mark.parametrize("reserved", [".lock", "token", "client-secret"])
def test_private_reference_paths_cannot_overlap_database_family(tmp_path, reserved):
    from flight_recorder.collector.canonical import canonical_hash

    cfg = config(tmp_path)
    d = cfg.document
    name = {
        "token": canonical_hash({"connection_id": cfg.connection.connection_id}) + ".json",
        "client-secret": cfg.connection.credential_reference + ".client-secret",
        ".lock": ".lock",
    }[reserved]
    d["domain_path"] = str(cfg.domain_path.parent / "private" / name)
    with pytest.raises(CRMRefused):
        feature("runtime_config", "CRMRuntimeConfig").from_document(d)
    assert not cfg.domain_path.exists() and not cfg.operational_path.exists()


@pytest.mark.parametrize("fault", ["remove", "replace"])
def test_domain_init_refuses_removed_or_replaced_reserved_file(tmp_path, monkeypatch, fault):
    from pathlib import Path

    import flight_recorder.adapters.crm.cli as cli

    cfg = config(tmp_path)
    original = cli.make_engine
    retained = {}

    def race(path):
        Path(path).unlink()
        if fault == "replace":
            Path(path).write_bytes(b"foreign file must stay unchanged")
            retained.update(hashes([Path(path)]))
        return original(path)

    monkeypatch.setattr(cli, "make_engine", race)
    with pytest.raises(CRMRefused, match="crm_init_incomplete"):
        cli.initialize(cfg)
    assert hashes([cfg.domain_path]) == retained


@pytest.mark.parametrize("value", [1, None, "true"])
def test_store_exclusive_creation_requires_exact_boolean(tmp_path, value):
    import inspect

    from flight_recorder.adapters.crm.store import CRMStore

    assert "exclusive" in inspect.signature(CRMStore).parameters, "exclusive initialization missing"
    with pytest.raises(CRMRefused):
        CRMStore(tmp_path / "new.sqlite", exclusive=value)
    assert not (tmp_path / "new.sqlite").exists()


def test_store_exclusive_init_does_not_change_existing_or_default_behavior(tmp_path):
    import inspect

    from flight_recorder.adapters.crm.store import CRMStore

    assert "exclusive" in inspect.signature(CRMStore).parameters, "exclusive initialization missing"
    for version in range(1, 5):
        path = tmp_path / f"op-{version}.sqlite"
        CRMStore(path, format_version=version, exclusive=True).close()
        before = hashes([path])
        with pytest.raises(CRMRefused):
            CRMStore(path, format_version=version, exclusive=True)
        assert hashes([path]) == before
        CRMStore(path, format_version=version).close()
        CRMStore(path, format_version=version, create=False).close()
    with pytest.raises(CRMRefused):
        CRMStore(tmp_path / "absent.sqlite", exclusive=True, create=False)
    assert not (tmp_path / "absent.sqlite").exists()


@pytest.mark.parametrize("boundary", ["first_schema_open", "after_schema"])
def test_exclusive_operational_inode_stays_pinned_through_final_binding(
    tmp_path, monkeypatch, boundary
):
    import os

    import flight_recorder.adapters.crm.cli as cli
    import flight_recorder.adapters.crm.store as stores
    from flight_recorder.adapters.crm.store import CRMStore

    cfg = config(tmp_path)
    foreign_path = tmp_path / "foreign.sqlite"
    foreign = CRMStore(foreign_path, format_version=4)
    foreign.record_receipt("retained-owner", "receipt", b"untouched foreign bytes")
    foreign.close()
    before = foreign_path.read_bytes()
    if boundary == "first_schema_open":
        original = stores.sqlite3.connect
        replaced = False

        def race(database, *args, **kwargs):
            nonlocal replaced
            if not replaced and str(cfg.operational_path) in str(database):
                replaced = True
                os.replace(foreign_path, cfg.operational_path)
            return original(database, *args, **kwargs)

        monkeypatch.setattr(stores.sqlite3, "connect", race)
    else:
        from flight_recorder.adapters.crm import runtime

        original = runtime.qualify_domain

        def race(engine):
            original(engine)
            os.replace(foreign_path, cfg.operational_path)

        monkeypatch.setattr(runtime, "qualify_domain", race)
    with pytest.raises(CRMRefused, match="crm_init_incomplete"):
        cli.initialize(cfg)
    assert cfg.operational_path.read_bytes() == before
    assert not cfg.operational_path.with_name(cfg.operational_path.name + ".runtime-lock").exists()


def test_config_refuses_token_temporary_file_namespace(tmp_path):
    cfg = config(tmp_path)
    d = cfg.document
    d["domain_path"] = str(cfg.domain_path.parent / "private" / ("." + "a" * 32 + ".tmp"))
    with pytest.raises(CRMRefused):
        feature("runtime_config", "CRMRuntimeConfig").from_document(d)
    assert not cfg.domain_path.exists() and not cfg.operational_path.exists()
