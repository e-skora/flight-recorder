"""INV01/02/03/04/05/06/08/09/10/11 for simulated writer observations."""

import copy
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.analytics.insights import insights
from flight_recorder.attribution.policy import ledger_maximum
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.schema import format_utc
from flight_recorder.fixtures import dataset_comparison_workflow_version, dataset_signals
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import reconstruct
from tests.adapters.hubspot.test_operation_admission import (
    accepted_case,
    ingest,
    make_intent,
    observation,
    rehash_intent,
)
from tests.adapters.hubspot.test_write_policy import Clock, api, writer_case
from tests.conftest import Harness, register_current_logic, seed_all
from tests.public_demo.conftest import full_state

pytestmark = pytest.mark.invariant


def preserved_domain(path):
    state = full_state(path)
    state["events"] = [row for row in state["events"] if row[3] != "external_operation.observed"]
    state["sqlite_sequence"] = [
        row for row in state.get("sqlite_sequence", []) if row[0] != "events"
    ]
    return state


def captures(path):
    with sqlite3.connect(path) as conn:
        return {
            name: conn.execute(f"SELECT * FROM {name}").fetchall()
            for name in (
                "source_identities",
                "raw_notifications",
                "processing_units",
                "receipts",
                "captures",
            )
        }


def metrics(harness):
    with harness.engine.connect() as conn:
        result = asdict(
            insights(
                conn,
                ledger_maximum(conn),
                signals=dataset_signals(),
                comparison_workflow_version=dataset_comparison_workflow_version(),
            )
        )
    result.pop("cutoff")  # New event appends advance the cutoff, not the measured populations.
    return result


def test_every_writer_state_preserves_history_and_actual_statistics(tmp_path, harness):
    writer, stores, contracts, fakes = api()
    context = accepted_case(tmp_path, harness)
    seed_all(harness)
    current = register_current_logic(harness)
    before = preserved_domain(harness.db_path)
    captured = captures(context["store"].path)
    actual = metrics(harness)
    legacy = harness.client.get("/accounts/novasignal-ai").content
    store = stores.WriterStore(context["store"])
    clock = Clock(context)
    intent = writer.prepare_intent(store, harness.collector, context["case"]["unit"], clock=clock)
    fake = fakes.FakeCRM()
    assert (
        writer.send_intent(store, harness.collector, intent, fake, clock=clock).state == "refused"
    )
    fake.mode = "timeout"
    policy = contracts.WritePolicy(
        enabled=True, allowlist=frozenset({intent.document["company_key"]})
    )
    assert (
        writer.send_intent(store, harness.collector, intent, fake, policy=policy, clock=clock).state
        == "unknown"
    )
    fake.apply_pending(intent.document["operation_id"])
    assert (
        writer.reconcile_intent(store, harness.collector, intent, fake, clock=clock).state
        == "observed_applied_at_t"
    )
    assert preserved_domain(harness.db_path) == before
    assert captures(store.path) == captured
    assert metrics(harness) == actual
    assert harness.client.get("/accounts/novasignal-ai").content == legacy
    with harness.engine.connect() as conn:
        original = reconstruct(conn, context["decision"]["event_id"])
        counterfactual = replay(conn, context["decision"]["event_id"], current)
    assert original.result == context["case"]["result"]
    assert counterfactual.original.result == original.result


def test_reconstruct_replay_and_gets_are_offline_and_cannot_call_writer(
    tmp_path, harness, monkeypatch
):
    c = writer_case(tmp_path, harness)
    current = register_current_logic(harness)
    fake_state = (c.fake.send_count, c.fake.read_count)
    before = full_state(harness.db_path)
    operational = c.store.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("read attempted writer/network side effect")

    monkeypatch.setattr(c.fake, "send", forbidden)
    monkeypatch.setattr(c.fake, "observe", forbidden)
    monkeypatch.setattr(c.writer, "send_intent", forbidden)
    monkeypatch.setattr(c.writer, "prepare_intent", forbidden)
    monkeypatch.setattr(harness.collector, "ingest_json", forbidden)
    import socket

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with harness.engine.connect() as conn:
        reconstruct(conn, c.context["decision"]["event_id"])
        replay(conn, c.context["decision"]["event_id"], current)
    path = (
        f"/accounts/{c.context['decision']['account_ref']}/decisions/"
        f"{c.context['decision']['event_id']}"
    )
    response = harness.client.get(path + "?current=" + current)
    assert response.status_code == 200 and "Ephemeral counterfactual" in response.text
    assert full_state(harness.db_path) == before
    assert c.store.path.read_bytes() == operational
    assert (c.fake.send_count, c.fake.read_count) == fake_state


@given(delta=st.sampled_from([-1, 0, 1]), offset=st.integers(min_value=-720, max_value=840))
def test_operation_time_boundaries_and_typed_targets(tmp_path_factory, delta, offset):
    directory = tmp_path_factory.mktemp("writer-boundary")
    h = Harness(directory)
    try:
        context = accepted_case(directory, h)
        intent = make_intent(context)
        document = observation(context, intent)
        instant = datetime.fromisoformat(intent["created_at"]) + timedelta(milliseconds=delta)
        alternate = instant.astimezone(timezone(timedelta(minutes=offset))).isoformat()
        document["occurred_at"] = document["recorded_at"] = document["payload"]["observed_at"] = (
            alternate
        )
        before = full_state(h.db_path)
        if delta < 0:
            with pytest.raises(CollectorError):
                ingest(h, document)
            assert full_state(h.db_path) == before
        else:
            result = ingest(h, document)
            normalized = copy.deepcopy(document)
            normalized["occurred_at"] = normalized["recorded_at"] = normalized["payload"][
                "observed_at"
            ] = format_utc(instant)
            assert result.canonical_hash == canonical_hash(normalized)
        wrong = make_intent(context, revision=2, shift=100)
        unit = context["case"]["unit"]
        wrong["company_key"] = canonical_text(
            [str(unit.notification.portal_id), unit.notification.company_id]
        )
        rehash_intent(wrong)
        with pytest.raises(CollectorError):
            ingest(h, observation(context, wrong))
    finally:
        h.client.close()
        h.engine.dispose()
        h.app.state.engine.dispose()
