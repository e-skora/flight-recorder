"""Owned bounded Salesforce HTTP, with injected transport explicitly distinguished."""

import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from flight_recorder.adapters.crm.contracts import ConnectionConfig, CRMRefused, require
from flight_recorder.adapters.crm.mapping import INPUT_FIELDS, OUTPUTS

SELECTED_FIELDS = tuple(sorted(INPUT_FIELDS | {"Id", "Name", "Website", "LastModifiedDate"}))
_MAX_BODY = 1024 * 1024
_TRANSIENT = {429, 502, 503, 504}
OUTPUT_FIELDS = tuple(sorted({"Id", *OUTPUTS.values()}))


def _mutation_complete(status, body, headers):
    """A narrow typed receipt, not a claim that every API variant is qualified."""
    if status not in (204, 412):
        return False
    try:
        critical = {}
        for key, value in headers:
            name = key.lower()
            if name not in {
                "content-length",
                "content-type",
                "transfer-encoding",
                "content-encoding",
            }:
                continue
            require(name not in critical, "invalid_provider_response")
            require(
                not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
                "invalid_provider_response",
            )
            critical[name] = value.strip()
        length = critical.get("content-length")
        transfer = critical.get("transfer-encoding")
        require(length is None or transfer is None, "invalid_provider_response")
        if length is not None:
            require(
                bool(re.fullmatch(r"0|[1-9][0-9]{0,6}", length, re.ASCII))
                and int(length) == len(body),
                "invalid_provider_response",
            )
        if transfer is not None:
            require(status == 412 and transfer.lower() == "chunked", "invalid_provider_response")
        require(
            critical.get("content-encoding", "identity").lower() == "identity",
            "invalid_provider_response",
        )
        media = critical.get("content-type")
        if media is not None:
            require(
                bool(
                    re.fullmatch(
                        r"application/json(?:\s*;\s*charset=(?:utf-8|utf8))?",
                        media,
                        re.ASCII | re.IGNORECASE,
                    )
                ),
                "invalid_provider_response",
            )
        if status == 204:
            return body == b"" and transfer is None
        if body == b"":
            return True
        require(media is not None, "invalid_provider_response")
        errors = strict_json(body)
        require(type(errors) is list and bool(errors), "invalid_provider_response")
        for item in errors:
            require(
                type(item) is dict
                and {"message", "errorCode"} <= set(item) <= {"message", "errorCode", "fields"},
                "invalid_provider_response",
            )
            require(
                type(item["message"]) is str
                and 0 < len(item["message"]) <= 4096
                and type(item["errorCode"]) is str
                and 0 < len(item["errorCode"]) <= 256,
                "invalid_provider_response",
            )
            if "fields" in item:
                require(
                    type(item["fields"]) is list
                    and all(type(v) is str and len(v) <= 256 for v in item["fields"]),
                    "invalid_provider_response",
                )
        return True
    except (ValueError, TypeError, RecursionError):
        return False


def utc_now(clock):
    try:
        value = clock()
        require(type(value) is datetime and value.utcoffset() is not None, "invalid_crm_clock")
        return value.astimezone(UTC)
    except Exception:
        raise CRMRefused("invalid_crm_clock") from None


def private_text(value):
    require(type(value) is str and 0 < len(value) <= 16384, "invalid_provider_request")
    require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), "invalid_provider_request")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CRMRefused("invalid_provider_request") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_provider_response")
        result[key] = value
    return result


def _constant(_):
    raise CRMRefused("invalid_provider_response")


def _float(value):
    result = float(value)
    require(math.isfinite(result), "invalid_provider_response")
    return result


def strict_json(body):
    try:
        require(type(body) is bytes and len(body) <= _MAX_BODY, "invalid_provider_response")
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (ValueError, TypeError, RecursionError):
        raise CRMRefused("invalid_provider_response") from None


def _retry_delay(response, attempt, clock):
    fallback = 0.01 * (attempt + 1)
    if response is None:
        return fallback
    values = response.headers.get_list("Retry-After")
    if len(values) != 1:
        return fallback
    value = values[0].strip()
    if re.fullmatch(r"[0-9]+", value, re.ASCII):
        # Bound before conversion, including arbitrarily long decimal headers.
        significant = value.lstrip("0") or "0"
        return 60 if len(significant) > 2 else min(int(significant), 60)
    try:
        until = parsedate_to_datetime(value)
        if until.utcoffset() is None:
            return fallback
        return min(60, max(0, (until - utc_now(clock)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return fallback


@dataclass(frozen=True, repr=False)
class PrivateResponse:
    body: bytes
    headers: tuple[tuple[str, str], ...]
    received_at: datetime

    def json(self):
        return strict_json(self.body)

    def header(self, name):
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        require(len(values) <= 1, "invalid_provider_response")
        return values[0] if values else None

    def __repr__(self):
        return "PrivateResponse(<redacted>)"


class OwnedHTTP:
    def __init__(self, config, *, transport=None, clock):
        require(type(config) is ConnectionConfig, "invalid_connection_config")
        config.__post_init__()
        self._config = config
        self._clock = clock
        self._mutex = threading.Lock()
        self._closed = False
        self._origin = "provider_http" if transport is None else "injected_transport"
        if transport is None:
            transport = httpx.HTTPTransport(verify=True, trust_env=False, retries=0)
        require(isinstance(transport, httpx.BaseTransport), "invalid_provider_transport")
        self._client = httpx.Client(
            transport=transport,
            verify=True,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
        )

    def __repr__(self):
        return "OwnedHTTP(<redacted>)"

    @property
    def observation_origin(self):
        return self._origin

    @property
    def config_binding(self):
        return self._config.binding

    def _get_url(self, url):
        private_text(url)
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise CRMRefused("invalid_provider_request") from None
        require(
            url == urlunsplit(parsed) and parsed.scheme == "https" and not parsed.fragment,
            "invalid_provider_request",
        )
        origin = f"https://{parsed.netloc}"
        cfg = self._config
        identity_paths = {
            f"/id/{org}/{user}"
            for org in (cfg.expected_instance_id, cfg.expected_instance_id[:15])
            for user in (cfg.expected_user_id, cfg.expected_user_id[:15])
        }
        if origin == cfg.identity_origin and parsed.path in identity_paths and not parsed.query:
            return
        require(origin == cfg.api_origin, "invalid_provider_request")
        if parsed.path == "/services/data" and not parsed.query:
            return
        paths = {
            f"/services/data/v{cfg.api_version}/sobjects/Account/{record}"
            for record in cfg.record_ids
        }
        require(
            parsed.path in paths
            and parse_qsl(parsed.query, keep_blank_values=True)
            == [("fields", ",".join(SELECTED_FIELDS))],
            "invalid_provider_request",
        )

    def _form(self, form):
        require(
            type(form) is dict and all(type(k) is str for k in form), "invalid_provider_request"
        )
        grant = form.get("grant_type")
        if grant == "authorization_code":
            keys = {"grant_type", "client_id", "redirect_uri", "code", "code_verifier"}
            secret = self._config.code_secret_required
            require(
                form.get("redirect_uri") == self._config.callback_uri, "invalid_provider_request"
            )
            verifier = form.get("code_verifier")
            require(
                type(verifier) is str
                and bool(re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier, re.ASCII)),
                "invalid_provider_request",
            )
        elif grant == "refresh_token":
            keys = {"grant_type", "client_id", "refresh_token"}
            secret = self._config.refresh_secret_required
        else:
            raise CRMRefused("invalid_provider_request")
        if secret:
            keys.add("client_secret")
        require(
            set(form) == keys and form.get("client_id") == self._config.client_id,
            "invalid_provider_request",
        )
        for value in form.values():
            private_text(value)

    def _write_document(self, intent):
        from flight_recorder.adapters.crm.writer_contracts import CRMWriteIntent

        require(type(intent) is CRMWriteIntent, "invalid_provider_request")
        # Revalidate bytes at the credential/transport boundary, never caller aliases.
        d = CRMWriteIntent(intent.canonical_bytes).document
        cfg = self._config
        require(
            d["connection_id"] == cfg.connection_id
            and d["target"]["provider"] == cfg.provider
            and d["target"]["instance_id"] == cfg.expected_instance_id
            and d["target"]["object_type"] == "Account"
            and d["target"]["record_id"] in cfg.record_ids
            and d["mapping_hash"] == cfg.mapping_hash
            and d["artifact_hash"] == cfg.artifact_hash
            and d["evaluator_version"] == cfg.evaluator_version,
            "invalid_provider_request",
        )
        return d

    def _output_url(self, intent):
        d = self._write_document(intent)
        return (
            self._config.api_origin
            + f"/services/data/v{self._config.api_version}"
            + f"/sobjects/Account/{d['target']['record_id']}?"
            + urlencode({"fields": ",".join(OUTPUT_FIELDS)})
        )

    def _request(self, method, url, material, *, output_intent=None):
        with self._mutex:
            require(not self._closed, "provider_transport_closed")
            if method == "GET":
                if output_intent is None:
                    self._get_url(url)
                else:
                    require(url == self._output_url(output_intent), "invalid_provider_request")
                private_text(material)
            else:
                self._form(material)
            attempts = 3 if method == "GET" else 1
            for attempt in range(attempts):
                response = None
                retry = False
                delay = 0.01 * (attempt + 1)
                try:
                    headers = {"Accept": "application/json"}
                    if method == "GET":
                        headers["Authorization"] = "Bearer " + material
                    request = httpx.Request(
                        method, url, headers=headers, data=material if method == "POST" else None
                    )
                    request.extensions["timeout"] = {
                        "connect": 10,
                        "read": 30,
                        "write": 30,
                        "pool": 10,
                    }
                    response = self._client.send(request, stream=True, follow_redirects=False)
                    if response.status_code != 200:
                        if method == "GET" and response.status_code in _TRANSIENT and attempt < 2:
                            retry = True
                            delay = _retry_delay(response, attempt, self._clock)
                        else:
                            reason = (
                                "provider_auth_refused"
                                if response.status_code in (401, 403)
                                else "provider_request_refused"
                            )
                            raise CRMRefused(reason) from None
                    else:
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            require(len(body) <= _MAX_BODY, "invalid_provider_response")
                        return PrivateResponse(
                            bytes(body), tuple(response.headers.multi_items()), utc_now(self._clock)
                        )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if method == "GET" and attempt < attempts - 1:
                        retry = True
                    else:
                        raise CRMRefused("provider_transport_uncertain") from None
                except CRMRefused:
                    raise
                except Exception:
                    raise CRMRefused("provider_transport_uncertain") from None
                finally:
                    try:
                        if response is not None:
                            response.close()
                        self._client.cookies.clear()
                    except Exception:
                        raise CRMRefused("provider_transport_uncertain") from None
                if retry:
                    time.sleep(delay)
            raise CRMRefused("provider_transport_uncertain")

    def get(self, url, access_token):
        return self._request("GET", url, access_token)

    def post_token(self, form):
        return self._request("POST", self._config.login_origin + "/services/oauth2/token", form)

    def get_outputs(self, intent, access_token):
        return self._request("GET", self._output_url(intent), access_token, output_intent=intent)

    def patch_once(self, intent, access_token, admission, client, credential_generation):
        from flight_recorder.adapters.crm.writer_contracts import ProviderResponse, _SendAdmission
        from flight_recorder.collector.canonical import canonical_bytes
        from flight_recorder.collector.crm_operations import request_digest

        require(type(admission) is _SendAdmission, "invalid_send_admission")
        with self._mutex:
            require(not self._closed, "provider_transport_closed")
            d = self._write_document(intent)
            private_text(access_token)
            url = (
                self._config.api_origin
                + f"/services/data/v{self._config.api_version}"
                + f"/sobjects/Account/{d['target']['record_id']}"
            )
            request = httpx.Request(
                "PATCH",
                url,
                headers={
                    "Authorization": "Bearer " + access_token,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                    "If-Match": d["source_precondition"],
                },
                content=canonical_bytes(
                    {remote: d["intended"][local] for local, remote in OUTPUTS.items()}
                ),
            )
            request.extensions["timeout"] = {"connect": 10, "read": 30, "write": 30, "pool": 10}
            admission.consume(intent, client, credential_generation=credential_generation)
            started = utc_now(self._clock)
            response = None
            status = digest = None
            complete = False
            try:
                # The only mutation send. There is no retry loop, refresh or redirect branch.
                response = self._client.send(request, stream=True, follow_redirects=False)
                status = response.status_code
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    require(len(body) <= _MAX_BODY, "invalid_provider_response")
                digest = hashlib.sha256(body).hexdigest()
                complete = _mutation_complete(status, bytes(body), response.headers.multi_items())
            except Exception:
                complete = False
            finally:
                try:
                    if response is not None:
                        response.close()
                except Exception:
                    complete = False
                try:
                    self._client.cookies.clear()
                except Exception:
                    complete = False
            finished = utc_now(self._clock)
            require(finished >= started, "invalid_crm_clock")

            def stamp(value):
                return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            return ProviderResponse.from_document(
                dict(
                    request_hash=request_digest(d),
                    started_at=stamp(started),
                    finished_at=stamp(finished),
                    status=status,
                    complete=complete,
                    body_sha256=digest,
                ),
                observation_origin=self.observation_origin,
            )

    def close(self):
        with self._mutex:
            if not self._closed:
                self._closed = True
                try:
                    self._client.close()
                except Exception:
                    raise CRMRefused("provider_transport_uncertain") from None
