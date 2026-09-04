"""Collector envelope schema, version "1".

Every model forbids unknown fields. Acceptance is decided by validating the raw
JSON request body in strict JSON mode (`validate_envelope_json`), so `"184"`
and `184.0` are rejected for integer fields while ISO-8601 strings are still
accepted for timestamps.

Schema v1 numeric rules: counts, contributions, scores, thresholds, windows and
play identifiers are strict integers. Exact monetary quantities are decimal
strings such as `"1.42"`. No schema-v1 field accepts a float.

Timestamp convention (D-010): timezone-aware on input, normalized to UTC, and
serialized as ISO-8601 text ending in `Z` with microsecond precision.
"""

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
    TypeAdapter,
    model_validator,
)

SCHEMA_VERSION = "1"

EVENT_TYPES = (
    "account.discovered",
    "evidence.recorded",
    "decision.recorded",
    "persona.selected",
    "action.recorded",
    "outcome.evaluated",
)


def format_utc(value: datetime) -> str:
    """Render a UTC datetime as ISO-8601 text with microseconds and a `Z`."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


Timestamp = Annotated[
    AwareDatetime,
    AfterValidator(_to_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]

NonEmptyStr = Annotated[str, Field(min_length=1)]
DecimalString = Annotated[str, Field(pattern=r"^-?\d+\.\d{2}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ScalarValue = int | str | bool | None


def _duplicates(keys) -> list[str]:
    """Keys appearing more than once, in first-seen order."""
    seen: dict[str, int] = {}
    for key in keys:
        seen[key] = seen.get(key, 0) + 1
    return [key for key, count in seen.items() if count > 1]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Payloads --------------------------------------------------------------


class AccountDiscoveredPayload(StrictModel):
    name: NonEmptyStr
    domain: NonEmptyStr


class _EvidenceItemBase(StrictModel):
    evidence_version_id: NonEmptyStr


class EmployeeCountEvidence(_EvidenceItemBase):
    evidence_type: Literal["employee_count"]
    value: int


class IndustryEvidence(_EvidenceItemBase):
    evidence_type: Literal["industry"]
    value: NonEmptyStr


class HeadquartersCountryEvidence(_EvidenceItemBase):
    evidence_type: Literal["headquarters_country"]
    value: NonEmptyStr


class FundingEventEvidence(_EvidenceItemBase):
    evidence_type: Literal["funding_event"]
    value: NonEmptyStr
    observed_at: date


class OpenPlatformEngineeringRolesEvidence(_EvidenceItemBase):
    evidence_type: Literal["open_platform_engineering_roles"]
    value: int


class HeadOfPlatformStartDateEvidence(_EvidenceItemBase):
    evidence_type: Literal["head_of_platform_start_date"]
    value: date
    observed_at: date


class VerifiedIntegrationPressureEvidence(_EvidenceItemBase):
    evidence_type: Literal["verified_integration_pressure"]
    value: Literal["LOW", "MEDIUM", "HIGH"]
    basis: list[NonEmptyStr] = Field(min_length=1)


EvidenceItem = Annotated[
    EmployeeCountEvidence
    | IndustryEvidence
    | HeadquartersCountryEvidence
    | FundingEventEvidence
    | OpenPlatformEngineeringRolesEvidence
    | HeadOfPlatformStartDateEvidence
    | VerifiedIntegrationPressureEvidence,
    Field(discriminator="evidence_type"),
]


class EvidenceRecordedPayload(StrictModel):
    items: list[EvidenceItem] = Field(min_length=1)


def _same_scalar(left: ScalarValue, right: ScalarValue) -> bool:
    """Exact scalar equality, including type.

    `bool` is a subclass of `int` in Python, so a plain `==` would treat JSON
    `true` and `1` as the same preserved value. A decision's preserved context
    must not blur them.
    """
    return type(left) is type(right) and left == right


class HistoricalContextEntry(StrictModel):
    input_key: NonEmptyStr
    value: ScalarValue
    availability: Literal["available", "unavailable"]
    evidence_version_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _availability_is_coherent(self) -> "HistoricalContextEntry":
        """INV-03/INV-09: an unavailable input carries no value and no evidence."""
        if self.availability == "available" and self.evidence_version_id is None:
            raise ValueError(
                f"historical_context[{self.input_key!r}]: an available input must "
                "reference an evidence_version_id"
            )
        if self.availability == "unavailable":
            if self.evidence_version_id is not None:
                raise ValueError(
                    f"historical_context[{self.input_key!r}]: an unavailable input must "
                    "not reference any evidence"
                )
            if self.value is not None:
                raise ValueError(
                    f"historical_context[{self.input_key!r}]: an unavailable input must "
                    "have a null value"
                )
        return self


class ConsumedInput(StrictModel):
    input_key: NonEmptyStr
    value: ScalarValue
    evidence_version_id: NonEmptyStr
    contribution: int


class LogicArtifactRef(StrictModel):
    logic_version: NonEmptyStr
    artifact_id: NonEmptyStr
    artifact_hash: Sha256Hex
    evaluator_version: NonEmptyStr


class DecisionResult(StrictModel):
    score: int
    threshold: int
    output: Literal["PRIORITIZE", "DO_NOT_PRIORITIZE"]


class DecisionRecordedPayload(StrictModel):
    """A recorded prioritization decision.

    The validators below reject envelopes whose own content is contradictory,
    so that `H(d)` and `U(d)` are unambiguous the moment the event is stored.
    Whether a referenced evidence version exists in an earlier stored event is
    a cross-event question and is not decided here.
    """

    decision_class: Literal["account_prioritization"]
    decision_boundary: Timestamp
    workflow_version: NonEmptyStr
    historical_context: list[HistoricalContextEntry] = Field(min_length=1)
    consumed_inputs: list[ConsumedInput] = Field(min_length=1)
    logic_artifact: LogicArtifactRef
    result: DecisionResult
    explanation: str | None = None

    @model_validator(mode="after")
    def _context_keys_are_unique(self) -> "DecisionRecordedPayload":
        duplicates = _duplicates(entry.input_key for entry in self.historical_context)
        if duplicates:
            raise ValueError(
                "historical_context has more than one entry for input_key(s): "
                + ", ".join(repr(key) for key in duplicates)
            )
        return self

    @model_validator(mode="after")
    def _consumed_keys_are_unique(self) -> "DecisionRecordedPayload":
        duplicates = _duplicates(consumed.input_key for consumed in self.consumed_inputs)
        if duplicates:
            raise ValueError(
                "consumed_inputs has more than one entry for input_key(s): "
                + ", ".join(repr(key) for key in duplicates)
            )
        return self

    @model_validator(mode="after")
    def _consumed_inputs_agree_with_context(self) -> "DecisionRecordedPayload":
        """INV-03/INV-04: every consumed input is an available context entry, unchanged."""
        context = {entry.input_key: entry for entry in self.historical_context}
        for consumed in self.consumed_inputs:
            entry = context.get(consumed.input_key)
            if entry is None:
                raise ValueError(
                    f"consumed_inputs[{consumed.input_key!r}]: no historical_context entry "
                    "with that input_key"
                )
            if entry.availability != "available":
                raise ValueError(
                    f"consumed_inputs[{consumed.input_key!r}]: the historical_context entry "
                    f"is {entry.availability}, so it cannot have been consumed"
                )
            if not _same_scalar(consumed.value, entry.value):
                raise ValueError(
                    f"consumed_inputs[{consumed.input_key!r}]: value {consumed.value!r} does "
                    f"not equal the historical_context value {entry.value!r}"
                )
            if consumed.evidence_version_id != entry.evidence_version_id:
                raise ValueError(
                    f"consumed_inputs[{consumed.input_key!r}]: evidence_version_id "
                    f"{consumed.evidence_version_id!r} does not equal the historical_context "
                    f"evidence_version_id {entry.evidence_version_id!r}"
                )
        return self


class PersonaSelectedPayload(StrictModel):
    persona: NonEmptyStr
    decision_event_id: NonEmptyStr
    explanation: str | None = None


class ActionRecordedPayload(StrictModel):
    action_type: Literal["outbound_play"]
    play_id: int
    target_persona: NonEmptyStr
    status: Literal["sent", "completed", "failed"]
    cost: DecimalString
    currency: Literal["USD"]
    decision_event_id: NonEmptyStr


class OutcomeEvaluatedPayload(StrictModel):
    window_days: int
    reply: bool
    meeting: bool
    opportunity: bool
    action_event_id: NonEmptyStr


# --- Envelope --------------------------------------------------------------


class _EnvelopeBase(StrictModel):
    schema_version: Literal["1"]
    event_id: NonEmptyStr
    source: NonEmptyStr
    account_ref: NonEmptyStr
    occurred_at: Timestamp
    recorded_at: Timestamp

    @model_validator(mode="after")
    def _recorded_not_before_occurred(self) -> "_EnvelopeBase":
        if self.recorded_at < self.occurred_at:
            raise ValueError("recorded_at must not be earlier than occurred_at")
        return self


class AccountDiscoveredEnvelope(_EnvelopeBase):
    event_type: Literal["account.discovered"]
    payload: AccountDiscoveredPayload


class EvidenceRecordedEnvelope(_EnvelopeBase):
    event_type: Literal["evidence.recorded"]
    payload: EvidenceRecordedPayload


class DecisionRecordedEnvelope(_EnvelopeBase):
    event_type: Literal["decision.recorded"]
    payload: DecisionRecordedPayload

    @model_validator(mode="after")
    def _boundary_is_the_occurrence_instant(self) -> "DecisionRecordedEnvelope":
        """INV-02: `T(d)` is the instant the decision occurred, in one representation."""
        if self.payload.decision_boundary != self.occurred_at:
            raise ValueError(
                f"payload.decision_boundary {format_utc(self.payload.decision_boundary)} "
                f"must be the same instant as occurred_at {format_utc(self.occurred_at)}"
            )
        return self


class PersonaSelectedEnvelope(_EnvelopeBase):
    event_type: Literal["persona.selected"]
    payload: PersonaSelectedPayload


class ActionRecordedEnvelope(_EnvelopeBase):
    event_type: Literal["action.recorded"]
    payload: ActionRecordedPayload


class OutcomeEvaluatedEnvelope(_EnvelopeBase):
    event_type: Literal["outcome.evaluated"]
    payload: OutcomeEvaluatedPayload


Envelope = Annotated[
    AccountDiscoveredEnvelope
    | EvidenceRecordedEnvelope
    | DecisionRecordedEnvelope
    | PersonaSelectedEnvelope
    | ActionRecordedEnvelope
    | OutcomeEvaluatedEnvelope,
    Field(discriminator="event_type"),
]

EnvelopeAdapter: TypeAdapter = TypeAdapter(Envelope)

AnyEnvelope = (
    AccountDiscoveredEnvelope
    | EvidenceRecordedEnvelope
    | DecisionRecordedEnvelope
    | PersonaSelectedEnvelope
    | ActionRecordedEnvelope
    | OutcomeEvaluatedEnvelope
)


def validate_envelope_json(body: bytes | str) -> AnyEnvelope:
    """Validate a raw JSON body strictly. Raises pydantic.ValidationError."""
    return EnvelopeAdapter.validate_json(body, strict=True)


def envelope_schema() -> dict:
    """JSON schema for the envelope, with component references for OpenAPI."""
    return EnvelopeAdapter.json_schema(
        ref_template="#/components/schemas/{model}", mode="validation"
    )


_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def is_stored_timestamp(text: str) -> bool:
    """True when `text` is in the persisted timestamp form."""
    return bool(_Z_RE.match(text))
