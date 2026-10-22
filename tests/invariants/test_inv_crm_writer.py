"""Finite generated command orderings preserve the durable single-attempt boundary."""

import itertools
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.adapters.crm.test_salesforce_writer import enable, feature, policy, prepare, send, setup

pytestmark = pytest.mark.invariant
ORDERINGS = tuple(itertools.permutations(("send", "observe", "disable")))


@given(mode=st.sampled_from(("normal", "lost", "412")), ordering=st.sampled_from(ORDERINGS))
def test_generated_command_ordering_never_retries_or_promotes_unknown(mode, ordering):
    with TemporaryDirectory(prefix="fr-writer-invariant-") as directory:
        runtime, provider, decision = setup(Path(directory).resolve())
        try:
            enable(runtime)
            intent = prepare(runtime, decision)
            provider.mode = mode
            for command in ordering:
                if command == "send":
                    send(runtime, intent)
                elif command == "observe":
                    feature().observe_write(runtime, intent.operation_id, clock=runtime.clock)
                else:
                    feature().set_write_policy(
                        runtime, policy(runtime, generation=2, enabled=False), clock=runtime.clock
                    )
                before = len(provider.sent)
                result = runtime._writer_store.progress(intent.operation_id)
                if command == "send":
                    assert send(runtime, intent) == result
                assert len(provider.sent) == before
                assert before <= 1
                if mode == "lost" and before:
                    assert result.state == "unknown" and result.held
                    # A later matching tuple remains an observation of uncertain execution.
                    assert (
                        feature()
                        .observe_write(runtime, intent.operation_id, clock=runtime.clock)
                        .state
                        == "unknown"
                    )
                runtime.validate_write_state()
        finally:
            runtime.close()
