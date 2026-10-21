"""Canonical target identities and immutable operational ownership values."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

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


@dataclass(frozen=True)
class CRMCapture:
    canonical_bytes: bytes

    def __post_init__(self):
        from flight_recorder.collector.crm_schema import CaptureDocument

        require(type(self.canonical_bytes) is bytes, "invalid_crm_capture")
        model = CaptureDocument.model_validate_json(self.canonical_bytes, strict=True)
        require(
            self.canonical_bytes == canonical_text(model.model_dump(mode="json")).encode(),
            "invalid_crm_capture",
        )

    @classmethod
    def from_document(cls, document):
        from flight_recorder.collector.crm_schema import CaptureDocument

        model = CaptureDocument.model_validate_json(canonical_text(document), strict=True)
        return cls(canonical_text(model.model_dump(mode="json")).encode())

    @property
    def document(self):
        return json.loads(self.canonical_bytes)

    @property
    def capture_hash(self):
        return canonical_hash(self.document)

    @property
    def unit_id(self):
        return self.document["unit_id"]


def _config_text(value, limit=256):
    require(type(value) is str and 0 < len(value) <= limit, "invalid_connection_config")
    require(
        not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), "invalid_connection_config"
    )
    value.encode("utf-8")


def _origin(value):
    _config_text(value)
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    hostname = (
        rf"(?:login\.salesforce\.com|test\.salesforce\.com|"
        rf"{label}(?:\.{label})*\.my\.salesforce\.com)"
    )
    require(
        bool(re.fullmatch(rf"https://{hostname}", value, re.ASCII)), "invalid_connection_config"
    )


@dataclass(frozen=True)
class ConnectionConfig:
    connection_id: str
    provider: str
    expected_instance_id: str
    expected_user_id: str
    login_origin: str
    api_origin: str
    identity_origin: str
    api_version: str
    client_id: str
    callback_uri: str
    credential_reference: str
    mapping_hash: str
    artifact_hash: str
    evaluator_version: str
    refresh_rotation: str
    code_secret_required: bool
    refresh_secret_required: bool
    record_ids: tuple[str, ...]
    write_enabled: bool

    def __post_init__(self):
        from flight_recorder.adapters.crm.mapping import MAPPING_HASH

        try:
            _config_text(self.connection_id)
            require(
                type(self.provider) is str and self.provider == "salesforce",
                "invalid_connection_config",
            )
            for identity, prefix in [
                (self.expected_instance_id, "00D"),
                (self.expected_user_id, "005"),
            ]:
                _config_text(identity)
                _salesforce_id(identity, prefix)
            for origin in [self.login_origin, self.api_origin, self.identity_origin]:
                _origin(origin)
            require(self.api_origin.endswith(".my.salesforce.com"), "invalid_connection_config")
            _config_text(self.api_version)
            require(
                bool(re.fullmatch(r"[1-9][0-9]\.0", self.api_version, re.ASCII)),
                "invalid_connection_config",
            )
            _config_text(self.client_id, 2048)
            require(
                bool(re.fullmatch(r"[A-Za-z0-9._-]+", self.client_id, re.ASCII)),
                "invalid_connection_config",
            )
            require(
                type(self.callback_uri) is str
                and self.callback_uri
                == "http://localhost:8768/api/internal/crm/oauth/salesforce/callback",
                "invalid_connection_config",
            )
            _config_text(self.credential_reference)
            require(
                bool(
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.credential_reference, re.ASCII)
                ),
                "invalid_connection_config",
            )
            validate_unit_id(self.mapping_hash)
            require(self.mapping_hash == MAPPING_HASH, "invalid_connection_config")
            validate_unit_id(self.artifact_hash)
            _config_text(self.evaluator_version)
            require(
                type(self.refresh_rotation) is str
                and self.refresh_rotation in ("required", "nonrotating"),
                "invalid_connection_config",
            )
            require(
                type(self.code_secret_required) is bool
                and type(self.refresh_secret_required) is bool,
                "invalid_connection_config",
            )
            require(
                type(self.write_enabled) is bool and self.write_enabled is False,
                "invalid_connection_config",
            )
            require(
                type(self.record_ids) is tuple and 1 <= len(self.record_ids) <= 3,
                "invalid_connection_config",
            )
            for record in self.record_ids:
                _config_text(record)
                _salesforce_id(record, "001")
            require(
                tuple(sorted(set(self.record_ids))) == self.record_ids, "invalid_connection_config"
            )
        except (ValueError, TypeError, UnicodeError):
            raise CRMRefused("invalid_connection_config") from None

    @property
    def document(self):
        fields = (
            "connection_id",
            "provider",
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
            "code_secret_required",
            "refresh_secret_required",
            "write_enabled",
        )
        return {**{key: getattr(self, key) for key in fields}, "record_ids": list(self.record_ids)}

    @property
    def binding(self):
        return canonical_hash(self.document)


@dataclass(frozen=True)
class ConfirmedConnection:
    connection_id: str
    provider: str
    instance_id: str
    user_id: str
    api_version: str
    confirmed_at: datetime
    observation_origin: str


def observation_time(value):
    require(type(value) is str, "invalid_observed_read")
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.utcoffset() is not None, "invalid_observed_read")
        normalized = parsed.astimezone(UTC)
        require(normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ") == value, "invalid_observed_read")
        return normalized
    except ValueError:
        raise CRMRefused("invalid_observed_read") from None


@dataclass(frozen=True, repr=False)
class ObservedRead:
    canonical_bytes: bytes

    def __post_init__(self):
        from flight_recorder.adapters.crm.mapping import INPUT_FIELDS, map_inputs

        try:
            require(
                type(self.canonical_bytes) is bytes and len(self.canonical_bytes) <= 1024 * 1024,
                "invalid_observed_read",
            )
            d = json.loads(self.canonical_bytes.decode("utf-8"))
            require(
                type(d) is dict
                and set(d)
                == {
                    "target",
                    "properties",
                    "unavailable_fields",
                    "read_started_at",
                    "read_finished_at",
                    "source_updated_at",
                    "precondition",
                    "request_id",
                    "response_body_sha256",
                    "observation_origin",
                },
                "invalid_observed_read",
            )
            require(
                self.canonical_bytes == canonical_text(d).encode("utf-8"), "invalid_observed_read"
            )
            require(type(d["target"]) is dict, "invalid_observed_read")
            target = CRMTarget(**d["target"])
            require(target.provider == "salesforce", "invalid_observed_read")
            require(
                type(d["properties"]) is dict
                and set(d["properties"])
                <= INPUT_FIELDS | {"Id", "Name", "Website", "LastModifiedDate"},
                "invalid_observed_read",
            )
            require(
                d["properties"].get("Id") == target.record_id
                and type(d["properties"].get("Name")) is str
                and d["properties"]["Name"]
                and "Website" in d["properties"],
                "invalid_observed_read",
            )
            started = observation_time(d["read_started_at"])
            finished = observation_time(d["read_finished_at"])
            updated = observation_time(d["source_updated_at"])
            require(started <= finished and updated <= finished, "invalid_observed_read")
            raw_time = d["properties"].get("LastModifiedDate")
            require(type(raw_time) is str, "invalid_observed_read")
            parsed = datetime.fromisoformat(raw_time)
            require(parsed.utcoffset() is not None and parsed == updated, "invalid_observed_read")
            map_inputs(d["properties"], d["unavailable_fields"])
            require(
                d["observation_origin"] in ("provider_http", "injected_transport"),
                "invalid_observed_read",
            )
            for name in ("precondition", "request_id"):
                require(
                    d[name] is None
                    or type(d[name]) is str
                    and bool(re.fullmatch(r"[\x20-\x7e]{1,1024}", d[name])),
                    "invalid_observed_read",
                )
            validate_unit_id(d["response_body_sha256"])
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CRMRefused("invalid_observed_read") from None

    @classmethod
    def from_document(cls, document):
        try:
            return cls(canonical_text(document).encode("utf-8"))
        except (ValueError, TypeError):
            raise CRMRefused("invalid_observed_read") from None

    @property
    def document(self):
        return json.loads(self.canonical_bytes)

    @property
    def properties(self):
        return self.document["properties"]

    @property
    def target(self):
        return CRMTarget(**self.document["target"])

    @property
    def unavailable_fields(self):
        return self.document["unavailable_fields"]

    @property
    def precondition(self):
        return self.document["precondition"]

    @property
    def request_id(self):
        return self.document["request_id"]

    @property
    def response_body_sha256(self):
        return self.document["response_body_sha256"]

    @property
    def observation_origin(self):
        return self.document["observation_origin"]

    @property
    def read_started_at(self):
        return observation_time(self.document["read_started_at"])

    @property
    def read_finished_at(self):
        return observation_time(self.document["read_finished_at"])

    @property
    def source_updated_at(self):
        return observation_time(self.document["source_updated_at"])

    def __repr__(self):
        return "ObservedRead(<frozen observation>)"
