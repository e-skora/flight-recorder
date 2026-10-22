"""Immutable local write facts and one-use process admission; never provider attestation."""

import json
import re
import threading
from dataclasses import dataclass, fields
from typing import ClassVar

from flight_recorder.adapters.crm.contracts import CRMRefused, CRMTarget, _salesforce_id, require
from flight_recorder.adapters.crm.mapping import MAX_INTEGER, OUTPUTS
from flight_recorder.collector.canonical import canonical_bytes, canonical_text
from flight_recorder.collector.crm_operations import CRMWriteIntent as IntentSchema
from flight_recorder.collector.crm_operations import OutputRead, ResponseReceipt

ORIGINS = frozenset({"fixture", "injected_transport", "provider_http"})


@dataclass(frozen=True, repr=False)
class _CanonicalFact:
    canonical_bytes: bytes
    _model: ClassVar[type]

    def __post_init__(self):
        try:
            require(type(self.canonical_bytes) is bytes, "invalid_write_fact")
            model = self._model.model_validate_json(self.canonical_bytes, strict=True)
            require(
                canonical_bytes(model.model_dump(mode="json")) == self.canonical_bytes,
                "invalid_write_fact",
            )
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_write_fact") from None

    @classmethod
    def from_document(cls, document, **kwargs):
        try:
            model = cls._model.model_validate_json(canonical_bytes(document), strict=True)
            return cls(canonical_bytes(model.model_dump(mode="json")), **kwargs)
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_write_fact") from None

    @property
    def document(self):
        return json.loads(self.canonical_bytes)

    def __repr__(self):
        return f"{type(self).__name__}(<immutable write fact>)"


@dataclass(frozen=True, repr=False)
class CRMWriteIntent(_CanonicalFact):
    _model = IntentSchema

    @property
    def operation_id(self):
        return self.document["operation_id"]


@dataclass(frozen=True, repr=False)
class ProviderResponse(_CanonicalFact):
    observation_origin: str
    _model = ResponseReceipt

    def __post_init__(self):
        super().__post_init__()
        require(
            type(self.observation_origin) is str and self.observation_origin in ORIGINS,
            "invalid_write_origin",
        )


@dataclass(frozen=True, repr=False)
class OutputObservation(_CanonicalFact):
    observation_origin: str
    _model = OutputRead

    def __post_init__(self):
        super().__post_init__()
        require(
            type(self.observation_origin) is str and self.observation_origin in ORIGINS,
            "invalid_write_origin",
        )


def _text(value):
    require(
        type(value) is str
        and 0 < len(value) <= 256
        and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
        "invalid_write_policy",
    )
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CRMRefused("invalid_write_policy") from None


@dataclass(frozen=True)
class CRMWritePolicy:
    enabled: bool
    connection_id: str
    expected_instance_id: str
    target_keys: tuple[str, ...]
    output_fields: tuple[str, ...]
    mapping_hash: str
    selection_identity: str
    generation: int

    def __post_init__(self):
        require(
            type(self.enabled) is bool
            and type(self.generation) is int
            and 0 < self.generation <= MAX_INTEGER,
            "invalid_write_policy",
        )
        for value in (
            self.connection_id,
            self.expected_instance_id,
            self.mapping_hash,
            self.selection_identity,
        ):
            _text(value)
        _salesforce_id(self.expected_instance_id, "00D")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", self.mapping_hash)), "invalid_write_policy")
        require(
            type(self.target_keys) is tuple
            and len(self.target_keys) <= 3
            and all(type(k) is str for k in self.target_keys)
            and self.target_keys == tuple(sorted(set(self.target_keys)))
            and (not self.enabled or bool(self.target_keys)),
            "invalid_write_policy",
        )
        for key in self.target_keys:
            try:
                target = CRMTarget(**json.loads(key))
                require(
                    target.key == key
                    and target.provider == "salesforce"
                    and target.instance_id == self.expected_instance_id,
                    "invalid_write_policy",
                )
            except (ValueError, TypeError, RecursionError):
                raise CRMRefused("invalid_write_policy") from None
        require(
            type(self.output_fields) is tuple
            and self.output_fields == tuple(sorted(OUTPUTS.values())),
            "invalid_write_policy",
        )

    @property
    def document(self):
        return dict(
            enabled=self.enabled,
            connection_id=self.connection_id,
            expected_instance_id=self.expected_instance_id,
            target_keys=list(self.target_keys),
            output_fields=list(self.output_fields),
            mapping_hash=self.mapping_hash,
            selection_identity=self.selection_identity,
            generation=self.generation,
        )

    @property
    def canonical_bytes(self):
        return canonical_bytes(self.document)

    @classmethod
    def from_document(cls, document):
        require(
            type(document) is dict and set(document) == {f.name for f in fields(cls)},
            "invalid_write_policy",
        )
        d = dict(document)
        require(
            type(d["target_keys"]) is list and type(d["output_fields"]) is list,
            "invalid_write_policy",
        )
        d["target_keys"], d["output_fields"] = tuple(d["target_keys"]), tuple(d["output_fields"])
        result = cls(**d)
        require(canonical_text(result.document) == canonical_text(document), "invalid_write_policy")
        return result


@dataclass(frozen=True)
class PolicyReceipt:
    generation: int
    policy_hash: str
    enabled: bool


@dataclass(frozen=True)
class WriteProgress:
    operation_id: str
    state: str
    reason: str
    admitted: bool
    attempted: bool
    held: bool
    observation_sequence: int


class _SendAdmission:
    """Ephemeral capability: its fresh domain grant cannot be serialized or reused."""

    def __init__(self, intent, client, credential_generation, guard):
        require(type(intent) is CRMWriteIntent and callable(guard), "invalid_send_admission")
        require(
            type(credential_generation) is int and credential_generation > 0,
            "invalid_send_admission",
        )
        self._intent_bytes = intent.canonical_bytes
        self._client = client
        self._credential_generation = credential_generation
        self._guard = guard
        self._consumed = False
        self._mutex = threading.Lock()

    def consume(self, intent, client, *, credential_generation):
        with self._mutex:
            require(not self._consumed, "send_admission_consumed")
            # Refusal also consumes a grant. No branch may turn it into a retry ticket.
            self._consumed = True
            require(
                type(intent) is CRMWriteIntent
                and intent.canonical_bytes == self._intent_bytes
                and client is self._client
                and type(credential_generation) is int
                and credential_generation == self._credential_generation,
                "send_admission_binding_changed",
            )
            self._guard()

    def __reduce__(self):
        raise TypeError("send admission cannot be serialized")

    def discard(self):
        with self._mutex:
            self._consumed = True

    def __repr__(self):
        return "SendAdmission(<one use>)"
