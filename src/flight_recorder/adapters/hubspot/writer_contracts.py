"""Immutable local-fake writer values. No caller-provided scoring or transport."""

from dataclasses import dataclass

from flight_recorder.adapters.hubspot.contracts import strict_json_loads
from flight_recorder.collector.canonical import canonical_bytes, canonical_text
from flight_recorder.collector.schema import OperationIntent


class WriteRefused(ValueError):
    """A named refusal/integrity failure; never a retry or repair authorization."""


def require(condition, reason):
    if not condition:
        raise WriteRefused(reason)


@dataclass(frozen=True)
class WritePolicy:
    enabled: bool = False
    allowlist: frozenset[str] = frozenset()

    def __post_init__(self):
        require(
            type(self.enabled) is bool and type(self.allowlist) is frozenset, "invalid_write_policy"
        )
        for key in self.allowlist:
            require(type(key) is str, "invalid_allowlist_company")
            try:
                pair = strict_json_loads(key.encode())
                require(
                    type(pair) is list and len(pair) == 2 and canonical_text(pair) == key,
                    "invalid_allowlist_company",
                )
                require(
                    all(type(x) is int or (type(x) is str and bool(x)) for x in pair),
                    "invalid_allowlist_company",
                )
            except (TypeError, ValueError) as exc:
                raise WriteRefused("invalid_allowlist_company") from exc


@dataclass(frozen=True)
class WriteIntent:
    canonical_bytes: bytes

    def __post_init__(self):
        require(type(self.canonical_bytes) is bytes, "immutable_intent_bytes_required")
        _ = self.document

    @classmethod
    def from_document(cls, document):
        return cls(canonical_bytes(document))

    @property
    def document(self):
        try:
            raw = strict_json_loads(self.canonical_bytes)
            normalized = OperationIntent.model_validate_json(
                self.canonical_bytes, strict=True
            ).model_dump(mode="json")
            require(
                canonical_bytes(raw) == self.canonical_bytes
                and canonical_bytes(normalized) == self.canonical_bytes,
                "noncanonical_write_intent",
            )
            return normalized
        except (TypeError, ValueError) as exc:
            raise WriteRefused("invalid_write_intent") from exc


@dataclass(frozen=True)
class WriteProgress:
    operation_id: str
    state: str
    reason: str


@dataclass(frozen=True)
class FakeObservation:
    properties_bytes: bytes
    settled_operation_ids: tuple[str, ...]
    pending_operation_ids: tuple[str, ...]
    not_applied_operation_ids: tuple[str, ...]

    @property
    def properties(self):
        return strict_json_loads(self.properties_bytes)
