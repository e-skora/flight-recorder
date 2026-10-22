"""The owned Salesforce mutation boundary makes one injected HTTP attempt."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.mapping import OUTPUTS
from flight_recorder.adapters.crm.salesforce import SalesforceClient
from flight_recorder.adapters.crm.secrets import TokenMaterial
from tests.acceptance.test_crm_operations import intent
from tests.adapters.crm.cases import make_case
from tests.adapters.crm.test_oauth import Clock, configuration, fake_provider, private_store
from tests.adapters.crm.test_salesforce_writer import feature


def client_case(tmp_path, responder):
    cfg, clock = configuration(artifact_hash=make_case()["capture"]["rule_hash"]), Clock()
    case = make_case(connection_id=cfg.connection_id, observation_origin="injected_transport")
    frozen = feature("writer_contracts").CRMWriteIntent.from_document(intent(case))
    store = private_store(tmp_path)
    store.install(
        cfg.connection_id, cfg.binding, TokenMaterial("fictional-access", "fictional-refresh", None)
    )
    http, requests = fake_provider(clock, config=cfg, account=responder)
    client = SalesforceClient(cfg, store, http, clock)
    return cfg, store, http, requests, client, frozen


def grant(client, frozen):
    assert callable(getattr(client, "send_once", None)), "owned one-attempt Salesforce send missing"
    assert callable(getattr(client, "write_generation", None)), (
        "write credential generation missing"
    )
    client.verify_connection()
    return feature("writer_contracts")._SendAdmission(
        frozen, client, client.write_generation(), lambda: None
    )


def cleanup(store, http):
    http.close()
    store.close()


def test_patch_has_only_captured_etag_and_six_original_fields_once(tmp_path):
    cfg, store, http, requests, client, frozen = client_case(
        tmp_path, lambda _: httpx.Response(204, content=b"")
    )
    try:
        admission = grant(client, frozen)
        result = client.send_once(frozen, admission)
        patches = [r for r in requests if r.method == "PATCH"]
        assert len(patches) == 1
        request = patches[0]
        assert (
            request.url.path
            == f"/services/data/v{cfg.api_version}/sobjects/Account/{cfg.record_ids[0]}"
        )
        assert not request.url.query
        assert request.headers["If-Match"] == frozen.document["source_precondition"]
        assert json.loads(request.content) == {
            remote: frozen.document["intended"][local] for local, remote in OUTPUTS.items()
        }
        assert request.extensions["timeout"] == dict(connect=10, read=30, write=30, pool=10)
        assert result.document["complete"] and result.document["status"] == 204
        assert result.observation_origin == "injected_transport"
        assert "fictional-access" not in repr(result)
        with pytest.raises(CRMRefused):
            client.send_once(frozen, admission)
        assert len([r for r in requests if r.method == "PATCH"]) == 1
    finally:
        cleanup(store, http)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 302])
def test_mutation_status_never_refreshes_retries_or_follows_redirect(tmp_path, status):
    cfg, store, http, requests, client, frozen = client_case(
        tmp_path,
        lambda _: httpx.Response(
            status,
            content=b"fictional-access never printable",
            headers={"Location": "https://evil.invalid"},
        ),
    )
    try:
        result = client.send_once(frozen, grant(client, frozen))
        assert not result.document["complete"]
        assert result.document["status"] == status
        assert [r.method for r in requests] == ["GET", "GET", "PATCH"]
        assert "fictional-access" not in repr(result)
    finally:
        cleanup(store, http)


@pytest.mark.parametrize(
    "status,body,headers,complete",
    [
        (204, b"", {}, True),
        (412, b"", {}, True),
        (
            412,
            b'[{"message":"fictional conflict","errorCode":"PRECONDITION_FAILED"}]',
            {"Content-Type": "application/json"},
            True,
        ),
        (204, b"{}", {"Content-Type": "application/json"}, False),
        (200, b"{}", {"Content-Type": "application/json"}, False),
        (201, b"{}", {"Content-Type": "application/json"}, False),
        (412, b"<html>error</html>", {"Content-Type": "text/html"}, False),
        (
            412,
            b'[{"message":"x","message":"y","errorCode":"X"}]',
            {"Content-Type": "application/json"},
            False,
        ),
        (
            412,
            b'[{"message":"x","errorCode":"X","unexpected":true}]',
            {"Content-Type": "application/json"},
            False,
        ),
        (
            412,
            '[{"message":"x","errorCode":"X"}]'.encode("utf-16"),
            {"Content-Type": "application/json"},
            False,
        ),
        (204, b"", {"Content-Length": "1"}, False),
        (204, b"", {"Transfer-Encoding": "chunked"}, False),
        (412, b"[]", {"Content-Type": "application/json"}, False),
        (412, b"[]", {"Content-Type": "application/json", "Content-Length": "1"}, False),
        (412, b"[]", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, False),
    ],
)
def test_only_closed_complete_response_shapes_can_qualify(
    tmp_path, status, body, headers, complete
):
    cfg, store, http, requests, client, frozen = client_case(
        tmp_path, lambda _: httpx.Response(status, content=body, headers=headers)
    )
    try:
        result = client.send_once(frozen, grant(client, frozen))
        assert result.document["complete"] is complete
        assert len([r for r in requests if r.method == "PATCH"]) == 1
    finally:
        cleanup(store, http)


@pytest.mark.parametrize(
    "header", ["Content-Length", "Content-Type", "Transfer-Encoding", "Content-Encoding"]
)
def test_duplicate_critical_mutation_headers_stay_unknown(tmp_path, header):
    values = {
        "Content-Length": "0",
        "Content-Type": "application/json",
        "Transfer-Encoding": "chunked",
        "Content-Encoding": "identity",
    }
    cfg, store, http, requests, client, frozen = client_case(
        tmp_path,
        lambda _: httpx.Response(
            204, content=b"", headers=[(header, values[header]), (header, values[header])]
        ),
    )
    try:
        result = client.send_once(frozen, grant(client, frozen))
        assert result.document["complete"] is False
        assert len([r for r in requests if r.method == "PATCH"]) == 1
    finally:
        cleanup(store, http)


class BrokenStream(httpx.SyncByteStream):
    def __init__(self, kind):
        self.kind = kind

    def __iter__(self):
        if self.kind == "body":
            yield b"{"
            raise httpx.ReadError("fictional-access must not escape")
        if self.kind == "overflow":
            yield b"x" * (1024 * 1024 + 1)

    def close(self):
        if self.kind == "close":
            raise httpx.ReadError("fictional-refresh must not escape")


@pytest.mark.parametrize("kind", ["timeout", "body", "close", "overflow"])
def test_response_loss_and_close_failure_do_not_manufacture_completion(tmp_path, kind):
    def responder(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("fictional-access must not escape")
        return httpx.Response(204, stream=BrokenStream(kind))

    cfg, store, http, requests, client, frozen = client_case(tmp_path, responder)
    try:
        result = client.send_once(frozen, grant(client, frozen))
        assert result.document["complete"] is False
        assert len([r for r in requests if r.method == "PATCH"]) == 1
        assert "fictional-access" not in repr(result) and "fictional-refresh" not in repr(result)
    finally:
        cleanup(store, http)


def test_output_get_is_separate_and_does_not_widen_old_input_get(tmp_path):
    observed = {}

    def responder(request):
        assert request.method == "GET"
        observed["fields"] = parse_qs(request.url.query.decode())["fields"][0].split(",")
        return httpx.Response(200, json={"Id": configuration().record_ids[0], "FR_Score__c": 86})

    cfg, store, http, requests, client, frozen = client_case(tmp_path, responder)
    try:
        assert callable(getattr(client, "read_outputs", None)), (
            "owned output-only Salesforce read missing"
        )
        client.verify_connection()
        with pytest.raises(CRMRefused):
            http.get(
                cfg.api_origin
                + f"/services/data/v{cfg.api_version}/sobjects/Account/{cfg.record_ids[0]}"
                + "?fields=Id,FR_Score__c",
                "fictional-access",
            )
        assert len(requests) == 2
        result = client.read_outputs(frozen)
        assert observed["fields"] == sorted({"Id", *OUTPUTS.values()})
        assert result.document["fields"] == sorted(OUTPUTS)
        assert result.document["values"] == {"fr_score": 86}
        assert result.observation_origin == "injected_transport"
    finally:
        cleanup(store, http)


def test_changed_token_generation_consumes_no_network_send(tmp_path):
    cfg, store, http, requests, client, frozen = client_case(
        tmp_path, lambda _: httpx.Response(204)
    )
    try:
        admission = grant(client, frozen)
        ready = store.read_ready(cfg.connection_id, cfg.binding)
        store.replace(
            cfg.connection_id,
            TokenMaterial("fictional-new-access", "fictional-new-refresh", None),
            binding=cfg.binding,
            expected_generation=ready.generation,
        )
        with pytest.raises(CRMRefused):
            client.send_once(frozen, admission)
        assert [r.method for r in requests] == ["GET", "GET"]
    finally:
        cleanup(store, http)
