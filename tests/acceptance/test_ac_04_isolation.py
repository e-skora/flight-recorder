"""AC-04 / INV-01: the present cannot reach back into a recorded decision.

After the canonical seed, later evidence versions arrive, a second account is
discovered, and a new logic artifact is registered. None of it may touch the
NovaSignal AI decision: the reconstruction stays identical field by field,
including every `evidence_version_id`, and the decision's own projection rows
stay byte-identical.
"""

import copy

import pytest
from sqlalchemy import select

from flight_recorder.ledger.schema import (
    decision_consumed_inputs,
    decision_context,
    decisions,
)
from flight_recorder.logic.evaluator import InputState
from flight_recorder.replay.reconstruct import reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    evidence_envelope,
    logic_artifact,
    seed_all,
)

#: The `-v1` evidence versions the decision preserved. Nothing appended later
#: may replace any of them in the reconstruction.
PRESERVED_EVIDENCE = {
    "employee_count": "ev-novasignal-employee-count-v1",
    "industry": "ev-novasignal-industry-v1",
    "funding_event": "ev-novasignal-funding-event-v1",
    "open_platform_engineering_roles": "ev-novasignal-open-platform-engineering-roles-v1",
    "headquarters_country": "ev-novasignal-headquarters-country-v1",
}

LATER_EVIDENCE = [
    evidence_envelope(
        "evt-novasignal-later-employee-count",
        [
            {
                "evidence_version_id": "ev-novasignal-employee-count-v9",
                "evidence_type": "employee_count",
                "value": 4000,
                "supersedes_evidence_version_id": "ev-novasignal-employee-count-v1",
            }
        ],
        occurred_at="2026-05-02T09:00:00Z",
    ),
    evidence_envelope(
        "evt-novasignal-later-industry",
        [
            {
                "evidence_version_id": "ev-novasignal-industry-v9",
                "evidence_type": "industry",
                "value": "Industrial Automation",
                "supersedes_evidence_version_id": "ev-novasignal-industry-v1",
            }
        ],
        occurred_at="2026-05-03T09:00:00Z",
    ),
]

SECOND_ACCOUNT = [
    {
        "schema_version": "1",
        "event_id": "evt-driftlane-01-account-discovered",
        "event_type": "account.discovered",
        "source": "apollo-sim",
        "account_ref": "driftlane-labs",
        "occurred_at": "2026-05-04T09:00:00Z",
        "recorded_at": "2026-05-04T09:00:00Z",
        "payload": {"name": "Driftlane Labs", "domain": "driftlane.example"},
    },
    evidence_envelope(
        "evt-driftlane-02-evidence",
        [
            {
                "evidence_version_id": "ev-driftlane-employee-count-v1",
                "evidence_type": "employee_count",
                "value": 61,
            },
            {
                "evidence_version_id": "ev-driftlane-industry-v1",
                "evidence_type": "industry",
                "value": "B2B AI Software",
            },
        ],
        occurred_at="2026-05-04T09:01:00Z",
        account_ref="driftlane-labs",
    ),
]


def new_artifact_envelope() -> dict:
    """A `v3.3` registration: same evaluator, different weights, new identity."""
    content = copy.deepcopy(logic_artifact("v3.2"))
    content["artifact_id"] = "logic-account-prioritization-v3.3"
    content["logic_version"] = "v3.3"
    for factor in content["factors"]:
        factor["weight"] = factor["weight"] + 5
    content["activation"] = {
        "activated_at": "2026-05-05T09:00:00.000000Z",
        "deactivated_at": None,
        "status": "current",
    }
    return {
        "schema_version": "1",
        "event_id": "evt-system-logic-artifact-v3.3",
        "event_type": "logic_artifact.registered",
        "source": "relaybridge-logic-registry",
        "account_ref": "_system",
        "occurred_at": "2026-05-05T09:00:00Z",
        "recorded_at": "2026-05-05T09:00:00Z",
        "payload": {"artifact": content},
    }


def decision_rows(harness) -> dict[str, list[tuple]]:
    """Every projected row belonging to the canonical decision."""
    with harness.engine.connect() as conn:
        return {
            table.name: [
                tuple(row)
                for row in conn.execute(
                    select(table)
                    .where(table.c.decision_event_id == DECISION_EVENT_ID)
                    .order_by(*table.primary_key.columns)
                )
            ]
            for table in (decisions, decision_context, decision_consumed_inputs)
        }


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def reconstruction(harness):
    with harness.engine.connect() as conn:
        return reconstruct(conn, DECISION_EVENT_ID)


def append_the_present(harness) -> None:
    for envelope in [*LATER_EVIDENCE, *SECOND_ACCOUNT, new_artifact_envelope()]:
        response = harness.post(envelope)
        assert response.status_code == 201, (envelope["event_id"], response.json())


def test_later_evidence_new_accounts_and_new_logic_leave_the_reconstruction_unchanged(seeded):
    before = reconstruction(seeded)
    rows_before = decision_rows(seeded)

    append_the_present(seeded)
    after = reconstruction(seeded)

    for field in (
        "decision_event_id",
        "artifact_hash",
        "logic_version",
        "evaluator_version",
        "decision_boundary",
        "stored_artifact_hash",
        "recomputed_artifact_hash",
        "runtime_evaluator_version",
    ):
        assert getattr(after, field) == getattr(before, field), field
    for field in ("score", "threshold", "output", "ignored_inputs"):
        assert getattr(after.result, field) == getattr(before.result, field), field
    assert after.result.factors == before.result.factors
    assert dict(after.result.context_states) == dict(before.result.context_states)
    assert after == before

    assert decision_rows(seeded) == rows_before


def test_the_reconstruction_still_resolves_the_v1_evidence_versions(seeded):
    append_the_present(seeded)
    result = reconstruction(seeded).result

    consumed = {
        f.key: f.evidence_version_id for f in result.factors if f.input_state is InputState.CONSUMED
    }
    assert consumed == PRESERVED_EVIDENCE
    assert result.score == 86

    # The superseding versions exist and are genuinely different; the
    # reconstruction simply never looks at them.
    with seeded.engine.connect() as conn:
        newer = conn.execute(
            select(decision_context.c.evidence_version_id).where(
                decision_context.c.decision_event_id == DECISION_EVENT_ID
            )
        ).scalars()
        assert "ev-novasignal-employee-count-v9" not in set(newer)
