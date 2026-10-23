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

# Literal route fixtures: labels describe syntax only, never valid or accepted URLs.
PATH_FAULTS = {
    "path_query": ("/services/data/v63.0?" + MARKER, "versions_path_query_fragment"),
    "path_fragment": ("/services/data/v63.0#" + MARKER, "versions_path_query_fragment"),
    "path_percent": ("/services/data/v%36%34.0", "versions_path_percent_encoding"),
    "path_percent_invalid": ("/services/data/v%ZZ", "versions_path_percent_encoding"),
    "path_percent_bare": ("/services/data/%", "versions_path_percent_encoding"),
    "path_backslash": ("/services/data\\v64.0", "versions_path_backslash"),
    "path_non_ascii": ("/services/data/vé", "versions_path_non_ascii"),
    "path_unicode_digit": ("/services/data/v６４.0", "versions_path_non_ascii"),
    "path_collection": ("/services/data", "versions_path_collection"),
    "path_collection_slash": ("/services/data/", "versions_path_collection"),
    "path_empty": ("/services//data/v64.0", "versions_path_empty_segment"),
    "path_root_only": ("/", "versions_path_empty_segment"),
    "path_short_slash": ("/services/", "versions_path_empty_segment"),
    "path_descendant_slash": ("/services/data/v64.0/Account/", "versions_path_empty_segment"),
    "path_dot": ("/services/./data/v64.0", "versions_path_dot_segment"),
    "path_dot_parent": ("/services/data/v64.0/..", "versions_path_dot_segment"),
    "path_prefix_case": ("/Services/DATA/v64.0", "versions_path_prefix_case"),
    "path_prefix_case_short": ("/Services/Data", "versions_path_prefix_case"),
    "path_prefix_other": ("/service/data/v64.0", "versions_path_prefix_other"),
    "path_short": ("/services", "versions_path_prefix_other"),
    "path_prefix_boundary": ("/services/database/v64.0", "versions_path_prefix_other"),
    "path_prefix_case_other": ("/SERVICES/OTHER/v64.0", "versions_path_prefix_other"),
    "path_entry_descendant": ("/services/data/v64.0/Account", "versions_path_entry_descendant"),
    "path_numeric_root": ("/services/data/v63.0", "versions_path_numeric_root"),
    "path_numeric_descendant": ("/services/data/v63.0/Account", "versions_path_numeric_descendant"),
    "path_alias": ("/services/data/latest", "versions_validation"),
    "path_alias_descendant": ("/services/data/latest/Account", "versions_path_alias"),
    "path_missing_v": ("/services/data/64.0", "versions_path_missing_v"),
    "path_missing_v_descendant": ("/services/data/63.0/Account", "versions_path_missing_v"),
    "path_version_case": ("/services/data/V64.0", "versions_path_version_case"),
    "path_version_case_descendant": ("/services/data/V63.0/Account", "versions_path_version_case"),
    "path_version_other": ("/services/data/" + MARKER, "versions_path_version_other"),
    "path_alias_case": ("/services/data/Latest", "versions_path_version_other"),
    "path_numeric_zero": ("/services/data/v0.0", "versions_path_version_other"),
    "path_numeric_leading_zero": ("/services/data/v064.0", "versions_path_version_other"),
    "path_numeric_minor": ("/services/data/v64.1", "versions_path_version_other"),
    "path_numeric_dot": ("/services/data/v64x0", "versions_path_version_other"),
    "path_missing_v_malformed": ("/services/data/064.0", "versions_path_version_other"),
    "path_version_case_malformed": ("/services/data/V64.1", "versions_path_version_other"),
    "path_query_percent": ("/services/data/v%ZZ?" + MARKER, "versions_path_query_fragment"),
    "path_percent_backslash": ("/services/data/%\\" + MARKER, "versions_path_percent_encoding"),
    "path_backslash_non_ascii": ("/services/data/\\é", "versions_path_backslash"),
    "path_non_ascii_empty": ("/services//data/é", "versions_path_non_ascii"),
    "path_empty_dot": ("/services//./data", "versions_path_empty_segment"),
    "path_dot_case": ("/Services/Data/../v64.0", "versions_path_dot_segment"),
    "path_case_numeric": ("/Services/Data/v63.0", "versions_path_prefix_case"),
    "path_foreign_alias": ("/other/data/latest", "versions_path_prefix_other"),
}


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
        ("versions_entry", "versions_path_prefix_other", "invalid_provider_response"),
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
        *[
            (
                fault,
                stage,
                "unsupported_api_version" if fault == "path_alias" else "invalid_provider_response",
            )
            for fault, (_, stage) in PATH_FAULTS.items()
        ],
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
        url_faults.update({fault: value for fault, (value, _) in PATH_FAULTS.items()})
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
        if fault.startswith("versions") or fault in PATH_FAULTS:
            expected_paths.append("/services/data")
        assert [r.url.path for r in requests] == expected_paths
        client.get(
            "/api/internal/crm/oauth/salesforce/callback", params=params, follow_redirects=False
        )
        assert [r.url.path for r in requests] == expected_paths
