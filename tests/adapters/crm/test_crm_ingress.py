"""Only the explicit local CRM app accepts closed, bounded authenticated work."""

import asyncio
import importlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flight_recorder.adapters.crm.contracts import CRMRefused
from tests.adapters.crm.test_crm_runtime import hashes
from tests.adapters.crm.test_poll import Provider, config, feature

HEADERS = {
    "Authorization": "Bearer fictional-ingress-secret-for-local-tests-only",
    "Content-Type": "application/json",
}
BODY = {"connection_id": "fictional-salesforce", "tick_id": "workflow:execution-1"}


def factory():
    try:
        return importlib.import_module("flight_recorder.crm_app").create_crm_app
    except (ModuleNotFoundError, AttributeError):
        pytest.fail("missing explicit authenticated CRM ingress app", pytrace=False)


def setup_app(tmp_path):
    cfg = config(tmp_path)
    feature("cli", "initialize")(cfg)
    provider = Provider()
    runtime = feature("runtime", "CRMRuntime")(
        cfg,
        client_factory=lambda: provider,
        clock=lambda: datetime(2026, 4, 17, 10, 10, tzinfo=UTC),
    )
    return factory()(cfg, runtime=runtime, background=False), runtime, provider


def test_202_means_durable_work_and_duplicate_delivery_uses_same_receipt(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.post("/api/internal/crm/poll", json=BODY, headers=HEADERS)
        assert response.status_code == 202 and response.json()["disposition"] == "active"
        assert runtime.store.receipt_count() == 1 and provider.calls == 0
        runtime.run_pending()
        response = client.post("/api/internal/crm/poll", json=BODY, headers=HEADERS)
        assert response.status_code == 202 and response.json()["disposition"] == "complete"
        assert runtime.store.receipt_count() == 1 and provider.calls == 1


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Content-Type": "application/json"}, 401),
        ({**HEADERS, "Authorization": "Bearer incorrect"}, 401),
        ({**HEADERS, "Host": "localhost.evil:8768"}, 403),
        ({**HEADERS, "Host": "127.0.0.1:8768"}, 403),
        ({**HEADERS, "Content-Type": "text/plain"}, 415),
        ({**HEADERS, "Content-Type": "application/json; charset=utf-16"}, 415),
    ],
)
def test_bad_security_headers_create_no_receipt(tmp_path, headers, status):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        assert (
            client.post(
                "/api/internal/crm/poll", content=json.dumps(BODY), headers=headers
            ).status_code
            == status
        )
        assert runtime.store.receipt_count() == 0 and provider.calls == 0


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"connection_id":"fictional-salesforce","tick_id":"t","record_id":"extra"}',
        b'{"connection_id":"fictional-salesforce","tick_id":"a","tick_id":"b"}',
        b'{"connection_id":"fictional-salesforce","tick_id":true}',
        b'{"connection_id":"foreign","tick_id":"t"}',
        b"{}\x00",
    ],
)
def test_closed_body_and_strict_utf8_fail_without_work(tmp_path, body):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        assert (
            client.post("/api/internal/crm/poll", content=body, headers=HEADERS).status_code == 400
        )
        assert runtime.store.receipt_count() == 0 and provider.calls == 0


def test_duplicate_authorization_and_query_are_refused_before_receipt(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        headers = list(HEADERS.items()) + [("Authorization", HEADERS["Authorization"])]
        assert client.post("/api/internal/crm/poll", json=BODY, headers=headers).status_code == 400
        assert (
            client.post(
                "/api/internal/crm/poll?record_id=foreign", json=BODY, headers=HEADERS
            ).status_code
            == 400
        )
        assert runtime.store.receipt_count() == 0 and provider.calls == 0


@pytest.mark.parametrize("length", [None, "1"])
def test_streaming_limit_stops_before_reading_remaining_chunks(tmp_path, length):
    app, runtime, provider = setup_app(tmp_path)
    headers = [
        (b"host", b"localhost:8768"),
        (b"authorization", HEADERS["Authorization"].encode()),
        (b"content-type", b"application/json"),
    ]
    if length is not None:
        headers.append((b"content-length", length.encode()))
    chunks = [b" " * (1024 * 1024), b"x", b"never-read"]
    consumed, sent = [], []

    async def receive():
        i = len(consumed)
        consumed.append(i)
        return {"type": "http.request", "body": chunks[i], "more_body": i < 2}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/internal/crm/poll",
        "raw_path": b"/api/internal/crm/poll",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("localhost", 8768),
        "client": ("127.0.0.1", 1234),
    }
    with TestClient(app, base_url="http://localhost:8768"):
        asyncio.run(app(scope, receive, send))
        assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 413
        assert consumed == [0, 1] and runtime.store.receipt_count() == 0 and provider.calls == 0


def test_factory_and_default_app_do_not_start_runner_or_lookup_tokens(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    from flight_recorder.app import create_app

    before = hashes(
        [runtime.config.domain_path, runtime.config.operational_path, runtime.authority.lock_path]
    )
    assert runtime._thread is None and runtime._tokens is None and runtime._http is None
    normal = create_app(runtime.config.domain_path)
    try:
        with TestClient(normal, base_url="http://localhost:8768") as client:
            assert (
                client.post("/api/internal/crm/poll", json=BODY, headers=HEADERS).status_code == 404
            )
        assert before == hashes(
            [
                runtime.config.domain_path,
                runtime.config.operational_path,
                runtime.authority.lock_path,
            ]
        )
    finally:
        runtime.close()
        normal.state.engine.dispose()


def test_bad_private_secret_mode_holds_startup_without_provider_calls(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    from pathlib import Path

    Path(runtime.config.document["ingress_secret_file"]).chmod(0o644)
    with pytest.raises(CRMRefused):
        with TestClient(app, base_url="http://localhost:8768"):
            pass
    assert provider.calls == 0


def test_oauth_callback_unknown_or_duplicate_state_never_calls_provider(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback?state=unknown&code=fictional-code"
        )
        assert response.status_code == 400 and "fictional-code" not in response.text
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback?state=a&state=b&code=fictional-code"
        )
        assert (
            response.status_code == 400
            and provider.calls == 0
            and runtime.store.receipt_count() == 0
        )


def _connect_injected_oauth(app, runtime, client):
    from pathlib import Path
    from urllib.parse import parse_qs, urlsplit

    from tests.adapters.crm.test_oauth import Clock, fake_provider

    runtime.clock = Clock()
    runtime._http, requests = fake_provider(runtime.clock, config=runtime.config.connection)
    path = Path(runtime.config.document["credential_directory"]) / (
        runtime.config.connection.credential_reference + ".client-secret"
    )
    path.write_text("fictional-client-secret")
    path.chmod(0o600)
    begin = client.post(
        "/api/internal/crm/oauth/salesforce/begin",
        json={"connection_id": BODY["connection_id"]},
        headers=HEADERS,
    )
    assert begin.status_code == 200
    state = parse_qs(urlsplit(begin.json()["authorization_url"]).query)["state"][0]
    callback = (
        "/api/internal/crm/oauth/salesforce/callback?state="
        + state
        + "&code=fictional-authorization-code"
    )
    response = client.get(callback)
    assert response.status_code == 200, response.text
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    assert client.get(callback).status_code == 400 and len(requests) == 3
    return response, requests


def test_oauth_success_discloses_injected_origin_and_never_exposes_credentials(tmp_path):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        response, requests = _connect_injected_oauth(app, runtime, client)
        assert response.json()["observation_origin"] == "injected_transport"
        assert not any(
            x in response.text
            for x in [
                "fictional-access",
                "fictional-refresh",
                "fictional-client-secret",
                "fictional-authorization-code",
            ]
        )


def test_post_oauth_client_reuses_owned_resources_without_reopening_them(tmp_path, monkeypatch):
    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        _connect_injected_oauth(app, runtime, client)
        http, tokens = runtime._http, runtime._tokens
        runtime._client_factory = None

        def forbidden(*args, **kwargs):
            pytest.fail("owned OAuth resources reopened or replaced")

        monkeypatch.setattr(
            importlib.import_module("flight_recorder.adapters.crm.http"), "OwnedHTTP", forbidden
        )
        monkeypatch.setattr(
            importlib.import_module("flight_recorder.adapters.crm.secrets"), "TokenStore", forbidden
        )
        runtime.client()
        assert runtime._http is http and runtime._tokens is tokens
