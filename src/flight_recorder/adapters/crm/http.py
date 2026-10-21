"""Owned bounded Salesforce HTTP, with injected transport explicitly distinguished."""

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from flight_recorder.adapters.crm.contracts import ConnectionConfig, CRMRefused, require
from flight_recorder.adapters.crm.mapping import INPUT_FIELDS

SELECTED_FIELDS = tuple(sorted(INPUT_FIELDS | {"Id", "Name", "Website", "LastModifiedDate"}))
_MAX_BODY = 1024 * 1024
_TRANSIENT = {429, 502, 503, 504}


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

    def _request(self, method, url, material):
        with self._mutex:
            require(not self._closed, "provider_transport_closed")
            if method == "GET":
                self._get_url(url)
                private_text(material)
            else:
                self._form(material)
            attempts = 3 if method == "GET" else 1
            for attempt in range(attempts):
                response = None
                retry = False
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
                    time.sleep(0.01 * (attempt + 1))
            raise CRMRefused("provider_transport_uncertain")

    def get(self, url, access_token):
        return self._request("GET", url, access_token)

    def post_token(self, form):
        return self._request("POST", self._config.login_origin + "/services/oauth2/token", form)

    def close(self):
        with self._mutex:
            if not self._closed:
                self._closed = True
                try:
                    self._client.close()
                except Exception:
                    raise CRMRefused("provider_transport_uncertain") from None
