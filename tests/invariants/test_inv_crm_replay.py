"""Generated local histories preserve the first original without provider or secret access."""

import itertools

import pytest

from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import reconstruct
from tests.adapters.crm.test_poll import dispatch, harness
from tests.invariants.test_inv_crm_collector import snapshot

pytestmark = pytest.mark.invariant


def test_generated_later_crm_mutations_do_not_change_sealed_replay(tmp_path):
    for index, sequence in enumerate(itertools.product((184, 700), repeat=3)):
        directory = tmp_path / str(index)
        directory.mkdir()
        runtime, provider = harness(directory)
        try:
            dispatch(runtime, "first")
            runtime.run_pending()
            first = runtime.poll_report("first").members[0].decision_event_id
            with runtime.collector.engine.connect() as conn:
                original = reconstruct(conn, first)
            for number, count in enumerate(sequence):
                provider.count = count
                dispatch(runtime, f"later-{number}")
                runtime.run_pending()

            def forbidden(*args, **kwargs):
                raise AssertionError("replay touched present CRM/token/clock")

            runtime.clock = forbidden
            runtime.client = forbidden
            provider.read_selected = forbidden
            provider.verify_connection = forbidden
            before = snapshot(runtime.collector.engine)
            with runtime.collector.engine.connect() as conn:
                assert reconstruct(conn, first) == original
                counterfactual = replay(conn, first, original.artifact_hash)
                assert counterfactual.label == "counterfactual"
                assert counterfactual.original == original
            assert snapshot(runtime.collector.engine) == before
            assert runtime._tokens is None and runtime._http is None
        finally:
            runtime.close()
