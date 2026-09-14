"""INV-03, AC-11 and D-014's aggregate invariants, at every cutoff of a seeded ledger.

Evidence: a Hypothesis property executed over every drawable cutoff of a fresh
small-config seed, and the canonical decision's available-but-ignored pressure
input read back through the engine.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_recorder.analytics.insights import decision_facts, insights
from flight_recorder.attribution.policy import ledger_maximum
from flight_recorder.fixtures import (
    canonical_envelope_paths,
    dataset_comparison_workflow_version,
    dataset_signals,
)
from flight_recorder.logic.evaluator import InputState
from tests.conftest import DECISION_EVENT_ID, Harness, seed_all, seed_dataset, small_dataset_config

pytestmark = pytest.mark.invariant

SIGNALS = dataset_signals()


@pytest.fixture(scope="module")
def small_seed(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("inv03-small"))
    _, report = seed_dataset(harness, config=small_dataset_config())
    assert report.fresh
    return harness


def rates(result):
    yield result.overall
    for row in result.signals:
        yield row.comparison.present
        yield row.comparison.absent
    for row in result.workflows:
        yield row.rate
    yield result.workflow_comparison.comparison.present
    yield result.workflow_comparison.comparison.absent


@settings(deadline=None)
@given(data=st.data())
def test_aggregate_states_hold_at_every_cutoff(small_seed, data):
    with small_seed.engine.connect() as conn:
        maximum = ledger_maximum(conn)
        cutoff = data.draw(
            st.integers(min_value=len(canonical_envelope_paths()), max_value=maximum),
            label="cutoff",
        )
        result = insights(
            conn,
            cutoff,
            signals=SIGNALS,
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )

    assert result.standings.total == result.population
    for rate in rates(result):
        assert 0 <= rate.positives <= rate.eligible <= rate.cohort_total
    for row in result.signals:
        states = (
            row.known_true,
            row.known_false,
            row.unavailable,
            row.absent,
            row.not_applicable,
            row.reconstruction_failed,
        )
        assert sum(states) == result.population, row.id
        assert row.input_consumed <= row.input_available, row.id
        assert row.known_true + row.known_false == (
            row.comparison.present.cohort_total + row.comparison.absent.cohort_total
        )
    assert result.observations.qualifying_90_day <= result.observations.closed_known


def test_available_but_ignored_is_never_consumed(harness):
    """AC-11, INV-03: the canonical decision under `v3.2` holds verified integration
    pressure available but ignored; the engine counts it available, never consumed."""
    for response in seed_all(harness):
        assert response.status_code == 201
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        result = insights(
            conn,
            cutoff,
            signals=SIGNALS,
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )
        facts = decision_facts(conn, DECISION_EVENT_ID, cutoff, signals=SIGNALS)

    pressure = next(row for row in result.signals if row.kind == "context_value")
    assert (pressure.input_available, pressure.input_consumed) == (1, 0)
    assert facts.context_states[pressure.input_key] == InputState.IGNORED
    assert pressure.input_key in facts.available_inputs
    assert pressure.input_key not in facts.consumed_inputs
    (match,) = pressure.historical_rule_matched
    assert (match.rule, match.matched) == (None, 0)
