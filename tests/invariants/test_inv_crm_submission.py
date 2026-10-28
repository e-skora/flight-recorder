"""INV-01/04/05/11: local delivery retries retain one exact accepted original."""

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.collector.canonical import canonical_bytes
from tests.adapters.crm.test_capture_submission import prepare, resume, rows, setup

pytestmark = pytest.mark.invariant


@given(
    st.integers(min_value=0, max_value=3),
    st.sampled_from(["before_commit", "after_commit", "ack"]),
    st.integers(min_value=1, max_value=3),
)
@settings(deadline=None)
def test_generated_return_or_ack_losses_never_change_delivery_bytes_or_duplicate_original(
    index, mode, failures
):
    with tempfile.TemporaryDirectory(
        prefix="fr-submit-fault-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        store, owner, cap, engine, collector, prerequisites = setup(Path(name))
        frozen = prepare(store, collector, owner, prerequisites)
        stage = frozen.document["stages"][index]
        original = collector.ingest_json
        observe = store.observe_submission
        remaining = failures
        sent = []

        def deliver(body):
            nonlocal remaining
            sent.append(body)
            if mode != "ack" and json.loads(body)["event_id"] == stage["event_id"] and remaining:
                remaining -= 1
                if mode == "after_commit":
                    original(body)
                raise OSError("fictional-private-source-value")
            return original(body)

        def save_ack(conn, owner_, plan, attempt, disposition, details):
            nonlocal remaining
            observe(conn, owner_, plan, attempt, disposition, details)
            if (
                mode == "ack"
                and store._submission_attempt(conn, plan, attempt)["stage"] == stage["stage"]
                and remaining
            ):
                remaining -= 1
                raise OSError("fictional-private-source-value")

        collector.ingest_json = deliver
        store.observe_submission = save_ack
        for _ in range(failures):
            with pytest.raises(CRMRefused):
                resume(store, collector, owner)
        assert remaining == 0
        assert resume(store, collector, owner).state == "complete"
        assert len(rows(engine, "events")) == 4 and len(rows(engine, "crm_decisions")) == 1
        expected = {
            s["event_id"]: canonical_bytes(s["envelope"]) for s in frozen.document["stages"]
        }
        assert all(body == expected[json.loads(body)["event_id"]] for body in sent)
        assert store.load_submission(owner) == frozen
        store.close()
        engine.dispose()
