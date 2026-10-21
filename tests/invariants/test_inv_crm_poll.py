"""INV-01/02/05/11: generated poll restarts retain one frozen original and read."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flight_recorder.collector.canonical import canonical_bytes
from tests.adapters.crm.test_poll import count, dispatch, feature, harness


@pytest.mark.invariant
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(stage=st.integers(0, 3), losses=st.integers(1, 3), after_commit=st.booleans())
def test_generated_local_delivery_restarts_never_reacquire_frozen_source(
    tmp_path, stage, losses, after_commit
):
    with TemporaryDirectory(dir=tmp_path, prefix="poll-restart-") as directory:
        runtime, provider = harness(Path(directory))
        cfg = runtime.config
        remaining = losses
        sent = []
        frozen = None
        stages = [
            "account.discovered",
            "logic_artifact.registered",
            "evidence.recorded",
            "decision.recorded",
        ]

        def forbidden(*args, **kwargs):
            pytest.fail("frozen poll restart fetched current source, token or clock")

        def inject(current):
            original = current.collector.ingest_json

            def send(body):
                nonlocal remaining
                d = json.loads(body)
                sent.append(body)
                if d["event_type"] == stages[stage] and remaining:
                    remaining -= 1
                    if after_commit:
                        original(body)
                    raise OSError("fictional-local-delivery-loss")
                return original(body)

            current.collector.ingest_json = send

        try:
            dispatch(runtime, "property-tick")
            for turn in range(losses + 1):
                inject(runtime)
                runtime.run_pending()
                result = runtime.poll_report("property-tick")
                assert (
                    provider.calls == 1
                    and count(runtime, "crm_decisions") <= 1
                    and count(runtime, "events") <= 4
                )
                if result.disposition == "complete":
                    assert turn == losses and remaining == 0
                    assert (
                        result.members[0].decision_event_id
                        == frozen.document["stages"][-1]["event_id"]
                    )
                    break
                owner = runtime.store.owner(cfg.targets[0])
                assert owner is not None
                current = runtime.store.load_submission(owner)
                assert current is not None
                if frozen is None:
                    frozen = current
                else:
                    assert current == frozen
                runtime.close()
                runtime = feature("runtime", "CRMRuntime")(
                    cfg, client_factory=forbidden, clock=forbidden
                )
                runtime.start(background=False)
            assert (
                result.disposition == "complete"
                and count(runtime, "crm_decisions") == 1
                and count(runtime, "events") == 4
            )
            expected = {
                s["event_id"]: canonical_bytes(s["envelope"]) for s in frozen.document["stages"]
            }
            assert all(body == expected[json.loads(body)["event_id"]] for body in sent)
        finally:
            runtime.close()
