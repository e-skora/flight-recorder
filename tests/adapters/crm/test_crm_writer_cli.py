"""Writer-capable file/app modes are explicit and local status never contacts a provider."""

import json

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.runtime import CRMRuntime
from flight_recorder.cli import main
from flight_recorder.crm_app import create_crm_app
from tests.adapters.crm.test_poll import config
from tests.adapters.crm.test_salesforce_writer import feature, setup


def config_file(cfg, tmp_path):
    path = tmp_path / "runtime.json"
    path.write_bytes(cfg.canonical_bytes)
    return path


@pytest.mark.parametrize("command", ["crm-write-status", "crm-writer-init", "crm-writer-serve"])
@pytest.mark.parametrize("global_db", [False, True])
@pytest.mark.parametrize("malformed", ["unknown_option", "extra_value", "missing_value"])
def test_writer_argument_refusal_is_named_and_never_echoes_private_paths(
    command, global_db, malformed, monkeypatch, capsys
):
    marker = "/private/fictional-credential-directory-DO-NOT-ECHO"
    prefix = ["--db", marker] if global_db else []
    suffix = {
        "unknown_option": ["--config", marker, "--token", marker],
        "extra_value": ["--config", marker, marker],
        "missing_value": ["--config"],
    }[malformed]
    monkeypatch.setattr(
        feature("cli"), "command", lambda *args: pytest.fail("bad arguments reached runtime")
    )
    with pytest.raises(SystemExit) as refused:
        main([*prefix, command, *suffix])
    assert refused.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert marker not in output.err
    assert output.err == "crm: refused: invalid_crm_arguments\n"


def test_writer_init_is_explicit_and_refuses_retained_files(tmp_path, capsys):
    cfg = config(tmp_path)
    path = config_file(cfg, tmp_path)
    assert main(["crm-writer-init", "--config", str(path)]) == 0
    runtime = CRMRuntime(
        cfg,
        writer_capable=True,
        client_factory=lambda: pytest.fail("initialization accessed provider"),
    )
    runtime.start(background=False)
    runtime.close()
    before = (cfg.domain_path.read_bytes(), cfg.operational_path.read_bytes())
    assert main(["crm-writer-init", "--config", str(path)]) == 1
    assert (cfg.domain_path.read_bytes(), cfg.operational_path.read_bytes()) == before
    assert "crm_init_requires_new_files" in capsys.readouterr().err


def test_status_command_starts_without_tokens_or_provider_and_does_not_enable(
    tmp_path, monkeypatch, capsys
):
    cfg = config(tmp_path)
    feature("cli").initialize(cfg, writer_capable=True)
    path = config_file(cfg, tmp_path)
    monkeypatch.setattr(CRMRuntime, "client", lambda _: pytest.fail("status accessed provider"))
    assert main(["crm-write-status", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["policy_generation"] == 0 and output["write_mode"] == "disabled"
    assert output["atomic_source_guard"] is False
    assert str(cfg.document["credential_directory"]) not in json.dumps(output)


@pytest.mark.parametrize("mode", [True, 1, "true"])
def test_app_requires_an_explicit_matching_strict_writer_mode(tmp_path, mode):
    runtime, _, _ = setup(tmp_path)
    runtime.close()
    reopened = CRMRuntime(runtime.config, writer_capable=True)
    try:
        if mode is True:
            with pytest.raises(CRMRefused, match="invalid_crm_runtime"):
                create_crm_app(runtime.config, runtime=reopened)
            app = create_crm_app(
                runtime.config, runtime=reopened, writer_capable=True, background=False
            )
            assert app.state.crm_runtime.writer_capable
        else:
            with pytest.raises(CRMRefused):
                create_crm_app(runtime.config, runtime=reopened, writer_capable=mode)
    finally:
        reopened.close()


def test_status_cli_catches_corrupt_domain_without_exposing_exception_data(
    tmp_path, monkeypatch, capsys
):
    import sqlite3

    from tests.acceptance.test_crm_writer_presentation import history

    runtime, _, _, _ = history(tmp_path)
    cfg = runtime.config
    runtime.close()
    with sqlite3.connect(cfg.domain_path) as conn:
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_operation_observations_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER crm_operation_observations_no_update")
        conn.execute("UPDATE crm_operation_observations SET state='unknown'")
        conn.execute(trigger)
    monkeypatch.setattr(
        CRMRuntime, "client", lambda _: pytest.fail("damaged status accessed provider")
    )
    assert main(["crm-write-status", "--config", str(config_file(cfg, tmp_path))]) == 1
    assert "write_status_history_integrity" in capsys.readouterr().err


def test_writer_serve_uses_explicit_mode_and_one_local_worker(tmp_path, monkeypatch):
    import uvicorn

    cfg = config(tmp_path)
    feature("cli").initialize(cfg, writer_capable=True)
    calls = []
    monkeypatch.setattr(
        CRMRuntime, "client", lambda _: pytest.fail("serve constructed provider early")
    )
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    try:
        assert main(["crm-writer-serve", "--config", str(config_file(cfg, tmp_path))]) == 0
        app, kwargs = calls[0]
        assert app.state.crm_runtime.writer_capable
        assert kwargs == dict(host="localhost", port=8768, workers=1, access_log=False)
    finally:
        for app, _ in calls:
            app.state.crm_runtime.close()
