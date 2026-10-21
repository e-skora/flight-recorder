"""INV-01/02/03/04/05/09: generated observation sequences retain frozen history."""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_recorder.adapters.crm.contracts import CRMRefused
from tests.adapters.crm.test_capture import freeze, read
from tests.adapters.crm.test_capture_store import store2, target

pytestmark = pytest.mark.invariant


@given(st.lists(st.integers(min_value=180, max_value=183), min_size=1, max_size=10))
@settings(deadline=None)
def test_promoted_digest_and_sequence_always_describe_latest_winning_capture(values):
    with tempfile.TemporaryDirectory(prefix="fr-capture-state-", dir="/private/tmp") as name:
        store = store2(Path(name) / "ops.sqlite")
        previous = None
        captures = []
        prior_owner = None
        for index, value in enumerate(values):
            owner = store.acquire(target(), f"{index + 1:064x}")
            assert owner.generation == index + 1
            if prior_owner is not None:
                with pytest.raises(CRMRefused, match="stale_ownership"):
                    store.next_capture_sequence(prior_owner)
            result = freeze(store, owner, read(seconds=index * 10, count=value))
            if value != previous:
                assert result.state == "captured"
                captures.append(result.capture)
            else:
                assert result.state == "scoring_inputs_unchanged" and result.capture is None
            state = store.polling_state(target())
            assert state["capture_sequence"] == len(captures)
            assert state["last_unit_id"] == captures[-1].unit_id
            for cap in captures:
                assert store.load_capture(cap.unit_id).canonical_bytes == cap.canonical_bytes
            store.release(owner)
            prior_owner = owner
            previous = value
        assert store.observation_count(target()) == len(values)
        store.close()
