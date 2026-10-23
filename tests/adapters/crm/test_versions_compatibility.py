"""Unused latest advertisements never select a version or become request targets."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from flight_recorder.adapters.crm.contracts import CRMRefused, CRMTarget
from tests.adapters.crm.test_oauth import (
    ACCOUNT,
    ORG,
    USER,
    Clock,
    configuration,
    fake_provider,
    feature,
    private_store,
    seed_tokens,
)
from tests.adapters.crm.test_read_control_routes import csrf_from_page, read_app_setup

PINNED = {"version": "64.0", "url": "/services/data/v64.0"}
ALIAS = {"version": "65.0", "url": "/services/data/latest"}


def route_result(tmp_path, versions, *, stage=None, reason=None):
    app, runtime, _, _ = read_app_setup(tmp_path)
    directory = Path(runtime.config.document["credential_directory"])
    secret = directory / (runtime.config.connection.credential_reference + ".client-secret")
    secret.write_text("fictional-client-secret")
    secret.chmod(0o600)
    with TestClient(app, base_url="http://localhost:8768") as client:
        csrf = csrf_from_page(client.get("/crm/read"))
        response = client.post(
            "/crm/read/connect",
            data={"csrf": csrf},
            headers={"Origin": "http://localhost:8768"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        runtime._http, requests = fake_provider(
            runtime.clock, config=runtime.config.connection, versions=versions
        )
        runtime._http._before_attempt = runtime._before_read_attempt
        callback = {"state": state, "code": "fictional-code"}
        response = client.get(
            "/api/internal/crm/oauth/salesforce/callback", params=callback, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/crm/read/result"
        assert response.headers["referrer-policy"] == "no-referrer"
        result = client.get("/crm/read/result")
        if reason is None:
            assert runtime._confirmed_connection is not None
            assert "connected" in result.text
            assert runtime._confirmed_connection.api_version == "64.0"
            assert runtime._confirmed_connection.observation_origin == "injected_transport"
            assert (
                runtime._tokens.read_ready(
                    runtime.config.connection.connection_id, runtime.config.connection.binding
                ).generation
                == 1
            )
        else:
            assert runtime._confirmed_connection is None
            assert 'data-oauth-stage="' + stage + '"' in result.text
            assert 'data-oauth-reason="' + reason + '"' in result.text
        bundles = list(directory.glob("*.json"))
        assert len(bundles) == int(reason is None)
        before = {p.name: p.read_bytes() for p in bundles}
        for private in [state, "fictional-code", "fictional-access-one", "fictional-refresh-one"]:
            assert private not in result.text
        assert [r.url.path for r in requests] == [
            "/services/oauth2/token",
            requests[1].url.path,
            "/services/data",
        ]
        assert requests[1].url.path.startswith("/id/")
        assert [r.method for r in requests] == ["POST", "GET", "GET"]
        client.get(
            "/api/internal/crm/oauth/salesforce/callback", params=callback, follow_redirects=False
        )
        assert len(requests) == 3
        assert {p.name: p.read_bytes() for p in directory.glob("*.json")} == before


@pytest.mark.parametrize("versions", [[ALIAS, PINNED], [PINNED, ALIAS]])
def test_private_route_alias_root_keeps_separate_numeric_support(tmp_path, versions):
    route_result(tmp_path, versions)


@pytest.mark.parametrize(
    "versions",
    [
        [ALIAS],
        [{"version": "64.0", "url": "/services/data/latest"}],
        [ALIAS, {"version": "63.0", "url": "/services/data/v63.0"}],
        [ALIAS, ALIAS],
    ],
)
def test_alias_never_supplies_pinned_numeric_membership(tmp_path, versions):
    route_result(tmp_path, versions, stage="versions_validation", reason="unsupported_api_version")


@pytest.mark.parametrize(
    "entry,stage",
    [
        ({"version": "latest", "url": "/services/data/latest"}, "versions_version_grammar"),
        ({"version": None, "url": "/services/data/latest"}, "versions_version_type"),
        ({"url": "/services/data/latest"}, "versions_version_type"),
        ({"version": "65.0", "url": "/services/data/latest/Account"}, "versions_path_alias"),
        ({"version": "65.0", "url": "/services/data/latest/"}, "versions_path_empty_segment"),
        ({"version": "65.0", "url": "/services/data/latest?x=1"}, "versions_path_query_fragment"),
        ({"version": "65.0", "url": "/services/data/latest#x"}, "versions_path_query_fragment"),
        ({"version": "65.0", "url": "/services/data/Latest"}, "versions_path_version_other"),
        ({"version": "65.0", "url": "/services/data/%6catest"}, "versions_path_percent_encoding"),
        ({"version": "65.0", "url": "/services/data/latest\\x"}, "versions_path_backslash"),
        ({"version": "65.0", "url": "/services/data/latest "}, "versions_url_whitespace"),
        (
            {"version": "65.0", "url": "//foreign.invalid/services/data/latest"},
            "versions_url_network_path",
        ),
        (
            {"version": "65.0", "url": "https://foreign.invalid/services/data/latest"},
            "versions_url_absolute_other",
        ),
        ({"version": "65.0", "url": "/services/data/v63.0"}, "versions_path_numeric_root"),
    ],
)
def test_unused_alias_does_not_hide_invalid_rows(tmp_path, entry, stage):
    route_result(tmp_path, [ALIAS, PINNED, entry], stage=stage, reason="invalid_provider_response")


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("versions", [[ALIAS, PINNED], [PINNED, ALIAS]])
def test_direct_and_refresh_keep_numeric_config_and_generation_binding(tmp_path, refresh, versions):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    original = seed_tokens(store, config)
    http, requests = fake_provider(clock, config=config, versions=versions)
    client = feature("salesforce", "SalesforceClient")(config, store, http, clock)
    try:
        confirmed = client.verify_connection()
        assert store.read_ready(config.connection_id, config.binding).material == original
        if refresh:
            confirmed = feature("oauth", "refresh_connection")(config, store, http, clock)
            before = len(requests)
            with pytest.raises(CRMRefused, match="^connection_confirmation_stale$"):
                client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
            assert len(requests) == before
        ready = store.read_ready(config.connection_id, config.binding)
        assert ready.generation == (3 if refresh else 1)
        assert (confirmed.instance_id, confirmed.user_id, confirmed.api_version) == (
            ORG,
            USER,
            "64.0",
        )
        assert confirmed.observation_origin == "injected_transport"
        if refresh:
            assert ready.material.refresh_token == "fictional-refresh-one"
            assert ready.material != original
            assert [r.method for r in requests] == ["GET", "GET", "POST", "GET", "GET"]
        else:
            assert client._verified == (config.binding, ready.generation, ready.material)
            assert [r.method for r in requests] == ["GET", "GET"]
        assert all(
            r.url.path == "/services/data"
            or r.url.path.startswith("/id/")
            or r.url.path == "/services/oauth2/token"
            for r in requests
        )
    finally:
        http.close()
        store.close()


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("version", ["64.0", "65.0"])
def test_direct_and_refresh_alias_only_cannot_confirm(tmp_path, refresh, version):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    original = seed_tokens(store, config)
    http, requests = fake_provider(
        clock, config=config, versions=[{"version": version, "url": "/services/data/latest"}]
    )
    try:
        with pytest.raises(CRMRefused, match="^unsupported_api_version$"):
            if refresh:
                feature("oauth", "refresh_connection")(config, store, http, clock)
            else:
                feature("salesforce", "SalesforceClient")(
                    config, store, http, clock
                ).verify_connection()
        if refresh:
            with pytest.raises(CRMRefused):
                store.read_ready(config.connection_id, config.binding)
        else:
            ready = store.read_ready(config.connection_id, config.binding)
            assert ready.generation == 1 and ready.material == original
        assert [r.method for r in requests] == (
            ["POST", "GET", "GET"] if refresh else ["GET", "GET"]
        )
        assert requests[-1].url.path == "/services/data"
    finally:
        http.close()
        store.close()


def test_documented_alias_is_still_forbidden_as_request_target():
    config, clock = configuration(), Clock()
    http, requests = fake_provider(clock, config=config)
    try:
        with pytest.raises(CRMRefused, match="^invalid_provider_request$"):
            http.get(config.api_origin + "/services/data/latest", "fictional-access-one")
        assert requests == []
    finally:
        http.close()
