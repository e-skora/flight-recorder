"""Synthetic refused callbacks identify verification boundaries without private data."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from flight_recorder.adapters.crm.http import OwnedHTTP
from tests.adapters.crm.test_oauth import identity_response, token_response
from tests.adapters.crm.test_read_control_routes import csrf_from_page, read_app_setup

MARKER = "fictional-private-verification-marker"


@pytest.mark.parametrize(
    "fault,stage,reason",
    [
        ("identity_size", "identity_request", "invalid_provider_response"),
        ("identity_json", "identity_json", "invalid_provider_response"),
        ("identity_duplicate", "identity_json", "invalid_provider_response"),
        ("identity_nonfinite", "identity_json", "invalid_provider_response"),
        ("identity_fields", "identity_validation", "provider_identity_mismatch"),
        ("versions_size", "versions_request", "invalid_provider_response"),
        ("versions_json", "versions_json", "invalid_provider_response"),
        ("versions_entry_type", "versions_entry_type", "invalid_provider_response"),
        ("versions_version_type", "versions_version_type", "invalid_provider_response"),
        ("versions_version_grammar", "versions_version_grammar", "invalid_provider_response"),
        ("versions_entry", "versions_url_relative_path", "invalid_provider_response"),
        ("versions_url_missing", "versions_url_missing", "invalid_provider_response"),
        ("versions_url_null", "versions_url_non_string", "invalid_provider_response"),
        ("versions_url_number", "versions_url_non_string", "invalid_provider_response"),
        ("versions_url_object", "versions_url_non_string", "invalid_provider_response"),
        ("versions_url_array", "versions_url_non_string", "invalid_provider_response"),
        ("versions_url_empty", "versions_url_empty", "invalid_provider_response"),
        ("versions_url_space", "versions_url_whitespace", "invalid_provider_response"),
        ("versions_url_control", "versions_url_whitespace", "invalid_provider_response"),
        ("versions_url_network", "versions_url_network_path", "invalid_provider_response"),
        ("versions_url_slash", "versions_url_trailing_slash", "invalid_provider_response"),
        ("versions_url_query", "versions_url_query_fragment", "invalid_provider_response"),
        ("versions_url_fragment", "versions_url_query_fragment", "invalid_provider_response"),
        ("versions_url_pinned", "versions_url_absolute_pinned", "invalid_provider_response"),
        ("versions_url_foreign", "versions_url_absolute_other", "invalid_provider_response"),
        ("versions_url_malformed", "versions_url_malformed", "invalid_provider_response"),
        ("versions_url_other", "versions_url_other", "invalid_provider_response"),
        ("versions_type", "versions_validation", "unsupported_api_version"),
        ("versions_missing", "versions_validation", "unsupported_api_version"),
    ],
)
def test_refused_verification_reports_safe_boundary_and_never_installs_credentials(
    tmp_path, capsys, caplog, fault, stage, reason
):
    app, runtime, provider, paths = read_app_setup(tmp_path)
    directory = Path(runtime.config.document["credential_directory"])
    secret = directory / (runtime.config.connection.credential_reference + ".client-secret")
    secret.write_text(MARKER)
    secret.chmod(0o600)
    requests = []

    def response_for(boundary):
        if fault == boundary + "_size":
            return httpx.Response(200, content=MARKER.encode() + b" " * (1024 * 1024))
        if fault == boundary + "_json":
            return httpx.Response(200, content=b'{"private":"' + MARKER.encode() + b'",')
        if fault == "identity_duplicate" and boundary == "identity":
            return httpx.Response(
                200, content=b'{"private":"' + MARKER.encode() + b'","private":0}'
            )
        if fault == "identity_nonfinite" and boundary == "identity":
            return httpx.Response(200, content=b'{"private":"' + MARKER.encode() + b'","n":NaN}')
        if boundary == "identity":
            return httpx.Response(
                200,
                json=identity_response(active=False, username=MARKER)
                if fault == "identity_fields"
                else identity_response(username=MARKER),
            )
        if fault == "versions_type":
            return httpx.Response(200, json={"private": MARKER})
        if fault == "versions_entry_type":
            return httpx.Response(200, json=[None])
        if fault == "versions_version_type":
            return httpx.Response(200, json=[{"version": 64, "url": "/" + MARKER}])
        if fault == "versions_version_grammar":
            return httpx.Response(
                200, json=[{"version": MARKER, "url": "/services/data/v" + MARKER}]
            )
        if fault == "versions_entry":
            return httpx.Response(200, json=[{"version": "64.0", "url": "/" + MARKER}])
        if fault == "versions_url_missing":
            return httpx.Response(200, json=[{"version": "64.0"}])
        url_faults = {
            "versions_url_null": None,
            "versions_url_number": 64,
            "versions_url_object": {"private": MARKER},
            "versions_url_array": [MARKER],
            "versions_url_empty": "",
            "versions_url_space": " " + MARKER,
            "versions_url_control": MARKER + "\n",
            "versions_url_network": "//" + MARKER + ".invalid/services/data/v64.0",
            "versions_url_slash": "/services/data/v64.0/",
            "versions_url_query": "/services/data/v64.0?private=" + MARKER,
            "versions_url_fragment": "/services/data/v64.0#" + MARKER,
            "versions_url_pinned": runtime.config.connection.api_origin
            + "/services/data/v64.0?private="
            + MARKER,
            "versions_url_foreign": "https://" + MARKER + ".invalid/services/data/v64.0",
            "versions_url_malformed": "https://[" + MARKER,
            "versions_url_other": MARKER + "/services/data/v64.0",
        }
        if fault in url_faults:
            return httpx.Response(200, json=[{"version": "64.0", "url": url_faults[fault]}])
        assert fault == "versions_missing"
        return httpx.Response(200, json=[{"version": "63.0", "url": "/services/data/v63.0"}])

    def handler(request):
        requests.append(request)
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(
                200,
                json=token_response(
                    access_token=MARKER + "-access", refresh_token=MARKER + "-refresh"
                ),
            )
        if request.url.path.startswith("/id/"):
            return response_for("identity")
        assert request.url.path == "/services/data"
        return response_for("versions")

    runtime._http = OwnedHTTP(
        runtime.config.connection, transport=httpx.MockTransport(handler), clock=runtime.clock
    )
    runtime._http._before_attempt = runtime._before_read_attempt
    with TestClient(app, base_url="http://localhost:8768") as client:
        csrf = csrf_from_page(client.get("/crm/read"))
        connect = client.post(
            "/crm/read/connect",
            data={"csrf": csrf},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        state = parse_qs(urlsplit(connect.headers["location"]).query)["state"][0]
        params = {"state": state, "code": MARKER + "-code"}
        callback = client.get(
            "/api/internal/crm/oauth/salesforce/callback", params=params, follow_redirects=False
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/crm/read/result"
        assert callback.headers["referrer-policy"] == "no-referrer"
        result = client.get("/crm/read/result")
        assert "refused" in result.text
        assert f'data-oauth-stage="{stage}"' in result.text
        assert f'data-oauth-reason="{reason}"' in result.text
        captured = capsys.readouterr()
        output = (
            result.text
            + callback.text
            + repr(callback.headers)
            + caplog.text
            + captured.out
            + captured.err
        )
        assert MARKER not in output and state not in output
        assert runtime._confirmed_connection is None
        assert not list(directory.glob("*.json"))
        expected_paths = ["/services/oauth2/token"]
        assert requests[1].url.path.startswith("/id/")
        expected_paths.append(requests[1].url.path)
        if fault.startswith("versions"):
            expected_paths.append("/services/data")
        assert [r.url.path for r in requests] == expected_paths
        client.get(
            "/api/internal/crm/oauth/salesforce/callback", params=params, follow_redirects=False
        )
        assert [r.url.path for r in requests] == expected_paths
