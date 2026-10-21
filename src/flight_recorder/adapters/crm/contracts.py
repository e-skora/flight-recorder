"""Canonical target identities and immutable operational ownership values."""

import re
from dataclasses import dataclass

from flight_recorder.collector.canonical import canonical_hash, canonical_text

_SF_ID = re.compile(r"[A-Za-z0-9]{18}", re.ASCII)
_DECIMAL_ID = re.compile(r"[1-9][0-9]*", re.ASCII)
_UNIT_ID = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CHECKSUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


class CRMRefused(ValueError):
    """A safe named refusal, without credentials, raw responses or hidden repair."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise CRMRefused(reason)


def _salesforce_id(value: str, prefix: str) -> None:
    require(bool(_SF_ID.fullmatch(value)) and value.startswith(prefix), "invalid_salesforce_id")
    base = value[:15]
    expected = "".join(
        _CHECKSUM[sum(1 << j for j, char in enumerate(base[i : i + 5]) if "A" <= char <= "Z")]
        for i in (0, 5, 10)
    )
    require(value[15:] == expected, "invalid_salesforce_id")


def validate_unit_id(value: str) -> None:
    require(type(value) is str and bool(_UNIT_ID.fullmatch(value)), "invalid_unit_id")


@dataclass(frozen=True)
class CRMTarget:
    provider: str
    instance_id: str
    object_type: str
    record_id: str

    def __post_init__(self):
        require(
            all(
                type(v) is str and v
                for v in (self.provider, self.instance_id, self.object_type, self.record_id)
            ),
            "invalid_target",
        )
        if self.provider == "salesforce":
            require(self.object_type == "Account", "invalid_target")
            _salesforce_id(self.instance_id, "00D")
            _salesforce_id(self.record_id, "001")
        elif self.provider == "hubspot":
            require(self.object_type == "company", "invalid_target")
            require(
                bool(_DECIMAL_ID.fullmatch(self.instance_id))
                and bool(_DECIMAL_ID.fullmatch(self.record_id)),
                "invalid_hubspot_id",
            )
        else:
            raise CRMRefused("invalid_target")

    @property
    def document(self) -> dict:
        return {
            "provider": self.provider,
            "instance_id": self.instance_id,
            "object_type": self.object_type,
            "record_id": self.record_id,
        }

    @property
    def key(self) -> str:
        return canonical_text(self.document)

    @property
    def account_ref(self) -> str:
        return "fr-crm-account-v1-" + canonical_hash(self.document)


@dataclass(frozen=True)
class Receipt:
    connection_id: str
    receipt_id: str
    body: bytes
    body_sha256: str


@dataclass(frozen=True)
class Ownership:
    target_key: str
    unit_id: str
    generation: int

    def __post_init__(self):
        validate_unit_id(self.unit_id)
        require(type(self.target_key) is str and bool(self.target_key), "invalid_target")
        require(type(self.generation) is int and self.generation > 0, "stale_ownership")
