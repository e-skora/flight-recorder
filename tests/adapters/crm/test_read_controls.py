"""A first read does not require or grant Account write permission."""

from pathlib import Path

import httpx
import pytest

from flight_recorder.adapters.crm.cli import initialize
from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.readiness import ReadinessManifest, validate_readiness
from flight_recorder.adapters.crm.runtime import CRMRuntime
from tests.adapters.crm.test_poll import config
from tests.adapters.crm.test_readiness import NOW, declaration


def test_read_only_declaration_allows_read_only_user(tmp_path):
    cfg = config(tmp_path)
    document = declaration(cfg, "read_only")
    document["permissions"]["account_update"] = False
    document["permissions"]["write_fields"] = []

    report = validate_readiness(ReadinessManifest.from_document(document), cfg, now=NOW)

    assert report.ready_for_read is True
    assert report.declarations_complete is True
    assert report.ready_for_write is False
    assert report.requires_runtime_intent_check is False


@pytest.mark.parametrize("stage", ["local_prepare", "qualification_send", "ordinary_send"])
def test_empty_write_permissions_never_qualify_other_stages(tmp_path, stage):
    cfg = config(tmp_path)
    document = declaration(cfg, stage)
    document["permissions"]["account_update"] = False
    document["permissions"]["write_fields"] = []

    report = validate_readiness(ReadinessManifest.from_document(document), cfg, now=NOW)

    assert report.declarations_complete is False
    assert report.ready_for_write is False
    assert "permissions_unqualified" in report.reasons


@pytest.mark.parametrize(
    "stage", ["read_only", "local_prepare", "qualification_send", "ordinary_send"]
)
def test_extra_write_destination_is_refused_at_every_stage(tmp_path, stage):
    cfg = config(tmp_path)
    document = declaration(cfg, stage)
    document["permissions"]["write_fields"].append("Description")

    report = validate_readiness(ReadinessManifest.from_document(document), cfg, now=NOW)

    assert report.ready_for_read is False
    assert report.ready_for_write is False
    assert report.declarations_complete is False
    assert "permissions_unqualified" in report.reasons


def test_read_only_does_not_treat_empty_fields_as_removed_object_permission(tmp_path):
    cfg = config(tmp_path)
    document = declaration(cfg, "read_only")
    document["permissions"]["account_update"] = True
    document["permissions"]["write_fields"] = []

    report = validate_readiness(ReadinessManifest.from_document(document), cfg, now=NOW)

    assert report.ready_for_read is False
    assert "permissions_unqualified" in report.reasons


def test_legacy_runtime_refuses_live_transport_before_construction(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    initialize(cfg)
    constructed = []

    def external_transport(**kwargs):
        constructed.append(True)
        return httpx.MockTransport(lambda request: httpx.Response(403))

    monkeypatch.setattr(httpx, "HTTPTransport", external_transport)
    runtime = CRMRuntime(cfg, clock=lambda: NOW)
    reason = None
    try:
        runtime.start(background=False)
        try:
            runtime.client()
        except CRMRefused as exc:
            reason = exc.reason
        assert constructed == [], "legacy runtime constructed a live transport without authority"
        assert reason == "live_read_authority_required"
    finally:
        runtime.close()


def authority_document(cfg, action="oauth_connect"):
    from tests.adapters.crm.test_readiness import EARLIER, LATER

    return dict(
        format_version=1,
        action=action,
        config_sha256=cfg.binding,
        expected_instance_id=cfg.connection.expected_instance_id,
        expected_user_id=cfg.connection.expected_user_id,
        record_ids=list(cfg.connection.record_ids),
        issued_at=EARLIER,
        expires_at=LATER,
        owner_request_sha256="c" * 64,
        tick_id="fictional-tick-1" if action == "capture_once" else None,
        metadata_receipt_sha256="d" * 64 if action == "capture_once" else None,
    )


@pytest.mark.parametrize("action", ["oauth_connect", "metadata_probe", "capture_once"])
def test_read_authority_preserves_immutable_canonical_grant(tmp_path, action):
    from dataclasses import FrozenInstanceError

    from flight_recorder.adapters.crm.read_control import ReadAuthority

    document = authority_document(config(tmp_path), action)
    authority = ReadAuthority.from_document(document)
    assert authority.document == document
    document["record_ids"].clear()
    assert len(authority.document["record_ids"]) == 1
    with pytest.raises(FrozenInstanceError):
        authority.canonical_bytes = b"{}"
    assert "c" * 64 not in repr(authority)


@pytest.mark.parametrize("action", ["ordinary_send", "PATCH", "fictional-secret-action", "", None])
def test_read_authority_never_accepts_or_echoes_unknown_action(tmp_path, action):
    from flight_recorder.adapters.crm.read_control import ReadAuthority

    document = authority_document(config(tmp_path))
    document["action"] = action
    with pytest.raises(CRMRefused, match="^invalid_read_authority$"):
        ReadAuthority.from_document(document)


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": True},
        {"format_version": 2},
        {"config_sha256": "bad"},
        {"owner_request_sha256": ""},
        {"expected_user_id": "005-not-canonical"},
        {"issued_at": "2026-04-17T11:00:00Z"},
        {"expires_at": "2026-04-17T10:00:00.000000Z"},
        {"tick_id": "an-unrequested-tick"},
        {"metadata_receipt_sha256": "d" * 64},
        {"access_token": "fictional-secret-extra"},
    ],
)
def test_read_authority_rejects_malformed_or_extra_grant_fields(tmp_path, change):
    from flight_recorder.adapters.crm.read_control import ReadAuthority

    document = authority_document(config(tmp_path))
    document.update(change)
    with pytest.raises(CRMRefused, match="^invalid_read_authority$"):
        ReadAuthority.from_document(document)


def test_read_authority_requires_exactly_one_canonical_target(tmp_path):
    from flight_recorder.adapters.crm.read_control import ReadAuthority

    document = authority_document(config(tmp_path))
    for records in ([], document["record_ids"] * 2, ["001-not-canonical"]):
        document["record_ids"] = records
        with pytest.raises(CRMRefused, match="^invalid_read_authority$"):
            ReadAuthority.from_document(document)


def test_capture_authority_requires_exact_tick_and_metadata_hash(tmp_path):
    from flight_recorder.adapters.crm.read_control import ReadAuthority

    cfg = config(tmp_path)
    for change in (
        {"tick_id": None},
        {"tick_id": ""},
        {"tick_id": "secret\nextra"},
        {"metadata_receipt_sha256": None},
        {"metadata_receipt_sha256": "bad"},
    ):
        document = authority_document(cfg, "capture_once")
        document.update(change)
        with pytest.raises(CRMRefused, match="^invalid_read_authority$"):
            ReadAuthority.from_document(document)


def test_docker_workflow_uses_only_fixed_private_transport_and_host():
    from flight_recorder.adapters.crm.cli import n8n_poll_workflow, validate_n8n_export
    from flight_recorder.collector.canonical import canonical_hash

    default = n8n_poll_workflow()
    assert (
        canonical_hash(default)
        == "14d128a70b28adf3cf83232c1de5712a4eef7b92ef77e319c1dcfc5f64d9b558"
    )
    docker = n8n_poll_workflow(mode="docker_host")
    request = docker["nodes"][1]["parameters"]
    assert request["url"] == "http://host.docker.internal:8768/api/internal/crm/poll"
    assert request["headerParameters"] == {
        "parameters": [{"name": "Host", "value": "localhost:8768"}]
    }
    assert request["sendHeaders"] is True
    assert docker["active"] is False
    assert (
        canonical_hash(docker) == "2aead7fedd0c46629b258b320e217968eab89b8c3fd629520c110801cb756f15"
    )
    assert validate_n8n_export(default) is True
    with pytest.raises(CRMRefused, match="^unreviewed_n8n_workflow$"):
        validate_n8n_export(docker)
    assert n8n_poll_workflow() == default


@pytest.mark.parametrize("mode", ["https://evil.invalid", "host_network", "", None, [], {}])
def test_workflow_transport_is_a_closed_enum(mode):
    from flight_recorder.adapters.crm.cli import n8n_poll_workflow

    with pytest.raises(CRMRefused, match="^invalid_n8n_mode$"):
        n8n_poll_workflow(mode=mode)


@pytest.mark.parametrize(
    "mode,host", [("local_loopback", "localhost"), ("docker_host", "host.docker.internal")]
)
def test_network_qualification_template_is_manual_and_has_no_poll(mode, host):
    import json

    from flight_recorder.adapters.crm.cli import n8n_qualification_workflow

    document = n8n_qualification_workflow(mode=mode)
    assert document["active"] is False
    assert [node["type"] for node in document["nodes"]] == [
        "n8n-nodes-base.manualTrigger",
        "n8n-nodes-base.httpRequest",
    ]
    request = document["nodes"][1]["parameters"]
    assert request["method"] == "POST"
    assert request["url"] == f"http://{host}:8768/api/internal/crm/network-check"
    assert "/poll" not in repr(document)
    assert json.loads(request["jsonBody"]) == dict(
        format_version=1, connection_id="REVIEWED_CONNECTION_ID", probe_id="0" * 64
    )
    assert request["options"]["redirect"]["redirect"]["followRedirects"] is False
    if mode == "docker_host":
        assert request["headerParameters"] == {
            "parameters": [{"name": "Host", "value": "localhost:8768"}]
        }


def n8n_binding_document(cfg, mode="docker_host"):
    from flight_recorder.adapters.crm.cli import n8n_poll_workflow, n8n_qualification_workflow
    from flight_recorder.collector.canonical import canonical_hash
    from tests.adapters.crm.test_readiness import EARLIER, LATER

    return dict(
        format_version=1,
        type="n8n_binding",
        owner_request_sha256="a" * 64,
        config_sha256=cfg.binding,
        connection_id=cfg.connection.connection_id,
        mode=mode,
        credential_id="fictionalN8nCredential01",
        credential_name="Flight Recorder local ingress",
        credential_type="httpHeaderAuth",
        issued_at=EARLIER,
        expires_at=LATER,
        poll_template_sha256=canonical_hash(n8n_poll_workflow(mode=mode)),
        qualification_template_sha256=canonical_hash(n8n_qualification_workflow(mode=mode)),
        probe_id="e" * 64,
    )


@pytest.mark.parametrize("mode", ["local_loopback", "docker_host"])
@pytest.mark.parametrize("kind", ["poll", "qualification"])
def test_bound_n8n_export_uses_exact_config_and_credential_references(tmp_path, mode, kind):
    import json

    from flight_recorder.adapters.crm.cli import (
        N8nBinding,
        bound_n8n_workflow,
        validate_bound_n8n_export,
    )

    cfg = config(tmp_path)
    document = n8n_binding_document(cfg, mode)
    binding = N8nBinding.from_document(document, cfg)
    workflow = bound_n8n_workflow(cfg, binding, kind=kind)
    node = workflow["nodes"][1]
    assert node["credentials"]["httpHeaderAuth"] == {
        "id": document["credential_id"],
        "name": "Flight Recorder local ingress",
    }
    body = node["parameters"]["jsonBody"]
    assert "REVIEWED_" not in repr(workflow)
    if kind == "qualification":
        assert json.loads(body) == dict(
            format_version=1, connection_id=cfg.connection.connection_id, probe_id="e" * 64
        )
    else:
        assert json.dumps(cfg.connection.connection_id, ensure_ascii=True) in body
        assert "$workflow.id + ':' + $execution.id" in body
    assert validate_bound_n8n_export(workflow, cfg, binding, kind=kind) is True
    workflow["nodes"][1]["parameters"]["url"] = "https://evil.invalid"
    with pytest.raises(CRMRefused, match="^unreviewed_n8n_workflow$"):
        validate_bound_n8n_export(workflow, cfg, binding, kind=kind)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "host_network"},
        {"config_sha256": "f" * 64},
        {"connection_id": "another-connection"},
        {"credential_id": "REVIEWED_LOCAL_INGRESS_CREDENTIAL"},
        {"credential_id": "Bearer fictional-secret"},
        {"credential_id": "={{ $env.SECRET }}"},
        {"credential_id": "../secret"},
        {"credential_name": "another credential"},
        {"credential_type": "salesforceOAuth2Api"},
        {"poll_template_sha256": "f" * 64},
        {"qualification_template_sha256": "f" * 64},
        {"probe_id": "bad"},
        {"probe_id": "0" * 64},
        {"access_token": "fictional-secret-extra"},
    ],
)
def test_n8n_binding_refuses_unreviewed_substitutions(tmp_path, change):
    from flight_recorder.adapters.crm.cli import N8nBinding

    cfg = config(tmp_path)
    document = n8n_binding_document(cfg)
    document.update(change)
    with pytest.raises(CRMRefused, match="^invalid_n8n_binding$"):
        N8nBinding.from_document(document, cfg)


def test_bound_n8n_expression_keeps_hostile_config_id_inside_json_literal(tmp_path):
    import json

    from flight_recorder.adapters.crm.cli import N8nBinding, bound_n8n_workflow
    from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig

    document = config(tmp_path).document
    hostile = 'fictional" }, evil: $env.SECRET, extra: "\\\u2028'
    document["connection"]["connection_id"] = hostile
    cfg = CRMRuntimeConfig.from_document(document)
    binding = N8nBinding.from_document(n8n_binding_document(cfg), cfg)
    workflow = bound_n8n_workflow(cfg, binding, kind="poll")
    body = workflow["nodes"][1]["parameters"]["jsonBody"]
    prefix, suffix = "={{ { connection_id: ", ", tick_id: $workflow.id + ':' + $execution.id } }}"
    assert body.startswith(prefix) and body.endswith(suffix)
    assert json.loads(body[len(prefix) : -len(suffix)]) == hostile
    assert "\u2028" not in body
    assert "fictional-secret-extra" not in repr(binding)


def test_readiness_can_check_bound_export_without_granting_execution(tmp_path):
    from flight_recorder.adapters.crm.cli import N8nBinding, bound_n8n_workflow
    from flight_recorder.collector.canonical import canonical_hash

    cfg = config(tmp_path)
    binding = N8nBinding.from_document(n8n_binding_document(cfg), cfg)
    document = declaration(cfg, "read_only")
    document["permissions"].update(account_update=False, write_fields=[])
    document["n8n"]["workflow_sha256"] = canonical_hash(bound_n8n_workflow(cfg, binding))
    manifest = ReadinessManifest.from_document(document)
    report = validate_readiness(manifest, cfg, now=NOW, n8n_binding=binding)
    assert report.ready_for_read is True
    assert report.ready_for_write is False
    assert report.evidence_notice.startswith("Checks supplied declarations only.")
    without_binding = validate_readiness(manifest, cfg, now=NOW)
    assert without_binding.ready_for_read is False
    assert "n8n_runtime_network_unqualified" in without_binding.reasons


@pytest.mark.parametrize("context", [True, {"qualified": True}])
def test_readiness_refuses_untyped_n8n_binding_context(tmp_path, context):
    cfg = config(tmp_path)
    manifest = ReadinessManifest.from_document(declaration(cfg, "read_only"))
    report = validate_readiness(manifest, cfg, now=NOW, n8n_binding=context)
    assert report.ready_for_read is False
    assert "n8n_runtime_network_unqualified" in report.reasons


def test_readiness_refuses_expired_bound_export_context(tmp_path):
    from flight_recorder.adapters.crm.cli import N8nBinding, bound_n8n_workflow
    from flight_recorder.collector.canonical import canonical_hash

    cfg = config(tmp_path)
    binding_document = n8n_binding_document(cfg)
    binding_document["expires_at"] = NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    binding = N8nBinding.from_document(binding_document, cfg)
    document = declaration(cfg, "read_only")
    document["n8n"]["workflow_sha256"] = canonical_hash(bound_n8n_workflow(cfg, binding))
    report = validate_readiness(
        ReadinessManifest.from_document(document), cfg, now=NOW, n8n_binding=binding
    )
    assert report.ready_for_read is False
    assert "n8n_runtime_network_unqualified" in report.reasons


def private_control_file(tmp_path, name="authority.json", document=None):
    from flight_recorder.collector.canonical import canonical_bytes

    directory = tmp_path / "read-controls"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_bytes(canonical_bytes(document or {"format_version": 1}))
    path.chmod(0o600)
    return path


def test_pinned_read_file_never_adopts_replacement_or_same_byte_rewrite(tmp_path):
    from flight_recorder.adapters.crm.read_control import PinnedReadFile

    path = private_control_file(tmp_path)
    pinned = PinnedReadFile(path)
    assert pinned.body == path.read_bytes(), "operator files must be loaded and pinned"
    assert pinned.check() == pinned.body
    path.write_bytes(pinned.body)
    with pytest.raises(CRMRefused, match="read_control_file_changed"):
        pinned.check()
    pinned = PinnedReadFile(path)
    replacement = path.with_name("new.json")
    replacement.write_bytes(pinned.body)
    replacement.chmod(0o600)
    replacement.replace(path)
    with pytest.raises(CRMRefused, match="read_control_file_changed"):
        pinned.check()


@pytest.mark.parametrize("mutation", ["public_file", "public_parent", "symlink", "hardlink"])
def test_read_file_security_checks_before_loading(tmp_path, mutation):
    from flight_recorder.adapters.crm.read_control import PinnedReadFile

    path = private_control_file(tmp_path)
    if mutation == "public_file":
        path.chmod(0o644)
    elif mutation == "public_parent":
        path.parent.chmod(0o755)
    elif mutation == "symlink":
        link = path.with_name("link.json")
        link.symlink_to(path)
        path = link
    else:
        path.with_name("hard.json").hardlink_to(path)
    with pytest.raises(CRMRefused):
        PinnedReadFile(path)


def test_read_evidence_is_content_addressed_immutable_private_and_pinned(tmp_path):
    from flight_recorder.adapters.crm.read_control import ReadEvidenceStore
    from flight_recorder.collector.canonical import canonical_bytes, canonical_hash

    directory = tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    store = ReadEvidenceStore(directory)
    doc = {"format_version": 1, "type": "test_observation", "value": "fictional"}
    digest = store.save(doc)
    assert digest == canonical_hash(doc), "receipt name must bind complete canonical bytes"
    path = directory / (digest + ".json")
    assert path.read_bytes() == canonical_bytes(doc)
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.save(doc) == digest
    assert store.load(digest) == doc
    path.write_bytes(canonical_bytes(doc))
    with pytest.raises(CRMRefused, match="read_control_file_changed"):
        store.load(digest)


def test_evidence_collision_never_overwrites_existing_file(tmp_path):
    from flight_recorder.adapters.crm.read_control import ReadEvidenceStore
    from flight_recorder.collector.canonical import canonical_hash

    directory = tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    doc = {"format_version": 1}
    path = directory / (canonical_hash(doc) + ".json")
    path.write_bytes(b'{"other":true}')
    path.chmod(0o600)
    with pytest.raises(CRMRefused, match="read_evidence_mismatch"):
        ReadEvidenceStore(directory).save(doc)
    assert path.read_bytes() == b'{"other":true}'


def test_evidence_directory_replacement_refuses(tmp_path):
    from flight_recorder.adapters.crm.read_control import ReadEvidenceStore

    directory = tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    store = ReadEvidenceStore(directory)
    directory.rename(tmp_path / "old-evidence")
    directory.mkdir(mode=0o700)
    with pytest.raises(CRMRefused, match="read_control_file_changed"):
        store.save({"format_version": 1})


def n8n_transport_document(cfg, store):
    """Fictional private artifacts for local gate validation, never live proof."""
    from flight_recorder.adapters.crm.cli import N8nBinding, bound_n8n_workflow
    from flight_recorder.collector.canonical import canonical_hash

    binding = N8nBinding.from_document(n8n_binding_document(cfg, "local_loopback"), cfg)
    b = binding.document
    bsha = store.save(b)
    body = dict(
        format_version=1, connection_id=cfg.connection.connection_id, probe_id=b["probe_id"]
    )
    result = dict(
        state="network_check_received",
        config_sha256=cfg.binding,
        probe_sha256=canonical_hash(b["probe_id"]),
        body_sha256=canonical_hash(body),
    )
    obs = dict(
        format_version=1,
        type="n8n_network_observation",
        qualification_sha256="c" * 64,
        binding_sha256=bsha,
        owner_request_sha256=b["owner_request_sha256"],
        mode=b["mode"],
        config_sha256=cfg.binding,
        method="POST",
        path="/api/internal/crm/network-check",
        received_host="localhost:8768",
        logical_ingress="http://localhost:8768",
        observed_at="2026-04-17T11:30:00.000000Z",
        probe_sha256=result["probe_sha256"],
        body_sha256=result["body_sha256"],
        result=result,
    )
    qualifier = bound_n8n_workflow(cfg, binding, kind="qualification")
    url = qualifier["nodes"][1]["parameters"]["url"]
    dummy = bound_n8n_workflow(cfg, binding, kind="qualification")
    dummy["nodes"][1]["parameters"]["url"] = url.replace(":8768/", ":18768/")
    dummy["nodes"][1]["credentials"]["httpHeaderAuth"]["id"] = "fictionalDummyCredential"
    receipt = dict(
        format_version=1,
        type="n8n_transport_qualification",
        config_sha256=cfg.binding,
        owner_request_sha256=b["owner_request_sha256"],
        binding_sha256=bsha,
        runtime=dict(id="fictional-n8n", version="fictional-1", container=None, image=None),
        poll_template_sha256=b["poll_template_sha256"],
        qualification_template_sha256=b["qualification_template_sha256"],
        bound_poll_sha256=canonical_hash(bound_n8n_workflow(cfg, binding)),
        bound_qualification_sha256=canonical_hash(qualifier),
        credential_id=b["credential_id"],
        connection_id=b["connection_id"],
        mode=b["mode"],
        transport_url=url,
        logical_host="localhost:8768",
        ingress_origin="http://localhost:8768",
        poll_workflow_id=None,
        dummy=dict(
            credential_id="fictionalDummyCredential",
            workflow_id="fictional-dummy",
            execution_id="execution-1",
            workflow_sha256=canonical_hash(dummy),
            request_url=dummy["nodes"][1]["parameters"]["url"],
            method="POST",
            received_host="localhost:18768",
            body_sha256=result["body_sha256"],
            probe_sha256=result["probe_sha256"],
            result=result,
            executed_at="2026-04-17T11:20:00.000000Z",
            observed_at="2026-04-17T11:20:00.000000Z",
            reviewed_at="2026-04-17T11:40:00.000000Z",
        ),
        actual=dict(
            observation_sha256=store.save(obs),
            workflow_id="fictional-actual",
            execution_id="execution-2",
            workflow_sha256=canonical_hash(qualifier),
            executed_at=obs["observed_at"],
            reviewed_at="2026-04-17T11:40:00.000000Z",
        ),
        observed_at=obs["observed_at"],
        reviewed_at="2026-04-17T11:40:00.000000Z",
    )
    # Both qualifier modes explicitly override Host to localhost:8768, including dummy.
    receipt["dummy"]["received_host"] = (
        "localhost:8768" if b["mode"] == "docker_host" else "localhost:18768"
    )
    return binding, receipt


def read_gate_setup(
    tmp_path, *, action="oauth_connect", missing_metadata=True, clock=lambda: NOW, change=None
):
    from flight_recorder.adapters.crm.read_control import ReadEvidenceStore, ReadSessionGate

    cfg = config(tmp_path)
    directory = tmp_path / "gate-evidence"
    directory.mkdir(mode=0o700)
    store = ReadEvidenceStore(directory)
    binding, transport = n8n_transport_document(cfg, store)
    manifest = declaration(cfg, "read_only")
    manifest["permissions"].update(account_update=False, write_fields=[])
    manifest["n8n"]["workflow_sha256"] = transport["bound_poll_sha256"]
    manifest["evidence"]["n8n"].update(
        sha256=store.save(transport),
        origin="operator_reviewed",
        observed_at=transport["observed_at"],
        reviewed_at=transport["reviewed_at"],
    )
    if missing_metadata:
        manifest["advertised_api_versions"] = []
        manifest["field_schema"] = []
        manifest["permissions"] = dict(
            user_id=None, account_read=None, account_update=None, read_fields=[], write_fields=[]
        )
        for key in ("api_versions", "field_schema", "permissions"):
            manifest["evidence"][key] = None
    authority = authority_document(cfg, action)
    if change:
        change(cfg, manifest, authority, transport, store)
    paths = [
        private_control_file(tmp_path, name, doc)
        for name, doc in [
            ("config.json", cfg.document),
            ("readiness.json", manifest),
            ("authority.json", authority),
        ]
    ]
    gate = ReadSessionGate(*paths, directory, clock=clock)
    return cfg, manifest, authority, paths, directory, gate


def test_bootstrap_accepts_only_named_future_metadata_gaps(tmp_path):
    cfg, manifest, authority, paths, directory, gate = read_gate_setup(tmp_path)
    context = gate.authorize_oauth()
    assert context is not None, "fresh exact bootstrap prerequisites must admit the OAuth handoff"
    assert context.action == "oauth_connect"
    with pytest.raises(CRMRefused):
        gate.authorize_metadata()
    with pytest.raises(CRMRefused):
        gate.authorize_capture("tick", cfg.targets[0])


@pytest.mark.parametrize(
    "change",
    [
        lambda c, m, a, t, s: m["n8n"].update(reachable=False),
        lambda c, m, a, t, s: m["evidence"].update(n8n=None),
        lambda c, m, a, t, s: m["environment"].update(production=True),
        lambda c, m, a, t, s: m["authority"].update(stage="ordinary_send"),
        lambda c, m, a, t, s: m.update(initial_write_policy_enabled=True),
        lambda c, m, a, t, s: a.update(config_sha256="f" * 64),
        lambda c, m, a, t, s: m["evidence"]["identity"].update(origin="injected_transport"),
        lambda c, m, a, t, s: m["field_schema"].append(
            dict(name="Name", kind="bad", length=1, precision=None, scale=None, picklist_values=[])
        ),
        lambda c, m, a, t, s: m.update(advertised_api_versions=["1.0"]),
    ],
)
def test_bootstrap_rejects_missing_n8n_and_supplied_bad_metadata(tmp_path, change):
    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, change=change)
    with pytest.raises(CRMRefused):
        gate.authorize_oauth()


def test_config_replacement_after_start_refuses(tmp_path):
    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path)
    gate.authorize_oauth()
    paths[0].write_bytes(paths[0].read_bytes())
    with pytest.raises(CRMRefused, match="read_control_file_changed"):
        gate.authorize_oauth()


def test_bootstrap_expiry_rechecked_on_same_context(tmp_path):
    from datetime import timedelta

    now = [NOW]
    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, clock=lambda: now[0])
    context = gate.authorize_oauth()
    assert context is not None, "read context must bind per-attempt authority"
    now[0] += timedelta(hours=1)
    with pytest.raises(CRMRefused):
        context.before_attempt("GET", "/services/data", "api_versions")


def capture_gate_setup(
    tmp_path, *, clock=lambda: NOW, finished="2026-04-17T11:50:00.000000Z", change=None
):
    from flight_recorder.collector.canonical import canonical_hash

    def prepare(cfg, m, a, t, store):
        c = cfg.connection
        observation = dict(
            format_version=1,
            type="account_metadata_observation",
            observation_origin="provider_http",
            connection_binding=c.binding,
            expected_instance_id=c.expected_instance_id,
            expected_user_id=c.expected_user_id,
            api_origin=c.api_origin,
            api_version=c.api_version,
            read_started_at=finished,
            read_finished_at=finished,
            versions=dict(
                path="/services/data",
                body_sha256="a" * 64,
                read_started_at=finished,
                read_finished_at=finished,
            ),
            describe=dict(
                path=f"/services/data/v{c.api_version}/sobjects/Account/describe",
                body_sha256="b" * 64,
                read_started_at=finished,
                read_finished_at=finished,
            ),
            advertised_versions=[c.api_version],
            account=dict(queryable=True, retrieveable=True, updateable=False),
            fields=[dict(**f, readable=True, updateable=False) for f in m["field_schema"]],
            missing_fields=[],
        )
        receipt = dict(
            format_version=1,
            type="metadata",
            config_sha256=cfg.binding,
            owner_request_sha256="d" * 64,
            authority_sha256="e" * 64,
            observation=observation,
        )
        a["metadata_receipt_sha256"] = store.save(receipt)
        for key in ("api_versions", "field_schema", "permissions"):
            m["evidence"][key].update(
                sha256=canonical_hash(receipt),
                observed_at=finished,
                reviewed_at="2026-04-17T11:59:00.000000Z",
            )
        if change:
            change(cfg, m, a, t, store, receipt)

    return read_gate_setup(
        tmp_path, action="capture_once", missing_metadata=False, clock=clock, change=prepare
    )


def test_capture_requires_matching_observed_metadata_and_full_readiness(tmp_path):
    cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path)
    ctx = gate.authorize_capture(a["tick_id"], cfg.targets[0])
    assert ctx.action == "capture_once"
    ctx.before_attempt(
        "GET",
        f"/services/data/v{cfg.connection.api_version}/sobjects/Account/{cfg.connection.record_ids[0]}",
        "account_read",
    )


@pytest.mark.parametrize("action", ["oauth_connect", "metadata_probe", "capture_once"])
@pytest.mark.parametrize(
    "method,path,category",
    [
        ("PATCH", "/services/data/v64.0/sobjects/Account/anything", "writer_send"),
        ("GET", "/services/data/v64.0/sobjects/Account/anything", "writer_outputs"),
        ("GET", "/services/data/v64.0/sobjects/Contact/describe", "account_metadata"),
        ("POST", "/services/oauth2/token", "password"),
    ],
)
def test_read_action_matrix_never_authorizes_other_operations(
    tmp_path, action, method, path, category
):
    if action == "capture_once":
        cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path)
        ctx = gate.authorize_capture(a["tick_id"], cfg.targets[0])
    else:
        cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, action=action)
        ctx = gate.authorize_oauth() if action == "oauth_connect" else gate.authorize_metadata()
    with pytest.raises(CRMRefused, match="read_request_refused"):
        ctx.before_attempt(method, path, category)


@pytest.mark.parametrize("seconds,allowed", [(899, True), (900, False), (901, False), (-1, False)])
def test_metadata_freshness_just_below_equal_above_fifteen_minutes(tmp_path, seconds, allowed):
    from datetime import timedelta

    finished = (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path, finished=finished)
    if allowed:
        gate.authorize_capture(a["tick_id"], cfg.targets[0])
    else:
        with pytest.raises(CRMRefused):
            gate.authorize_capture(a["tick_id"], cfg.targets[0])


def test_metadata_expiry_between_identity_and_data_read_refuses(tmp_path):
    from datetime import timedelta

    now = [NOW]
    cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path, clock=lambda: now[0])
    ctx = gate.authorize_capture(a["tick_id"], cfg.targets[0])
    ctx.before_attempt(
        "GET",
        f"/id/{cfg.connection.expected_instance_id}/{cfg.connection.expected_user_id}",
        "identity",
    )
    now[0] += timedelta(minutes=5)
    with pytest.raises(CRMRefused, match="metadata_expired"):
        ctx.before_attempt(
            "GET",
            f"/services/data/v{cfg.connection.api_version}/sobjects/Account/{cfg.connection.record_ids[0]}",
            "account_read",
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda c, m, a, t, s, r: m["evidence"]["permissions"].update(sha256="f" * 64),
        lambda c, m, a, t, s, r: m["evidence"]["field_schema"].update(
            observed_at="2026-04-17T11:49:00.000000Z"
        ),
        lambda c, m, a, t, s, r: m["permissions"].update(account_update=True),
        lambda c, m, a, t, s, r: r["observation"].update(observation_origin="injected_transport"),
        lambda c, m, a, t, s, r: r["observation"].update(connection_binding="f" * 64),
        lambda c, m, a, t, s, r: r["observation"]["describe"].update(
            path="/services/data/v64.0/sobjects/Contact/describe"
        ),
    ],
)
def test_capture_refuses_bad_metadata_binding_and_declarations(tmp_path, change):
    def mutate(c, m, a, t, s, r):
        change(c, m, a, t, s, r)
        from flight_recorder.collector.canonical import canonical_hash

        old = a["metadata_receipt_sha256"]
        new = s.save(r)
        if old != new:
            a["metadata_receipt_sha256"] = new
            for key in ("api_versions", "field_schema", "permissions"):
                m["evidence"][key]["sha256"] = canonical_hash(r)

    cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path, change=mutate)
    with pytest.raises(CRMRefused):
        gate.authorize_capture(a["tick_id"], cfg.targets[0])


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.update(transport_url="http://evil.invalid"),
        lambda t: t.update(binding_sha256="f" * 64),
        lambda t: t.update(bound_poll_sha256=t["poll_template_sha256"]),
        lambda t: t["actual"].update(workflow_sha256="f" * 64),
        lambda t: t["actual"].update(execution_id=None),
        lambda t: t["dummy"].update(received_host="localhost.evil:8768"),
        lambda t: t["dummy"].update(credential_id=t["credential_id"]),
        lambda t: t.update(poll_workflow_id="unapproved-activation"),
        lambda t: t.update(extra="unreviewed"),
    ],
)
def test_bootstrap_does_not_trust_hash_context_without_transport_receipts(tmp_path, change):
    def mutate(c, m, a, t, s):
        change(t)
        m["evidence"]["n8n"]["sha256"] = s.save(t)

    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, change=mutate)
    with pytest.raises(CRMRefused, match="n8n_transport_unqualified"):
        gate.authorize_oauth()


def test_legacy_and_cached_runtime_cannot_bypass_gate_with_noncontext_object(tmp_path):
    cfg = config(tmp_path)
    initialize(cfg)
    runtime = CRMRuntime(cfg, clock=lambda: NOW)
    runtime._client = object()
    runtime.start(background=False)
    runtime.read_gate = object()
    try:
        with pytest.raises(CRMRefused, match="live_read_authority_required"):
            runtime.client()
    finally:
        runtime.close()


def test_expired_refresh_does_not_claim_token(tmp_path):
    from datetime import timedelta

    now = [NOW]
    cfg, m, a, paths, directory, gate = read_gate_setup(
        tmp_path, action="metadata_probe", clock=lambda: now[0]
    )
    initialize(cfg)
    runtime = CRMRuntime(cfg, clock=lambda: now[0])
    runtime.read_gate = gate
    runtime._read_context = gate.authorize_metadata()
    calls = []

    class ExpiredClient:
        def verify_connection(self):
            now[0] += timedelta(hours=1)
            raise CRMRefused("token_expired")

    class NoClaim:
        def read_ready(self, *args):
            calls.append("token-access")
            raise CRMRefused("fixture_refused")

        def close(self):
            pass

    from types import SimpleNamespace

    runtime._http = SimpleNamespace(config_binding=cfg.connection.binding, close=lambda: None)
    runtime._tokens = NoClaim()
    runtime.start(background=False)
    try:
        with pytest.raises(CRMRefused):
            runtime.verify_for_read(ExpiredClient())
        assert calls == [], "expired read grant touched refresh state before admission"
    finally:
        runtime.close()


def capture_runtime(tmp_path, *, clock=lambda: NOW):
    from tests.adapters.crm.test_poll import Provider

    cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path, clock=clock)
    initialize(cfg)
    provider = Provider()
    runtime = CRMRuntime(cfg, client_factory=lambda: provider, clock=clock)
    runtime.read_gate = gate
    runtime.start(background=False)
    return runtime, provider, a, paths, directory


def test_capture_admission_links_exact_evidence_and_tick(tmp_path):
    import json

    runtime, provider, a, paths, directory = capture_runtime(tmp_path)
    try:
        report = runtime.admit_read_tick(a["tick_id"])
        assert report is not None, "capture must save an admission before queueing its one tick"
        assert report.disposition == "active" and provider.calls == 0
        docs = [json.loads(p.read_bytes()) for p in directory.iterdir()]
        admissions = [d for d in docs if d.get("type") == "capture_admission"]
        assert len(admissions) == 1
        admission = admissions[0]
        assert admission["config_sha256"] == runtime.config.binding
        assert admission["authority_sha256"] == runtime.read_gate.authority_file.sha256
        assert admission["readiness_sha256"] == runtime.read_gate.readiness_file.sha256
        assert admission["metadata_receipt_sha256"] == a["metadata_receipt_sha256"]
        assert (
            admission["tick_id"] == a["tick_id"]
            and admission["target_key"] == runtime.config.targets[0].key
        )
        assert any(d.get("type") == "capture_result" for d in docs)
        runtime.run_pending()
        assert runtime.poll_report(a["tick_id"]).disposition == "complete"
        assert provider.calls == 1
        runtime.admit_read_tick(a["tick_id"])
        runtime.run_pending()
        assert provider.calls == 1
        assert (
            len(
                [
                    p
                    for p in directory.iterdir()
                    if json.loads(p.read_bytes()).get("type") == "capture_admission"
                ]
            )
            == 1
        )
        with pytest.raises(CRMRefused):
            runtime.admit_read_tick("a-different-tick")
    finally:
        runtime.close()


def test_expired_pre_freeze_recovery_refuses_network(tmp_path):
    from datetime import timedelta

    now = [NOW]
    runtime, provider, a, paths, directory = capture_runtime(tmp_path, clock=lambda: now[0])
    try:
        report = runtime.admit_read_tick(a["tick_id"])
        assert report is not None, "read action must bind the queued tick"
        now[0] += timedelta(minutes=15)
        with pytest.raises(CRMRefused):
            runtime.run_pending()
        assert provider.calls == 0
    finally:
        runtime.close()


def test_duplicate_and_frozen_recovery_never_reread_after_expiry(tmp_path, monkeypatch):
    from datetime import timedelta

    import flight_recorder.adapters.crm.poll as poll

    now = [NOW]
    runtime, provider, a, paths, directory = capture_runtime(tmp_path, clock=lambda: now[0])
    original = poll.freeze_submission

    class Crash(BaseException):
        pass

    try:
        report = runtime.admit_read_tick(a["tick_id"])
        assert report is not None, "read action must bind the queued tick"
        monkeypatch.setattr(
            poll, "freeze_submission", lambda *args, **kwargs: (_ for _ in ()).throw(Crash())
        )
        with pytest.raises(Crash):
            runtime.run_pending()
        assert provider.calls == 1
        now[0] += timedelta(hours=2)
        monkeypatch.setattr(poll, "freeze_submission", original)
        runtime.run_pending()
        assert runtime.poll_report(a["tick_id"]).disposition == "complete"
        assert provider.calls == 1
    finally:
        runtime.close()


def test_restart_audits_exact_frozen_link_before_expired_collector_recovery(tmp_path, monkeypatch):
    from datetime import timedelta

    import flight_recorder.adapters.crm.poll as poll
    from flight_recorder.adapters.crm.read_control import ReadSessionGate

    now = [NOW]
    runtime, provider, a, paths, directory = capture_runtime(tmp_path, clock=lambda: now[0])
    cfg = runtime.config
    original = poll.freeze_submission

    class Crash(BaseException):
        pass

    try:
        runtime.admit_read_tick(a["tick_id"])
        monkeypatch.setattr(
            poll, "freeze_submission", lambda *args, **kwargs: (_ for _ in ()).throw(Crash())
        )
        with pytest.raises(Crash):
            runtime.run_pending()
        assert provider.calls == 1
    finally:
        runtime.close()
    now[0] += timedelta(hours=2)
    monkeypatch.setattr(poll, "freeze_submission", original)
    gate = ReadSessionGate(*paths, directory, clock=lambda: now[0])
    resumed = CRMRuntime(cfg, client_factory=lambda: provider, clock=lambda: now[0])
    resumed.read_gate = gate
    try:
        resumed.start(background=False)
        assert resumed.poll_report(a["tick_id"]).disposition == "complete"
        assert provider.calls == 1
    finally:
        resumed.close()


def test_ingress_removed_at_signature_boundary_has_fixed_refusal(tmp_path, monkeypatch):
    import flight_recorder.adapters.crm.read_control as module

    cfg, _, _, _, _, gate = read_gate_setup(tmp_path)
    original = module._file_signature

    def removed(path):
        if path == Path(cfg.document["ingress_secret_file"]):
            path.unlink()
        return original(path)

    monkeypatch.setattr(module, "_file_signature", removed)
    with pytest.raises(CRMRefused):
        gate.check()


def test_capture_result_missing_member_fields_has_fixed_refusal(tmp_path):
    runtime, _, authority, _, directory = capture_runtime(tmp_path)
    try:
        runtime.admit_read_tick(authority["tick_id"])
        import json

        result = next(
            json.loads(p.read_bytes())
            for p in directory.iterdir()
            if json.loads(p.read_bytes()).get("type") == "capture_result"
        )
        result["report"]["members"] = [{"target_key": runtime.config.targets[0].key}]
        runtime.read_gate.evidence.save(result)
        with pytest.raises(CRMRefused):
            runtime.admit_read_tick(authority["tick_id"])
    finally:
        runtime.close()


def test_runtime_retries_recheck_held_process_authority(tmp_path):
    import httpx

    from flight_recorder.adapters.crm.cli import initialize
    from flight_recorder.adapters.crm.http import OwnedHTTP
    from flight_recorder.adapters.crm.runtime import CRMRuntime
    from flight_recorder.adapters.crm.secrets import TokenMaterial, TokenStore
    from tests.adapters.crm.test_readiness import NOW

    cfg, _, _, _, _, gate = read_gate_setup(tmp_path, action="metadata_probe")
    initialize(cfg)
    runtime = CRMRuntime(cfg, clock=lambda: NOW)
    runtime.read_gate = gate
    runtime.start(background=False)
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            path = runtime.authority.lock_path
            replacement = path.with_name("replacement-lock")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
            return httpx.Response(503)
        if request.url.path.startswith("/id/"):
            return httpx.Response(
                200,
                json={
                    "asserted_user": True,
                    "active": True,
                    "organization_id": cfg.connection.expected_instance_id,
                    "user_id": cfg.connection.expected_user_id,
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "version": cfg.connection.api_version,
                    "url": "/services/data/v" + cfg.connection.api_version,
                }
            ],
        )

    context = gate.authorize_metadata()
    runtime._read_context = context
    runtime._http = OwnedHTTP(
        cfg.connection,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
        before_attempt=getattr(runtime, "_before_read_attempt", context.before_attempt),
    )
    runtime._tokens = TokenStore(cfg.document["credential_directory"])
    runtime._tokens.install(
        cfg.connection.connection_id,
        cfg.connection.binding,
        TokenMaterial("fictional-access", "fictional-refresh", None),
    )
    try:
        client = runtime.client()
        with pytest.raises(CRMRefused):
            runtime.verify_for_read(client)
        assert len(requests) == 1
    finally:
        runtime.close()


def test_capture_result_cannot_claim_terminal_for_pending_tick(tmp_path):
    import json

    runtime, provider, authority, _, directory = capture_runtime(tmp_path)
    try:
        runtime.admit_read_tick(authority["tick_id"])
        result = next(
            json.loads(p.read_bytes())
            for p in directory.iterdir()
            if json.loads(p.read_bytes()).get("type") == "capture_result"
        )
        result["report"]["disposition"] = "complete"
        result["report"]["members"][0]["state"] = "complete"
        runtime.read_gate.evidence.save(result)
        with pytest.raises(CRMRefused):
            runtime.run_pending()
        assert provider.calls == 0
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(disposition=[]),
        lambda r: r.update(disposition="unknown"),
        lambda r: r.update(members=[None]),
        lambda r: r["members"][0].update(state={}),
        lambda r: r["members"][0].update(reason={}),
        lambda r: r["members"][0].update(state="unknown"),
        lambda r: r["members"][0].update(reason="a private arbitrary message"),
        lambda r: r["members"][0].update(unit_id="f" * 64),
        lambda r: r["members"][0].update(decision_event_id="fr-crm-decision-v1-" + "f" * 64),
    ],
)
def test_malformed_capture_result_reports_refuse_before_network(tmp_path, change):
    import json

    runtime, provider, authority, paths, directory = capture_runtime(tmp_path)
    try:
        runtime.admit_read_tick(authority["tick_id"])
        result = next(
            json.loads(p.read_bytes())
            for p in directory.iterdir()
            if json.loads(p.read_bytes()).get("type") == "capture_result"
        )
        change(result["report"])
        runtime.read_gate.evidence.save(result)
        with pytest.raises(CRMRefused, match="capture_result_mismatch"):
            runtime.run_pending()
        assert provider.calls == provider.verifications == 0
    finally:
        runtime.close()


def test_process_binding_loss_before_refresh_never_claims_tokens(tmp_path):
    from types import SimpleNamespace

    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, action="metadata_probe")
    initialize(cfg)
    runtime = CRMRuntime(cfg, clock=lambda: NOW)
    runtime.read_gate = gate
    runtime.start(background=False)
    calls = []

    class LostOwnership:
        def verify_connection(self):
            path = runtime.authority.lock_path
            replacement = path.with_name("replacement-lock")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
            raise CRMRefused("token_expired")

    class NoClaim:
        def read_ready(self, *args):
            calls.append("token-access")
            raise CRMRefused("fixture_refused")

        def close(self):
            pass

    runtime._tokens = NoClaim()
    runtime._http = SimpleNamespace(config_binding=cfg.connection.binding, close=lambda: None)
    try:
        with pytest.raises(CRMRefused):
            runtime.verify_for_read(LostOwnership())
        assert calls == []
    finally:
        runtime.close()


def test_one_target_refuses_multi_target_before_receipt(tmp_path):
    from flight_recorder.adapters.crm.contracts import CRMTarget
    from flight_recorder.adapters.crm.read_control import ReadSessionGate
    from flight_recorder.adapters.crm.salesforce import source_identity_id
    from flight_recorder.collector.canonical import canonical_bytes

    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path)
    document = cfg.document
    second = source_identity_id(cfg.connection.record_ids[0][:14] + "2", "001")
    if second == cfg.connection.record_ids[0]:
        second = source_identity_id(cfg.connection.record_ids[0][:14] + "3", "001")
    target = CRMTarget("salesforce", cfg.connection.expected_instance_id, "Account", second)
    document["connection"]["record_ids"].append(second)
    import copy

    discovery = copy.deepcopy(next(iter(document["discoveries"].values())))
    discovery["account_ref"] = target.account_ref
    document["discoveries"][second] = discovery
    paths[0].write_bytes(canonical_bytes(document))
    before = {p: p.read_bytes() for p in directory.iterdir()}
    gate = ReadSessionGate(*paths, directory, clock=lambda: NOW)
    with pytest.raises(CRMRefused, match="read_authority_binding_mismatch"):
        gate.authorize_oauth()
    assert {p: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("version,writer", [(4, False), (5, True)])
def test_retained_format4_and5_are_unchanged(tmp_path, version, writer):
    import sqlite3

    cfg = config(tmp_path)
    original = cfg.canonical_bytes
    initialize(cfg, writer_capable=writer)

    def schema():
        with sqlite3.connect(cfg.operational_path) as conn:
            return conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()

    before = schema()
    runtime = CRMRuntime(cfg, writer_capable=writer)
    try:
        assert runtime.store._format_version == version
        assert runtime.config.canonical_bytes == original
        runtime.start(background=False)
        assert schema() == before
    finally:
        runtime.close()


def test_invalid_metadata_context_never_constructs_client(tmp_path, monkeypatch):
    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, action="metadata_probe")
    initialize(cfg)
    runtime = CRMRuntime(cfg, clock=lambda: NOW)
    runtime.read_gate = gate
    runtime.start(background=False)

    def forbidden():
        pytest.fail("invalid metadata context constructed a client")

    monkeypatch.setattr(runtime, "client", forbidden)
    try:
        with pytest.raises(CRMRefused, match="metadata_unqualified"):
            gate.observe_metadata(runtime, None)
    finally:
        runtime.close()
