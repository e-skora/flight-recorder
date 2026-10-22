"""Pure checks of supplied setup declarations, never provider attestation or send authority."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, create_model

from flight_recorder.adapters.crm.cli import n8n_poll_workflow
from flight_recorder.adapters.crm.contracts import CRMRefused, observation_time, require
from flight_recorder.adapters.crm.http import SELECTED_FIELDS, strict_json
from flight_recorder.adapters.crm.mapping import ALLOWED, MAX_INTEGER, NUMERIC, OUTPUTS
from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash

NOTICE = (
    "Checks supplied declarations only. This does not attest provider facts, verify a retained "
    "intent, authenticate owner authority or authorize execution."
)
STAGES = frozenset({"read_only", "local_prepare", "qualification_send", "ordinary_send"})
SEND_STAGES = frozenset({"qualification_send", "ordinary_send"})
CONNECTION_TEXT = (
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
)
RUNTIME_TEXT = (
    "domain_path",
    "operational_path",
    "credential_directory",
    "ingress_secret_file",
    "ingress_origin",
    "selection_identity",
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


_Binding = create_model(
    "ReadinessBinding",
    __base__=_Closed,
    **{name: (str | None, ...) for name in (*CONNECTION_TEXT, *RUNTIME_TEXT, "config_sha256")},
    record_ids=(list[str], ...),
    code_secret_required=(bool | None, ...),
    refresh_secret_required=(bool | None, ...),
)


class _Environment(_Closed):
    edition: str | None
    production: bool | None
    fictional_records_only: bool | None


class _OAuth(_Closed):
    client_type: str | None
    scopes: list[str]


class _Field(_Closed):
    name: str
    kind: str
    length: int | None
    precision: int | None
    scale: int | None
    picklist_values: list[str]


class _Permissions(_Closed):
    user_id: str | None
    account_read: bool | None
    account_update: bool | None
    read_fields: list[str]
    write_fields: list[str]


class _N8N(_Closed):
    runtime_id: str | None
    version: str | None
    expected_version: str | None
    workflow_sha256: str | None
    interval_seconds: int | None
    from_runtime_id: str | None
    to_origin: str | None
    reachable: bool | None


class _Evidence(_Closed):
    sha256: str | None
    origin: str | None
    observed_at: str | None
    reviewed_at: str | None
    config_sha256: str | None


_EvidenceSet = create_model(
    "ReadinessEvidence",
    __base__=_Closed,
    **{
        name: (_Evidence | None, ...)
        for name in (
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
)


class _Guard(_Closed):
    object_type: str | None
    api_version: str | None
    selected_fields: list[str]
    captured_etag: str | None
    stale_etag: str | None
    completed_status: int | None
    response_complete: bool | None
    outputs_match: bool | None
    stale_status: int | None
    stale_response_complete: bool | None
    stale_authority_sha256: str | None
    success_operation_id: str | None
    stale_operation_id: str | None


class _Intended(_Closed):
    fr_score: int
    fr_output: str
    fr_logic_version: str
    fr_decision_ref: str
    fr_op_id: str
    fr_op_rev: int


class _Authority(_Closed):
    stage: str | None
    issued_at: str | None
    expires_at: str | None
    reviewed_request_sha256: str | None
    config_sha256: str | None
    expected_instance_id: str | None
    record_ids: list[str]
    decision_event_id: str | None
    capture_hash: str | None
    operation_id: str | None
    revision: int | None
    policy_generation: int | None
    artifact_hash: str | None
    evaluator_version: str | None
    mapping_hash: str | None
    selection_identity: str | None
    intended: _Intended | None
    intended_outputs_sha256: str | None
    source_precondition: str | None
    source_precondition_sha256: str | None


class _Manifest(_Closed):
    format_version: int
    issued_at: str | None
    expires_at: str | None
    binding: _Binding
    environment: _Environment
    oauth: _OAuth
    advertised_api_versions: list[str]
    initial_write_policy_enabled: bool
    field_schema: list[_Field]
    permissions: _Permissions
    n8n: _N8N
    evidence: _EvidenceSet
    guard: _Guard | None
    authority: _Authority


@dataclass(frozen=True, repr=False)
class ReadinessManifest:
    canonical_bytes: bytes

    def __post_init__(self):
        try:
            require(type(self.canonical_bytes) is bytes, "invalid_readiness_manifest")
            model = _Manifest.model_validate(strict_json(self.canonical_bytes), strict=True)
            require(model.format_version == 1, "invalid_readiness_manifest")
            require(
                canonical_bytes(model.model_dump()) == self.canonical_bytes,
                "invalid_readiness_manifest",
            )
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_readiness_manifest") from None

    @classmethod
    def from_document(cls, document):
        try:
            return cls(canonical_bytes(document))
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_readiness_manifest") from None

    @property
    def document(self):
        return strict_json(self.canonical_bytes)

    def __repr__(self):
        return "ReadinessManifest(<operator declarations only>)"


@dataclass(frozen=True)
class ReadinessReport:
    ready_for_read: bool
    ready_for_write: bool
    requested_stage: str | None
    declarations_complete: bool
    requires_runtime_intent_check: bool
    reasons: tuple[str, ...]
    manifest_sha256: str
    config_sha256: str
    evidence_notice: str = NOTICE


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _text(value):
    return (
        type(value) is str
        and 0 < len(value) <= 2048
        and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    )


def _etag(value):
    return type(value) is str and re.fullmatch(r'"[\x21\x23-\x7e]{1,1022}"', value) is not None


def _time(value):
    try:
        return observation_time(value)
    except CRMRefused:
        return None


def _window(issued, expires, now):
    first, last = _time(issued), _time(expires)
    return first is not None and last is not None and first <= now < last


def _proof(document, config_hash, now, *, provider=False):
    if document is None:
        return False
    observed, reviewed = _time(document["observed_at"]), _time(document["reviewed_at"])
    origins = {"provider_http"} if provider else {"provider_http", "operator_reviewed"}
    return (
        document["origin"] in origins
        and _hash(document["sha256"])
        and document["config_sha256"] == config_hash
        and observed is not None
        and reviewed is not None
        and observed <= reviewed <= now
    )


def _schema_compatible(fields, artifact, authority):
    if len(fields) != len(ALLOWED) or {f["name"] for f in fields} != ALLOWED:
        return False
    by_name = {f["name"]: f for f in fields}
    dates = {"FR_Funding_Observed_On__c", "FR_Head_Of_Platform_Start__c"}
    for name, field in by_name.items():
        if name in NUMERIC:
            if not (
                field["kind"] in {"int", "double"}
                and field["scale"] == 0
                and type(field["precision"]) is int
                and 1 <= field["precision"] <= 64
            ):
                return False
        elif name in dates:
            if field["kind"] != "date":
                return False
        elif name == "LastModifiedDate":
            if field["kind"] != "datetime":
                return False
        elif name == "Id":
            if field["kind"] != "id" or field["length"] != 18:
                return False
        elif not (
            field["kind"] in {"string", "picklist", "textarea", "url"}
            and type(field["length"]) is int
            and 1 <= field["length"] <= 1_000_000
        ):
            return False
    lengths = {
        "FR_Output__c": len("DO_NOT_PRIORITIZE"),
        "FR_Operation_Id__c": 64,
        "FR_Decision_Ref__c": len("fr-crm-decision-v1-") + 64,
        "FR_Logic_Version__c": len(artifact["logic_version"]),
    }
    for name in lengths:
        kinds = {"string", "picklist"} if name == "FR_Output__c" else {"string"}
        if by_name[name]["kind"] not in kinds:
            return False
    if any(by_name[name]["length"] < length for name, length in lengths.items()):
        return False
    for name, values in {
        "FR_Output__c": {"PRIORITIZE", "DO_NOT_PRIORITIZE"},
        "FR_Integration_Pressure__c": {"HIGH", "MEDIUM", "LOW"},
    }.items():
        if by_name[name]["kind"] == "picklist" and set(by_name[name]["picklist_values"]) != values:
            return False
    score_bound = sum(max(0, factor["weight"]) for factor in artifact["factors"])
    revision = authority["revision"] if type(authority["revision"]) is int else 1
    intended = authority["intended"]
    if intended is not None and intended["fr_score"] >= 10 ** by_name["FR_Score__c"]["precision"]:
        return False
    return (
        score_bound < 10 ** by_name["FR_Score__c"]["precision"]
        and revision < 10 ** by_name["FR_Operation_Revision__c"]["precision"]
    )


def _send_declaration(authority, connection, runtime_document):
    intended = authority["intended"]
    artifact = runtime_document["artifact_registration"]["payload"]["artifact"]
    if intended is None:
        return False
    return (
        re.fullmatch(r"fr-crm-decision-v1-[0-9a-f]{64}", authority["decision_event_id"] or "")
        is not None
        and all(
            _hash(authority[key])
            for key in (
                "capture_hash",
                "operation_id",
                "intended_outputs_sha256",
                "source_precondition_sha256",
                "artifact_hash",
                "mapping_hash",
            )
        )
        and type(authority["revision"]) is int
        and 0 < authority["revision"] <= MAX_INTEGER
        and type(authority["policy_generation"]) is int
        and authority["policy_generation"] > 0
        and len(authority["record_ids"]) == 1
        and authority["artifact_hash"] == connection["artifact_hash"]
        and authority["evaluator_version"] == connection["evaluator_version"]
        and authority["mapping_hash"] == connection["mapping_hash"]
        and authority["selection_identity"] == runtime_document["selection_identity"]
        and _etag(authority["source_precondition"])
        and authority["source_precondition_sha256"]
        == canonical_hash(authority["source_precondition"])
        and authority["intended_outputs_sha256"] == canonical_hash(intended)
        and 0 <= intended["fr_score"] <= MAX_INTEGER
        and intended["fr_output"] in {"PRIORITIZE", "DO_NOT_PRIORITIZE"}
        and intended["fr_logic_version"] == artifact["logic_version"]
        and intended["fr_decision_ref"] == authority["decision_event_id"]
        and intended["fr_op_id"] == authority["operation_id"]
        and intended["fr_op_rev"] == authority["revision"]
    )


def _guard_qualified(guard, proof, connection, config_hash, now):
    return (
        guard is not None
        and _proof(proof, config_hash, now, provider=True)
        and guard["object_type"] == "Account"
        and guard["api_version"] == connection["api_version"]
        and sorted(guard["selected_fields"]) == list(SELECTED_FIELDS)
        and _etag(guard["captured_etag"])
        and _etag(guard["stale_etag"])
        and guard["captured_etag"] != guard["stale_etag"]
        and guard["completed_status"] == 204
        and guard["response_complete"] is True
        and guard["outputs_match"] is True
        and guard["stale_status"] == 412
        and guard["stale_response_complete"] is True
        and _hash(guard["stale_authority_sha256"])
        and _hash(guard["success_operation_id"])
        and _hash(guard["stale_operation_id"])
        and guard["success_operation_id"] != guard["stale_operation_id"]
    )


def validate_readiness(manifest, config, *, now):
    require(
        type(manifest) is ReadinessManifest and type(config) is CRMRuntimeConfig,
        "invalid_readiness_input",
    )
    require(type(now) is datetime and now.utcoffset() is not None, "invalid_readiness_clock")
    now = now.astimezone(UTC)
    d, cfg = manifest.document, config.document
    connection, config_hash = cfg["connection"], config.binding
    artifact = cfg["artifact_registration"]["payload"]["artifact"]
    authority, reasons = d["authority"], set()
    stage = authority["stage"]
    if not _window(d["issued_at"], d["expires_at"], now):
        reasons.add("manifest_time_unqualified")
    expected = {
        **{
            key: connection[key]
            for key in (
                *CONNECTION_TEXT,
                "record_ids",
                "code_secret_required",
                "refresh_secret_required",
            )
        },
        **{key: cfg[key] for key in RUNTIME_TEXT},
        "config_sha256": config_hash,
    }
    if d["binding"] != expected:
        reasons.add("config_binding_mismatch")
    if d["environment"] != dict(
        edition="developer_edition", production=False, fictional_records_only=True
    ):
        reasons.add("isolated_fictional_environment_unqualified")
    oauth = d["oauth"]
    if (
        oauth["client_type"] not in {"public", "confidential"}
        or sorted(oauth["scopes"]) != ["api", "refresh_token"]
        or (
            oauth["client_type"] == "public"
            and (connection["code_secret_required"] or connection["refresh_secret_required"])
        )
    ):
        reasons.add("oauth_policy_unqualified")
    if connection["api_version"] not in d["advertised_api_versions"]:
        reasons.add("api_version_unqualified")
    if d["initial_write_policy_enabled"] is not False:
        reasons.add("initial_write_policy_not_disabled")
    if not _schema_compatible(d["field_schema"], artifact, authority):
        reasons.add("field_schema_unqualified")
    permissions = d["permissions"]
    if (
        permissions["user_id"] != connection["expected_user_id"]
        or permissions["account_read"] is not True
        or sorted(permissions["read_fields"]) != sorted(ALLOWED)
        or sorted(permissions["write_fields"]) != sorted(OUTPUTS.values())
    ):
        reasons.add("permissions_unqualified")
    n8n = d["n8n"]
    if (
        not _text(n8n["runtime_id"])
        or not _text(n8n["version"])
        or n8n["version"] != n8n["expected_version"]
        or n8n["from_runtime_id"] != n8n["runtime_id"]
        or n8n["to_origin"] != cfg["ingress_origin"]
        or n8n["reachable"] is not True
        or n8n["interval_seconds"] != 60
        or n8n["workflow_sha256"] != canonical_hash(n8n_poll_workflow())
    ):
        reasons.add("n8n_runtime_network_unqualified")
    for key in (
        "environment",
        "api_versions",
        "oauth_policy",
        "field_schema",
        "permissions",
        "n8n",
    ):
        if not _proof(d["evidence"][key], config_hash, now):
            reasons.add("evidence_" + key + "_unqualified")
    for key in ("identity", "read"):
        if d["evidence"][key] is not None and not _proof(
            d["evidence"][key], config_hash, now, provider=True
        ):
            reasons.add("evidence_" + key + "_unqualified")
    if (
        stage not in STAGES
        or not _window(authority["issued_at"], authority["expires_at"], now)
        or not _hash(authority["reviewed_request_sha256"])
        or authority["config_sha256"] != config_hash
        or authority["expected_instance_id"] != connection["expected_instance_id"]
        or not authority["record_ids"]
        or len(authority["record_ids"]) != len(set(authority["record_ids"]))
        or not set(authority["record_ids"]) <= set(connection["record_ids"])
    ):
        reasons.add("requested_action_unqualified")
    common_reasons = set(reasons)
    needs_intent = stage in SEND_STAGES
    if needs_intent:
        if permissions["account_update"] is not True or not _send_declaration(
            authority, connection, cfg
        ):
            reasons.add("send_declaration_unqualified")
        for key in ("identity", "read"):
            if not _proof(d["evidence"][key], config_hash, now, provider=True):
                reasons.add("evidence_" + key + "_unqualified")
    elif any(
        authority[key] is not None
        for key in (
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
        )
    ):
        reasons.add("unexpected_send_declaration")
    guard_ok = _guard_qualified(d["guard"], d["evidence"]["guard"], connection, config_hash, now)
    if stage == "ordinary_send" and not guard_ok:
        reasons.add("conditional_guard_unqualified")
    complete = not reasons
    if stage == "qualification_send" and not guard_ok:
        reasons.add("conditional_guard_unqualified")
    return ReadinessReport(
        ready_for_read=not common_reasons,
        ready_for_write=complete and stage == "ordinary_send" and guard_ok,
        requested_stage=stage,
        declarations_complete=complete,
        requires_runtime_intent_check=needs_intent,
        reasons=tuple(sorted(reasons)),
        manifest_sha256=canonical_hash(d),
        config_sha256=config_hash,
    )
