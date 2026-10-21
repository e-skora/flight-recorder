"""Closed CRM capture facts and separately built evidence/decision v3 forms."""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from flight_recorder.adapters.crm.contracts import CRMTarget, require
from flight_recorder.adapters.crm.mapping import FIELDS, MAPPING_HASH, MAX_INTEGER, map_inputs
from flight_recorder.collector.canonical import canonical_hash, canonical_text

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1)]
Positive = Annotated[int, Field(gt=0, le=MAX_INTEGER)]
SQLiteInteger = Annotated[int, Field(ge=-MAX_INTEGER - 1, le=MAX_INTEGER)]


def _utc(value):
    return value.astimezone(UTC)


def _time(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Independent of the legacy envelope module so either import order is safe.
Timestamp = Annotated[
    AwareDatetime, AfterValidator(_utc), PlainSerializer(_time, return_type=str, when_used="json")
]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TargetDocument(Closed):
    provider: Literal["salesforce", "hubspot"]
    instance_id: Text
    object_type: Text
    record_id: Text

    @model_validator(mode="after")
    def _identity(self):
        CRMTarget(**self.model_dump())
        return self


class NumberToken(Closed):
    json_number: Text


class InputEntry(Closed):
    state: Literal["available", "unavailable", "absent"]
    value: int | str | None
    observed_at: str | None
    basis: list[Text] | None


_OBSERVED = (
    "target",
    "connection_id",
    "observation_origin",
    "read_started_at",
    "read_finished_at",
    "source_updated_at",
    "precondition",
    "request_id",
    "response_body_sha256",
    "properties",
    "unavailable_fields",
)
_PROCESSING = (
    "mapping_hash",
    "rule_hash",
    "evaluator_version",
    "selection_identity",
    "adapter_version",
)


def identities(document):
    observation_id = canonical_hash({k: document[k] for k in _OBSERVED})
    logical_key = canonical_hash({"target": document["target"], "observation_id": observation_id})
    processing_version = canonical_hash({k: document[k] for k in _PROCESSING})
    unit_id = canonical_hash(
        dict(
            logical_key=logical_key,
            processing_version=processing_version,
            reprocesses=document["reprocesses"],
        )
    )
    return dict(
        observation_id=observation_id,
        logical_key=logical_key,
        processing_version=processing_version,
        unit_id=unit_id,
    )


class CaptureDocument(Closed):
    format_version: Annotated[int, Field(ge=1, le=1)]
    data_origin: Literal["fictional_records_real_crm"]
    observation_origin: Literal["provider_http", "injected_transport", "fixture"]
    target: TargetDocument
    connection_id: Text
    read_started_at: Timestamp
    read_finished_at: Timestamp
    capture_boundary: Timestamp
    source_updated_at: Timestamp | None
    precondition: Text | None
    request_id: Text | None
    response_body_sha256: Hash
    properties: dict[str, int | str | bool | NumberToken | None]
    unavailable_fields: list[Text]
    inputs: dict[str, InputEntry]
    mapping_hash: Hash
    rule_hash: Hash
    evaluator_version: Text
    selection_identity: Text
    adapter_version: Literal["crm-adapter-v1"]
    capture_sequence: Positive
    ownership_generation: Positive
    reprocesses: Hash | None
    observation_id: Hash
    logical_key: Hash
    processing_version: Hash
    unit_id: Hash

    @model_validator(mode="after")
    def _complete_binding(self):
        d = self.model_dump(mode="json")
        require(
            self.target.provider == "salesforce" and self.mapping_hash == MAPPING_HASH,
            "invalid_crm_mapping",
        )
        target = CRMTarget(**d["target"])
        require(d["properties"].get("Id") == target.record_id, "invalid_crm_capture")
        require(
            self.read_started_at <= self.read_finished_at <= self.capture_boundary,
            "invalid_crm_capture",
        )
        require(
            self.source_updated_at is not None and self.source_updated_at <= self.capture_boundary,
            "invalid_crm_capture",
        )
        raw_time = d["properties"].get("LastModifiedDate")
        require(type(raw_time) is str, "invalid_crm_capture")
        try:
            parsed = datetime.fromisoformat(raw_time)
            require(
                parsed.tzinfo is not None
                and parsed.utcoffset() is not None
                and parsed == self.source_updated_at,
                "invalid_crm_capture",
            )
        except ValueError as exc:
            raise ValueError("invalid_crm_capture") from exc
        require(
            self.precondition is None
            or bool(re.fullmatch(r"[\x20-\x7e]{1,1024}", self.precondition)),
            "invalid_crm_capture",
        )
        mapped = map_inputs(d["properties"], d["unavailable_fields"])
        require(canonical_text(mapped) == canonical_text(d["inputs"]), "invalid_crm_capture")
        for key, entry in mapped.items():
            if entry["state"] != "available":
                continue
            dated = entry["observed_at"]
            if key == "head_of_platform_start_date":
                dated = entry["value"]
            require(
                dated is None or date.fromisoformat(dated) <= self.capture_boundary.date(),
                "invalid_crm_capture",
            )
        require(all(d[k] == v for k, v in identities(d).items()), "invalid_crm_capture")
        return self


def input_digest(capture):
    return canonical_hash({k: capture[k] for k in ("mapping_hash", "inputs")})


def available_keys(capture):
    return sorted(k for k, entry in capture["inputs"].items() if entry["state"] == "available")


def observation_body(account_ref, capture, item):
    semantic = {
        k: v
        for k, v in item.items()
        if k not in {"evidence_type", "evidence_version_id", "supersedes_evidence_version_id"}
    }
    return dict(
        account_ref=account_ref,
        source="salesforce-test-account",
        target=capture["target"],
        observation_id=capture["observation_id"],
        mapping_hash=capture["mapping_hash"],
        evidence_type=item["evidence_type"],
        semantic=semantic,
        source_fields=FIELDS[item["evidence_type"]],
        read_started_at=capture["read_started_at"],
        read_finished_at=capture["read_finished_at"],
        source_updated_at=capture["source_updated_at"],
        eligible_at=max(capture["read_finished_at"], capture["source_updated_at"]),
        observation_origin=capture["observation_origin"],
        response_body_sha256=capture["response_body_sha256"],
        precondition=capture["precondition"],
        supersedes_evidence_version_id=item["supersedes_evidence_version_id"],
    )


def bound_item(account_ref, capture, key, supersedes=None):
    entry = capture["inputs"][key]
    item = dict(evidence_type=key, value=entry["value"], supersedes_evidence_version_id=supersedes)
    if key in ("funding_event", "head_of_platform_start_date"):
        item["observed_at"] = entry["observed_at"]
    if key == "verified_integration_pressure":
        item["basis"] = entry["basis"]
    item["evidence_version_id"] = "fr-crm-evidence-version-v1-" + canonical_hash(
        observation_body(account_ref, capture, item)
    )
    return item


def expected_context(account_ref, capture, supersessions):
    result = []
    for key, entry in sorted(capture["inputs"].items()):
        if entry["state"] == "absent":
            continue
        available = entry["state"] == "available"
        item = bound_item(account_ref, capture, key, supersessions.get(key)) if available else None
        result.append(
            dict(
                input_key=key,
                value=entry["value"] if available else None,
                availability="available" if available else "unavailable",
                evidence_version_id=item["evidence_version_id"] if item else None,
            )
        )
    return result


def validate_wire(n):
    p = n["payload"]
    cap = p["capture"]
    require(canonical_hash(cap) == p["capture_hash"], "invalid_crm_capture")
    require(
        n["source"] == "salesforce-test-account"
        and n["account_ref"] == CRMTarget(**cap["target"]).account_ref,
        "invalid_crm_capture",
    )
    is_evidence = n["event_type"] == "evidence.recorded"
    prefix = "fr-crm-evidence-v1-" if is_evidence else "fr-crm-decision-v1-"
    require(n["event_id"] == prefix + cap["unit_id"], "invalid_crm_capture")
    if is_evidence:
        require(n["occurred_at"] == cap["read_finished_at"], "invalid_crm_capture")
        require(
            [x["evidence_type"] for x in p["items"]] == available_keys(cap), "invalid_crm_capture"
        )
        for item in p["items"]:
            expected = bound_item(
                n["account_ref"], cap, item["evidence_type"], item["supersedes_evidence_version_id"]
            )
            require(canonical_text(item) == canonical_text(expected), "invalid_crm_capture")
    else:
        require(
            n["occurred_at"] == p["decision_boundary"] == cap["capture_boundary"]
            and p["workflow_version"] == cap["processing_version"]
            and p["logic_artifact"]["artifact_hash"] == cap["rule_hash"]
            and p["logic_artifact"]["evaluator_version"] == cap["evaluator_version"],
            "invalid_crm_capture",
        )


def build_envelopes():
    """Called after legacy types exist, avoiding a CRM/legacy import cycle."""
    from flight_recorder.collector.schema import (
        ConsumedInput,
        DecisionRecordedEnvelope,
        DecisionRecordedPayload,
        DecisionResult,
        EvidenceItemV2,
        EvidenceRecordedEnvelope,
        EvidenceRecordedPayload,
    )

    class CRMConsumedInput(ConsumedInput):
        contribution: SQLiteInteger

    class CRMDecisionResult(DecisionResult):
        score: SQLiteInteger
        threshold: SQLiteInteger

    class EvidenceRecordedV3Payload(EvidenceRecordedPayload):
        items: list[EvidenceItemV2] = Field(min_length=1)
        capture: CaptureDocument
        capture_hash: Hash

    class DecisionRecordedV3Payload(DecisionRecordedPayload):
        consumed_inputs: list[CRMConsumedInput] = Field(min_length=1)
        result: CRMDecisionResult
        capture: CaptureDocument
        capture_hash: Hash

    class EvidenceRecordedV3Envelope(EvidenceRecordedEnvelope):
        schema_version: Literal["3"]
        source: Literal["salesforce-test-account"]
        payload: EvidenceRecordedV3Payload

        @model_validator(mode="after")
        def _crm_contract(self):
            validate_wire(self.model_dump(mode="json"))
            return self

    class DecisionRecordedV3Envelope(DecisionRecordedEnvelope):
        schema_version: Literal["3"]
        source: Literal["salesforce-test-account"]
        payload: DecisionRecordedV3Payload

        @model_validator(mode="after")
        def _crm_contract(self):
            validate_wire(self.model_dump(mode="json"))
            return self

    return EvidenceRecordedV3Envelope, DecisionRecordedV3Envelope
