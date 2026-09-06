"""AC-01: preserved NovaSignal AI context + logic `v3.2` -> score 86, `PRIORITIZE`.

The reconstruction runs from the projection tables alone, after verifying the
artifact hash and the evaluator identity (INV-05). It resolves evidence only
through the ids the decision preserved, never through the account and never
through "the latest" version (INV-02). It writes nothing and reads no `events`
or `accounts` row.
"""

import re
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select

from flight_recorder.ledger.schema import decision_consumed_inputs, evidence_versions
from flight_recorder.logic.evaluator import EVALUATOR_VERSION, InputState, evaluate
from flight_recorder.logic.rules import RuleTypeError
from flight_recorder.replay.reconstruct import load_context, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    canonical_boundary,
    captured_statements,
    logic_artifact_model,
    replace_context,
    seed_all,
)

ARTIFACT_HASH = "db3a8bdebf2befe286ab49a2381dfe6fb931ac6f848923d35e0e732adcc82db0"
FUNDING_EVIDENCE_ID = "ev-novasignal-funding-event-v1"

#: Any statement that would change stored state.
WRITE_STATEMENT = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\b", re.IGNORECASE)
#: The two tables reconstruction must never read: the raw event log and the
#: account table whose current state the past must not be re-derived from.
FORBIDDEN_TABLE = re.compile(r"\b(events|accounts)\b", re.IGNORECASE)


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def reconstruction(harness):
    with harness.engine.connect() as conn:
        return reconstruct(conn, DECISION_EVENT_ID)


def test_the_canonical_decision_reconstructs_exactly(seeded):
    result = reconstruction(seeded).result

    assert result.score == 86
    assert result.threshold == 75
    assert result.output == "PRIORITIZE"


def test_the_reconstruction_reports_the_verified_logic_identity(seeded):
    reconstructed = reconstruction(seeded)

    assert reconstructed.decision_event_id == DECISION_EVENT_ID
    assert reconstructed.artifact_hash == ARTIFACT_HASH
    assert reconstructed.stored_artifact_hash == ARTIFACT_HASH
    assert reconstructed.recomputed_artifact_hash == ARTIFACT_HASH
    assert reconstructed.logic_version == "v3.2"
    assert reconstructed.evaluator_version == EVALUATOR_VERSION == "evaluator-v1"
    assert reconstructed.runtime_evaluator_version == EVALUATOR_VERSION
    assert reconstructed.decision_boundary == canonical_boundary()


def test_every_factor_matches_the_stored_consumed_inputs(seeded):
    with seeded.engine.connect() as conn:
        stored = {
            row.input_key: (row.evidence_version_id, row.contribution)
            for row in conn.execute(
                select(decision_consumed_inputs).where(
                    decision_consumed_inputs.c.decision_event_id == DECISION_EVENT_ID
                )
            )
        }

    result = reconstruction(seeded).result
    assert len(stored) == 5
    assert {
        f.key: (f.evidence_version_id, f.contribution)
        for f in result.factors
        if f.input_state is InputState.CONSUMED
    } == stored


def test_the_funding_rule_reads_the_preserved_evidence_versions_observation_date(seeded):
    """The temporal factor's date comes from `ev-novasignal-funding-event-v1`,
    not from the decision payload, whose preserved value is `"Series B"`."""
    with seeded.engine.connect() as conn:
        observed_at = conn.execute(
            select(evidence_versions.c.observed_at).where(
                evidence_versions.c.evidence_version_id == FUNDING_EVIDENCE_ID
            )
        ).scalar_one()
        context = load_context(conn, DECISION_EVENT_ID)

    assert observed_at == "2026-03-30"
    funding = next(entry for entry in context if entry.key == "funding_event")
    assert funding.observed_at == date(2026, 3, 30)
    assert funding.evidence_version_id == FUNDING_EVIDENCE_ID
    assert funding.value == "Series B"

    # Without that date the rule cannot be decided at all: the preserved value
    # alone can never satisfy it.
    dateless = replace_context(context, "funding_event", replace(funding, observed_at=None))
    with pytest.raises(RuleTypeError):
        evaluate(logic_artifact_model(), dateless, canonical_boundary())

    result = reconstruction(seeded).result
    funding_factor = next(f for f in result.factors if f.key == "funding_event")
    assert funding_factor.matched and funding_factor.contribution == 18


def test_the_context_states_distinguish_consumed_ignored_and_unavailable(seeded):
    states = reconstruction(seeded).result.context_states

    assert states["website_intent"] is InputState.UNAVAILABLE
    assert states["verified_integration_pressure"] is InputState.IGNORED
    assert states["head_of_platform_start_date"] is InputState.IGNORED


def test_two_reconstructions_of_the_same_decision_are_equal(seeded):
    """AC-13: same seed, same context, same logic version, same result."""
    assert reconstruction(seeded) == reconstruction(seeded)


def test_reconstruction_writes_nothing_and_reads_no_events_or_accounts_row(seeded):
    before = seeded.snapshot()

    with captured_statements(seeded.engine) as statements:
        reconstruction(seeded)

    # The listener sees real SQL text, so a negative match below means something.
    assert [s for s in statements if "decision_context" in s]
    assert not [s for s in statements if WRITE_STATEMENT.search(s)]
    assert not [s for s in statements if FORBIDDEN_TABLE.search(s)]
    assert seeded.snapshot() == before
