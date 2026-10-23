"""Explicit local read-only handoff; no browser-supplied authority or provider targets."""

import hashlib
import html
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from flight_recorder.adapters.crm import oauth
from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.adapters.crm.http import strict_json, utc_now
from flight_recorder.adapters.crm.routes import _headers, _LazyHTTP, _LazyTokens, read_private_file

_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_STATES = frozenset(
    {
        "not_started",
        "connected",
        "metadata_observed",
        "capture_queued",
        "capture_complete",
        "refused",
    }
)


def read_control_router(runtime):
    router = APIRouter()
    sessions, pending = {}, {}
    state = ["not_started"]
    gate = runtime.read_gate

    def result_redirect():
        return RedirectResponse("/crm/read/result", status_code=303, headers=_HEADERS)

    def session(request):
        now = utc_now(runtime.clock)
        for key in list(sessions):
            if sessions[key][1] <= now:
                del sessions[key]
        key = request.cookies.get("fr_read_session")
        if key not in sessions:
            if len(sessions) >= 16:
                raise HTTPException(403, "read_session_refused")
            key = secrets.token_urlsafe(32)
            sessions[key] = (secrets.token_urlsafe(32), now + timedelta(minutes=10))
        return key, sessions[key][0]

    async def post_body(request, action, keys):
        types = (b"application/json", b"application/json; charset=utf-8")
        if action == "oauth_connect":
            types += (b"application/x-www-form-urlencoded",)
        length = _headers(request, authenticated=False, body=True, content_types=types)
        origins = [v for k, v in request.scope["headers"] if k.lower() == b"origin"]
        if origins != [b"http://localhost:8768"] or request.scope.get("query_string"):
            raise HTTPException(403, "read_origin_refused")
        if length is not None and length > 4096:
            raise HTTPException(413, "request_too_large")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 4096:
                raise HTTPException(413, "request_too_large")
        try:
            require(length is None or length == len(data), "invalid_read_request")
            if (
                request.headers.get("content-type", "").lower()
                == "application/x-www-form-urlencoded"
            ):
                pairs = parse_qsl(
                    bytes(data).decode("utf-8"),
                    keep_blank_values=True,
                    encoding="utf-8",
                    errors="strict",
                    max_num_fields=4,
                )
                require(len(pairs) == len({k for k, v in pairs}), "invalid_read_request")
                body = dict(pairs)
            else:
                body = strict_json(bytes(data))
            require(type(body) is dict and set(body) == {"csrf", *keys}, "invalid_read_request")
            key = request.cookies.get("fr_read_session")
            require(type(key) is str and key in sessions, "read_csrf_refused")
            csrf, expires = sessions[key]
            require(
                utc_now(runtime.clock) < expires
                and type(body["csrf"]) is str
                and secrets.compare_digest(body["csrf"], csrf),
                "read_csrf_refused",
            )
            require(gate.authority.document["action"] == action, "read_action_refused")
            gate.check()
            return body
        except (ValueError, TypeError, KeyError):
            raise HTTPException(403, "read_request_refused") from None

    def page(request):
        _headers(request, authenticated=False, body=False)
        key, csrf = session(request)
        require(state[0] in _STATES, "read_status_refused")
        action = gate.authority.document["action"]
        name, endpoint = {
            "oauth_connect": ("Connect Salesforce", "connect"),
            "metadata_probe": ("Check Account fields", "metadata"),
            "capture_once": ("Capture the fictional Account", "capture"),
        }[action]
        tick = gate.authority.document["tick_id"]
        tick_input = (
            f'<input type="hidden" name="tick_id" value="{html.escape(tick, quote=True)}">'
            if tick is not None
            else ""
        )
        markup = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Flight Recorder · Salesforce test</title><body>
<h1>Salesforce test connection</h1>
<p>This session can {html.escape(name.lower())}. Score writes are unavailable.</p>
<p>Status: <strong>{state[0]}</strong></p>
<form id="read-action"><input type="hidden" name="csrf" value="{csrf}">{tick_input}
<button type="submit">{name}</button></form>
<script>document.getElementById('read-action').addEventListener('submit', async (event) => {{
 event.preventDefault(); const form = event.currentTarget;
 const body = Object.fromEntries(new FormData(form));
 const response = await fetch('/crm/read/{endpoint}', {{method:'POST',
 headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
 if (response.redirected) window.location.assign(response.url);
 else window.location.assign('/crm/read/result');
}});</script></body></html>'''
        # A normal form submission makes the OAuth redirect a top-level navigation.
        if action == "oauth_connect":
            markup = markup[: markup.index("<script>")] + "</body></html>"
            markup = markup.replace(
                '<form id="read-action">',
                '<form id="read-action" method="post" action="/crm/read/connect">',
            )
        response = HTMLResponse(markup, headers=_HEADERS)
        response.set_cookie(
            "fr_read_session", key, httponly=True, samesite="strict", path="/crm/read", max_age=600
        )
        return response

    @router.get("/crm/read", response_class=HTMLResponse)
    @router.get("/crm/read/result", response_class=HTMLResponse)
    async def read_page(request: Request):
        return page(request)

    @router.post("/crm/read/connect")
    async def connect(request: Request):
        await post_body(request, "oauth_connect", set())
        context = gate.authorize_oauth()
        runtime._read_context = context
        runtime.authority.assert_held()
        result = oauth.begin_oauth(
            runtime.config.connection, request.app.state.oauth_sessions, runtime.clock
        )
        # Pending state belongs to this process and exact held action; no state survives restart.
        pending[result.state_reference] = (gate.fingerprint, context)
        return RedirectResponse(result.url, status_code=303, headers=_HEADERS)

    @router.get("/api/internal/crm/oauth/salesforce/callback")
    async def callback(request: Request):
        states = []
        try:
            _headers(request, authenticated=False, body=False)
            runtime.authority.assert_held()
            context = gate.authorize_oauth()
            raw = request.scope.get("query_string", b"")
            require(len(raw) <= 16384, "read_callback_refused")
            items = parse_qsl(
                raw.decode("ascii"),
                keep_blank_values=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=8,
            )
            states = [value for key, value in items if key == "state"]
            require(len(states) == 1 and type(states[0]) is str, "read_callback_refused")
            reference = hashlib.sha256(states[0].encode("utf-8")).hexdigest()
            bound = pending.pop(reference, None)
            require(
                bound is not None
                and bound[0] == gate.fingerprint
                and bound[1].fingerprint == context.fingerprint,
                "read_callback_refused",
            )
            bound[1].check()
            runtime._read_context = bound[1]
            secret_path = gate.config.document["credential_directory"]
            from pathlib import Path

            secret_path = Path(secret_path) / (
                gate.config.connection.credential_reference + ".client-secret"
            )
            secret = None
            if secret_path.exists() or secret_path.is_symlink():
                secret = read_private_file(secret_path).decode("utf-8")
            confirmed = oauth.finish_oauth(
                gate.config.connection,
                request.app.state.oauth_sessions,
                _LazyTokens(runtime),
                items,
                _LazyHTTP(runtime),
                runtime.clock,
                client_secret=secret,
            )
            runtime._confirmed_connection = confirmed
            state[0] = "connected"
        except Exception:
            runtime._confirmed_connection = None
            request.app.state.oauth_sessions._discard(states)
            state[0] = "refused"
        return result_redirect()

    @router.post("/crm/read/metadata")
    async def metadata(request: Request):
        await post_body(request, "metadata_probe", set())
        try:
            runtime.authority.assert_held()
            context = gate.authorize_metadata()
            runtime._read_context = context
            client = runtime.client()
            runtime.verify_for_read(client)
            gate.observe_metadata(runtime, context)
            state[0] = "metadata_observed"
        except CRMRefused:
            state[0] = "refused"
        return result_redirect()

    @router.post("/crm/read/capture")
    async def capture(request: Request):
        body = await post_body(request, "capture_once", {"tick_id"})
        try:
            require(body["tick_id"] == gate.authority.document["tick_id"], "read_action_refused")
        except CRMRefused:
            raise HTTPException(403, "read_request_refused") from None
        try:
            runtime.admit_read_tick(body["tick_id"])
            state[0] = "capture_queued"
            runtime.run_pending()
            report = runtime.poll_report(body["tick_id"])
            if report.disposition == "complete":
                state[0] = (
                    "capture_complete" if report.members[0].state == "complete" else "refused"
                )
        except CRMRefused:
            state[0] = "refused"
        return result_redirect()

    return router
