"""Bounded owned transport and selected Salesforce reads; all HTTP is injected."""

import hashlib
import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused, CRMTarget
from flight_recorder.adapters.crm.secrets import TokenMaterial
from tests.adapters.crm.test_oauth import (
    ACCOUNT,
    BASIS,
    LOGIN,
    ORG,
    ORIGIN,
    USER,
    Clock,
    configuration,
    fake_provider,
    feature,
    private_store,
)


@pytest.mark.parametrize(
    "status,expected",
    [(429, 3), (502, 3), (503, 3), (504, 3), (401, 1), (403, 1), (404, 1), (500, 1), (302, 1)],
)
def test_get_has_only_bounded_transient_retries(status, expected):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.extensions["timeout"] == {"connect": 10, "read": 30, "write": 30, "pool": 10}
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid/secret"},
            text="fictional-access-one must never leak",
        )

    http = feature("http", "OwnedHTTP")(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    with pytest.raises(CRMRefused) as caught:
        http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
    assert len(requests) == expected
    assert "fictional-access" not in str(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None
    http.close()


@pytest.mark.parametrize("status", [401, 429, 500, 502, 302])
def test_token_post_never_retries_or_redirects(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="fictional-refresh-one must never leak",
            headers={"Location": "https://evil.invalid"},
        )

    http = feature("http", "OwnedHTTP")(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    with pytest.raises(CRMRefused):
        http.post_token(
            {
                "grant_type": "refresh_token",
                "client_id": "fictional-public-client-id",
                "refresh_token": "fictional-refresh-one",
                "client_secret": "fictional-client-secret",
            }
        )
    assert len(requests) == 1 and requests[0].method == "POST"
    http.close()


@pytest.mark.parametrize("method,expected", [("get", 3), ("post", 1)])
def test_timeout_errors_are_safe_and_attempt_count_is_fixed(method, expected):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("fictional-refresh-one must never leak", request=request)

    http = feature("http", "OwnedHTTP")(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    with pytest.raises(CRMRefused) as caught:
        if method == "get":
            http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
        else:
            http.post_token(
                {
                    "grant_type": "refresh_token",
                    "client_id": "fictional-public-client-id",
                    "refresh_token": "fictional-refresh-one",
                    "client_secret": "fictional-client-secret",
                }
            )
    assert len(requests) == expected
    assert "fictional-refresh-one" not in str(caught.value)
    assert caught.value.__suppress_context__
    http.close()


@pytest.mark.parametrize(
    "body",
    [
        b'{"organization_id":"one","organization_id":"two"}',
        b'{"expires_in":NaN}',
        b"\xff",
        b"[" * 2000,
        b'"' + b"x" * (1024 * 1024) + b'"',
    ],
    ids=["duplicate", "nonfinite", "invalid-utf8", "depth", "oversize"],
)
def test_malformed_success_body_is_not_retried(body):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=body)

    http = feature("http", "OwnedHTTP")(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    with pytest.raises(CRMRefused):
        response = http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
        response.json()
    assert len(requests) == 1
    http.close()


def test_cookie_headers_never_cross_requests_and_close_refuses():
    requests = []

    def handler(request):
        requests.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(200, json={}, headers={"Set-Cookie": "fictional-cookie=value"})

    http = feature("http", "OwnedHTTP")(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
    http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
    http.close()
    http.close()
    with pytest.raises(CRMRefused):
        http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one")
    assert len(requests) == 2


def test_unapproved_get_target_never_receives_token():
    requests = []
    http = feature("http", "OwnedHTTP")(
        configuration(),
        transport=httpx.MockTransport(lambda request: requests.append(request)),
        clock=Clock(),
    )
    for url in [
        "https://evil.invalid/id/x/y",
        ORIGIN + "/services/data/v64.0/query?q=Account",
        LOGIN + "/id/other/user",
        ORIGIN + "/services/data/latest",
    ]:
        with pytest.raises(CRMRefused):
            http.get(url, "fictional-access-one")
    assert requests == []
    http.close()


def test_overflowed_json_number_is_nonfinite_even_without_nan_literal():
    http = feature("http", "OwnedHTTP")(
        configuration(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"unexpected":1e9999}')
        ),
        clock=Clock(),
    )
    with pytest.raises(CRMRefused):
        http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access-one").json()
    http.close()


def test_transport_origin_and_binding_cannot_be_relabelled_by_public_flags():
    http = feature("http", "OwnedHTTP")(
        configuration(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        clock=Clock(),
    )
    for field, value in [("observation_origin", "provider_http"), ("config_binding", "b" * 64)]:
        with pytest.raises(AttributeError):
            setattr(http, field, value)
    assert http.observation_origin == "injected_transport"
    assert http.config_binding == configuration().binding
    http.close()


def test_malformed_url_is_safe_named_refusal_before_transport():
    requests = []
    http = feature("http", "OwnedHTTP")(
        configuration(),
        transport=httpx.MockTransport(lambda request: requests.append(request)),
        clock=Clock(),
    )
    with pytest.raises(CRMRefused):
        http.get("https://[malformed", "fictional-access-one")
    assert requests == []
    http.close()


def ready_client(tmp_path, *, body=None, etag='"fictional-source-etag"', response_status=200):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    store.install(
        config.connection_id,
        config.binding,
        TokenMaterial("fictional-access", "fictional-refresh", None),
    )
    record = {
        "Id": ACCOUNT,
        "Name": "Fictional Company",
        "Website": None,
        "LastModifiedDate": "2026-10-21T20:59:00.000+0000",
        "NumberOfEmployees": 120,
    }
    if body is None:
        body = json.dumps(record).encode()

    def account(request):
        headers = {"Sforce-Request-Id": "fictional-request-id"}
        if etag is not None:
            headers["ETag"] = etag
        return httpx.Response(response_status, content=body, headers=headers)

    http, requests = fake_provider(clock, account=account)
    client = feature("salesforce", "SalesforceClient")(config, store, http, clock)
    return config, clock, store, http, requests, client, body


def test_client_constructor_and_unverified_read_do_not_load_or_contact_provider(
    tmp_path, monkeypatch
):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    http, requests = fake_provider(clock)

    def forbidden(*args, **kwargs):
        pytest.fail("constructor or unverified read acquired a token")

    with monkeypatch.context() as patch:
        patch.setattr(store, "read_ready", forbidden)
        client = feature("salesforce", "SalesforceClient")(config, store, http, clock)
        with pytest.raises(CRMRefused):
            client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert requests == []
    http.close()
    store.close()


@pytest.mark.parametrize("etag", [None, 'W/"fictional-weak"', '"fictional-strong"'])
def test_selected_read_preserves_exact_body_version_null_and_absent(tmp_path, etag):
    config, clock, store, http, requests, client, body = ready_client(tmp_path, etag=etag)
    assert requests == []
    confirmed = client.verify_connection()
    assert confirmed.instance_id == ORG
    read = client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert read.precondition == etag
    assert read.response_body_sha256 == hashlib.sha256(body).hexdigest()
    assert read.observation_origin == "injected_transport"
    assert read.properties["Website"] is None
    assert "FR_Industry_Segment__c" not in read.properties
    assert read.unavailable_fields == []
    assert read.read_started_at == BASIS == read.read_finished_at
    assert read.source_updated_at == BASIS - timedelta(minutes=1)
    assert read.request_id == "fictional-request-id"
    expected_fields = [
        "BillingCountry",
        "FR_Funding_Observed_On__c",
        "FR_Funding_Stage__c",
        "FR_Head_Of_Platform_Start__c",
        "FR_Industry_Segment__c",
        "FR_Integration_Pressure_Basis__c",
        "FR_Integration_Pressure__c",
        "FR_Open_Platform_Roles__c",
        "Id",
        "LastModifiedDate",
        "Name",
        "NumberOfEmployees",
        "Website",
    ]
    assert parse_qs(requests[-1].url.query.decode()) == {"fields": [",".join(expected_fields)]}
    assert requests[-1].url.path == f"/services/data/v64.0/sobjects/Account/{ACCOUNT}"
    document = read.document
    document["properties"]["Name"] = "changed locally"
    assert read.properties["Name"] == "Fictional Company"
    http.close()
    store.close()


@pytest.mark.parametrize("kind", ["new_generation", "pending", "expired", "foreign_target"])
def test_changed_authority_refuses_before_any_account_request(tmp_path, kind):
    config, clock, store, http, requests, client, _ = ready_client(tmp_path)
    client.verify_connection()
    if kind == "new_generation":
        store.replace(
            config.connection_id,
            TokenMaterial("fictional-new-access", "fictional-new-refresh", None),
            binding=config.binding,
            expected_generation=1,
        )
    elif kind == "pending":
        store.begin_refresh(config.connection_id, config.binding, expected_generation=1)
    elif kind == "expired":
        store.replace(
            config.connection_id,
            TokenMaterial("fictional-new-access", "fictional-new-refresh", BASIS),
            binding=config.binding,
            expected_generation=1,
        )
    target = CRMTarget(
        "salesforce", ORG if kind != "foreign_target" else "00D000000000002EAA", "Account", ACCOUNT
    )
    before = len(requests)
    with pytest.raises(CRMRefused):
        client.read_selected(target)
    assert len(requests) == before
    http.close()
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"Id": "001000000000002AAA"},
        {"LastModifiedDate": None},
        {"LastModifiedDate": "2026-10-22T00:00:00Z"},
        {"LastModifiedDate": "2026-10-21T20:59:00"},
        {"FR_Score__c": 99},
        {"Name": None},
    ],
)
def test_unbound_or_unrequested_record_is_refused_without_repair(tmp_path, change):
    record = {
        "Id": ACCOUNT,
        "Name": "Fictional Company",
        "Website": None,
        "LastModifiedDate": "2026-10-21T20:59:00.000+0000",
        "NumberOfEmployees": 120,
    }
    record.update(change)
    config, clock, store, http, requests, client, _ = ready_client(
        tmp_path, body=json.dumps(record).encode()
    )
    client.verify_connection()
    with pytest.raises(CRMRefused):
        client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert len(requests) == 3
    http.close()
    store.close()


def test_forbidden_selected_field_read_is_not_retried_with_fewer_fields(tmp_path):
    config, clock, store, http, requests, client, _ = ready_client(tmp_path, response_status=403)
    client.verify_connection()
    with pytest.raises(CRMRefused):
        client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert len(requests) == 3
    assert sum(r.method == "POST" for r in requests) == 0
    http.close()
    store.close()


def test_selected_numbers_keep_lossless_json_lexical_tokens(tmp_path):
    body = (
        b'{"Id":"001000000000001AAA","Name":"Fictional Company","Website":null,'
        b'"LastModifiedDate":"2026-10-21T20:59:00Z",'
        b'"NumberOfEmployees":1e3,"FR_Open_Platform_Roles__c":7.00}'
    )
    config, clock, store, http, requests, client, _ = ready_client(tmp_path, body=body)
    client.verify_connection()
    observed = client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert observed.properties["NumberOfEmployees"] == {"json_number": "1e3"}
    assert observed.properties["FR_Open_Platform_Roles__c"] == {"json_number": "7.00"}
    assert observed.response_body_sha256 == hashlib.sha256(body).hexdigest()
    http.close()
    store.close()


def test_expired_token_cannot_verify_connection_or_start_account_read(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    store.install(
        config.connection_id,
        config.binding,
        TokenMaterial("fictional-access", "fictional-refresh", BASIS),
    )
    http, requests = fake_provider(clock)
    client = feature("salesforce", "SalesforceClient")(config, store, http, clock)
    with pytest.raises(CRMRefused, match="token_expired"):
        client.verify_connection()
    with pytest.raises(CRMRefused, match="connection_not_verified"):
        client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
    assert requests == []
    http.close()
    store.close()


def test_identity_revocation_is_not_refreshed_or_retried_implicitly(tmp_path):
    config, clock = configuration(), Clock()
    store = private_store(tmp_path)
    store.install(
        config.connection_id,
        config.binding,
        TokenMaterial("fictional-access", "fictional-refresh", None),
    )
    requests = []

    def revoked(request):
        requests.append(request)
        return httpx.Response(401, text="fictional-provider-error contains no usable session")

    http = feature("http", "OwnedHTTP")(config, transport=httpx.MockTransport(revoked), clock=clock)
    client = feature("salesforce", "SalesforceClient")(config, store, http, clock)
    with pytest.raises(CRMRefused, match="provider_auth_refused"):
        client.verify_connection()
    assert len(requests) == 1 and requests[0].method == "GET"
    assert store.status(config.connection_id, config.binding).generation == 1
    http.close()
    store.close()


@pytest.mark.parametrize(
    "encoding",
    ["utf-16", "utf-32", "utf-8-sig", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"],
)
def test_selected_record_requires_utf8_even_for_other_valid_json_encodings(tmp_path, encoding):
    record = dict(
        Id=ACCOUNT,
        Name="Fictional Company",
        Website=None,
        LastModifiedDate="2026-10-21T20:59:00Z",
        NumberOfEmployees=120,
    )
    config, clock, store, http, requests, client, _ = ready_client(
        tmp_path, body=json.dumps(record).encode(encoding)
    )
    client.verify_connection()
    try:
        with pytest.raises(CRMRefused):
            client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
        assert len(requests) == 3
    finally:
        http.close()
        store.close()


@pytest.mark.parametrize(
    "body", [b"\xfffictional-private-response", b'{"unknown":"fictional-private-response"}']
)
def test_selected_decode_errors_have_no_printable_raw_response_chain(tmp_path, body):
    import traceback

    config, clock, store, http, requests, client, _ = ready_client(tmp_path, body=body)
    client.verify_connection()
    try:
        with pytest.raises(CRMRefused) as caught:
            client.read_selected(CRMTarget("salesforce", ORG, "Account", ACCOUNT))
        rendered = "".join(traceback.format_exception(caught.value))
        assert "fictional-private-response" not in rendered
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ or caught.value.__context__ is None
        assert len(requests) == 3
    finally:
        http.close()
        store.close()


@pytest.mark.parametrize(
    "retry_after,expected",
    [
        ("2", 2),
        ("120", 60),
        ("Wed, 21 Oct 2026 21:00:07 GMT", 7),
        ("Wed, 21 Oct 2026 20:59:59 GMT", 0),
        ("invalid", 0.01),
        ("-1", 0.01),
    ],
)
def test_transient_get_honors_retry_after_with_sixty_second_cap(monkeypatch, retry_after, expected):
    import flight_recorder.adapters.crm.http as owned_http

    requests, delays = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={})

    monkeypatch.setattr(owned_http.time, "sleep", delays.append)
    http = owned_http.OwnedHTTP(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    try:
        assert http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access").json() == {}
        assert len(requests) == 2
        assert delays == [expected]
    finally:
        http.close()


@pytest.mark.parametrize("suffix,expected", [("2", 2), ("120", 60), ("", 0)])
def test_retry_after_handles_arbitrarily_padded_decimal_without_int_conversion_error(
    monkeypatch, suffix, expected
):
    import flight_recorder.adapters.crm.http as owned_http

    delays = []
    monkeypatch.setattr(owned_http.time, "sleep", delays.append)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0" * 5000 + suffix})
            if count == 1
            else httpx.Response(200, json={})
        )

    http = owned_http.OwnedHTTP(
        configuration(), transport=httpx.MockTransport(handler), clock=Clock()
    )
    try:
        assert http.get(f"{LOGIN}/id/{ORG}/{USER}", "fictional-access").json() == {}
        assert delays == [expected]
    finally:
        http.close()


def test_exact_account_describe_metadata_path_is_bounded():
    cfg, clock = configuration(), Clock()
    requests = []

    def metadata(request):
        requests.append(request)
        return httpx.Response(200, json={"name": "Account", "fields": []})

    http = feature("http", "OwnedHTTP")(cfg, transport=httpx.MockTransport(metadata), clock=clock)
    try:
        path = f"/services/data/v{cfg.api_version}/sobjects/Account/describe"
        response = http.get(cfg.api_origin + path, "fictional-access-one")
        assert response.json() == {"name": "Account", "fields": []}
        assert [request.url.path for request in requests] == [path]
        assert http.observation_origin == "injected_transport"
        for suffix in ("?fields=Name", "/", "?q=Account"):
            with pytest.raises(CRMRefused, match="^invalid_provider_request$"):
                http.get(cfg.api_origin + path + suffix, "fictional-access-one")
        with pytest.raises(CRMRefused, match="^invalid_provider_request$"):
            http.get(cfg.api_origin + path.replace("Account", "Contact"), "fictional-access-one")
        assert len(requests) == 1
    finally:
        http.close()


def test_authority_is_rechecked_before_every_http_retry():
    cfg, clock = configuration(), Clock()
    attempts, admissions = [], []

    def guard(method, path, category):
        admissions.append((method, path, category))
        if len(admissions) > 1:
            raise CRMRefused("read_authority_expired")

    def provider(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 200, json=[])

    http = feature("http", "OwnedHTTP")(
        cfg, transport=httpx.MockTransport(provider), clock=clock, before_attempt=guard
    )
    try:
        with pytest.raises(CRMRefused, match="^read_authority_expired$"):
            http.get(cfg.api_origin + "/services/data", "fictional-access-one")
        assert len(attempts) == 1
        assert admissions == [("GET", "/services/data", "api_versions")] * 2
        assert "fictional-access-one" not in repr(admissions)
    finally:
        http.close()


def describe_document():
    from flight_recorder.adapters.crm.mapping import ALLOWED

    return dict(
        name="Account",
        queryable=True,
        retrieveable=True,
        updateable=False,
        fields=[
            dict(
                name=name,
                type="string",
                length=255,
                precision=0,
                scale=0,
                updateable=False,
                picklistValues=[],
            )
            for name in sorted(ALLOWED)
        ]
        + [
            dict(
                name="Unmapped_Secret__c",
                type="string",
                length=255,
                precision=0,
                scale=0,
                updateable=True,
                picklistValues=[],
            )
        ],
    )


def test_metadata_can_never_fetch_account(tmp_path):
    body = json.dumps(describe_document()).encode()
    cfg, clock, store, http, requests, client, _ = ready_client(tmp_path, body=body)
    client.verify_connection()
    requests.clear()
    observed = client.read_metadata()
    assert observed is not None, "metadata probe must return a bounded immutable observation"
    from flight_recorder.adapters.crm.mapping import ALLOWED

    d = observed.document
    assert d["observation_origin"] == "injected_transport"
    assert d["connection_binding"] == cfg.binding
    assert d["expected_instance_id"] == ORG and d["expected_user_id"] == USER
    assert d["api_version"] == cfg.api_version
    assert d["describe"]["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert d["account"] == dict(queryable=True, retrieveable=True, updateable=False)
    assert {f["name"] for f in d["fields"]} == ALLOWED
    assert all(f["readable"] and not f["updateable"] for f in d["fields"])
    assert d["missing_fields"] == []
    assert len(requests) == 2
    assert [r.url.path for r in requests] == [
        "/services/data",
        f"/services/data/v{cfg.api_version}/sobjects/Account/describe",
    ]
    assert all(r.method == "GET" and not r.url.query for r in requests)
    assert "Unmapped_Secret" not in observed.canonical_bytes.decode()
    d["fields"][0]["readable"] = False
    assert observed.document["fields"][0]["readable"] is True
    assert "fictional-access" not in repr(observed)
    http.close()
    store.close()


def test_unverified_metadata_refuses_before_token_or_network(tmp_path):
    cfg, clock, store, http, requests, client, _ = ready_client(tmp_path)
    with pytest.raises(CRMRefused, match="connection_not_verified"):
        client.read_metadata()
    assert requests == []
    http.close()
    store.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(name="Contact"),
        lambda d: d.update(queryable="true"),
        lambda d: d["fields"].append(d["fields"][0]),
        lambda d: d["fields"][0].update(updateable="false"),
        lambda d: d["fields"][0].update(length=True),
        lambda d: d["fields"][0].update(picklistValues=[{"value": "x", "active": "true"}]),
    ],
)
def test_malformed_metadata_is_refused_without_record_read(tmp_path, change):
    doc = describe_document()
    change(doc)
    cfg, clock, store, http, requests, client, _ = ready_client(
        tmp_path, body=json.dumps(doc).encode()
    )
    client.verify_connection()
    with pytest.raises(CRMRefused, match="invalid_account_metadata"):
        client.read_metadata()
    assert all(not r.url.query for r in requests)
    http.close()
    store.close()


def test_metadata_preserves_missing_fields_as_missing(tmp_path):
    doc = describe_document()
    missing = doc["fields"].pop(0)["name"]
    cfg, clock, store, http, requests, client, _ = ready_client(
        tmp_path, body=json.dumps(doc).encode()
    )
    client.verify_connection()
    observed = client.read_metadata()
    assert observed is not None, "missing fields must remain observable"
    assert observed.document["missing_fields"] == [missing]
    assert missing not in {f["name"] for f in observed.document["fields"]}
    http.close()
    store.close()
