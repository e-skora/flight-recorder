"""Closed local OAuth diagnostics never expose callback, token or exception data."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.http import OwnedHTTP
from tests.adapters.crm.test_oauth import fake_provider, identity_response, token_response
from tests.adapters.crm.test_read_control_routes import csrf_from_page, read_app_setup

MARKER = "fictional-private-diagnostic-marker"


@pytest.mark.parametrize(
    "fault,stage,reason",
    [
        ("unbound", "callback_binding", "read_callback_refused"),
        ("denied", "callback_validation", "oauth_authorization_denied"),
        ("denied_description", "callback_validation", "invalid_oauth_callback"),
        ("token_http", "token_exchange", "provider_auth_refused"),
        ("token_schema", "token_response", "invalid_oauth_response"),
        ("token_origin", "token_response", "invalid_oauth_response"),
        ("identity", "identity_validation", "provider_identity_mismatch"),
        ("version", "versions_validation", "unsupported_api_version"),
        ("install", "credential_installation", "private_file_invalid"),
        ("unknown_reason", "token_exchange", "unclassified_refusal"),
    ],
)
def test_oauth_refusal_reports_only_closed_stage_and_reason(
    tmp_path, monkeypatch, capsys, caplog, fault, stage, reason
):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    directory = Path(runtime.config.document["credential_directory"])
    secret = directory / (runtime.config.connection.credential_reference + ".client-secret")
    secret.write_text(MARKER)
    secret.chmod(0o600)
    with TestClient(app, base_url="http://localhost:8768") as client:
        csrf = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            data={"csrf": csrf},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        token = token_response(access_token=MARKER + "-access", refresh_token=MARKER + "-refresh")
        if fault == "token_schema":
            token["token_type"] = MARKER
        if fault == "token_origin":
            token["instance_url"] = "https://untrusted.invalid/" + MARKER
        runtime._http, requests = fake_provider(
            runtime.clock,
            config=runtime.config.connection,
            token=token,
            identity=identity_response(active=False) if fault == "identity" else None,
            versions=[] if fault == "version" else None,
        )
        if fault == "token_http":

            def refused(request):
                requests.append(request)
                return httpx.Response(403, text=MARKER)

            runtime._http.close()
            runtime._http = OwnedHTTP(
                runtime.config.connection,
                transport=httpx.MockTransport(refused),
                clock=runtime.clock,
            )
        if fault == "unknown_reason":

            def unknown(form):
                raise CRMRefused(MARKER)

            monkeypatch.setattr(runtime._http, "post_token", unknown)
        runtime._http._before_attempt = runtime._before_read_attempt
        if fault == "install":
            from flight_recorder.adapters.crm.secrets import TokenStore

            def failed_install(*args):
                raise CRMRefused("private_file_invalid")

            monkeypatch.setattr(TokenStore, "install", failed_install)
        params = {"state": state, "code": MARKER + "-code"}
        if fault == "unbound":
            params["state"] = "unbound-private-state"
        if fault == "denied":
            params = {"state": state, "error": MARKER}
        if fault == "denied_description":
            params = {"state": state, "error": "access_denied", "error_description": MARKER}
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params=params,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/crm/read/result"
        assert response.headers["referrer-policy"] == "no-referrer"
        result = client.get("/crm/read/result")
        assert "refused" in result.text
        assert f'data-oauth-stage="{stage}"' in result.text
        assert f'data-oauth-reason="{reason}"' in result.text
        output = result.text + response.text + repr(response.headers) + caplog.text
        captured = capsys.readouterr()
        output += captured.out + captured.err
        for private in (MARKER, state, "unbound-private-state", "code_challenge"):
            assert private not in output
        assert runtime._confirmed_connection is None
        assert not list(directory.glob("*.json")), "a refused flow must not install credentials"
        assert all("/sobjects/" not in r.url.path for r in requests)
        count = len(requests)
        client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params=params,
            follow_redirects=False,
        )
        assert len(requests) == count, "diagnostics must never permit callback replay"


def test_unexpected_oauth_exception_never_serializes_its_type_or_message(tmp_path, monkeypatch):
    from flight_recorder.adapters.crm import oauth

    app, runtime, provider, paths = read_app_setup(tmp_path)

    def failed(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(oauth, "finish_oauth", failed)
    with TestClient(app, base_url="http://localhost:8768") as client:
        csrf = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            data={"csrf": csrf},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": state, "code": MARKER},
            follow_redirects=False,
        )
        result = client.get("/crm/read/result")
        assert 'data-oauth-reason="unexpected_failure"' in result.text
        assert "RuntimeError" not in result.text and MARKER not in result.text
        assert runtime._confirmed_connection is None


@pytest.mark.parametrize("reason", [MARKER, [MARKER], {"private": MARKER}, None, True, 403])
def test_arbitrary_refusal_fields_are_not_displayable(reason):
    from fastapi import HTTPException

    from flight_recorder.adapters.crm.read_control_routes import _oauth_refusal_reason

    assert _oauth_refusal_reason(CRMRefused(reason)) == "unclassified_refusal"
    assert _oauth_refusal_reason(HTTPException(403, detail=reason)) == "unclassified_refusal"


def test_unknown_progress_is_rejected_without_storing_input():
    from flight_recorder.adapters.crm.oauth import OAuthProgress

    progress = OAuthProgress()
    with pytest.raises(CRMRefused, match="^invalid_oauth_diagnostic$"):
        progress.advance(MARKER)
    assert progress.stage == "callback_headers"
    assert MARKER not in repr(progress)


def test_fresh_connect_and_success_do_not_show_an_old_diagnostic(tmp_path):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    directory = Path(runtime.config.document["credential_directory"])
    secret = directory / (runtime.config.connection.credential_reference + ".client-secret")
    secret.write_text(MARKER)
    secret.chmod(0o600)
    with TestClient(app, base_url="http://localhost:8768") as client:
        client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": "unbound", "code": MARKER},
            follow_redirects=False,
        )
        response = client.get("/crm/read/result")
        assert "data-oauth-stage" in response.text
        csrf = csrf_from_page(response)
        response = client.post(
            "/crm/read/connect",
            data={"csrf": csrf},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        assert "data-oauth-stage" not in client.get("/crm/read/result").text
        runtime._http, requests = fake_provider(runtime.clock, config=runtime.config.connection)
        runtime._http._before_attempt = runtime._before_read_attempt
        client.get(
            "/api/internal/crm/oauth/salesforce/callback",
            params={"state": state, "code": MARKER},
            follow_redirects=False,
        )
        result = client.get("/crm/read/result")
        assert "connected" in result.text and "data-oauth-stage" not in result.text
        assert runtime._confirmed_connection.observation_origin == "injected_transport"
        assert len(requests) == 3
