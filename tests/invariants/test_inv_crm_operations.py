"""Generated receipt faults cannot promote an uncertain write to success."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.acceptance.test_crm_operations import (
    intent,
    observation,
    output_read,
    post_ok,
    prepare,
    receipt,
    sent,
    time_at,
)
from tests.conftest import Harness
from tests.invariants.test_inv_crm_collector import snapshot

pytestmark = pytest.mark.invariant


@given(
    status=st.sampled_from([None, 200, 204, 401, 412, 429, 500, 503]),
    missing=st.sets(
        st.sampled_from(
            [
                "fr_score",
                "fr_output",
                "fr_logic_version",
                "fr_decision_ref",
                "fr_op_id",
                "fr_op_rev",
            ]
        )
    ),
)
def test_incomplete_response_cannot_become_terminal_from_any_output_subset(status, missing):
    with TemporaryDirectory(prefix="fr-op-invariant-") as directory:
        harness = Harness(Path(directory))
        try:
            case = prepare(harness)
            i = intent(case)
            sent(harness, case, i)
            post_ok(
                harness,
                observation(
                    case, i, 3, "response", "unknown", response=receipt(case, i, status, False)
                ),
            )
            read = output_read(case, i)
            for key in missing:
                read["values"].pop(key)
            post_ok(
                harness,
                observation(
                    case, i, 4, "output_observed", "unknown", at=time_at(case, 7), output_read=read
                ),
            )
            before = snapshot(harness.engine)
            forged = observation(
                case,
                i,
                5,
                "output_observed",
                "observed_applied_at_t",
                at=time_at(case, 8),
                output_read=output_read(case, i),
            )
            assert harness.post(forged).status_code == 422
            assert snapshot(harness.engine) == before
        finally:
            harness.client.close()
            harness.engine.dispose()
