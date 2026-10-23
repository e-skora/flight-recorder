"""Finite read-order permutations cannot send, mutate history or qualify a connection."""

import itertools
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.web.crm_decision_view import load_crm_decision_page
from tests.acceptance.test_crm_writer_presentation import history, operation_api
from tests.adapters.crm.test_salesforce_writer import feature

pytestmark = pytest.mark.invariant


@given(
    order=st.sampled_from(tuple(itertools.permutations(("decision", "status", "decision_again"))))
)
def test_generated_read_order_never_changes_unknown_or_creates_provider_work(order):
    operation_api()
    with TemporaryDirectory(prefix="fr-write-view-") as directory:
        runtime, provider, decision, _ = history(Path(directory).resolve(), "unknown")
        try:
            before = (
                runtime.config.domain_path.read_bytes(),
                runtime.config.operational_path.read_bytes(),
            )
            calls = (
                provider.calls,
                provider.verifications,
                len(provider.sent),
                provider.output_reads,
            )
            for action in order:
                if action == "status":
                    status = feature("status").connection_status(runtime)
                    assert (
                        status.write_mode == "uncertainty_hold"
                        and status.atomic_source_guard is False
                    )
                else:
                    with runtime.collector.engine.connect() as conn:
                        page = load_crm_decision_page(
                            conn, runtime.config.targets[0].account_ref, decision, None
                        )
                    assert page.operations[0].state == "unknown"
                    assert page.reason_rows
                    assert page.result_label
                    for reason in page.reason_rows:
                        if reason.contribution is not None:
                            factor = next(
                                f for f in page.original.result.factors if f.key == reason.key
                            )
                            assert reason.contribution == factor.contribution
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
