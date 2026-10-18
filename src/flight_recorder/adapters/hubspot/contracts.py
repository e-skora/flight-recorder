"""Frozen operational types for FR-V2-01 local synthetic capture.

Everything here is operational: a `FrozenCapture` is the adapter's immutable
record of what it observed, not a collector envelope, not domain provenance and
not a decision. Task 2 defines the domain forms separately.

Source parsing is strict: the supplied payload must be exactly what the retained
raw bytes say, with duplicate keys, non-finite numbers and bool/number
disagreement refused, so the logical identity never drifts from the bytes kept.
"""

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from flight_recorder.adapters.hubspot import identity
from flight_recorder.collector.canonical import canonical_bytes as _canonical_bytes
from flight_recorder.collector.canonical import canonical_hash, canonical_text

#: Source namespace root for every notification this adapter accepts.
SOURCE = "hubspot"

#: The only notification kind in scope: classic company property changes.
SUPPORTED_SUBSCRIPTION_TYPE = "company.propertyChange"

_REQUIRED_KEYS = (
    "portalId",
    "appId",
    "subscriptionId",
    "objectId",
    "propertyName",
    "propertyValue",
    "occurredAt",
    "eventId",
)
_TYPE_ALIASES = ("subscriptionType", "eventType")
_OPTIONAL_SEMANTIC_KEYS = ("changeSource",)
#: Delivery-attempt metadata: retained in the raw bytes, excluded from identity.
ATTEMPT_METADATA_KEYS = ("attemptNumber", "messageId", "messageType")
_KNOWN_KEYS = frozenset(
    _REQUIRED_KEYS + _TYPE_ALIASES + _OPTIONAL_SEMANTIC_KEYS + ATTEMPT_METADATA_KEYS
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class CaptureRefused(ValueError):
    """A visible operational refusal; nothing is captured for the refused attempt."""


# --- Strict source parsing ---------------------------------------------------


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"source JSON repeats key {key!r}")
        result[key] = value
    return result


def _refuse_constant(name):
    raise ValueError(f"source JSON contains non-finite number {name}")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"source JSON number {text} is not finite")
    return value


def strict_json_loads(raw: bytes):
    """Parse JSON, refusing duplicate keys and non-finite or overflowing numbers."""
    return json.loads(
        raw,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_refuse_constant,
        parse_float=_finite_float,
    )


def typed_equal(left, right) -> bool:
    """JSON equality that never collapses types (`False` != `0`, `1` != `1.0`)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(typed_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(typed_equal, left, right))
    return left == right


def _identifier(name: str, value):
    if isinstance(value, bool) or not (
        isinstance(value, int) or (isinstance(value, str) and value)
    ):
        raise ValueError(f"source field {name} must be an integer or nonempty text")
    return value


@dataclass(frozen=True)
class SourceNotification:
    """A normalized source notification with its exact raw bytes retained."""

    raw_body: bytes
    #: Canonical JSON text of the normalized namespace and semantic body.
    semantic_json: str

    @classmethod
    def from_payload(cls, payload: dict, *, raw_body: bytes) -> "SourceNotification":
        if not isinstance(payload, dict):
            raise ValueError("source payload must be a JSON object")
        if not isinstance(raw_body, bytes | bytearray):
            raise ValueError("raw_body must be bytes")
        raw_body = bytes(raw_body)
        try:
            parsed = strict_json_loads(raw_body)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(f"raw source body is not strict JSON: {error}") from error
        if not typed_equal(parsed, payload):
            raise ValueError("raw source body does not describe the supplied payload")

        unknown = sorted(set(payload) - _KNOWN_KEYS)
        if unknown:
            raise ValueError(f"unsupported semantic source fields: {unknown}")
        missing = [key for key in _REQUIRED_KEYS if key not in payload]
        if missing:
            raise ValueError(f"source notification lacks identity fields: {missing}")
        aliases = [payload[key] for key in _TYPE_ALIASES if key in payload]
        if not aliases:
            raise ValueError("source notification lacks subscriptionType/eventType")
        if any(not typed_equal(alias, aliases[0]) for alias in aliases):
            raise ValueError("subscriptionType and eventType conflict")
        subscription_type = aliases[0]
        if subscription_type != SUPPORTED_SUBSCRIPTION_TYPE:
            raise ValueError(f"unsupported subscription type {subscription_type!r}")

        property_name = payload["propertyName"]
        if not isinstance(property_name, str) or not property_name:
            raise ValueError("source field propertyName must be nonempty text")
        occurred_at = payload["occurredAt"]
        if isinstance(occurred_at, bool) or not isinstance(occurred_at, int):
            raise ValueError("source field occurredAt must be integer epoch milliseconds")

        body = {
            "namespace": {
                "source": SOURCE,
                "portal_id": _identifier("portalId", payload["portalId"]),
                "app_id": _identifier("appId", payload["appId"]),
                "subscription_id": _identifier("subscriptionId", payload["subscriptionId"]),
                "subscription_type": subscription_type,
            },
            "object_id": _identifier("objectId", payload["objectId"]),
            "property_name": property_name,
            # Any JSON value, preserved with its type; null stays distinct from absent.
            "property_value": payload["propertyValue"],
            "occurred_at": occurred_at,
            "event_id": _identifier("eventId", payload["eventId"]),
        }
        if "changeSource" in payload:
            change_source = payload["changeSource"]
            if change_source is not None and not isinstance(change_source, str):
                raise ValueError("source field changeSource must be text or null")
            body["change_source"] = change_source
        return cls(raw_body=raw_body, semantic_json=canonical_text(body))

    def semantic_body(self) -> dict:
        """A fresh copy of the normalized namespace and semantic body."""
        return json.loads(self.semantic_json)

    @property
    def portal_id(self):
        return self.semantic_body()["namespace"]["portal_id"]

    @property
    def company_id(self):
        return self.semantic_body()["object_id"]


# --- Processing identity -------------------------------------------------------


def _nonempty_text(name: str, value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


@dataclass(frozen=True)
class ProcessingUnit:
    """One logical source event under one processing version.

    `unit_id` and `processing_version` derive only from the logical key and the
    three processing identities. `reprocesses` is lineage to an earlier unit; it
    never alters that unit.
    """

    notification: SourceNotification
    mapping_identity: str
    selection_identity: str
    adapter_identity: str
    reprocesses: str | None = None
    logical_key: str = field(init=False)
    processing_version: str = field(init=False)
    unit_id: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.notification, SourceNotification):
            raise ValueError("notification must be a SourceNotification")
        _nonempty_text("mapping_identity", self.mapping_identity)
        _nonempty_text("selection_identity", self.selection_identity)
        _nonempty_text("adapter_identity", self.adapter_identity)
        if self.reprocesses is not None:
            _nonempty_text("reprocesses", self.reprocesses)
        key = identity.logical_key(self.notification)
        version = canonical_hash(
            {
                "mapping_identity": self.mapping_identity,
                "selection_identity": self.selection_identity,
                "adapter_identity": self.adapter_identity,
            }
        )
        unit_id = canonical_hash({"logical_key": key, "processing_version": version})
        if self.reprocesses == unit_id:
            raise ValueError("a processing unit cannot reprocess itself")
        object.__setattr__(self, "logical_key", key)
        object.__setattr__(self, "processing_version", version)
        object.__setattr__(self, "unit_id", unit_id)

    @property
    def company_key(self) -> str:
        """Canonical text of `(portal_id, company_id)`, types preserved."""
        body = self.notification.semantic_body()
        return canonical_text([body["namespace"]["portal_id"], body["object_id"]])


@dataclass(frozen=True)
class RuleIdentity:
    """Exact owned rule identity: content hash plus evaluator. No label stands in."""

    artifact_hash: str
    evaluator_version: str

    def __post_init__(self):
        if not isinstance(self.artifact_hash, str) or not _SHA256_HEX.fullmatch(self.artifact_hash):
            raise ValueError("artifact_hash must be a lowercase SHA-256 hex digest")
        _nonempty_text("evaluator_version", self.evaluator_version)


class ReadResult:
    """A supplied property-read result, copied on entry and on every access.

    A property missing from `properties` is absent. A property named in
    `unavailable` is explicitly unavailable. A property named in `ignored` is
    available but not used by owned rules. Source timestamps are never
    manufactured: `available_at` holds only what the caller supplied.
    """

    __slots__ = ("_properties", "_available_at", "_ignored", "_unavailable")

    def __init__(self, properties, *, available_at=None, ignored=(), unavailable=()):
        if not isinstance(properties, Mapping):
            raise CaptureRefused("invalid read result: properties must be a mapping")
        if any(not isinstance(key, str) for key in properties):
            raise CaptureRefused("invalid read result: property names must be text")
        if available_at is not None and not isinstance(available_at, Mapping):
            raise CaptureRefused("invalid read result: available_at must be a mapping")
        self._properties = copy.deepcopy(dict(properties))
        self._available_at = dict(available_at or {})
        self._ignored = _name_tuple("ignored", ignored)
        self._unavailable = _name_tuple("unavailable", unavailable)

    @property
    def properties(self) -> dict:
        return copy.deepcopy(self._properties)

    @property
    def available_at(self) -> dict:
        return dict(self._available_at)

    @property
    def ignored(self) -> tuple:
        return self._ignored

    @property
    def unavailable(self) -> tuple:
        return self._unavailable


def _name_tuple(name: str, values) -> tuple:
    if isinstance(values, str | bytes):
        raise CaptureRefused(f"invalid read result: {name} must be a collection of names")
    names = tuple(values)
    if any(not isinstance(value, str) for value in names) or len(set(names)) != len(names):
        raise CaptureRefused(f"invalid read result: {name} must hold distinct property names")
    return tuple(sorted(names))


@dataclass(frozen=True)
class CaptureOwnership:
    """Per-company capture ownership carrying a monotonic fencing generation."""

    company_key: str
    unit_id: str
    generation: int


@dataclass(frozen=True)
class FrozenCapture:
    """An immutable operational capture: canonical bytes, decoded fresh on access."""

    canonical_bytes: bytes

    @classmethod
    def from_document(cls, document: dict) -> "FrozenCapture":
        try:
            return cls(_canonical_bytes(document))
        except (TypeError, ValueError) as error:
            raise CaptureRefused(f"capture is not canonical JSON: {error}") from error

    @property
    def document(self) -> dict:
        return json.loads(self.canonical_bytes)
