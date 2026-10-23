"""Single-use local PKCE sessions and one-attempt Salesforce authorization."""

import base64
import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from flight_recorder.adapters.crm.contracts import ConnectionConfig, CRMRefused, require
from flight_recorder.adapters.crm.http import private_text, utc_now
from flight_recorder.adapters.crm.salesforce import identity_url, verify_material
from flight_recorder.adapters.crm.secrets import TokenMaterial


class OAuthProgress:
    """Process-local diagnostic labels only; never credentials or authorization."""

    _STAGES = frozenset(
        {
            "callback_headers",
            "callback_authority",
            "callback_parameters",
            "callback_binding",
            "callback_validation",
            "state_validation",
            "credential_precondition",
            "token_exchange",
            "token_response",
            "connection_verification",
            "identity_request",
            "identity_json",
            "identity_validation",
            "versions_request",
            "versions_json",
            "versions_validation",
            "credential_installation",
        }
    )

    def __init__(self):
        self._stage = "callback_headers"

    @property
    def stage(self):
        return self._stage

    def advance(self, stage):
        require(type(stage) is str and stage in self._STAGES, "invalid_oauth_diagnostic")
        self._stage = stage


@dataclass(frozen=True, repr=False)
class AuthorizationRequest:
    url: str
    state_reference: str

    def __repr__(self):
        return "AuthorizationRequest(<redacted>)"


@dataclass(frozen=True, repr=False)
class _Pending:
    binding: str
    created_at: object
    expires_at: object
    verifier: str

    def __repr__(self):
        return "PendingOAuth(<redacted>)"


class OAuthSessionStore:
    def __init__(self):
        self._mutex = threading.Lock()
        self._pending = {}

    def __repr__(self):
        return "OAuthSessionStore(<redacted>)"

    def _begin(self, config, clock):
        now = utc_now(clock)
        with self._mutex:
            self._pending = {
                key: value for key, value in self._pending.items() if value.expires_at > now
            }
            require(len(self._pending) < 16, "oauth_sessions_full")
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            reference = hashlib.sha256(state.encode("ascii")).hexdigest()
            self._pending[reference] = _Pending(
                config.binding, now, now + timedelta(minutes=10), verifier
            )
            return state, reference, verifier

    def _take(self, config, state, clock):
        require(
            type(state) is str and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", state, re.ASCII)),
            "oauth_state_invalid",
        )
        reference = hashlib.sha256(state.encode("ascii")).hexdigest()
        now = utc_now(clock)
        with self._mutex:
            pending = self._pending.pop(reference, None)
            require(
                pending is not None
                and pending.binding == config.binding
                and pending.created_at <= now < pending.expires_at,
                "oauth_state_invalid",
            )
            return pending.verifier

    def _discard(self, states):
        references = {
            hashlib.sha256(state.encode("ascii")).hexdigest()
            for state in states
            if type(state) is str and re.fullmatch(r"[A-Za-z0-9_-]{43}", state, re.ASCII)
        }
        with self._mutex:
            for reference in references:
                self._pending.pop(reference, None)


def begin_oauth(config, session_store, clock):
    require(type(config) is ConnectionConfig, "invalid_connection_config")
    config.__post_init__()
    require(type(session_store) is OAuthSessionStore, "oauth_state_invalid")
    state, reference, verifier = session_store._begin(config, clock)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urlencode(
        dict(
            response_type="code",
            client_id=config.client_id,
            redirect_uri=config.callback_uri,
            scope="api refresh_token",
            state=state,
            code_challenge=challenge.decode("ascii").rstrip("="),
            code_challenge_method="S256",
        )
    )
    return AuthorizationRequest(
        config.login_origin + "/services/oauth2/authorize?" + query, reference
    )


def _parse_tokens(config, response, client_secret, *, previous=None):
    value = response.json()
    require(type(value) is dict and value.get("token_type") == "Bearer", "invalid_oauth_response")
    require(
        type(value.get("instance_url")) is str
        and value["instance_url"] in (config.api_origin, config.api_origin + "/"),
        "invalid_oauth_response",
    )
    identity = identity_url(config, value.get("id"))
    if "scope" in value:
        scope = value["scope"]
        require(type(scope) is str, "invalid_oauth_response")
        scopes = scope.split(" ")
        require(
            len(scopes) == len(set(scopes))
            and {"api", "refresh_token"} <= set(scopes)
            and set(scopes) <= {"api", "refresh_token", "id"},
            "invalid_oauth_response",
        )
    expiry = None
    if "expires_in" in value:
        seconds = value["expires_in"]
        require(type(seconds) is int and 0 < seconds <= 86400, "invalid_oauth_response")
        expiry = response.received_at + timedelta(seconds=seconds)
    refresh_token = value.get("refresh_token")
    if previous is not None:
        if config.refresh_rotation == "required":
            require(
                "refresh_token" in value and refresh_token != previous.refresh_token,
                "refresh_rotation_missing",
            )
        elif "refresh_token" not in value:
            refresh_token = previous.refresh_token
    material = TokenMaterial(value.get("access_token"), refresh_token, expiry, client_secret)
    return material, identity


def finish_oauth(
    config, session_store, token_store, callback, http, clock, *, client_secret=None, progress=None
):
    require(progress is None or type(progress) is OAuthProgress, "invalid_oauth_diagnostic")
    mark = progress.advance if progress is not None else lambda stage: None
    mark("callback_validation")
    require(type(config) is ConnectionConfig, "invalid_connection_config")
    config.__post_init__()
    require(type(session_store) is OAuthSessionStore, "oauth_state_invalid")
    if type(callback) is dict:
        items = list(callback.items())
    else:
        require(type(callback) in (list, tuple), "invalid_oauth_callback")
        items = list(callback)
    states = [
        item[1]
        for item in items
        if type(item) in (tuple, list) and len(item) == 2 and item[0] == "state"
    ]
    unambiguous = (
        bool(states)
        and type(states[0]) is str
        and all(type(state) is str and state == states[0] for state in states)
    )
    if not unambiguous:
        session_store._discard(states)
        raise CRMRefused("invalid_oauth_callback") from None
    mark("state_validation")
    verifier = session_store._take(config, states[0], clock)
    mark("callback_validation")
    require(
        all(
            type(item) in (tuple, list) and len(item) == 2 and type(item[0]) is str
            for item in items
        ),
        "invalid_oauth_callback",
    )
    require(len(items) == len({item[0] for item in items}), "invalid_oauth_callback")
    callback = dict(items)
    require(set(callback) in ({"state", "code"}, {"state", "error"}), "invalid_oauth_callback")
    try:
        for value in callback.values():
            private_text(value)
    except CRMRefused:
        raise CRMRefused("invalid_oauth_callback") from None
    if "error" in callback:
        raise CRMRefused("oauth_authorization_denied") from None
    mark("credential_precondition")
    if client_secret is not None:
        private_text(client_secret)
    require(
        not (config.code_secret_required or config.refresh_secret_required)
        or client_secret is not None,
        "oauth_client_secret_required",
    )
    require(http.config_binding == config.binding, "invalid_connection_binding")
    try:
        token_store.status(config.connection_id, config.binding)
    except CRMRefused as error:
        if error.reason != "token_material_missing":
            raise
    else:
        raise CRMRefused("token_record_exists")
    form = dict(
        grant_type="authorization_code",
        client_id=config.client_id,
        redirect_uri=config.callback_uri,
        code=callback["code"],
        code_verifier=verifier,
    )
    if config.code_secret_required:
        form["client_secret"] = client_secret
    mark("token_exchange")
    response = http.post_token(form)
    mark("token_response")
    material, identity = _parse_tokens(config, response, client_secret)
    mark("connection_verification")
    confirmed = verify_material(config, material, http, clock, identity=identity, progress=progress)
    mark("credential_installation")
    token_store.install(config.connection_id, config.binding, material)
    return confirmed


def refresh_connection(config, token_store, http, clock):
    require(type(config) is ConnectionConfig, "invalid_connection_config")
    config.__post_init__()
    require(http.config_binding == config.binding, "invalid_connection_binding")
    ready = token_store.read_ready(config.connection_id, config.binding)
    if config.refresh_secret_required:
        require(ready.material.client_secret is not None, "oauth_client_secret_required")
    claim = token_store.begin_refresh(
        config.connection_id, config.binding, expected_generation=ready.generation
    )
    try:
        form = dict(
            grant_type="refresh_token",
            client_id=config.client_id,
            refresh_token=claim.material.refresh_token,
        )
        if config.refresh_secret_required:
            form["client_secret"] = claim.material.client_secret
        material, identity = _parse_tokens(
            config, http.post_token(form), claim.material.client_secret, previous=claim.material
        )
        confirmed = verify_material(config, material, http, clock, identity=identity)
        token_store.finish_refresh(claim, material)
        return confirmed
    except Exception as error:
        # Failure never permits another grant. If persistence is already uncertain,
        # fail_refresh itself refuses and the durable pending/paused state is retained.
        try:
            token_store.fail_refresh(claim)
        except CRMRefused:
            pass
        if isinstance(error, CRMRefused):
            raise error from None
        raise CRMRefused("oauth_refresh_uncertain") from None
