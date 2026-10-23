"""The private network qualifier has no CRM, OAuth, capture or writer resources."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flight_recorder.adapters.crm.cli import N8nBinding
from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.read_control import ReadEvidenceStore
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.crm_app import create_crm_network_check_app
from tests.adapters.crm.test_crm_ingress import HEADERS
from tests.adapters.crm.test_poll import config
from tests.adapters.crm.test_read_controls import n8n_binding_document, private_control_file
from tests.adapters.crm.test_readiness import EARLIER, LATER, NOW


def network_setup(tmp_path, *, clock=lambda: NOW):
    cfg = config(tmp_path)
    directory = tmp_path / "read-evidence"
    directory.mkdir(mode=0o700)
    binding = N8nBinding.from_document(n8n_binding_document(cfg), cfg)
    binding_sha = ReadEvidenceStore(directory).save(binding.document)
    grant = dict(
        format_version=1,
        config_sha256=cfg.binding,
        owner_request_sha256=binding.document["owner_request_sha256"],
        mode=binding.document["mode"],
        probe_id=binding.document["probe_id"],
        issued_at=EARLIER,
        expires_at=LATER,
        binding_sha256=binding_sha,
    )
    path = private_control_file(tmp_path, "network.json", grant)
    app = create_crm_network_check_app(cfg, path, directory, clock=clock)
    body = dict(
        format_version=1, connection_id=cfg.connection.connection_id, probe_id=grant["probe_id"]
    )
    return cfg, directory, path, grant, app, body


def test_network_check_is_the_only_route_and_constructs_no_crm_resources(tmp_path, monkeypatch):
    import flight_recorder.adapters.crm.secrets as secrets_module
    import flight_recorder.crm_app as module

    def forbidden(*args, **kwargs):
        pytest.fail("network check constructed a CRM resource")

    monkeypatch.setattr(module, "CRMRuntime", forbidden)
    monkeypatch.setattr(module, "create_app", forbidden)
    monkeypatch.setattr(module, "OAuthSessionStore", forbidden)
    monkeypatch.setattr(secrets_module, "TokenStore", forbidden)
    cfg, directory, path, grant, app, body = network_setup(tmp_path)
    assert [(r.path, r.methods) for r in app.routes] == [
        ("/api/internal/crm/network-check", {"POST"})
    ], "qualifier must expose exactly one no-work route"
    before = set(directory.iterdir())
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.post("/api/internal/crm/network-check", json=body, headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == dict(
            state="network_check_received",
            config_sha256=cfg.binding,
            probe_sha256=canonical_hash(grant["probe_id"]),
            body_sha256=canonical_hash(body),
        )
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "fictional-ingress-secret" not in response.text
        assert "ready" not in response.text
    created = set(directory.iterdir()) - before
    assert len(created) == 1
    import json

    observation = json.loads(created.pop().read_bytes())
    assert observation["type"] == "n8n_network_observation"
    assert observation["body_sha256"] == canonical_hash(body)
    assert observation["binding_sha256"] == grant["binding_sha256"]
    assert observation["received_host"] == "localhost:8768"
    assert "fictional-ingress-secret" not in str(observation)
    assert not Path(cfg.document["domain_path"]).exists()
    assert not Path(cfg.document["operational_path"]).exists()


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("extra", 400),
        ("wrong_probe", 400),
        ("wrong_connection", 400),
        ("bool_version", 400),
        ("query", 400),
        ("bad_host", 403),
        ("missing_bearer", 401),
        ("duplicate_host", 400),
        ("bad_json", 400),
        ("bad_type", 415),
        ("wrong_framing", 400),
        ("get", 405),
        ("poll", 404),
        ("docs", 404),
    ],
)
def test_network_check_bad_request_creates_no_receipt(tmp_path, mutation, status):
    cfg, directory, path, grant, app, body = network_setup(tmp_path)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    headers, url, method, content = dict(HEADERS), "/api/internal/crm/network-check", "POST", None
    if mutation == "extra":
        body["tick_id"] = "unapproved"
    if mutation == "wrong_probe":
        body["probe_id"] = "f" * 64
    if mutation == "wrong_connection":
        body["connection_id"] = "other"
    if mutation == "bool_version":
        body["format_version"] = True
    if mutation == "query":
        url += "?extra=1"
    if mutation == "bad_host":
        headers["Host"] = "host.docker.internal:8768"
    if mutation == "missing_bearer":
        headers.pop("Authorization")
    if mutation == "duplicate_host":
        headers = [*headers.items(), ("Host", "localhost:8768"), ("Host", "localhost:8768")]
    if mutation == "bad_json":
        content = b'{"format_version":1,"format_version":1}'
    if mutation == "bad_type":
        headers["Content-Type"] = "text/plain"
    if mutation == "wrong_framing":
        headers["Content-Length"] = "1"
    if mutation == "get":
        method = "GET"
    if mutation == "poll":
        url = "/api/internal/crm/poll"
    if mutation == "docs":
        url, method = "/docs", "GET"
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.request(
            method, url, headers=headers, **({"content": content} if content else {"json": body})
        )
        assert response.status_code == status
    assert {p: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize("change", ["grant", "binding", "secret", "expiry"])
def test_network_check_rechecks_private_files_and_time_on_every_probe(tmp_path, change):
    from datetime import timedelta

    now = [NOW]
    cfg, directory, path, grant, app, body = network_setup(tmp_path, clock=lambda: now[0])
    with TestClient(app, base_url="http://localhost:8768") as client:
        if change == "grant":
            path.write_bytes(path.read_bytes())
        if change == "binding":
            file = directory / (grant["binding_sha256"] + ".json")
            file.write_bytes(file.read_bytes())
        if change == "secret":
            file = Path(cfg.document["ingress_secret_file"])
            file.write_bytes(file.read_bytes())
        if change == "expiry":
            now[0] += timedelta(hours=1)
        before = set(directory.iterdir())
        response = client.post("/api/internal/crm/network-check", json=body, headers=HEADERS)
        assert response.status_code == 403, "changed private authority must refuse before recording"
        assert set(directory.iterdir()) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_sha256", "f" * 64),
        ("mode", "host_network"),
        ("probe_id", "0" * 64),
        ("owner_request_sha256", "e" * 64),
        ("extra", True),
    ],
)
def test_network_grant_must_match_closed_binding(tmp_path, field, value):
    cfg, directory, path, grant, app, body = network_setup(tmp_path)
    grant[field] = value
    from flight_recorder.collector.canonical import canonical_bytes

    path.write_bytes(canonical_bytes(grant))
    with pytest.raises(CRMRefused):
        create_crm_network_check_app(cfg, path, directory, clock=lambda: NOW)


def read_app_setup(tmp_path, *, action="oauth_connect", clock=lambda: NOW):
    from flight_recorder.adapters.crm.cli import initialize
    from flight_recorder.adapters.crm.runtime import CRMRuntime
    from flight_recorder.crm_app import create_crm_read_app
    from tests.adapters.crm.test_poll import Provider
    from tests.adapters.crm.test_read_controls import capture_gate_setup, read_gate_setup

    if action == "capture_once":
        cfg, m, a, paths, directory, gate = capture_gate_setup(tmp_path, clock=clock)
    else:
        cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path, action=action, clock=clock)
    initialize(cfg)
    provider = Provider()
    runtime = CRMRuntime(cfg, client_factory=lambda: provider, clock=clock)
    app = create_crm_read_app(gate, runtime=runtime)
    return app, runtime, provider, paths


def csrf_from_page(response):
    import re

    found = re.search(r'name="csrf" value="([A-Za-z0-9_-]{43})"', response.text)
    assert found is not None, "handoff page must issue a real CSRF token"
    return found[1]


def test_read_gets_never_contact_provider_or_queue_work(tmp_path):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.get("/crm/read")
        assert response.status_code == 200, "explicit read app must show the handoff"
        token = csrf_from_page(response)
        assert token not in repr(app)
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        # A normal form POST with no-referrer sends Origin:null in a real browser.
        # The connect page must preserve its same-origin POST without leaking a
        # referrer to Salesforce. Redirect/callback responses still suppress it.
        assert response.headers["referrer-policy"] == "same-origin"
        result = client.get("/crm/read/result")
        assert result.status_code == 200
        assert result.headers["referrer-policy"] == "same-origin"
        assert provider.calls == provider.verifications == 0 and runtime.store.receipt_count() == 0


def test_live_handoff_never_serializes_authorization_url(tmp_path):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            json={"csrf": token},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            runtime.config.connection.login_origin + "/services/oauth2/authorize?"
        )
        assert "authorization_url" not in response.text and "code_challenge" not in response.text
        assert (
            client.post(
                "/api/internal/crm/oauth/salesforce/begin",
                headers=HEADERS,
                json={"connection_id": "fictional-salesforce"},
            ).status_code
            == 404
        )
        assert provider.calls == 0 and runtime._http is None


@pytest.mark.parametrize(
    "change",
    ["no_cookie", "wrong_token", "wrong_origin", "wrong_host", "extra", "wrong_action", "oversize"],
)
def test_read_posts_require_same_origin_csrf_and_closed_body(tmp_path, change):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        body, headers, path = (
            {"csrf": token},
            {"Origin": "http://localhost:8768"},
            "/crm/read/connect",
        )
        if change == "no_cookie":
            client.cookies.clear()
        if change == "wrong_token":
            body["csrf"] = "wrong"
        if change == "wrong_origin":
            headers["Origin"] = "http://evil.invalid"
        if change == "wrong_host":
            headers["Host"] = "127.0.0.1:8768"
        if change == "extra":
            body["authority_path"] = "/tmp/other"
        if change == "wrong_action":
            path = "/crm/read/metadata"
        if change == "oversize":
            body["csrf"] = "x" * 5000
        response = client.post(path, json=body, headers=headers, follow_redirects=False)
        assert response.status_code in {400, 403, 413}
        assert runtime._http is runtime._tokens is None
        assert provider.calls == provider.verifications == 0 and runtime.store.receipt_count() == 0


def test_callback_cannot_adopt_new_authority(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    import flight_recorder.adapters.crm.oauth as oauth

    app, runtime, provider, paths = read_app_setup(tmp_path)
    calls = []
    monkeypatch.setattr(oauth, "finish_oauth", lambda *a, **kw: calls.append(True))
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            json={"csrf": token},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        paths[2].write_bytes(paths[2].read_bytes())
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": state, "code": "fictional-code"},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/crm/read/result"
        assert calls == [] and runtime._http is runtime._tokens is None
        assert "fictional-code" not in response.text and "?" not in response.headers["location"]


def test_callback_failure_redirect_is_query_free_and_redacted(tmp_path):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback?state=bad&code=fictional-secret-code",
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/crm/read/result"
        assert "fictional-secret" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "refused" in client.get("/crm/read/result").text


@pytest.mark.parametrize("action", ["metadata_probe", "capture_once"])
def test_non_oauth_pages_keep_referrer_suppression(tmp_path, action):
    app, runtime, provider, paths = read_app_setup(tmp_path, action=action)
    with TestClient(app, base_url="http://localhost:8768") as client:
        for path in ("/crm/read", "/crm/read/result"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["referrer-policy"] == "no-referrer"
        assert runtime._http is runtime._tokens is None
        assert provider.calls == provider.verifications == 0


@pytest.mark.parametrize("change", [None, "csrf", "origin", "extra", "authority"])
def test_metadata_route_never_upgrades_injected_alias_observation(tmp_path, change):
    """Exercise the real route/client without pretending synthetic HTTP is live proof."""
    import json

    from flight_recorder.adapters.crm.secrets import TokenMaterial, TokenStore
    from tests.adapters.crm.test_oauth import fake_provider

    app, runtime, provider, paths = read_app_setup(tmp_path, action="metadata_probe")
    cfg = runtime.config.connection
    runtime._client_factory = None
    runtime._tokens = TokenStore(runtime.config.document["credential_directory"])
    runtime._tokens.install(
        cfg.connection_id, cfg.binding, TokenMaterial("fictional-access", "fictional-refresh", None)
    )
    runtime._http, requests = fake_provider(
        runtime.clock,
        config=cfg,
        versions=[
            {"version": "65.0", "url": "/services/data/latest"},
            {"version": "64.0", "url": "/services/data/v64.0"},
        ],
    )
    runtime._http._before_attempt = runtime._before_read_attempt
    directory = runtime.read_gate.evidence.directory
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with TestClient(app, base_url="http://localhost:8768") as client:
        body = {"csrf": csrf_from_page(client.get("/crm/read"))}
        headers = {"Origin": "http://localhost:8768"}
        if change == "csrf":
            body["csrf"] = "wrong"
        if change == "origin":
            headers["Origin"] = "http://foreign.invalid"
        if change == "extra":
            body["target"] = "unapproved"
        if change == "authority":
            paths[2].write_bytes(paths[2].read_bytes())
        response = client.post(
            "/crm/read/metadata", json=body, headers=headers, follow_redirects=False
        )
        if change is not None:
            assert response.status_code in {400, 403}
        else:
            assert response.status_code == 303
            assert response.headers["location"] == "/crm/read/result"
            result = client.get("/crm/read/result")
            assert "refused" in result.text and "metadata_observed" not in result.text
            assert "fictional-access" not in result.text
        if change is None:
            assert runtime._confirmed_connection.observation_origin == "injected_transport"
            assert [r.url.path for r in requests] == [
                f"/id/{cfg.expected_instance_id}/{cfg.expected_user_id}",
                "/services/data",
            ]
        else:
            assert requests == []
        assert provider.calls == provider.verifications == 0
        assert runtime.store.receipt_count() == 0
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
        assert not any(
            json.loads(p.read_bytes()).get("type") == "metadata" for p in directory.iterdir()
        )


@pytest.mark.parametrize(
    "origins",
    [
        [],
        [("Origin", "null")],
        [("Origin", "http://other.invalid")],
        [("Origin", "http://localhost:8768"), ("Origin", "http://localhost:8768")],
    ],
)
def test_oauth_form_still_rejects_missing_null_foreign_and_duplicate_origins(tmp_path, origins):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect", data={"csrf": token}, headers=origins, follow_redirects=False
        )
        assert response.status_code == 403
        assert runtime._http is runtime._tokens is None
        assert provider.calls == provider.verifications == 0


def test_read_capture_uses_only_operator_tick_and_one_target(tmp_path):
    app, runtime, provider, paths = read_app_setup(tmp_path, action="capture_once")
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/capture",
            json={"csrf": token, "tick_id": "foreign"},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        assert response.status_code == 403 and provider.calls == 0
        tick = runtime.read_gate.authority.document["tick_id"]
        response = client.post(
            "/crm/read/capture",
            json={"csrf": token, "tick_id": tick},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/crm/read/result"
        assert provider.calls == 1
        assert "capture_complete" in client.get("/crm/read/result").text


def test_oauth_form_redirect_and_injected_callback_keep_origin_truthful(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    import flight_recorder.adapters.crm.oauth as oauth_module
    from tests.adapters.crm.test_oauth import fake_provider

    original = oauth_module.finish_oauth
    safe_failures = []

    def tracked_finish(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as exc:
            safe_failures.append(getattr(exc, "reason", type(exc).__name__))
            raise

    monkeypatch.setattr(oauth_module, "finish_oauth", tracked_finish)
    app, runtime, provider, paths = read_app_setup(tmp_path)
    secret = Path(runtime.config.document["credential_directory"]) / (
        runtime.config.connection.credential_reference + ".client-secret"
    )
    secret.write_text("fictional-client-secret")
    secret.chmod(0o600)
    with TestClient(app, base_url="http://localhost:8768") as client:
        token = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            data={"csrf": token},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        runtime._http, requests = fake_provider(runtime.clock, config=runtime.config.connection)
        runtime._http._before_attempt = runtime._before_read_attempt
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": state, "code": "fictional-code"},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/crm/read/result"
        assert runtime._confirmed_connection is not None, safe_failures
        assert runtime._confirmed_connection.observation_origin == "injected_transport"
        assert len(requests) == 3
        assert "connected" in client.get("/crm/read/result").text
        client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": state, "code": "fictional-code"},
            follow_redirects=False,
        )
        assert len(requests) == 3


def test_read_check_is_local_and_never_initializes_stores(tmp_path, capsys):
    from flight_recorder.adapters.crm.cli import read_command
    from tests.adapters.crm.test_read_controls import read_gate_setup

    cfg, m, a, paths, directory, gate = read_gate_setup(tmp_path)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    assert read_command("crm-read-check", *paths, directory, clock=lambda: NOW) == 0
    output = capsys.readouterr().out
    assert "read_session_checked" in output and "oauth_connect" in output
    assert not Path(cfg.document["domain_path"]).exists()
    assert not Path(cfg.document["operational_path"]).exists()
    assert {p: p.read_bytes() for p in directory.iterdir()} == before


def test_network_check_command_binds_only_loopback_and_disables_access_log(tmp_path, monkeypatch):
    import uvicorn

    from flight_recorder.adapters.crm.cli import network_check_command

    cfg, directory, path, grant, app, body = network_setup(tmp_path)
    config_path = private_control_file(tmp_path, "config.json", cfg.document)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert network_check_command(config_path, path, directory, clock=lambda: NOW) == 0
    assert len(calls) == 1
    assert calls[0][1] == dict(host="127.0.0.1", port=8768, workers=1, access_log=False)


def test_network_signature_race_returns_fixed_refusal_without_receipt(tmp_path, monkeypatch):
    import flight_recorder.adapters.crm.read_control as module

    cfg, directory, path, grant, app, body = network_setup(tmp_path)
    original = module._file_signature

    def removed(path):
        if path == Path(cfg.document["ingress_secret_file"]):
            path.unlink()
        return original(path)

    with TestClient(app, base_url="http://localhost:8768") as client:
        monkeypatch.setattr(module, "_file_signature", removed)
        before = set(directory.iterdir())
        response = client.post("/api/internal/crm/network-check", json=body, headers=HEADERS)
        assert response.status_code == 403
        assert response.json() == {"detail": "network_qualification_refused"}
        assert set(directory.iterdir()) == before
