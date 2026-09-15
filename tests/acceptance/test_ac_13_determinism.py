"""AC-13, INV-11, D-014 Q4: the seeded dataset as a bounded, repeatable operation schedule.

Evidence, each executed through `POST /api/v1/decision-events` on real SQLite
ledgers:

1. Two fresh seeds of the shipped config give the same ordered digest, the pinned
   `FRESH_SEED_DIGEST`, the same aggregates, and the nine pinned canonical hashes.
2. Every generated decision reconstructs exactly; the demo ledger holds every
   `demo_states` minimum.
3. A completed-seed retry changes nothing; recovery from an interruption at five
   stop points yields the uninterrupted ledger; an independent `attribute` run is
   preserved and the seed reports not fresh; five pre-existing divergences are
   refused by the read-only entry check with full ledger equality.
4. The seed never attributes the stage-2 outcomes, and the CLI follows the
   coordinator's recipe.
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

import flight_recorder.dataset.schedule as schedule_module
from flight_recorder.analytics.insights import STANDING_EVALUATED, decision_facts, insights
from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    STATUS_DIRECT,
    VALID_SOURCE_ACTION,
    VALID_SOURCE_DECISION,
    attribution_event_id,
    effective_attribution,
    effective_outcome_versions,
    ledger_maximum,
)
from flight_recorder.cli import main
from flight_recorder.collector.schema import format_utc
from flight_recorder.dataset.generator import generate
from flight_recorder.dataset.schedule import ScheduleDiverged
from flight_recorder.fixtures import (
    canonical_artifacts,
    canonical_envelope_paths,
    dataset_comparison_workflow_version,
    dataset_config,
    dataset_signals,
    load_json,
    planted_effects,
)
from flight_recorder.ledger.schema import actions, decisions, events, outcomes
from flight_recorder.replay.reconstruct import reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    FixedClock,
    Harness,
    attribute_ledger,
    attribution_envelope,
    attribution_rows,
    discovery_envelope,
    logic_artifact,
    max_sequence,
    post_created,
    seed_dataset,
    small_dataset_config,
)

#: The ordered logical digest of a fresh seed of the shipped config. Changing the
#: generator or `fixtures/dataset/config.json` changes it; update it deliberately.
FRESH_SEED_DIGEST = "530f992c3f08f2c01f4e1a5bcc84b05fcc47660c14e51543efa40b2fe41c35ea"

#: `events.canonical_hash` of the nine canonical envelopes, duplicated from
#: `BASELINE_CANONICAL_HASHES` in `tests/unit/test_outcome_schema_versions.py` (the
#: pinned literals; the task file names `test_fixture_integrity.py`, which holds none).
BASELINE_CANONICAL_HASHES = {
    "00a-logic-artifact-v3.2.json": (
        "bfb2ad92007b511fe196c0ee400ba5793ce9b6946e458ecc9471d78930c7aa29"
    ),
    "00b-logic-artifact-v5.1.json": (
        "569142d25e6ac9768b653147ae5faaf48c3d182d5c23e099f9eb22338e80d5a6"
    ),
    "01-account-discovered.json": (
        "60df3c9ccb161d7f63e6beb420873136b930a0399084e4528a405921918501e8"
    ),
    "02-evidence-recorded-enrichment.json": (
        "a2daa528754cd34ab88caa4aac50e0ab678cf9ac7173b8ef4fe9507e82053417"
    ),
    "03-evidence-recorded-integration-pressure.json": (
        "0eecf8535bcf00359dda091f7994158dbd7628b9d60413f672377ae394763d4b"
    ),
    "04-decision-recorded.json": (
        "c6036673cf0888e09ab25a0001b8019767b9cf8b9b31b8cef08def9c51859f34"
    ),
    "05-persona-selected.json": (
        "bc0cc67280cffc7931c4378f38c6c302067de695a587456da02bbde998669779"
    ),
    "06-action-recorded.json": ("87b79ca4f5ae368992431b2a6a24888ff2afd04977d5bb26db384ad8be0fbe0a"),
    "07-outcome-evaluated.json": (
        "0d72dfdc0a37854930a9229d50fef19aba6c6e5d1e00c83e943505d861396429"
    ),
}

SMALL = small_dataset_config()
SMALL_SCHEDULE = generate(SMALL, artifacts=canonical_artifacts())


def stop_points(schedule) -> dict[str, int]:
    """Item counts after which a seed is interrupted, derived from the schedule."""
    cutoff_1, operations = schedule.cutoff_1, len(schedule.operations)
    return {
        "mid-stage-1": len(schedule.canonical) + len(schedule.stage_1) // 2,
        "at-cutoff-1": cutoff_1,
        # After the canonical operation, before the last generated one.
        "mid-operations": cutoff_1 + 1 + (operations - 1) // 2,
        "after-operations": cutoff_1 + operations,
        "mid-stage-2": cutoff_1 + operations + 1,
    }


STOPS = stop_points(SMALL_SCHEDULE)


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("ac13-fresh"))
    schedule, report = seed_dataset(harness)
    return harness, schedule, report


@pytest.fixture(scope="module")
def small_fresh(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("ac13-small"))
    schedule, report = seed_dataset(harness, config=SMALL)
    assert report.fresh
    return harness, schedule, report


def read_insights(harness: Harness):
    with harness.engine.connect() as conn:
        return insights(
            conn,
            ledger_maximum(conn),
            signals=dataset_signals(),
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )


def ledger_state(harness: Harness) -> tuple:
    """`harness.snapshot()` plus every stored event row (duplicated from
    `test_attribution_ingest.py`), so equality covers content, not counts."""
    with harness.engine.connect() as conn:
        stored = conn.execute(select(events).order_by(events.c.ingest_sequence)).all()
    return harness.snapshot(), [tuple(row) for row in stored]


def effective_results(harness: Harness) -> dict:
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        return {
            outcome: effective_attribution(conn, outcome, POLICY_VERSION, cutoff=cutoff)
            for outcome in effective_outcome_versions(conn, cutoff=cutoff)
        }


def awaiting(harness: Harness) -> set[str]:
    return {outcome for outcome, result in effective_results(harness).items() if result is None}


def stage_2_ids(schedule) -> set[str]:
    return {envelope["event_id"] for envelope in schedule.stage_2}


def attributed_event_count(harness: Harness) -> int:
    with harness.engine.connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(events)
            .where(events.c.event_type == "outcome.attributed")
        ).scalar_one()


# --- 1. Determinism ---------------------------------------------------------------------


def test_two_fresh_seeds_produce_the_same_digest_and_aggregates(fresh, tmp_path):
    harness, schedule, report = fresh
    second = Harness(tmp_path)
    _, again = seed_dataset(second)

    for run in (report, again):
        assert run.fresh
        assert (run.created, run.duplicate) == (schedule.scheduled_total, 0)
        assert run.events_total == run.scheduled_total == schedule.scheduled_total
    assert report.digest == again.digest == FRESH_SEED_DIGEST
    assert read_insights(harness).as_dict() == read_insights(second).as_dict()

    with second.engine.connect() as conn:
        stored = conn.execute(
            select(events.c.event_id, events.c.canonical_hash)
            .where(events.c.ingest_sequence <= len(schedule.canonical))
            .order_by(events.c.ingest_sequence)
        ).all()
    paths = canonical_envelope_paths()
    assert [tuple(row) for row in stored] == [
        (load_json(path)["event_id"], BASELINE_CANONICAL_HASHES[path.name]) for path in paths
    ]


def test_every_generated_decision_reconstructs_exactly(fresh):
    harness, schedule, _ = fresh
    with harness.engine.connect() as conn:
        ids = conn.execute(select(decisions.c.decision_event_id)).scalars().all()
        for decision_event_id in ids:
            reconstruct(conn, decision_event_id)  # raises on any divergence
    generated = [e for e in schedule.stage_1 if e["event_type"] == "decision.recorded"]
    assert len(ids) == len(generated) + 1  # plus the canonical decision
    assert read_insights(harness).reconstruction_failures == ()


def test_the_demo_ledger_holds_every_required_state(fresh):
    harness, schedule, _ = fresh
    demo = planted_effects()["demo_states"]
    result = read_insights(harness)
    coverage = result.observations

    standings = {
        "direct": coverage.direct,
        "inferred": coverage.inferred,
        "unresolved": coverage.unresolved,
        "awaiting attribution": coverage.awaiting_attribution,
    }
    for name, minimum in demo["attribution_standings"].items():
        assert standings[name] >= minimum, name
    states = {
        "open": coverage.open,
        "closed known": coverage.closed_known,
        "closed unknown": coverage.closed_unknown,
    }
    for name, minimum in demo["observation_states"].items():
        assert states[name] >= minimum, name
    assert coverage.other_period >= demo["other_period_observations"]

    effective = effective_results(harness)
    effective_ids = {r.attribution_event_id for r in effective.values() if r is not None}
    with harness.engine.connect() as conn:
        acted = set(conn.execute(select(actions.c.decision_event_id)).scalars())
        logic = dict(
            conn.execute(select(decisions.c.decision_event_id, decisions.c.logic_version)).all()
        )
        supersessions = conn.execute(
            select(outcomes.c.outcome_event_id, outcomes.c.supersedes_outcome_event_id).where(
                outcomes.c.supersedes_outcome_event_id.is_not(None)
            )
        ).all()

    # Observations recorded for a decision with no action, credited to that decision.
    without_action = [
        row
        for row in attribution_rows(harness)
        if row.attribution_event_id in effective_ids
        and row.status == STATUS_DIRECT
        and row.reason == VALID_SOURCE_DECISION
        and row.resolved_action_event_id is None
        and row.resolved_decision_event_id not in acted
    ]
    assert len(without_action) >= demo["outcomes_without_action"]
    later_version = logic_artifact("v5.1")["logic_version"]
    assert any(logic[row.resolved_decision_event_id] == later_version for row in without_action)

    funded = next(row for row in result.signals if row.kind == "rule")
    assert funded.comparison.absent.eligible > 0

    # A correction history: the predecessor keeps its result, the successor awaits.
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        histories = [
            successor
            for successor, predecessor in supersessions
            if effective_attribution(conn, predecessor, POLICY_VERSION, cutoff=cutoff) is not None
            and effective_attribution(conn, successor, POLICY_VERSION, cutoff=cutoff) is None
        ]
    assert len(histories) >= demo["correction_histories"]

    assert awaiting(harness) == stage_2_ids(schedule)
    assert OUTCOME_EVENT_ID not in stage_2_ids(schedule)

    (canonical,) = [
        row for row in attribution_rows(harness) if row.outcome_event_id == OUTCOME_EVENT_ID
    ]
    assert (canonical.status, canonical.reason) == (STATUS_DIRECT, VALID_SOURCE_ACTION)
    assert canonical.ingest_cutoff == schedule.cutoff_1
    assert canonical.attributed_at == format_utc(schedule.attribution_instant)

    with harness.engine.connect() as conn:
        facts = decision_facts(conn, DECISION_EVENT_ID, cutoff, signals=dataset_signals())
    assert facts.standing == STANDING_EVALUATED
    assert facts.eligible


# --- 2. Retries, interruptions, independent runs, divergence ----------------------------


def test_a_completed_seed_retry_is_a_no_op(tmp_path):
    harness = Harness(tmp_path)
    schedule, first = seed_dataset(harness)
    before = ledger_state(harness)
    aggregates = read_insights(harness).as_dict()
    attributed = attributed_event_count(harness)

    _, again = seed_dataset(harness)

    assert (again.created, again.duplicate) == (0, schedule.scheduled_total)
    assert again.fresh
    assert again.digest == first.digest
    assert ledger_state(harness) == before
    assert read_insights(harness).as_dict() == aggregates
    assert awaiting(harness) == stage_2_ids(schedule)
    assert attributed_event_count(harness) == attributed


@pytest.mark.parametrize("stop", list(STOPS))
def test_interrupted_stage_recovery(tmp_path, small_fresh, stop):
    reference, schedule, uninterrupted = small_fresh
    k = STOPS[stop]
    harness = Harness(tmp_path)

    _, partial = seed_dataset(harness, config=SMALL, stop_after=k)
    assert (partial.created, harness.event_count()) == (k, k)
    assert not partial.fresh

    _, completed = seed_dataset(harness, config=SMALL)
    assert (completed.created, completed.duplicate) == (schedule.scheduled_total - k, k)
    assert completed.fresh
    assert ledger_state(harness) == ledger_state(reference)
    rows = attribution_rows(harness)
    assert len(rows) == len(schedule.operations)
    for row in rows:
        assert row.ingest_cutoff == schedule.cutoff_1
        assert row.attributed_at == format_utc(schedule.attribution_instant)
    assert awaiting(harness) == stage_2_ids(schedule)
    assert completed.digest == uninterrupted.digest


def test_an_independent_attribute_run_after_a_completed_seed_is_preserved(tmp_path):
    harness = Harness(tmp_path)
    schedule, _ = seed_dataset(harness, config=SMALL)
    maximum = max_sequence(harness)

    run = attribute_ledger(harness, clock=FixedClock())
    assert run.cutoff == maximum
    assert sorted(s.outcome_event_id for s in run.created) == sorted(stage_2_ids(schedule))
    assert [s.http_status for s in run.submissions] == [201, 201]
    before = ledger_state(harness)

    _, again = seed_dataset(harness, config=SMALL)

    assert (again.created, again.duplicate) == (0, schedule.scheduled_total)
    assert again.fresh is False
    assert again.events_total == schedule.scheduled_total + 2
    assert ledger_state(harness) == before
    assert awaiting(harness) == set()


def _leading_unrelated_event(harness: Harness) -> None:
    post_created(harness, discovery_envelope("filler-lead"))


def _same_identity_other_instant(harness: Harness) -> None:
    seed_dataset(harness, config=SMALL, stop_after=SMALL_SCHEDULE.cutoff_1)
    later = FixedClock(SMALL_SCHEDULE.attribution_instant + timedelta(days=1))
    run = attribute_ledger(harness, clock=later)
    assert [s.http_status for s in run.submissions] == [201] * len(SMALL_SCHEDULE.operations)


def _interleaved_event(harness: Harness) -> None:
    seed_dataset(harness, config=SMALL, stop_after=STOPS["mid-operations"])
    post_created(harness, discovery_envelope("filler-mid"))


def _operation_at_the_wrong_sequence(harness: Harness) -> None:
    seed_dataset(harness, config=SMALL, stop_after=SMALL_SCHEDULE.cutoff_1)
    second = SMALL_SCHEDULE.operations[1].outcome_event_id
    envelope = attribution_envelope(
        harness,
        second,
        cutoff=SMALL_SCHEDULE.cutoff_1,
        clock=FixedClock(SMALL_SCHEDULE.attribution_instant),
    )
    post_created(harness, envelope)


def _conflicting_root_at_another_cutoff(harness: Harness) -> None:
    seed_dataset(harness, config=SMALL, stop_after=SMALL_SCHEDULE.cutoff_1)
    post_created(harness, discovery_envelope("filler-root"))
    first = SMALL_SCHEDULE.operations[0].outcome_event_id
    post_created(harness, attribution_envelope(harness, first, cutoff=max_sequence(harness)))


CUTOFF_1 = SMALL_SCHEDULE.cutoff_1
OPERATIONS = SMALL_SCHEDULE.operations
#: case -> (ledger builder, diverging sequence, operation named or None)
DIVERGENCES = {
    "a-leading-unrelated-event": (_leading_unrelated_event, 1, None),
    "b-same-identity-other-instant": (_same_identity_other_instant, CUTOFF_1 + 1, OPERATIONS[0]),
    "c-interleaved-event": (
        _interleaved_event,
        STOPS["mid-operations"] + 1,
        OPERATIONS[STOPS["mid-operations"] - CUTOFF_1],
    ),
    "d-operation-at-the-wrong-sequence": (
        _operation_at_the_wrong_sequence,
        CUTOFF_1 + 1,
        OPERATIONS[0],
    ),
    "e-conflicting-root-at-another-cutoff": (
        _conflicting_root_at_another_cutoff,
        CUTOFF_1 + 1,
        OPERATIONS[0],
    ),
}


@pytest.mark.parametrize("case", list(DIVERGENCES))
def test_pre_existing_divergence_is_rejected_before_any_write(tmp_path, case):
    build, sequence, operation = DIVERGENCES[case]
    harness = Harness(tmp_path)
    build(harness)
    before = ledger_state(harness)

    with pytest.raises(ScheduleDiverged) as diverged:
        seed_dataset(harness, config=SMALL)

    assert ledger_state(harness) == before
    assert diverged.value.sequence == sequence
    assert diverged.value.operation == operation
    found = diverged.value.found
    if case.startswith("b-"):
        # The same operation identity, stored with other content.
        assert found[0] == diverged.value.expected[0]
        assert found[1] != diverged.value.expected[1]
    if case.startswith("d-"):
        assert found[0] == SMALL_SCHEDULE.items[CUTOFF_1 + 1].event_id  # the second operation
    if case.startswith("e-"):
        # The conflicting root exists as well: a result for the first scheduled outcome
        # under an identity other than the scheduled operation's.
        scheduled = attribution_event_id(operation.outcome_event_id, POLICY_VERSION, CUTOFF_1)
        conflicting = [
            r for r in attribution_rows(harness) if r.outcome_event_id == operation.outcome_event_id
        ]
        assert [(r.ingest_cutoff, r.attribution_event_id != scheduled) for r in conflicting] == [
            (CUTOFF_1 + 1, True)
        ]


# --- 3. The seed never runs general attribution -------------------------------------------


def test_stage_2_outcomes_are_never_attributed_by_the_seed(tmp_path):
    harness = Harness(tmp_path)
    for _ in range(3):
        schedule, report = seed_dataset(harness, config=SMALL)
    assert report.fresh

    effective = effective_results(harness)
    assert {outcome for outcome, result in effective.items() if result is None} == stage_2_ids(
        schedule
    )
    assert effective[OUTCOME_EVENT_ID] is not None

    tree = ast.parse(Path(schedule_module.__file__).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert names.isdisjoint({"run_attribution", "_run", "attribute_ledger"})


def test_the_seed_dataset_command_follows_the_recipe(tmp_path, capsys):
    """§5's four commands on a disposable database: fresh, then every item
    duplicate and still fresh, then `attribute` creates exactly the two stage-2
    results, then every item duplicate and not fresh."""
    db = str(tmp_path / "dataset.db")
    schedule = generate(dataset_config(), artifacts=canonical_artifacts())
    total = schedule.scheduled_total

    assert main(["--db", db, "reset"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "seed-dataset"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"seed-dataset: {total} created, 0 duplicate ({total} items); "
        f"events {total} of {total} scheduled; digest {FRESH_SEED_DIGEST}"
    )
    assert main(["--db", db, "seed-dataset"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"seed-dataset: 0 created, {total} duplicate ({total} items); "
        f"events {total} of {total} scheduled; digest {FRESH_SEED_DIGEST}"
    )
    assert main(["--db", db, "attribute"]) == 0
    attributed = capsys.readouterr().out.splitlines()
    created = [line.split()[2].rstrip(":") for line in attributed if line.startswith("201 created")]
    assert sorted(created) == sorted(stage_2_ids(schedule))
    assert "; 2 created, 0 unchanged," in attributed[-1]
    assert main(["--db", db, "seed-dataset"]) == 0
    summary, not_fresh = capsys.readouterr().out.splitlines()[-2:]
    assert summary.startswith(
        f"seed-dataset: 0 created, {total} duplicate ({total} items); "
        f"events {total + 2} of {total} scheduled; digest "
    )
    assert FRESH_SEED_DIGEST not in summary
    assert not_fresh.startswith("not a fresh seed")
