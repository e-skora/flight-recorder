"""Source-aware CRM history is read-only, verifiable and separate from connection health."""

import copy
import html
import importlib
import importlib.util

import pytest
from sqlalchemy import event

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.ledger.crm_schema import create_crm_schema
from tests.acceptance.test_hubspot_v2_presentation import corrupt
from tests.adapters.crm.cases import envelopes, make_case, seal
from tests.conftest import canonical_envelopes, register_current_logic
from tests.invariants.test_inv_crm_collector import snapshot


def page_api():
    name = "flight_recorder.web.crm_decision_view"
    assert importlib.util.find_spec(name) is not None, "CRM history presentation missing"
    return importlib.import_module(name)


def history(harness, case=None):
    create_crm_schema(harness.engine)
    case = case or make_case()
    for key in ("account", "artifact", "evidence", "decision"):
        response = harness.post(case[key])
        assert response.status_code == 201, response.text
    harness.app.state.crm_mode = True
    case["url"] = (
        f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
    )
    return case


def get(harness, case, query=""):
    response = harness.client.get(case["url"] + query)
    assert response.status_code == 200, response.text
    return html.unescape(response.text)


def test_original_evidence_rules_and_current_replay_are_read_only(harness):
    case = history(harness)
    current = register_current_logic(harness)
    before = snapshot(harness.engine)
    body = get(harness, case, "?current=" + current)
    assert "Verified original" in body
    assert "Ephemeral counterfactual" in body
    assert "Fictional fixture" in body
    assert case["capture"]["unit_id"] in body
    for factor in case["artifact"]["payload"]["artifact"]["factors"]:
        assert factor["rule"] in body
    for value in (
        case["capture"]["read_started_at"],
        case["capture"]["read_finished_at"],
        case["capture"]["source_updated_at"],
        case["capture"]["capture_boundary"],
    ):
        assert value in body
    assert snapshot(harness.engine) == before


@pytest.mark.parametrize(
    "origin,label",
    [
        ("fixture", "Fictional fixture"),
        ("injected_transport", "Injected transport test"),
        ("provider_http", "Recorded provider HTTP observation"),
    ],
)
def test_origin_label_is_recorded_not_current_connection(harness, origin, label):
    case = history(harness, make_case(observation_origin=origin))
    body = get(harness, case)
    assert label in body
    assert "current connection health" in body
    assert "All accounts, vendor-like events" not in body
    trace = harness.client.get("/accounts/" + case["decision"]["account_ref"]).text
    assert label in trace


@pytest.mark.parametrize(
    "side", ["historical_missing", "historical_mismatch", "historical_evaluator", "current_missing"]
)
def test_rule_failures_do_not_substitute_current_or_original(harness, side):
    case = history(harness)
    current = register_current_logic(harness)
    original = case["decision"]["payload"]["logic_artifact"]["artifact_hash"]
    if side == "current_missing":
        current = "0" * 64
    else:
        raw = harness.engine.raw_connection()
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DROP TRIGGER logic_artifacts_no_update")
            raw.execute("DROP TRIGGER logic_artifacts_no_delete")
            if side == "historical_missing":
                raw.execute("DELETE FROM logic_artifacts WHERE artifact_hash=?", (original,))
            elif side == "historical_mismatch":
                raw.execute(
                    "UPDATE logic_artifacts SET artifact_json=? WHERE artifact_hash=?",
                    ("{}", original),
                )
            else:
                raw.execute(
                    "UPDATE logic_artifacts SET evaluator_version=? WHERE artifact_hash=?",
                    ("unsupported", original),
                )
            raw.commit()
        finally:
            raw.close()
    body = get(harness, case, "?current=" + current)
    if side == "current_missing":
        assert "Verified original" in body and "ArtifactMissing" in body
    else:
        assert "Verified original" not in body
        assert "original could not be verified" in body
    assert "Ephemeral counterfactual" not in body


@pytest.mark.parametrize(
    "table,values,delete,drop",
    [
        ("crm_captures", {"body_json": "{}"}, False, False),
        ("crm_decisions", {"score": 999}, False, False),
        ("crm_decisions", {"workflow_version": "foreign"}, False, False),
        ("crm_decisions", {}, True, False),
        ("crm_decisions", {}, False, True),
        ("crm_decision_context", {"value_text": "999"}, False, False),
        ("crm_decision_context", {}, True, False),
        ("crm_decision_consumed_inputs", {"contribution": 999}, False, False),
        ("crm_decision_consumed_inputs", {}, True, False),
        ("crm_evidence_versions", {"available_at": "2999-01-01T00:00:00.000000Z"}, False, False),
    ],
)
def test_known_crm_corruption_is_named_without_repair(harness, table, values, delete, drop):
    case = history(harness)
    corrupt(harness, table, values=values, delete=delete, drop=drop)
    before = snapshot(harness.engine)
    body = get(harness, case, "?current=" + "0" * 64)
    assert "IntegrityFailure" in body
    assert "Verified original" not in body and "Ephemeral counterfactual" not in body
    assert snapshot(harness.engine) == before


def test_four_input_states_and_zero_consumption_are_distinct(harness):
    case = make_case()
    cap = case["capture"]
    cap["properties"]["NumberOfEmployees"] = 1
    del cap["properties"]["FR_Funding_Stage__c"]
    del cap["properties"]["FR_Funding_Observed_On__c"]
    cap["unavailable_fields"] = ["FR_Open_Platform_Roles__c"]
    del cap["properties"]["FR_Open_Platform_Roles__c"]
    from flight_recorder.adapters.crm.mapping import map_inputs

    cap["inputs"] = map_inputs(cap["properties"], cap["unavailable_fields"])
    case = history(harness, envelopes(seal(cap)))
    with harness.engine.connect() as conn:
        page = page_api().load_crm_decision_page(
            conn, case["decision"]["account_ref"], case["decision"]["event_id"], None
        )
    rows = {r.key: r for r in page.context_rows}
    assert rows["employee_count"].state == "available and consumed"
    assert rows["employee_count"].contribution == 0
    assert rows["verified_integration_pressure"].state == "available but ignored"
    assert rows["open_platform_engineering_roles"].state == "explicitly unavailable"
    assert rows["funding_event"].state == "absent"


def test_cross_account_url_is_404_and_explanation_is_escaped(harness):
    case = make_case()
    case["decision"]["payload"]["explanation"] = "<script>fictional()</script>"
    case = history(harness, case)
    body = harness.client.get(case["url"]).text
    assert "<script>fictional()</script>" not in body
    assert "&lt;script&gt;fictional()&lt;/script&gt;" in body
    foreign = copy.deepcopy(case["account"])
    foreign["account_ref"] = "foreign"
    foreign["event_id"] = "foreign-discovery"
    assert harness.post(foreign).status_code == 201
    assert (
        harness.client.get(
            "/accounts/foreign/decisions/" + case["decision"]["event_id"]
        ).status_code
        == 404
    )


def test_legacy_readers_never_query_crm_tables_or_expose_status(harness):
    for envelope in canonical_envelopes():
        assert harness.post(envelope).status_code == 201
    forbidden = []

    def inspect_sql(conn, cursor, statement, parameters, context, executemany):
        if "crm_" in statement.lower():
            forbidden.append(statement)

    event.listen(harness.engine, "before_cursor_execute", inspect_sql)
    event.listen(harness.app.state.engine, "before_cursor_execute", inspect_sql)
    case = next(e for e in canonical_envelopes() if e["event_type"] == "decision.recorded")
    response = harness.client.get(
        "/accounts/" + case["account_ref"] + "/decisions/" + case["event_id"]
    )
    assert response.status_code == 200
    assert harness.client.get("/crm/connections").status_code == 404
    assert not forbidden


def test_operation_is_distinct_from_buyer_outcome(harness):
    case = history(harness)
    body = get(harness, case)
    assert "buyer outcome" in body
    assert "No CRM write observation recorded" in body


@pytest.mark.parametrize("origin", ["injected_transport", "provider_http"])
def test_status_uses_only_saved_confirmation_and_never_claims_current_health(
    tmp_path, monkeypatch, origin
):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from flight_recorder.adapters.crm.contracts import ConfirmedConnection
    from tests.adapters.crm.test_crm_ingress import setup_app

    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        cfg = runtime.config.connection
        provider.verify_connection = lambda: ConfirmedConnection(
            cfg.connection_id,
            cfg.provider,
            cfg.expected_instance_id,
            cfg.expected_user_id,
            cfg.api_version,
            datetime(2026, 4, 17, 10, tzinfo=UTC),
            origin,
        )
        runtime.verify_for_read(provider)

        def forbidden(*args, **kwargs):
            raise AssertionError("status accessed provider/token/clock")

        monkeypatch.setattr(runtime, "client", forbidden)
        monkeypatch.setattr(runtime, "clock", forbidden)
        before = snapshot(runtime.collector.engine)
        response = client.get("/crm/connections")
        assert response.status_code == 200, response.text
        assert "Identity last confirmed" in response.text
        assert "current authorization or health" in response.text
        assert "Disabled" in response.text and "Not qualified" in response.text
        assert (
            "Injected transport test"
            if origin == "injected_transport"
            else "Recorded provider HTTP observation"
        ) in response.text
        assert snapshot(runtime.collector.engine) == before
        assert runtime._tokens is None and runtime._http is None


@pytest.mark.parametrize(
    "mutation", ["absent", "foreign_org", "foreign_user", "naive_time", "invalid_origin"]
)
def test_bad_or_absent_confirmation_does_not_upgrade_configured_identity(tmp_path, mutation):
    from dataclasses import replace
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from flight_recorder.adapters.crm.contracts import ConfirmedConnection
    from tests.adapters.crm.test_crm_ingress import setup_app

    app, runtime, provider = setup_app(tmp_path)
    with TestClient(app, base_url="http://localhost:8768") as client:
        cfg = runtime.config.connection
        value = ConfirmedConnection(
            cfg.connection_id,
            cfg.provider,
            cfg.expected_instance_id,
            cfg.expected_user_id,
            cfg.api_version,
            datetime(2026, 4, 17, tzinfo=UTC),
            "provider_http",
        )
        if mutation == "foreign_org":
            value = replace(value, instance_id="foreign-org")
        elif mutation == "foreign_user":
            value = replace(value, user_id="foreign-user")
        elif mutation == "naive_time":
            value = replace(value, confirmed_at=datetime(2026, 4, 17))
        elif mutation == "invalid_origin":
            value = replace(value, observation_origin="live")
        elif mutation == "absent":
            value = None
        runtime._confirmed_connection = value
        response = client.get("/crm/connections")
        assert response.status_code == 200, response.text
        assert "Identity not checked in this process" in response.text
        assert "Identity last confirmed" not in response.text
        assert runtime._tokens is None and runtime._http is None and provider.calls == 0


def test_reprocessed_original_has_verified_predecessor_not_admission_self_conflict(harness):
    case = history(harness)
    registration = copy.deepcopy(case["artifact"])
    registration["event_id"] = "crm-reprocessing-artifact"
    artifact = registration["payload"]["artifact"]
    artifact["artifact_id"] = "crm-reprocessed-v2"
    artifact["logic_version"] = "test-crm-reprocessed"
    artifact["factors"][0]["weight"] += 1
    assert harness.post(registration).status_code == 201
    cap = copy.deepcopy(case["capture"])
    cap.update(
        rule_hash=canonical_hash(artifact),
        capture_sequence=2,
        ownership_generation=2,
        reprocesses=cap["unit_id"],
    )
    later = envelopes(seal(cap), registration)
    assert harness.post(later["decision"]).status_code == 201
    later["url"] = (
        f"/accounts/{later['decision']['account_ref']}/decisions/{later['decision']['event_id']}"
    )
    body = get(harness, later)
    assert "Verified original" in body and case["capture"]["unit_id"] in body
    assert case["url"] in body
    raw = harness.engine.raw_connection()
    try:
        raw.execute("DROP TRIGGER crm_decisions_no_update")
        raw.execute(
            "UPDATE crm_decisions SET score=999 WHERE decision_event_id=?",
            (case["decision"]["event_id"],),
        )
        raw.commit()
    finally:
        raw.close()
    body = get(harness, later)
    assert "IntegrityFailure" in body
    assert "Verified original" not in body


def test_missing_original_artifact_table_is_visible_named_failure(harness):
    case = history(harness)
    corrupt(harness, "logic_artifacts", drop=True)
    body = get(harness, case)
    assert "IntegrityFailure" in body and "Verified original" not in body


def test_archived_provider_origin_does_not_confirm_connection_after_restart(tmp_path):
    from fastapi.testclient import TestClient

    from flight_recorder.adapters.crm.contracts import ObservedRead
    from flight_recorder.adapters.crm.runtime import CRMRuntime
    from flight_recorder.crm_app import create_crm_app
    from tests.adapters.crm.test_crm_ingress import BODY, HEADERS, setup_app

    app, runtime, provider = setup_app(tmp_path)
    original = provider.read_selected

    def archived_claim(target):
        d = original(target).document
        d["observation_origin"] = "provider_http"
        return ObservedRead.from_document(d)

    provider.read_selected = archived_claim
    with TestClient(app, base_url="http://localhost:8768") as client:
        assert client.post("/api/internal/crm/poll", headers=HEADERS, json=BODY).status_code == 202
        runtime.run_pending()
        assert runtime.poll_report(BODY["tick_id"]).disposition == "complete"

    def forbidden():
        raise AssertionError("read-only status fetched credentials/provider")

    restarted = CRMRuntime(runtime.config, client_factory=forbidden)
    app = create_crm_app(runtime.config, runtime=restarted, background=False)
    with TestClient(app, base_url="http://localhost:8768") as client:
        before = snapshot(restarted.collector.engine)
        body = client.get("/crm/connections").text
        assert "Recorded provider HTTP observation" in body
        assert "Identity not checked in this process" in body
        assert "Identity last confirmed" not in body
        assert snapshot(restarted.collector.engine) == before
        assert restarted._tokens is None and restarted._http is None


def test_unregistered_current_selection_stays_visible_in_selector(harness):
    from tests.acceptance.test_decision_detail_page import element

    case = history(harness)
    unknown = "0" * 64
    body = get(harness, case, "?current=" + unknown)
    selector = element(body, "current-artifact")
    assert f'value="{unknown}" selected' in selector
    assert "Unregistered selection" in selector
    assert "Verified original" in body and "ArtifactMissing" in body
