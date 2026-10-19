"""INV-01/02/03/04/05/06/09/11 across the frozen submission boundary."""

import socket
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import event

from flight_recorder.adapters.hubspot.contracts import CaptureRefused
from flight_recorder.collector.canonical import canonical_bytes
from flight_recorder.ledger.schema import decisions_v2, events
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import reconstruct
from tests.adapters.hubspot.test_submission_recovery import api, count, setup_case
from tests.conftest import Harness, canonical_by_type

pytestmark = pytest.mark.invariant


@contextmanager
def local_harness(root):
    harness = Harness(root)
    try:
        yield harness
    finally:
        harness.client.close()
        harness.engine.dispose()


@given(delta=st.integers(-1, 1), offset=st.sampled_from([-8, 0, 5]))
def test_source_availability_before_at_after_boundary_is_never_repaired(delta, offset):
    original = canonical_by_type("decision.recorded")
    boundary = datetime.fromisoformat(
        original["payload"]["decision_boundary"].replace("Z", "+00:00")
    )
    source = (
        (boundary + timedelta(microseconds=delta))
        .astimezone(timezone(timedelta(hours=offset)))
        .isoformat()
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with local_harness(root) as harness:
            if delta > 0:
                with pytest.raises(CaptureRefused):
                    setup_case(
                        root,
                        harness,
                        available_at={"numberofemployees": datetime.fromisoformat(source)},
                    )
                assert count(harness, events) == 0
            else:
                submit, _, store, case, frozen, _ = setup_case(
                    root,
                    harness,
                    available_at={"numberofemployees": datetime.fromisoformat(source)},
                )
                assert (
                    submit.resume_submission(store, harness.collector, case["unit"]).state
                    == "complete"
                )
                proof = frozen.document["stages"][2]["envelope"]["payload"]["capture"][
                    "input_provenance"
                ]["employee_count"]
                assert datetime.fromisoformat(proof["available_at"]) == datetime.fromisoformat(
                    source
                )
                assert count(harness, decisions_v2) == 1


@given(employee_now=st.integers(0, 1000000), clock_shift=st.integers(1, 1000000))
def test_current_data_and_clock_cannot_rebuild_a_frozen_request(employee_now, clock_shift):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with local_harness(root) as harness:
            submit, _, store, case, frozen, prereqs = setup_case(root, harness)
            case["capture"]["observation"]["properties"]["numberofemployees"] = employee_now
            changed = replace(
                prereqs,
                recorded_at=(
                    datetime.fromisoformat(prereqs.recorded_at) + timedelta(seconds=clock_shift)
                ).isoformat(),
                artifact_envelope=b"current logic unavailable",
            )
            assert (
                submit.freeze_submission(
                    store, harness.collector, case["unit"], prerequisites=changed
                ).canonical_bytes
                == frozen.canonical_bytes
            )
            assert (
                submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
            )
            with harness.engine.connect() as conn:
                assert (
                    reconstruct(conn, frozen.document["stages"][-1]["event_id"]).result.score
                    == case["result"].score
                )
            assert count(harness, events) == 4


def test_domain_writes_only_inside_collector_and_replay_is_offline(tmp_path, harness, monkeypatch):
    submit, _, _ = api()
    in_collector = False

    def check_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            assert in_collector, "adapter or replay bypassed Collector"

    event.listen(harness.engine, "before_cursor_execute", check_sql)
    original = harness.collector.ingest_json

    def ingest(body):
        nonlocal in_collector
        in_collector = True
        try:
            return original(body)
        finally:
            in_collector = False

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)

    def no_network(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    _, _, store, case, frozen, _ = setup_case(tmp_path, harness)
    assert count(harness, events) == 0
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    # Register the existing alternate fixture only through the collector.
    from flight_recorder.collector.canonical import canonical_hash
    from tests.conftest import canonical_envelopes

    alternate = next(
        e
        for e in canonical_envelopes()
        if e["event_type"] == "logic_artifact.registered"
        and e["event_id"] != case["artifact"]["event_id"]
    )
    harness.collector.ingest_json(canonical_bytes(alternate))
    with harness.engine.connect() as conn:
        decision = frozen.document["stages"][-1]["event_id"]
        assert reconstruct(conn, decision).result.score == case["result"].score
        assert (
            replay(conn, decision, canonical_hash(alternate["payload"]["artifact"])).label
            == "counterfactual"
        )
    assert count(harness, decisions_v2) == 1
