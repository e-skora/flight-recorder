"""Salesforce identity checks, separate from credential storage and business reads."""

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

from flight_recorder.adapters.crm.contracts import (
    ConfirmedConnection,
    ConnectionConfig,
    CRMRefused,
    CRMTarget,
    ObservedRead,
    _salesforce_id,
    require,
)
from flight_recorder.adapters.crm.http import SELECTED_FIELDS, private_text, utc_now
from flight_recorder.adapters.crm.mapping import decode_salesforce_record


def source_identity_id(value, prefix):
    """Explicit source-response conversion only; configuration/targets remain canonical18."""
    require(
        type(value) is str
        and bool(re.fullmatch(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?", value, re.ASCII))
        and value.startswith(prefix),
        "invalid_provider_identity",
    )
    if len(value) == 15:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        value += "".join(
            alphabet[sum(1 << j for j, c in enumerate(value[i : i + 5]) if "A" <= c <= "Z")]
            for i in (0, 5, 10)
        )
    try:
        _salesforce_id(value, prefix)
    except ValueError:
        raise CRMRefused("invalid_provider_identity") from None
    return value


def identity_url(config, url):
    try:
        private_text(url)
        parsed = urlsplit(url)
        parts = parsed.path.split("/")
        require(
            url == urlunsplit(parsed)
            and parsed.scheme == "https"
            and f"https://{parsed.netloc}" == config.identity_origin
            and not parsed.query
            and not parsed.fragment
            and len(parts) == 4
            and parts[:2] == ["", "id"],
            "invalid_provider_identity",
        )
        require(
            source_identity_id(parts[2], "00D") == config.expected_instance_id
            and source_identity_id(parts[3], "005") == config.expected_user_id,
            "provider_identity_mismatch",
        )
    except (ValueError, TypeError):
        raise CRMRefused("invalid_provider_identity") from None
    return url


def verify_material(config, material, http, clock, *, identity=None):
    require(http.config_binding == config.binding, "invalid_connection_binding")
    now = utc_now(clock)
    require(material.expires_at is None or material.expires_at > now, "token_expired")
    identity = identity_url(
        config,
        identity
        if identity is not None
        else f"{config.identity_origin}/id/{config.expected_instance_id}/{config.expected_user_id}",
    )
    response = http.get(identity, material.access_token).json()
    require(
        type(response) is dict
        and response.get("asserted_user") is True
        and response.get("active") is True,
        "provider_identity_mismatch",
    )
    require(
        source_identity_id(response.get("organization_id"), "00D") == config.expected_instance_id
        and source_identity_id(response.get("user_id"), "005") == config.expected_user_id,
        "provider_identity_mismatch",
    )
    if "id" in response:
        identity_url(config, response["id"])
    versions = http.get(config.api_origin + "/services/data", material.access_token).json()
    require(type(versions) is list and 1 <= len(versions) <= 1000, "unsupported_api_version")
    supported = set()
    for entry in versions:
        require(
            type(entry) is dict
            and type(entry.get("version")) is str
            and bool(re.fullmatch(r"[1-9][0-9]*\.0", entry["version"], re.ASCII))
            and entry.get("url") == "/services/data/v" + entry["version"],
            "invalid_provider_response",
        )
        supported.add(entry["version"])
    require(config.api_version in supported, "unsupported_api_version")
    return ConfirmedConnection(
        config.connection_id,
        config.provider,
        config.expected_instance_id,
        config.expected_user_id,
        config.api_version,
        utc_now(clock),
        http.observation_origin,
    )


class SalesforceClient:
    def __init__(self, config, token_store, http, clock):
        require(type(config) is ConnectionConfig, "invalid_connection_config")
        config.__post_init__()
        require(http.config_binding == config.binding, "invalid_connection_binding")
        self._config, self._tokens, self._http, self._clock = config, token_store, http, clock
        self._verified = None

    def __repr__(self):
        return "SalesforceClient(<redacted>)"

    def verify_connection(self):
        self._verified = None
        cfg = self._config
        ready = self._tokens.read_ready(cfg.connection_id, cfg.binding)
        confirmed = verify_material(cfg, ready.material, self._http, self._clock)
        current = self._tokens.read_ready(cfg.connection_id, cfg.binding)
        require(current == ready, "connection_confirmation_stale")
        self._verified = (cfg.binding, ready.generation, ready.material)
        return confirmed

    def read_selected(self, target):
        cfg = self._config
        require(self._verified is not None, "connection_not_verified")
        require(
            type(target) is CRMTarget
            and target.provider == "salesforce"
            and target.instance_id == cfg.expected_instance_id
            and target.object_type == "Account"
            and target.record_id in cfg.record_ids,
            "target_not_allowlisted",
        )
        current = self._tokens.read_ready(cfg.connection_id, cfg.binding)
        require(
            self._verified == (cfg.binding, current.generation, current.material),
            "connection_confirmation_stale",
        )
        started = utc_now(self._clock)
        require(
            current.material.expires_at is None or current.material.expires_at > started,
            "token_expired",
        )
        url = (
            cfg.api_origin
            + f"/services/data/v{cfg.api_version}/sobjects/Account/{target.record_id}?"
            + urlencode({"fields": ",".join(SELECTED_FIELDS)})
        )
        response = self._http.get(url, current.material.access_token)
        try:
            # Validate strict UTF-8 JSON before the legacy lossless byte decoder.
            # Merely decoding UTF-16 ASCII bytes as UTF-8 leaves NULs unvalidated.
            response.json()
            properties = decode_salesforce_record(response.body)
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_provider_response") from None
        raw_time = properties.get("LastModifiedDate")
        try:
            require(type(raw_time) is str, "invalid_observed_read")
            updated = datetime.fromisoformat(raw_time)
            require(updated.utcoffset() is not None, "invalid_observed_read")
            updated = updated.astimezone(UTC)
        except ValueError:
            raise CRMRefused("invalid_observed_read") from None

        def stamp(value):
            return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        return ObservedRead.from_document(
            dict(
                target=target.document,
                properties=properties,
                unavailable_fields=[],
                read_started_at=stamp(started),
                read_finished_at=stamp(response.received_at),
                source_updated_at=stamp(updated),
                precondition=response.header("ETag"),
                request_id=response.header("Sforce-Request-Id"),
                response_body_sha256=hashlib.sha256(response.body).hexdigest(),
                observation_origin=self._http.observation_origin,
            )
        )
