"""Offline checks of fictional operator declarations; no live capability is proved."""

import copy
import importlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.adapters.crm.cli import n8n_poll_workflow
from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.http import SELECTED_FIELDS
from flight_recorder.adapters.crm.mapping import ALLOWED, MAX_INTEGER, NUMERIC, OUTPUTS
from flight_recorder.collector.canonical import canonical_hash
from tests.adapters.crm.test_poll import config

NOW = datetime(2026, 4, 17, 12, tzinfo=UTC)
EARLIER = "2026-04-17T11:00:00.000000Z"
LATER = "2026-04-17T13:00:00.000000Z"
ROOT = Path(__file__).resolve().parents[3]


def feature():
    try:
        return importlib.import_module("flight_recorder.adapters.crm.readiness")
    except ModuleNotFoundError:
        pytest.fail("missing offline Salesforce readiness contract", pytrace=False)


def declaration(cfg, stage="ordinary_send"):
    d, c = cfg.document, cfg.document["connection"]
    binding = {
        **{
            k: c[k]
            for k in (
                "connection_id",
                "expected_instance_id",
                "expected_user_id",
                "login_origin",
                "api_origin",
                "identity_origin",
                "api_version",
                "client_id",
                "callback_uri",
                "credential_reference",
                "mapping_hash",
                "artifact_hash",
                "evaluator_version",
                "record_ids",
                "refresh_rotation",
                "code_secret_required",
                "refresh_secret_required",
            )
        },
        **{
            k: d[k]
            for k in (
                "domain_path",
                "operational_path",
                "credential_directory",
                "ingress_secret_file",
                "ingress_origin",
                "selection_identity",
            )
        },
        "config_sha256": cfg.binding,
    }
    proof = dict(
        sha256="a" * 64,
        origin="provider_http",
        observed_at=EARLIER,
        reviewed_at=EARLIER,
        config_sha256=cfg.binding,
    )
    fields = []
    for name in sorted(ALLOWED):
        kind = "string"
        if name in NUMERIC:
            kind = "int" if name == "NumberOfEmployees" else "double"
        elif name.endswith("_On__c") or name == "FR_Head_Of_Platform_Start__c":
            kind = "date"
        elif name == "LastModifiedDate":
            kind = "datetime"
        elif name == "Id":
            kind = "id"
        elif name == "Website":
            kind = "url"
        fields.append(
            dict(
                name=name,
                kind=kind,
                length=255 if kind in ("string", "url") else 18,
                precision=18,
                scale=0,
                picklist_values=[],
            )
        )
    op = "1" * 64
    original = "fr-crm-decision-v1-" + "2" * 64
    values = dict(
        fr_score=86,
        fr_output="PRIORITIZE",
        fr_logic_version=d["artifact_registration"]["payload"]["artifact"]["logic_version"],
        fr_decision_ref=original,
        fr_op_id=op,
        fr_op_rev=3,
    )
    authority = dict(
        stage=stage,
        issued_at=EARLIER,
        expires_at=LATER,
        reviewed_request_sha256="b" * 64,
        config_sha256=cfg.binding,
        expected_instance_id=c["expected_instance_id"],
        record_ids=c["record_ids"],
        decision_event_id=original,
        capture_hash="3" * 64,
        operation_id=op,
        revision=3,
        policy_generation=2,
        artifact_hash=c["artifact_hash"],
        evaluator_version=c["evaluator_version"],
        mapping_hash=c["mapping_hash"],
        selection_identity=d["selection_identity"],
        intended=values,
        intended_outputs_sha256=canonical_hash(values),
        source_precondition='"fictional-new"',
        source_precondition_sha256=canonical_hash('"fictional-new"'),
    )
    if stage in ("read_only", "local_prepare"):
        for key in [
            "decision_event_id",
            "capture_hash",
            "operation_id",
            "revision",
            "policy_generation",
            "artifact_hash",
            "evaluator_version",
            "mapping_hash",
            "selection_identity",
            "intended",
            "intended_outputs_sha256",
            "source_precondition",
            "source_precondition_sha256",
        ]:
            authority[key] = None
    result = dict(
        format_version=1,
        issued_at=EARLIER,
        expires_at=LATER,
        binding=binding,
        environment=dict(
            edition="developer_edition", production=False, fictional_records_only=True
        ),
        oauth=dict(client_type="confidential", scopes=["api", "refresh_token"]),
        advertised_api_versions=[c["api_version"]],
        initial_write_policy_enabled=False,
        field_schema=fields,
        permissions=dict(
            user_id=c["expected_user_id"],
            account_read=True,
            account_update=True,
            read_fields=sorted(ALLOWED),
            write_fields=sorted(OUTPUTS.values()),
        ),
        n8n=dict(
            runtime_id="fictional-n8n",
            version="fictional-1",
            expected_version="fictional-1",
            workflow_sha256=canonical_hash(n8n_poll_workflow()),
            interval_seconds=60,
            from_runtime_id="fictional-n8n",
            to_origin=d["ingress_origin"],
            reachable=True,
        ),
        evidence={
            key: copy.deepcopy(proof)
            for key in (
                "environment",
                "api_versions",
                "oauth_policy",
                "field_schema",
                "permissions",
                "n8n",
                "identity",
                "read",
                "guard",
            )
        },
        guard=dict(
            object_type="Account",
            api_version=c["api_version"],
            selected_fields=list(SELECTED_FIELDS),
            captured_etag='"fictional-qualified"',
            stale_etag='"fictional-stale"',
            completed_status=204,
            response_complete=True,
            outputs_match=True,
            stale_status=412,
            stale_response_complete=True,
            stale_authority_sha256="4" * 64,
            success_operation_id="5" * 64,
            stale_operation_id="6" * 64,
        ),
        authority=authority,
    )
    if stage in ("read_only", "local_prepare", "qualification_send"):
        result["guard"] = None
        result["evidence"]["guard"] = None
    return result


def report(cfg, document, now=NOW):
    api = feature()
    return api.validate_readiness(api.ReadinessManifest.from_document(document), cfg, now=now)


def test_complete_declarations_are_not_attestation_or_execution_authority(tmp_path):
    cfg = config(tmp_path)
    result = report(cfg, declaration(cfg))
    assert result.ready_for_read and result.ready_for_write and result.declarations_complete
    assert result.requires_runtime_intent_check and result.requested_stage == "ordinary_send"
    assert "declarations" in result.evidence_notice.lower()
    assert "authorize" in result.evidence_notice.lower()
    assert result.config_sha256 == cfg.binding
    assert str(cfg.document["credential_directory"]) not in repr(result)


@pytest.mark.parametrize("stage", ["read_only", "local_prepare", "qualification_send"])
def test_preparation_and_first_qualification_do_not_require_or_invent_prior_guard(tmp_path, stage):
    cfg = config(tmp_path)
    result = report(cfg, declaration(cfg, stage))
    assert result.ready_for_read and result.declarations_complete
    assert result.ready_for_write is False
    assert result.requires_runtime_intent_check is (stage == "qualification_send")
    assert result.requested_stage == stage
    if stage == "qualification_send":
        assert "conditional_guard_unqualified" in result.reasons


@pytest.mark.parametrize("stage", ["read_only", "local_prepare"])
def test_first_read_or_local_preparation_does_not_require_prior_account_read(tmp_path, stage):
    cfg = config(tmp_path)
    d = declaration(cfg, stage)
    d["evidence"]["identity"] = d["evidence"]["read"] = None
    result = report(cfg, d)
    assert result.ready_for_read and result.declarations_complete
    assert not result.ready_for_write and not result.requires_runtime_intent_check


@pytest.mark.parametrize(
    "stage", ["read_only", "local_prepare", "qualification_send", "ordinary_send"]
)
@pytest.mark.parametrize("key", ["identity", "read"])
@pytest.mark.parametrize("origin", ["fixture", "injected_transport"])
def test_supplied_nonlive_identity_or_read_never_qualifies_any_stage(tmp_path, stage, key, origin):
    cfg = config(tmp_path)
    d = declaration(cfg, stage)
    d["evidence"][key]["origin"] = origin
    result = report(cfg, d)
    assert not result.ready_for_read and not result.ready_for_write
    assert not result.declarations_complete


@pytest.mark.parametrize(
    "key",
    [
        "expected_instance_id",
        "expected_user_id",
        "api_version",
        "api_origin",
        "callback_uri",
        "client_id",
        "credential_reference",
        "mapping_hash",
        "artifact_hash",
        "evaluator_version",
        "selection_identity",
        "domain_path",
        "operational_path",
        "credential_directory",
        "ingress_secret_file",
        "record_ids",
    ],
)
def test_config_binding_mismatch_cannot_qualify(tmp_path, key):
    cfg = config(tmp_path)
    d = declaration(cfg)
    d["binding"][key] = [] if key == "record_ids" else "different-fictional-binding"
    result = report(cfg, d)
    assert not result.ready_for_read and not result.ready_for_write
    assert result.reasons == tuple(sorted(set(result.reasons)))


@pytest.mark.parametrize(
    "damage",
    [
        "production",
        "environment",
        "extra_record",
        "api_version",
        "n8n_version",
        "n8n_network",
        "n8n_workflow",
        "schedule",
        "schema_missing",
        "schema_extra",
        "numeric_scale",
        "short_operation_id",
        "output_picklist",
        "permissions",
        "permission_user",
        "missing_guard",
        "bad_guard_status",
        "weak_guard",
        "same_guard_etags",
        "guard_fields",
        "guard_api_version",
        "guard_origin",
        "identity_origin",
        "read_origin",
        "proof_missing",
        "proof_config",
        "proof_future",
        "expired",
        "future",
        "naive_time",
        "action_expired",
        "action_config",
        "action_instance",
        "action_target",
        "action_digest",
        "action_source_hash",
        "action_original",
        "action_revision",
        "action_mapping",
        "policy_enabled",
    ],
)
def test_incomplete_or_conflicting_declarations_fail_closed(tmp_path, damage):
    cfg = config(tmp_path)
    d = declaration(cfg)

    def field(name):
        return next(f for f in d["field_schema"] if f["name"] == name)

    if damage == "production":
        d["environment"]["production"] = True
    elif damage == "environment":
        d["environment"]["edition"] = "production"
    elif damage == "extra_record":
        d["binding"]["record_ids"].append("unlisted-fictional-record")
    elif damage == "api_version":
        d["advertised_api_versions"] = []
    elif damage == "n8n_version":
        d["n8n"]["version"] = "different"
    elif damage == "n8n_network":
        d["n8n"]["to_origin"] = "http://localhost:9999"
    elif damage == "n8n_workflow":
        d["n8n"]["workflow_sha256"] = "f" * 64
    elif damage == "schedule":
        d["n8n"]["interval_seconds"] = 30
    elif damage == "schema_missing":
        d["field_schema"].pop()
    elif damage == "schema_extra":
        d["field_schema"].append(dict(d["field_schema"][0], name="Other__c"))
    elif damage == "numeric_scale":
        field("FR_Score__c")["scale"] = 2
    elif damage == "short_operation_id":
        field("FR_Operation_Id__c")["length"] = 63
    elif damage == "output_picklist":
        field("FR_Output__c").update(kind="picklist", picklist_values=["PRIORITIZE"])
    elif damage == "permissions":
        d["permissions"]["write_fields"].append("NumberOfEmployees")
    elif damage == "permission_user":
        d["permissions"]["user_id"] = "different"
    elif damage == "missing_guard":
        d["guard"] = None
    elif damage == "bad_guard_status":
        d["guard"]["stale_status"] = 200
    elif damage == "weak_guard":
        d["guard"]["captured_etag"] = 'W/"weak"'
    elif damage == "same_guard_etags":
        d["guard"]["stale_etag"] = d["guard"]["captured_etag"]
    elif damage == "guard_fields":
        d["guard"]["selected_fields"] = []
    elif damage == "guard_api_version":
        d["guard"]["api_version"] = "10.0"
    elif damage in ("guard_origin", "identity_origin", "read_origin"):
        d["evidence"][damage.removesuffix("_origin")]["origin"] = "injected_transport"
    elif damage == "proof_missing":
        d["evidence"]["field_schema"] = None
    elif damage == "proof_config":
        d["evidence"]["permissions"]["config_sha256"] = "f" * 64
    elif damage == "proof_future":
        d["evidence"]["read"]["observed_at"] = LATER
    elif damage == "expired":
        d["expires_at"] = "2026-04-17T12:00:00.000000Z"
    elif damage == "future":
        d["issued_at"] = LATER
    elif damage == "naive_time":
        d["issued_at"] = "2026-04-17T11:00:00"
    elif damage == "action_expired":
        d["authority"]["expires_at"] = EARLIER
    elif damage == "action_config":
        d["authority"]["config_sha256"] = "f" * 64
    elif damage == "action_instance":
        d["authority"]["expected_instance_id"] = "different"
    elif damage == "action_target":
        d["authority"]["record_ids"] = ["unlisted"]
    elif damage == "action_digest":
        d["authority"]["intended_outputs_sha256"] = "f" * 64
    elif damage == "action_source_hash":
        d["authority"]["source_precondition_sha256"] = "f" * 64
    elif damage == "action_original":
        d["authority"]["intended"]["fr_decision_ref"] = "different"
    elif damage == "action_revision":
        d["authority"]["revision"] = 0
    elif damage == "action_mapping":
        d["authority"]["mapping_hash"] = "f" * 64
    elif damage == "policy_enabled":
        d["initial_write_policy_enabled"] = True
    try:
        result = report(cfg, d)
    except CRMRefused as refused:
        assert refused.reason == "invalid_readiness_manifest"
    else:
        assert not result.ready_for_write and not result.declarations_complete


@pytest.mark.parametrize("stage", ["read_only", "local_prepare"])
def test_send_authority_cannot_be_relabelled_as_a_preparation_or_read(tmp_path, stage):
    cfg = config(tmp_path)
    d = declaration(cfg)
    d["authority"]["stage"] = stage
    assert not report(cfg, d).declarations_complete


@pytest.mark.parametrize("damage", ["unknown_key", "secret", "duplicate_json", "nonfinite_json"])
def test_closed_manifest_rejects_unknown_or_secret_material_without_echo(tmp_path, damage):
    cfg = config(tmp_path)
    d = declaration(cfg)
    marker = "FICTIONAL-ACCESS-TOKEN-DO-NOT-ECHO"
    api = feature()
    with pytest.raises(CRMRefused) as refused:
        if damage == "unknown_key":
            api.ReadinessManifest.from_document(dict(d, unexpected=True))
        elif damage == "secret":
            d["binding"]["access_token"] = marker
            api.ReadinessManifest.from_document(d)
        elif damage == "duplicate_json":
            api.ReadinessManifest(b'{"format_version":1,"format_version":1}')
        else:
            api.ReadinessManifest(b'{"format_version":NaN}')
    assert refused.value.reason == "invalid_readiness_manifest"
    assert marker not in str(refused.value) and marker not in repr(refused.value)


def test_manifest_is_immutable_and_validation_has_no_io_or_ambient_clock(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    api = feature()
    d = declaration(cfg)
    manifest = api.ReadinessManifest.from_document(d)
    before = manifest.canonical_bytes
    d["binding"]["expected_instance_id"] = "mutated"
    manifest.document["binding"]["expected_instance_id"] = "also-mutated"
    monkeypatch.setattr(Path, "read_bytes", lambda *a: pytest.fail("readiness opened a file"))
    monkeypatch.setattr(Path, "is_symlink", lambda *a: pytest.fail("readiness revalidated paths"))
    monkeypatch.setattr("builtins.open", lambda *a, **kw: pytest.fail("readiness opened a file"))
    monkeypatch.setattr(
        "socket.create_connection", lambda *a, **kw: pytest.fail("readiness used network")
    )
    assert api.validate_readiness(manifest, cfg, now=NOW).ready_for_write
    assert manifest.canonical_bytes == before
    assert str(cfg.document["credential_directory"]) not in repr(manifest)
    with pytest.raises(CRMRefused, match="invalid_readiness_clock"):
        api.validate_readiness(manifest, cfg, now=NOW.replace(tzinfo=None))


@pytest.mark.invariant
@given(
    key=st.sampled_from(
        [
            "config_sha256",
            "connection_id",
            "expected_instance_id",
            "expected_user_id",
            "login_origin",
            "api_origin",
            "identity_origin",
            "api_version",
            "client_id",
            "callback_uri",
            "credential_reference",
            "mapping_hash",
            "artifact_hash",
            "evaluator_version",
            "refresh_rotation",
            "domain_path",
            "operational_path",
            "credential_directory",
            "ingress_secret_file",
            "ingress_origin",
            "selection_identity",
            "record_ids",
            "code_secret_required",
            "refresh_secret_required",
        ]
    )
)
def test_generated_missing_binding_never_qualifies(key):
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="fr-readiness-") as directory:
        cfg = config(Path(directory).resolve())
        d = declaration(cfg)
        d["binding"][key] = [] if key == "record_ids" else None
        result = report(cfg, d)
        assert not result.ready_for_read and not result.ready_for_write


def test_template_is_structurally_valid_but_never_ready(tmp_path):
    cfg = config(tmp_path)
    api = feature()
    template = ROOT / "integrations/salesforce/readiness-manifest.template.json"
    assert template.is_file(), "missing unqualified readiness template"
    result = api.validate_readiness(api.ReadinessManifest(template.read_bytes()), cfg, now=NOW)
    assert not result.ready_for_read and not result.ready_for_write and result.reasons


def test_live_skeleton_is_inert_even_when_opt_in_variables_are_set(tmp_path):
    feature()
    target = ROOT / "tests/integration/test_salesforce_live.py"
    assert target.is_file(), "missing credential-free inert live skeleton"
    env = dict(
        os.environ,
        FR_SALESFORCE_LIVE="1",
        FR_SALESFORCE_CONFIG="/FICTIONAL-MISSING-CONFIG",
        FR_SALESFORCE_AUTHORITY="/FICTIONAL-MISSING-AUTHORITY",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-s",
            "-m",
            "pytest",
            "-q",
            str(target),
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "inert-skeleton"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0 and "1 skipped" in result.stdout
    assert "FICTIONAL-MISSING" not in result.stdout + result.stderr


@pytest.mark.parametrize("stage", ["qualification_send", "ordinary_send"])
def test_declared_score_must_fit_the_reviewed_output_field(tmp_path, stage):
    cfg = config(tmp_path)
    d = declaration(cfg, stage)
    score_field = next(f for f in d["field_schema"] if f["name"] == "FR_Score__c")
    d["authority"]["intended"]["fr_score"] = 10 ** score_field["precision"]
    d["authority"]["intended_outputs_sha256"] = canonical_hash(d["authority"]["intended"])
    result = report(cfg, d)
    assert not result.declarations_complete and not result.ready_for_write
    assert "field_schema_unqualified" in result.reasons


@pytest.mark.parametrize("stage", ["qualification_send", "ordinary_send"])
@pytest.mark.parametrize("key", ["fr_score", "fr_op_rev"])
def test_declared_output_integer_must_fit_the_existing_mapping(tmp_path, stage, key):
    cfg = config(tmp_path)
    d = declaration(cfg, stage)
    for field in d["field_schema"]:
        if field["name"] in ("FR_Score__c", "FR_Operation_Revision__c"):
            field["precision"] = 64
    d["authority"]["intended"][key] = MAX_INTEGER + 1
    if key == "fr_op_rev":
        d["authority"]["revision"] = MAX_INTEGER + 1
    d["authority"]["intended_outputs_sha256"] = canonical_hash(d["authority"]["intended"])
    result = report(cfg, d)
    assert not result.declarations_complete and not result.ready_for_write
    assert "send_declaration_unqualified" in result.reasons
