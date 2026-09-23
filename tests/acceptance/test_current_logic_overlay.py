"""D-017: the `v5.2` overlay registers, stays outside the canonical fixture, and appends.

`fixtures/current/` holds a successor artifact and its registration envelope.
Registering it is an append and nothing else: no existing artifact, event or
stored row is edited, and the canonical nine envelopes and the generator's
two-artifact pair are unaffected (INV-01, INV-05, INV-11; AC-07, AC-17).

The supported setup order is a requirement, not a preference. The dataset's
seed schedule requires the ledger to be a prefix of it, so the overlay is
registered *after* a complete `seed-dataset` and never before one. Both
directions are proven here: the supported order succeeds and stays additive,
and the unsupported order refuses with `ScheduleDiverged` leaving full ledger
and projection state unchanged.

Tests 1 to 5 and 13 of the task's numbering.
"""

import json

import pytest
from sqlalchemy import select

from flight_recorder.cli import main
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.dataset.schedule import ScheduleDiverged
from flight_recorder.fixtures import (
    canonical_artifacts,
    canonical_envelope_paths,
    current_logic_artifact,
    current_logic_artifact_path,
    current_logic_registration_path,
    load_json,
)
from flight_recorder.ledger.schema import events, logic_artifacts
from tests.conftest import (
    Harness,
    register_current_logic,
    seed_all,
    seed_dataset,
    v5_2_hash,
)

#: Computed independently by the coordinator and by the reviewer before any
#: code existed. It is the one value this module never derives from the file.
PINNED_V5_2_HASH = "cbefd0508d9c999de299b8ed7a5d60f38b746dfc81520e798bd6c25515e0b889"

REGISTRATION_EVENT_ID = "evt-system-current-logic-artifact-v5.2"


def event_ids(harness: Harness) -> list[str]:
    with harness.engine.connect() as conn:
        return [
            row.event_id for row in conn.execute(select(events).order_by(events.c.ingest_sequence))
        ]


def artifact_hashes(harness: Harness) -> list[str]:
    with harness.engine.connect() as conn:
        return [
            row.artifact_hash
            for row in conn.execute(
                select(logic_artifacts).order_by(logic_artifacts.c.artifact_hash)
            )
        ]


def ledger_state(harness: Harness) -> tuple:
    """`harness.snapshot()` plus every stored event row, in ingest order.

    `snapshot()` compares the event *count*, the accounts and the projections.
    It cannot see an altered stored payload or canonical hash while the count,
    the ids and the projections stay as they were. Following `ledger_state` in
    `tests/acceptance/test_ac_13_determinism.py`, equality here covers every
    column of every event as well, so these tests compare content and not
    merely shape.
    """
    with harness.engine.connect() as conn:
        stored = conn.execute(select(events).order_by(events.c.ingest_sequence)).all()
    return harness.snapshot(), [tuple(row) for row in stored]


def run_cli(harness: Harness, command: str) -> int:
    return main(["--db", str(harness.db_path), command])


# --- 1. The artifact file ------------------------------------------------------


def test_the_v5_2_fixture_validates_strictly_at_the_pinned_content_hash():
    """Test 1. The artifact's identity is its content hash, not its label (INV-05)."""
    artifact = LogicArtifact.model_validate_json(
        current_logic_artifact_path().read_bytes(), strict=True
    )

    assert artifact.logic_version == "v5.2"
    assert artifact.artifact_id == "logic-account-prioritization-v5.2"
    assert artifact.decision_class == "account_prioritization"
    assert artifact.evaluator_version == "evaluator-v1"
    assert artifact.threshold == 75
    assert canonical_hash(artifact.model_dump(mode="json")) == PINNED_V5_2_HASH
    assert v5_2_hash() == PINNED_V5_2_HASH


# --- 2. The registration envelope ----------------------------------------------


def test_the_envelope_payload_is_the_artifact_file_byte_for_byte():
    """Test 2. Mirrors `test_registered_artifacts_are_the_logic_artifact_files_byte_for_byte`."""
    envelope = load_json(current_logic_registration_path())

    assert envelope["event_id"] == REGISTRATION_EVENT_ID
    assert envelope["event_type"] == "logic_artifact.registered"
    assert envelope["source"] == "relaybridge-logic-registry"
    assert envelope["account_ref"] == "_system"
    assert envelope["occurred_at"] == "2026-09-21T00:00:01Z"
    assert envelope["recorded_at"] == "2026-09-21T00:00:01Z"
    assert envelope["payload"]["artifact"] == json.loads(current_logic_artifact_path().read_text())


# --- 3. The canonical loaders do not see it -------------------------------------


def test_the_canonical_loaders_are_unaware_of_the_overlay_directory():
    """Test 3. Both canonical globs stay closed, so the generator's pair is intact."""
    assert len(canonical_envelope_paths()) == 9
    assert all(path.parent.name == "canonical" for path in canonical_envelope_paths())
    assert set(canonical_artifacts()) == {"v3.2", "v5.1"}

    assert current_logic_artifact_path().exists()
    assert current_logic_artifact_path().parent.name == "current"
    assert current_logic_artifact().logic_version == "v5.2"
    assert "v5.2" not in canonical_artifacts()


# --- 4. The command -------------------------------------------------------------


def test_register_current_logic_creates_once_and_is_then_a_no_op(harness, capsys):
    """Test 4. Idempotent by canonical-JSON event identity (INV-11)."""
    assert run_cli(harness, "register-current-logic") == 0
    assert "1 created, 0 duplicate" in capsys.readouterr().out

    after_first = ledger_state(harness)
    assert event_ids(harness) == [REGISTRATION_EVENT_ID]
    assert artifact_hashes(harness) == [PINNED_V5_2_HASH]

    assert run_cli(harness, "register-current-logic") == 0
    assert "0 created, 1 duplicate" in capsys.readouterr().out

    assert ledger_state(harness) == after_first, "the second run changed an event or a row"


def test_the_command_registers_on_an_empty_database_and_creates_the_system_principal(harness):
    """Test 4, continued. The command invents no prerequisite of its own.

    Creating the `_system` principal here is legitimate: it is the first event
    in this ledger. Test 13 covers the populated case, where nothing new may
    appear beside the two permitted rows.
    """
    assert harness.is_empty()

    assert run_cli(harness, "register-current-logic") == 0

    assert harness.event_count() == 1
    with harness.engine.connect() as conn:
        row = conn.execute(
            select(logic_artifacts).where(logic_artifacts.c.artifact_hash == PINNED_V5_2_HASH)
        ).one()
    assert row.logic_version == "v5.2"
    assert row.artifact_id == "logic-account-prioritization-v5.2"
    # `_system` is infrastructure metadata, never a listed account.
    assert [r[0] for r in harness.account_rows()] == ["_system"]


# --- 5a. The supported order ----------------------------------------------------


def test_the_overlay_registers_after_a_complete_dataset_and_the_retry_is_all_duplicate(harness):
    """Test 5a. Registration after the dataset succeeds and extends it additively.

    The extended ledger is deliberately *not* asserted against the fresh-seed
    digest or count: it holds an event outside the schedule, so it is correctly
    not a fresh seed. The actual totals are asserted relationally below.
    """
    schedule, first = seed_dataset(harness)
    assert first.fresh
    assert first.created == len(schedule.items)
    scheduled_total = first.scheduled_total

    register_current_logic(harness)

    assert harness.event_count() == scheduled_total + 1
    assert PINNED_V5_2_HASH in artifact_hashes(harness)

    _, retry = seed_dataset(harness)
    assert retry.created == 0
    assert retry.duplicate == len(schedule.items)
    assert retry.events_total == scheduled_total + 1
    assert retry.scheduled_total == scheduled_total
    assert not retry.fresh, "a ledger holding the overlay is not a fresh seed"


# --- 5b. The unsupported order --------------------------------------------------


@pytest.mark.parametrize("prior_seed", ["empty", "canonical"])
def test_registering_the_overlay_before_the_dataset_makes_the_seed_refuse(harness, prior_seed):
    """Test 5b. The schedule prefix check refuses, and nothing is written.

    `dataset/schedule.py` requires every stored event inside the scheduled
    prefix to occupy its exact scheduled sequence. An overlay event inside that
    prefix displaces one, at sequence 1 on an empty database and at sequence 10
    after `seed`. Full ledger and projection state is unchanged across the
    refusal: the seed is refused whole, never partially applied.
    """
    if prior_seed == "canonical":
        for response in seed_all(harness):
            assert response.status_code == 201, response.json()

    register_current_logic(harness)
    before = ledger_state(harness)

    with pytest.raises(ScheduleDiverged):
        seed_dataset(harness)

    assert ledger_state(harness) == before, "the refused seed changed ledger or projection state"


# --- 13. Registration into a populated ledger is an append ----------------------


def test_registering_into_a_populated_ledger_changes_nothing_that_was_there(harness):
    """Test 13. Exactly one new `events` row and one new `logic_artifacts` row.

    The starting ledger is the canonical trace plus the full generated dataset,
    so it holds historical decision, action, outcome and attribution rows. Every
    one of them, and every account, must come through untouched (INV-01).
    """
    seed_dataset(harness)

    (before_count, before_accounts, before_projections), before_rows = ledger_state(harness)
    before_events = event_ids(harness)
    assert before_count > 0 and before_accounts and before_projections["decisions"]
    assert before_projections["outcome_attributions"], "the starting ledger has attribution rows"

    register_current_logic(harness)

    (after_count, after_accounts, after_projections), after_rows = ledger_state(harness)
    assert after_count == before_count + 1
    assert event_ids(harness) == [*before_events, REGISTRATION_EVENT_ID]
    assert after_accounts == before_accounts, "`_system` already existed; no account changed"

    # Every column of every preexisting event, not just its id: an altered
    # payload or canonical hash under an unchanged id fails here.
    assert len(after_rows) == len(before_rows) + 1, "exactly one new events row"
    assert after_rows[: len(before_rows)] == before_rows, "a stored event row changed"

    for table, rows in before_projections.items():
        after = after_projections[table]
        if table == "logic_artifacts":
            new_rows = [row for row in after if row not in rows]
            assert len(new_rows) == 1, "exactly one new logic_artifacts row"
            assert PINNED_V5_2_HASH in new_rows[0]
            assert [row for row in rows if row not in after] == [], "a stored artifact changed"
        else:
            assert after == rows, f"{table} changed"

    # An exact retry leaves the whole of it identical again, rows included.
    state = ledger_state(harness)
    envelope = load_json(current_logic_registration_path())
    assert harness.post(envelope).status_code == 200
    assert ledger_state(harness) == state
