"""OAuth orchestration over real private files and explicitly injected fake HTTP."""

import base64
import hashlib
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.mapping import MAPPING_HASH
from flight_recorder.adapters.crm.secrets import TokenMaterial, TokenStore

ORG = "00D000000000001EAA"
USER = "005000000000001AAA"
ACCOUNT = "001000000000001AAA"
BASIS = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
ORIGIN = "https://fictional.my.salesforce.com"
LOGIN = "https://login.salesforce.com"


def feature(module, name):
    try:
        value = importlib.import_module("flight_recorder.adapters.crm." + module)
    except ModuleNotFoundError:
        pytest.fail("missing owned Salesforce auth/read boundary", pytrace=False)
    if not hasattr(value, name):
        pytest.fail("missing owned Salesforce auth/read boundary", pytrace=False)
    return getattr(value, name)


def configuration(**updates):
    values = dict(
        connection_id="fictional-salesforce",
        provider="salesforce",
        expected_instance_id=ORG,
        expected_user_id=USER,
        login_origin=LOGIN,
        api_origin=ORIGIN,
        identity_origin=LOGIN,
        api_version="64.0",
        client_id="fictional-public-client-id",
        callback_uri="http://localhost:8768/api/internal/crm/oauth/salesforce/callback",
        credential_reference="fictional-private-handle",
        mapping_hash=MAPPING_HASH,
        artifact_hash="a" * 64,
        evaluator_version="evaluator-v1",
        refresh_rotation="required",
        code_secret_required=True,
        refresh_secret_required=True,
        record_ids=(ACCOUNT,),
        write_enabled=False,
    )
    values.update(updates)
    return feature("contracts", "ConnectionConfig")(**values)


class Clock:
    def __init__(self):
        self.value = BASIS

    def __call__(self):
        return self.value


def private_store(tmp_path):
    directory = tmp_path / "private-tokens"
    directory.mkdir(mode=0o700)
    return TokenStore(directory)


def token_response(**updates):
    result = dict(
        access_token="fictional-access-one",
        refresh_token="fictional-refresh-one",
        token_type="Bearer",
        instance_url=ORIGIN,
        id=f"{LOGIN}/id/{ORG}/{USER}",
        scope="id api refresh_token",
        issued_at="1792616400000",
    )
    result.update(updates)
    return result


def identity_response(**updates):
    result = dict(
        id=f"{LOGIN}/id/{ORG}/{USER}",
        organization_id=ORG,
        user_id=USER,
        asserted_user=True,
        active=True,
        username="fictional@example.invalid",
        photos={"picture": "https://ignored.invalid"},
    )
    result.update(updates)
    return result


def fake_provider(clock, *, token=None, identity=None, versions=None, account=None, config=None):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host in {"login.salesforce.com", "fictional.my.salesforce.com"}
        if request.url.path == "/services/oauth2/token":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json=token(request)
                if callable(token)
                else token
                if token is not None
                else token_response(),
            )
        if request.url.path.startswith("/id/"):
            assert request.method == "GET"
            return httpx.Response(
                200, json=identity if identity is not None else identity_response()
            )
        if request.url.path == "/services/data":
            return httpx.Response(
                200,
                json=versions
                if versions is not None
                else [
                    {
                        "version": "64.0",
                        "label": "fictional pinned version",
                        "url": "/services/data/v64.0",
                    }
                ],
            )
        if "/sobjects/Account/" in request.url.path and account is not None:
            return account(request)
        pytest.fail("unexpected provider request")

    http = feature("http", "OwnedHTTP")(
        config or configuration(), transport=httpx.MockTransport(handler), clock=clock
    )
    return http, requests


def start(config, clock):
    sessions = feature("oauth", "OAuthSessionStore")()
    request = feature("oauth", "begin_oauth")(config, sessions, clock)
    query = parse_qs(urlsplit(request.url).query)
    return sessions, request, query


def finish(config, sessions, store, callback, http, clock, secret="fictional-client-secret"):
    return feature("oauth", "finish_oauth")(
        config, sessions, store, callback, http, clock, client_secret=secret
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "hubspot"),
        ("expected_instance_id", ORG[:15]),
        ("expected_user_id", USER[:15]),
        ("record_ids", [ACCOUNT]),
        ("record_ids", (ACCOUNT, ACCOUNT)),
        ("record_ids", ()),
        ("api_version", "latest"),
        ("api_version", "64"),
        ("write_enabled", True),
        ("write_enabled", 0),
        ("code_secret_required", 1),
        ("mapping_hash", "b" * 64),
        ("refresh_rotation", "auto"),
        ("artifact_hash", "A" * 64),
        ("client_id", ""),
        ("credential_reference", "bad\n"),
        ("callback_uri", "http://127.0.0.1:8768/api/internal/crm/oauth/salesforce/callback"),
    ],
)
def test_invalid_configuration_never_becomes_authority(field, value):
    with pytest.raises(CRMRefused):
        configuration(**{field: value})


@pytest.mark.parametrize(
    "origin",
    [
        "http://fictional.my.salesforce.com",
        "https://fictional.my.salesforce.com/",
        "https://user@fictional.my.salesforce.com",
        "https://fictional.my.salesforce.com:443",
        "https://fictional.my.salesforce.com.evil.invalid",
        "https://127.0.0.1",
        "https://fictional.my.salesforce.com?secret=x",
        "https://fictional.my.salesforce.com#x",
        "https://fictional.my.salesforce.com.",
        "https://FICTIONAL.my.salesforce.com",
        "https://fictional.my.salesforce.com\\@evil.invalid",
        "https://fictión.my.salesforce.com",
    ],
)
def test_non_pinned_origin_shapes_are_refused(origin):
    with pytest.raises(CRMRefused):
        configuration(api_origin=origin)


def test_config_binding_changes_with_credential_and_rule_selection():
    original = configuration()
    assert original.binding != configuration(credential_reference="other-private-handle").binding
    assert original.binding != configuration(artifact_hash="b" * 64).binding
    document = original.document
    document["record_ids"].clear()
    assert original.record_ids == (ACCOUNT,)


def test_pkce_and_initial_oauth_verify_then_persist_without_account_read(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, authorization, query = start(config, clock)
    assert query["scope"] == ["api refresh_token"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [config.callback_uri]
    assert "verifier" not in query
    http, requests = fake_provider(clock)
    result = finish(
        config,
        sessions,
        store,
        {"state": query["state"][0], "code": "fictional-authorization-code"},
        http,
        clock,
    )
    assert result.instance_id == ORG and result.user_id == USER
    assert result.observation_origin == "injected_transport"
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    form = parse_qs(requests[0].content.decode())
    verifier = form["code_verifier"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert expected.decode().rstrip("=") == query["code_challenge"][0]
    assert 43 <= len(verifier) <= 128 and len(query["state"][0]) >= 43
    assert form["client_secret"] == ["fictional-client-secret"]
    assert "fictional-authorization-code" not in repr(authorization)
    assert "fictional-access" not in repr(result)
    read = store.read_ready(config.connection_id, config.binding)
    assert read.material.access_token == "fictional-access-one"
    assert read.material.refresh_token == "fictional-refresh-one"
    assert read.material.expires_at is None
    assert read.material.client_secret == "fictional-client-secret"
    http.close()
    store.close()


@pytest.mark.parametrize("kind", ["wrong", "expired", "exact_expiry", "cross_config", "restart"])
def test_state_refusal_sends_no_token_request(tmp_path, kind):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    state = query["state"][0]
    if kind == "wrong":
        state = "wrong-state"
    elif kind in {"expired", "exact_expiry"}:
        clock.value += timedelta(seconds=601 if kind == "expired" else 600)
    elif kind == "cross_config":
        config = configuration(client_id="different-app")
    else:
        sessions = feature("oauth", "OAuthSessionStore")()
    http, requests = fake_provider(clock)
    with pytest.raises(CRMRefused):
        finish(config, sessions, store, {"state": state, "code": "fictional-code"}, http, clock)
    assert requests == []
    with pytest.raises(CRMRefused, match="token_material_missing"):
        store.load(config.connection_id, config.binding)
    http.close()
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"instance_url": "https://evil.invalid"},
        {"id": f"https://evil.invalid/id/{ORG}/{USER}"},
        {"id": f"{LOGIN}/id/00D000000000002EAA/{USER}"},
        {"refresh_token": None},
        {"refresh_token": ""},
        {"token_type": "Basic"},
        {"scope": "id api refresh_token full"},
        {"expires_in": True},
    ],
)
def test_bad_token_response_cannot_install_or_access_account(tmp_path, change):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock, token=token_response(**change))
    with pytest.raises(CRMRefused):
        finish(
            config,
            sessions,
            store,
            {"state": query["state"][0], "code": "fictional-code"},
            http,
            clock,
        )
    assert len(requests) == 1 and requests[0].method == "POST"
    with pytest.raises(CRMRefused, match="token_material_missing"):
        store.load(config.connection_id, config.binding)
    with pytest.raises(CRMRefused):
        finish(
            config,
            sessions,
            store,
            {"state": query["state"][0], "code": "fictional-code"},
            http,
            clock,
        )
    assert len(requests) == 1
    http.close()
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"organization_id": "00D000000000002EAA"},
        {"user_id": "005000000000002AAA"},
        {"active": False},
        {"asserted_user": 1},
    ],
)
def test_wrong_identity_success_cannot_authorize_business_read(tmp_path, change):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock, identity=identity_response(**change))
    with pytest.raises(CRMRefused):
        finish(
            config,
            sessions,
            store,
            {"state": query["state"][0], "code": "fictional-code"},
            http,
            clock,
        )
    assert [r.method for r in requests] == ["POST", "GET"]
    with pytest.raises(CRMRefused, match="token_material_missing"):
        store.load(config.connection_id, config.binding)
    http.close()
    store.close()


def test_source_fifteen_character_identity_is_explicitly_canonicalized(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(
        clock,
        token=token_response(id=f"{LOGIN}/id/{ORG[:15]}/{USER[:15]}"),
        identity=identity_response(
            organization_id=ORG[:15], user_id=USER[:15], id=f"{LOGIN}/id/{ORG[:15]}/{USER[:15]}"
        ),
    )
    result = finish(
        config, sessions, store, {"state": query["state"][0], "code": "fictional-code"}, http, clock
    )
    assert result.instance_id == ORG and result.user_id == USER
    assert requests[1].url.path == f"/id/{ORG[:15]}/{USER[:15]}"
    http.close()
    store.close()


def test_concurrent_reused_callback_has_one_exchange(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock)
    barrier = threading.Barrier(2)

    def call():
        barrier.wait(timeout=5)
        try:
            finish(
                config,
                sessions,
                store,
                {"state": query["state"][0], "code": "fictional-code"},
                http,
                clock,
            )
            return "connected"
        except CRMRefused:
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    assert sorted(results) == ["connected", "refused"]
    assert sum(r.method == "POST" for r in requests) == 1
    http.close()
    store.close()


def test_existing_token_record_prevents_new_grant(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    store.install(
        config.connection_id,
        config.binding,
        TokenMaterial("fictional-old-access", "fictional-old-refresh", None),
    )
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock)
    with pytest.raises(CRMRefused):
        finish(
            config,
            sessions,
            store,
            {"state": query["state"][0], "code": "fictional-code"},
            http,
            clock,
        )
    assert requests == []
    assert store.read_ready(config.connection_id, config.binding).generation == 1
    http.close()
    store.close()


def seed_tokens(store, config):
    old = TokenMaterial(
        "fictional-old-access", "fictional-old-refresh", None, "fictional-client-secret"
    )
    store.install(config.connection_id, config.binding, old)
    return old


def test_explicit_refresh_persists_rotated_tokens_before_return(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    seed_tokens(store, config)
    http, requests = fake_provider(clock, token=token_response(expires_in=120))
    result = feature("oauth", "refresh_connection")(config, store, http, clock)
    assert result.instance_id == ORG
    read = store.read_ready(config.connection_id, config.binding)
    assert read.generation == 3 and read.material.refresh_token == "fictional-refresh-one"
    assert read.material.expires_at == BASIS + timedelta(seconds=120)
    assert read.material.client_secret == "fictional-client-secret"
    form = parse_qs(requests[0].content.decode())
    assert form["refresh_token"] == ["fictional-old-refresh"]
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    http.close()
    store.close()


@pytest.mark.parametrize("mode", ["required", "nonrotating"])
def test_only_explicit_nonrotating_mode_retains_omitted_refresh_token(tmp_path, mode):
    config, clock = configuration(refresh_rotation=mode), Clock()
    store = private_store(tmp_path)
    seed_tokens(store, config)
    response = token_response()
    del response["refresh_token"]
    http, requests = fake_provider(clock, token=response, config=config)
    if mode == "nonrotating":
        feature("oauth", "refresh_connection")(config, store, http, clock)
        assert (
            store.load(config.connection_id, config.binding).refresh_token
            == "fictional-old-refresh"
        )
    else:
        with pytest.raises(CRMRefused):
            feature("oauth", "refresh_connection")(config, store, http, clock)
        assert store.status(config.connection_id, config.binding).state == "needs_attention"
        with pytest.raises(CRMRefused):
            feature("oauth", "refresh_connection")(config, store, http, clock)
    assert sum(r.method == "POST" for r in requests) == 1
    http.close()
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"refresh_token": None},
        {"refresh_token": ""},
        {"refresh_token": "fictional-old-refresh"},
        {"instance_url": "https://evil.invalid"},
    ],
)
def test_invalid_rotation_blocks_old_material_without_second_post(tmp_path, change):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    seed_tokens(store, config)
    http, requests = fake_provider(clock, token=token_response(**change))
    with pytest.raises(CRMRefused):
        feature("oauth", "refresh_connection")(config, store, http, clock)
    assert store.status(config.connection_id, config.binding).state == "needs_attention"
    for action in [
        lambda: store.load(config.connection_id, config.binding),
        lambda: feature("oauth", "refresh_connection")(config, store, http, clock),
    ]:
        with pytest.raises(CRMRefused):
            action()
    assert len(requests) == 1
    http.close()
    store.close()


@pytest.mark.parametrize("different_store", [False, True])
def test_pending_refresh_across_callers_has_exactly_one_post(tmp_path, different_store):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    seed_tokens(store, config)
    other = TokenStore(tmp_path / "private-tokens") if different_store else store
    entered, release = threading.Event(), threading.Event()

    def held_response(request):
        entered.set()
        assert release.wait(timeout=5)
        return token_response()

    http, requests = fake_provider(clock, token=held_response)
    refresh = feature("oauth", "refresh_connection")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(refresh, config, store, http, clock)
        assert entered.wait(timeout=5)
        second = pool.submit(refresh, config, other, http, clock)
        with pytest.raises(CRMRefused):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).instance_id == ORG
    assert sum(r.method == "POST" for r in requests) == 1
    assert other.read_ready(config.connection_id, config.binding).generation == 3
    http.close()
    store.close()
    other.close()


def test_rotated_token_persistence_failure_never_reuses_old_token(tmp_path, monkeypatch):
    import os

    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    seed_tokens(store, config)
    http, requests = fake_provider(clock)
    original = os.fsync
    calls = 0

    def fail_rotated_save(fd):
        nonlocal calls
        calls += 1
        if calls == 3:  # begin_refresh completed both fsyncs; fail finish_refresh's file fsync.
            raise OSError("fictional-refresh-one must never leak")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_rotated_save)
        with pytest.raises(CRMRefused, match="secret_persistence_uncertain"):
            feature("oauth", "refresh_connection")(config, store, http, clock)
    store.close()
    reopened = TokenStore(tmp_path / "private-tokens")
    assert reopened.status(config.connection_id, config.binding).state == "refresh_started"
    with pytest.raises(CRMRefused):
        feature("oauth", "refresh_connection")(config, reopened, http, clock)
    assert sum(r.method == "POST" for r in requests) == 1
    http.close()
    reopened.close()


@pytest.mark.parametrize("shape", ["duplicate_code", "duplicate_state", "unknown", "bad_error"])
def test_matched_malformed_callback_is_consumed_without_exchange(tmp_path, shape):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    state = query["state"][0]
    if shape == "duplicate_code":
        callback = [("state", state), ("code", "fictional-one"), ("code", "fictional-two")]
    elif shape == "duplicate_state":
        callback = [("state", state), ("state", state), ("code", "fictional-code")]
    elif shape == "unknown":
        callback = {"state": state, "code": "fictional-code", "extra": "fictional-secret"}
    else:
        callback = {"state": state, "error": None}
    http, requests = fake_provider(clock)
    with pytest.raises(CRMRefused, match="^invalid_oauth_callback$"):
        finish(config, sessions, store, callback, http, clock)
    with pytest.raises(CRMRefused, match="^oauth_state_invalid$"):
        finish(config, sessions, store, {"state": state, "code": "fictional-code"}, http, clock)
    assert requests == []
    http.close()
    store.close()


def test_denied_callback_is_consumed_without_exchange(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock)
    callback = {"state": query["state"][0], "error": "access_denied"}
    with pytest.raises(CRMRefused, match="oauth_authorization_denied"):
        finish(config, sessions, store, callback, http, clock)
    with pytest.raises(CRMRefused, match="oauth_state_invalid"):
        finish(config, sessions, store, callback, http, clock)
    assert requests == []
    http.close()
    store.close()


def test_session_limit_is_bounded_and_expired_entries_release_capacity():
    config, clock = configuration(), Clock()
    sessions = feature("oauth", "OAuthSessionStore")()
    begin = feature("oauth", "begin_oauth")
    urls = {begin(config, sessions, clock).url for _ in range(16)}
    assert len(urls) == 16
    with pytest.raises(CRMRefused, match="oauth_sessions_full"):
        begin(config, sessions, clock)
    clock.value += timedelta(seconds=600)
    assert begin(config, sessions, clock).url not in urls


def test_unsupported_api_version_does_not_install_tokens(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(
        clock,
        versions=[
            {"version": "65.0", "url": "/services/data/v65.0", "label": "fictional other version"}
        ],
    )
    with pytest.raises(CRMRefused, match="unsupported_api_version"):
        finish(
            config,
            sessions,
            store,
            {"state": query["state"][0], "code": "fictional-code"},
            http,
            clock,
        )
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    with pytest.raises(CRMRefused, match="token_material_missing"):
        store.load(config.connection_id, config.binding)
    http.close()
    store.close()


def test_initial_token_save_failure_cannot_return_connected_or_reexchange(tmp_path, monkeypatch):
    import os

    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    sessions, _, query = start(config, clock)
    http, requests = fake_provider(clock)
    callback = {"state": query["state"][0], "code": "fictional-code"}

    def failed_sync(fd):
        raise OSError("fictional-refresh-one must never leak")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed_sync)
        with pytest.raises(CRMRefused, match="secret_persistence_uncertain"):
            finish(config, sessions, store, callback, http, clock)
    with pytest.raises(CRMRefused, match="oauth_state_invalid"):
        finish(config, sessions, store, callback, http, clock)
    assert sum(r.method == "POST" for r in requests) == 1
    assert not list((tmp_path / "private-tokens").glob("*.json"))
    http.close()
    store.close()
