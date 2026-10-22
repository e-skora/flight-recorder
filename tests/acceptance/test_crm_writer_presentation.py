"""Saved Salesforce write evidence is original-bound, offline and honest about uncertainty."""

import html
import importlib
import importlib.util
import sqlite3

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.web.crm_decision_view import load_crm_decision_page
from flight_recorder.web.summaries import summarize
from tests.adapters.crm.test_salesforce_writer import enable, feature, policy, prepare, send, setup


def operation_api():
    name = "flight_recorder.web.crm_operation_view"
    assert importlib.util.find_spec(name) is not None, "source-aware CRM write history missing"
    return importlib.import_module(name)


def history(tmp_path, state="observed_applied_at_t"):
    runtime, provider, decision = setup(tmp_path)
    enable(runtime)
    intent = prepare(runtime, decision)
    if state != "prepared":
        if state == "refused":
            feature().set_write_policy(
                runtime, policy(runtime, generation=2, enabled=False), clock=runtime.clock
            )
        else:
            provider.mode = {
                "unknown": "lost",
                "awaiting_observation": "partial",
                "refused_no_effect": "412",
            }.get(state, "normal")
        assert send(runtime, intent).state == state
    return runtime, provider, decision, intent


def page_body(runtime, decision):
    app = create_app(runtime.config.domain_path)
    app.state.crm_mode = True
    account = runtime.config.targets[0].account_ref
    try:
        with TestClient(app) as client:
            response = client.get(f"/accounts/{account}/decisions/{decision}")
            assert response.status_code == 200
            return html.unescape(response.text)
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize(
    "state",
    [
        "prepared",
        "refused",
        "unknown",
        "awaiting_observation",
        "observed_applied_at_t",
        "refused_no_effect",
    ],
)
def test_each_write_state_shows_exact_original_tuple_and_saved_origin(tmp_path, state):
    operation_api()
    runtime, _, decision, intent = history(tmp_path, state)
    try:
        with runtime.collector.engine.connect() as conn:
            page = load_crm_decision_page(
                conn, runtime.config.targets[0].account_ref, decision, None
            )
        assert page.original is not None and page.operation_failure is None
        assert len(page.operations) == 1
        operation = page.operations[0]
        assert operation.state == state
        assert operation.decision_event_id == decision
        assert operation.operation_id == intent.operation_id
        assert operation.intended == intent.document["intended"]
        body = page_body(runtime, decision)
        for value in [
            decision,
            intent.operation_id,
            state,
            "Injected transport test",
            intent.document["source_precondition"],
        ]:
            assert value in body
        assert "No CRM write observation recorded by this version" not in body
        assert "does not create a buyer outcome" in body
        if state == "unknown":
            assert "matching record does not confirm" in body
        if state == "observed_applied_at_t":
            assert "at the recorded read" in body
    finally:
        runtime.close()


def test_decision_and_trace_render_without_operational_or_provider_access(tmp_path, monkeypatch):
    operation_api()
    runtime, provider, decision, _ = history(tmp_path, "unknown")
    try:
        domain, op = (
            runtime.config.domain_path.read_bytes(),
            runtime.config.operational_path.read_bytes(),
        )
        before = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
        monkeypatch.setattr(
            runtime, "client", lambda: pytest.fail("view accessed provider credentials")
        )
        monkeypatch.setattr(
            runtime.store, "_read", lambda: pytest.fail("decision view accessed operational state")
        )
        assert "unknown" in page_body(runtime, decision)
        assert runtime.config.domain_path.read_bytes() == domain
        assert runtime.config.operational_path.read_bytes() == op
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == before
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["projection", "missing_table"])
def test_corrupt_saved_operation_is_visible_without_losing_verified_original(tmp_path, damage):
    operation_api()
    runtime, _, decision, _ = history(tmp_path)
    try:
        with sqlite3.connect(runtime.config.domain_path) as conn:
            if damage == "projection":
                trigger = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name='crm_operation_observations_no_update'"
                ).fetchone()[0]
                conn.execute("DROP TRIGGER crm_operation_observations_no_update")
                conn.execute("UPDATE crm_operation_observations SET state='unknown'")
                conn.execute(trigger)
            else:
                conn.execute("DROP TABLE crm_operation_observations")
        body = page_body(runtime, decision)
        assert "Verified original" in body
        assert "Write history unavailable" in body
        assert "No accepted CRM write observation" not in body
    finally:
        runtime.close()


def test_schema2_trace_is_source_aware_and_fake_schema1_summary_stays_exact(tmp_path):
    operation_api()
    runtime, _, _, intent = history(tmp_path, "unknown")
    try:
        with runtime.collector.engine.connect() as conn:
            from flight_recorder.collector.crm_operations import operation_history

            docs = operation_history(conn, runtime.config.targets[0].account_ref)
        text = summarize("external_operation.observed", docs[-1]["payload"], "2")
        assert "Injected transport test" in text and "Simulated operation" not in text
        assert intent.document["decision_event_id"] in text and "unknown" in text
        assert (
            summarize(
                "external_operation.observed",
                dict(state="prepared", observed_at="T", reason="R"),
                "1",
            )
            == "Simulated operation observation: prepared at T (R)"
        )
    finally:
        runtime.close()


def test_status_does_not_confuse_local_enablement_or_unknown_with_live_readiness(tmp_path):
    operation_api()
    runtime, provider, _, _ = history(tmp_path, "unknown")
    try:
        before = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
        status = feature("status").connection_status(runtime)
        assert status.write_mode == "uncertainty_hold"
        assert status.atomic_source_guard is False
        assert status.policy_generation == 1
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == before
    finally:
        runtime.close()


def test_busy_status_has_no_false_disabled_policy_generation(tmp_path):
    operation_api()
    runtime, _, _, _ = history(tmp_path, "unknown")
    try:
        runtime._execution.acquire()
        try:
            status = feature("status").connection_status(runtime)
            assert status.write_mode == "check_in_progress"
            assert status.policy_generation is None
        finally:
            runtime._execution.release()
    finally:
        runtime.close()


def test_status_reports_damaged_domain_as_a_hold_without_repair(tmp_path):
    operation_api()
    runtime, provider, _, _ = history(tmp_path)
    try:
        with sqlite3.connect(runtime.config.domain_path) as conn:
            trigger = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='crm_operation_observations_no_update'"
            ).fetchone()[0]
            conn.execute("DROP TRIGGER crm_operation_observations_no_update")
            conn.execute("UPDATE crm_operation_observations SET state='unknown'")
            conn.execute(trigger)
        before = (
            runtime.config.domain_path.read_bytes(),
            runtime.config.operational_path.read_bytes(),
        )
        calls = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
        status = feature("status").connection_status(runtime)
        assert status.write_mode == "integrity_hold" and status.policy_generation is None
        assert (
            runtime.config.domain_path.read_bytes(),
            runtime.config.operational_path.read_bytes(),
        ) == before
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == calls
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "origin,label",
    [
        ("fixture", "Fictional fixture"),
        ("injected_transport", "Injected transport test"),
        ("provider_http", "Recorded provider HTTP observation"),
    ],
)
def test_archived_origin_labels_never_claim_an_active_connection(harness, origin, label):
    operation_api()
    from tests.acceptance.test_crm_operations import intent, observation, post_ok
    from tests.acceptance.test_crm_operations import prepare as prepare_history
    from tests.adapters.crm.cases import make_case

    # These are locally authored archived-source representations, never actual provider requests.
    case = prepare_history(harness, make_case(observation_origin=origin))
    frozen = intent(case)
    post_ok(harness, observation(case, frozen))
    harness.app.state.crm_mode = True
    response = harness.client.get(
        f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
    )
    assert response.status_code == 200
    body = html.unescape(response.text)
    assert label in body and "not current connection health" in body
    assert frozen["operation_id"] in body


def test_old_crm_domain_without_operation_schema_remains_read_only(harness):
    operation_api()
    from tests.acceptance.test_crm_presentation import history as capture_history

    case = capture_history(harness)
    before = harness.db_path.read_bytes()
    body = harness.client.get(case["url"]).text
    assert "Verified original" in body and "No CRM write observation recorded" in body
    assert "Write history unavailable" not in body
    assert harness.db_path.read_bytes() == before


def test_exact_destination_field_names_are_visible_with_original_values(tmp_path):
    from flight_recorder.adapters.crm.mapping import OUTPUTS

    runtime, _, decision, _ = history(tmp_path, "prepared")
    try:
        body = page_body(runtime, decision)
        assert all(remote in body for remote in OUTPUTS.values())
    finally:
        runtime.close()


def test_disabled_policy_is_visible_separately_from_unknown_hold(tmp_path):
    runtime, _, _, _ = history(tmp_path, "unknown")
    try:
        feature().set_write_policy(
            runtime, policy(runtime, generation=2, enabled=False), clock=runtime.clock
        )
        status = feature("status").connection_status(runtime)
        assert status.write_mode == "uncertainty_hold"
        assert status.policy_enabled is False and status.policy_generation == 2
    finally:
        runtime.close()
