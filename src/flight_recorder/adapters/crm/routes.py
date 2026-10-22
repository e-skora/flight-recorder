"""Credential-safe local ingress; a receipt is not a provider observation."""

import os
import secrets
import stat
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request

from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.adapters.crm.http import private_text, strict_json
from flight_recorder.adapters.crm.oauth import begin_oauth, finish_oauth
from flight_recorder.adapters.crm.poll import dispatch_poll
from flight_recorder.adapters.crm.runtime_config import absolute_path

_LIMIT = 1024 * 1024


def read_private_file(path, *, limit=16384):
    fd = None
    try:
        path = absolute_path(str(path))
        parent = path.parent.stat()
        require(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and stat.S_IMODE(parent.st_mode) == 0o700,
            "private_file_invalid",
        )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.getuid()
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) == 0o600
            and 0 < before.st_size <= limit,
            "private_file_invalid",
        )
        body = bytearray()
        while len(body) <= limit:
            part = os.read(fd, min(4096, limit + 1 - len(body)))
            if not part:
                break
            body.extend(part)
        after, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        require(
            len(body) == before.st_size
            and len(body) <= limit
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and (current.st_dev, current.st_ino) == (after.st_dev, after.st_ino),
            "private_file_invalid",
        )
        return bytes(body)
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("private_file_invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def ingress_secret(config):
    body = read_private_file(config.document["ingress_secret_file"], limit=512)
    require(
        32 <= len(body) <= 512 and all(33 <= value <= 126 for value in body), "private_file_invalid"
    )
    return body


def _headers(request, *, authenticated, body):
    values = {}
    protected = {
        b"host",
        b"authorization",
        b"content-type",
        b"content-length",
        b"transfer-encoding",
    }
    for key, value in request.scope["headers"]:
        key = key.lower()
        if key in protected:
            if key in values:
                raise HTTPException(400, "invalid_request_headers")
            values[key] = value
    if values.get(b"host") != b"localhost:8768":
        raise HTTPException(403, "invalid_local_host")
    if authenticated:
        value = values.get(b"authorization", b"")
        if not value.startswith(b"Bearer ") or not secrets.compare_digest(
            value[7:], request.app.state.ingress_secret
        ):
            raise HTTPException(401, "invalid_ingress_authorization")
    if body:
        content_type = values.get(b"content-type", b"").lower()
        if content_type not in (b"application/json", b"application/json; charset=utf-8"):
            raise HTTPException(415, "json_content_type_required")
        length = values.get(b"content-length")
        if length is not None:
            if not length or any(char < 48 or char > 57 for char in length) or len(length) > 12:
                raise HTTPException(400, "invalid_content_length")
            if int(length) > _LIMIT:
                raise HTTPException(413, "request_too_large")
        transfer = values.get(b"transfer-encoding")
        if transfer is not None and (transfer.lower() != b"chunked" or length is not None):
            raise HTTPException(400, "invalid_request_framing")
        return None if length is None else int(length)
    return None


async def _body(request):
    length = _headers(request, authenticated=True, body=True)
    if request.scope.get("query_string"):
        raise HTTPException(400, "query_parameters_not_allowed")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _LIMIT:
            raise HTTPException(413, "request_too_large")
        body.extend(chunk)
    if length is not None and length != len(body):
        raise HTTPException(400, "invalid_content_length")
    try:
        value = strict_json(bytes(body))
        require(type(value) is dict, "invalid_crm_request")
        return value
    except Exception:
        raise HTTPException(400, "invalid_crm_request") from None


class _LazyHTTP:
    def __init__(self, runtime):
        self.runtime = runtime
        self.config_binding = runtime.config.connection.binding

    def _get(self):
        from flight_recorder.adapters.crm.http import OwnedHTTP

        if self.runtime._http is None:
            self.runtime._http = OwnedHTTP(self.runtime.config.connection, clock=self.runtime.clock)
        return self.runtime._http

    @property
    def observation_origin(self):
        return self._get().observation_origin

    def post_token(self, form):
        return self._get().post_token(form)

    def get(self, *args, **kwargs):
        return self._get().get(*args, **kwargs)


class _LazyTokens:
    def __init__(self, runtime):
        self.runtime = runtime

    def _get(self):
        from flight_recorder.adapters.crm.secrets import TokenStore

        if self.runtime._tokens is None:
            self.runtime._tokens = TokenStore(self.runtime.config.document["credential_directory"])
        return self.runtime._tokens

    def status(self, *args, **kwargs):
        return self._get().status(*args, **kwargs)

    def install(self, *args, **kwargs):
        return self._get().install(*args, **kwargs)


def crm_router():
    router = APIRouter()

    @router.post("/api/internal/crm/poll", status_code=202)
    async def poll(request: Request):
        value = await _body(request)
        if set(value) != {"connection_id", "tick_id"}:
            raise HTTPException(400, "invalid_crm_request")
        runtime = request.app.state.crm_runtime
        try:
            report = dispatch_poll(
                runtime, value["connection_id"], value["tick_id"], clock=runtime.clock
            )
            return asdict(report)
        except CRMRefused as exc:
            status = 409 if exc.reason == "poll_config_conflict" else 400
            raise HTTPException(status, exc.reason) from None

    @router.post("/api/internal/crm/oauth/salesforce/begin")
    async def oauth_begin(request: Request):
        value = await _body(request)
        runtime = request.app.state.crm_runtime
        if (
            set(value) != {"connection_id"}
            or value["connection_id"] != runtime.config.connection.connection_id
        ):
            raise HTTPException(400, "unknown_crm_connection")
        try:
            runtime.authority.assert_held()
            result = begin_oauth(
                runtime.config.connection, request.app.state.oauth_sessions, runtime.clock
            )
            return {"authorization_url": result.url}
        except CRMRefused:
            raise HTTPException(400, "oauth_begin_refused") from None

    @router.get("/api/internal/crm/oauth/salesforce/callback")
    async def oauth_callback(request: Request):
        _headers(request, authenticated=False, body=False)
        runtime = request.app.state.crm_runtime
        try:
            runtime.authority.assert_held()
            raw = request.scope.get("query_string", b"")
            require(len(raw) <= 16384, "invalid_oauth_callback")
            items = parse_qsl(
                raw.decode("ascii"),
                keep_blank_values=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=8,
            )
            secret_path = Path(runtime.config.document["credential_directory"]) / (
                runtime.config.connection.credential_reference + ".client-secret"
            )
            client_secret = None
            if secret_path.exists() or secret_path.is_symlink():
                client_secret = read_private_file(secret_path).decode("utf-8")
                private_text(client_secret)
            confirmed = finish_oauth(
                runtime.config.connection,
                request.app.state.oauth_sessions,
                _LazyTokens(runtime),
                items,
                _LazyHTTP(runtime),
                runtime.clock,
                client_secret=client_secret,
            )
            runtime._confirmed_connection = confirmed
            return {
                "state": "connected",
                "provider": confirmed.provider,
                "instance_id": confirmed.instance_id,
                "observation_origin": confirmed.observation_origin,
            }
        except Exception:
            runtime._confirmed_connection = None
            raise HTTPException(400, "oauth_callback_refused") from None

    return router
