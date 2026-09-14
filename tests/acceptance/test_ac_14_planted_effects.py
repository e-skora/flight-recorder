"""AC-14, D-008, D-014 Q1 and Q4, D-015: the three planted effects, recovered from events.

Evidence, executed through the collector and `analytics.insights`:

1. On a fresh seed of the shipped config every effect meets the manifest's bound in
   its direction, on unrounded rates with non-empty arms; the funding signal's
   `known false` arm holds a decision with no action whose observation opened at
   its boundary and is credited to it directly; NovaSignal AI takes part wherever
   it is eligible.
2. A controlled outcome change and a controlled cohort change move exactly the
   counts they should, and the engine opens no fixture file.
3. A hand-built ledger matches every field of `Insights` computed by hand.
"""

import copy
from datetime import datetime, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import select

import flight_recorder.fixtures as fixtures_module
from flight_recorder.analytics.insights import (
    ABSENT,
    KNOWN_FALSE,
    KNOWN_TRUE,
    STANDING_EVALUATED,
    STATE_CLOSED_KNOWN,
    UNAVAILABLE,
    Comparison,
    DecisionStandings,
    Insights,
    ObservationCoverage,
    Rate,
    RuleMatch,
    SignalRow,
    WorkflowComparison,
    WorkflowRow,
    decision_facts,
    insights,
)
from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    STATUS_DIRECT,
    STATUS_UNRESOLVED,
    VALID_SOURCE_DECISION,
    effective_attribution,
    ledger_maximum,
)
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import format_utc
from flight_recorder.dataset.schedule import build_schedule, run_schedule
from flight_recorder.fixtures import (
    dataset_comparison_workflow_version,
    dataset_signals,
    planted_effects,
)
from flight_recorder.ledger.schema import actions, decision_context, decisions, outcomes
from tests.conftest import (
    ACCOUNT_REF,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    Harness,
    attribution_rows,
    canonical_by_type,
    canonical_envelopes,
    decision_copy_envelope,
    discovery_envelope,
    evidence_envelope,
    logic_artifact,
    max_sequence,
    outcome_row,
    outcome_v2_envelope,
    post_created,
    seed_dataset,
    seed_through_decision,
    small_dataset_config,
    submit_attribution,
)

SIGNALS = dataset_signals()
FUNDED = next(signal for signal in SIGNALS if signal.kind == "rule")
PRESSURE = next(signal for signal in SIGNALS if signal.kind == "context_value")
COMPARED = canonical_by_type("decision.recorded")["payload"]["workflow_version"]


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("ac14-fresh"))
    schedule, report = seed_dataset(harness)
    assert report.fresh
    return harness


@pytest.fixture(scope="module")
def fresh_facts(fresh):
    return all_decision_facts(fresh)


def read(harness: Harness, *, signals=SIGNALS, comparison: str | None = None):
    with harness.engine.connect() as conn:
        return insights(
            conn,
            ledger_maximum(conn),
            signals=signals,
            comparison_workflow_version=(
                comparison if comparison is not None else dataset_comparison_workflow_version()
            ),
        )


def all_decision_facts(harness: Harness) -> dict:
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        ids = conn.execute(
            select(decisions.c.decision_event_id).order_by(decisions.c.ingest_sequence)
        ).scalars()
        return {d: decision_facts(conn, d, cutoff, signals=SIGNALS) for d in ids}


def workflow_rate(result, version: str) -> Rate:
    (row,) = [row for row in result.workflows if row.workflow_version == version]
    return row.rate


# --- 1. The shipped dataset -------------------------------------------------------------


def assert_within_bound(comparison: Comparison, effect: dict) -> None:
    difference = comparison.difference_points
    assert isinstance(difference, Fraction), effect["id"]
    match effect["direction"]:
        case "within":
            assert abs(difference) <= effect["max_difference_points"], (effect["id"], difference)
        case "higher":
            assert difference >= effect["min_difference_points"], (effect["id"], difference)
        case "lower":
            assert difference <= -effect["min_difference_points"], (effect["id"], difference)
        case other:
            pytest.fail(f"unknown direction {other!r}")


def test_all_three_effects_are_recovered_from_ingested_events(fresh):
    manifest = planted_effects()
    result = read(fresh)
    rows = {row.id: row for row in result.signals}
    for effect in manifest["effects"]:
        cohort = effect["cohort"]
        if cohort["kind"] == "workflow":
            workflow = result.workflow_comparison
            assert workflow.workflow_version == cohort["workflow_version"]
            assert workflow.comparison_workflow_version == manifest["comparison_workflow_version"]
            (row,) = [
                r for r in result.workflows if r.workflow_version == cohort["workflow_version"]
            ]
            assert row.decisions >= effect["minimum_decisions"]
            comparison = workflow.comparison
        else:
            row = rows[effect["id"]]
            assert (row.kind, row.input_key) == (cohort["kind"], cohort["input_key"])
            assert row.known_true >= effect["minimum_decisions"]
            comparison = row.comparison
        assert comparison.present.eligible > 0, effect["id"]
        assert comparison.absent.eligible > 0, effect["id"]
        assert_within_bound(comparison, effect)


def test_a_no_action_observation_populates_the_funding_known_false_arm(fresh, fresh_facts):
    with fresh.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        acted = set(conn.execute(select(actions.c.decision_event_id)).scalars())
        boundaries = dict(
            conn.execute(select(decisions.c.decision_event_id, decisions.c.decision_boundary)).all()
        )
        windows = {
            row.outcome_event_id: row
            for row in conn.execute(
                select(
                    outcomes.c.outcome_event_id,
                    outcomes.c.window_opened_at,
                    outcomes.c.window_closes_at,
                )
            )
        }
        funding = dict(
            conn.execute(
                select(decision_context.c.decision_event_id, decision_context.c.availability).where(
                    decision_context.c.input_key == FUNDED.input_key
                )
            ).all()
        )
        effective = {
            observation.outcome_event_id: effective_attribution(
                conn, observation.outcome_event_id, POLICY_VERSION, cutoff=cutoff
            ).attribution_event_id
            for facts in fresh_facts.values()
            for observation in facts.observations
        }
    stored = {row.attribution_event_id: row for row in attribution_rows(fresh)}

    arm = [
        facts
        for facts in fresh_facts.values()
        if facts.signal_states[FUNDED.id] == KNOWN_FALSE and facts.eligible
    ]
    found = []
    for facts in arm:
        if facts.decision_event_id in acted:
            continue
        for observation in facts.qualifying_observations:
            row = stored[effective[observation.outcome_event_id]]
            if (
                windows[observation.outcome_event_id].window_opened_at
                == boundaries[facts.decision_event_id]
                and (row.status, row.reason) == (STATUS_DIRECT, VALID_SOURCE_DECISION)
                and row.resolved_action_event_id is None
                and row.resolved_decision_event_id == facts.decision_event_id
            ):
                found.append((facts, observation))
    assert found
    decision, observation = found[0]

    # Funding evidence available and not matching: the `known false` state, not absent.
    assert funding[decision.decision_event_id] == "available"
    assert FUNDED.input_key in decision.available_inputs
    # Closed known with a recorded 90-day period.
    assert observation.state == STATE_CLOSED_KNOWN and observation.exactly_90_days
    window = windows[observation.outcome_event_id]
    opened = datetime.fromisoformat(window.window_opened_at)
    assert datetime.fromisoformat(window.window_closes_at) - opened == timedelta(days=90)

    # The decision is counted in the `known false` arm's eligible decisions.
    row = next(r for r in read(fresh).signals if r.id == FUNDED.id)
    assert decision in arm
    assert row.comparison.absent.eligible == len(arm) >= 1

    # No action exists for it and none was synthesized: the account's actions, if any,
    # belong to other decisions.
    with fresh.engine.connect() as conn:
        account_actions = (
            conn.execute(
                select(actions.c.decision_event_id).where(
                    actions.c.account_ref == decision.account_ref
                )
            )
            .scalars()
            .all()
        )
    assert decision.decision_event_id not in account_actions

    # Decisions whose funding evidence is absent or unavailable are not in the arm.
    excluded = [
        facts
        for facts in fresh_facts.values()
        if facts.signal_states[FUNDED.id] in (ABSENT, UNAVAILABLE)
    ]
    assert excluded
    assert {f.decision_event_id for f in excluded}.isdisjoint(f.decision_event_id for f in arm)
    assert {funding.get(f.decision_event_id) for f in excluded} <= {None, "unavailable"}


def test_novasignal_ai_participates_wherever_eligible(fresh, fresh_facts):
    facts = fresh_facts[DECISION_EVENT_ID]
    assert facts.standing == STANDING_EVALUATED
    assert facts.eligible
    (qualifying,) = facts.qualifying_observations
    assert qualifying.outcome_event_id == OUTCOME_EVENT_ID
    assert (qualifying.schema_version, qualifying.standing) == ("1", STATUS_DIRECT)
    window_days = canonical_by_type("outcome.evaluated")["payload"]["window_days"]
    assert outcome_row(fresh, OUTCOME_EVENT_ID).window_days == window_days == 90

    # Funding 18 days before the boundary: `known true`. Pressure `LOW`: `known false`.
    assert facts.signal_states[FUNDED.id] == KNOWN_TRUE
    assert facts.signal_states[PRESSURE.id] == KNOWN_FALSE

    result = read(fresh)
    eligible = [f for f in fresh_facts.values() if f.eligible]
    assert facts in eligible and result.overall.eligible == len(eligible)
    funded = next(row for row in result.signals if row.id == FUNDED.id)
    present = [f for f in eligible if f.signal_states[FUNDED.id] == KNOWN_TRUE]
    assert facts in present and funded.comparison.present.eligible == len(present)
    pressure = next(row for row in result.signals if row.id == PRESSURE.id)
    absent = [f for f in eligible if f.signal_states[PRESSURE.id] == KNOWN_FALSE]
    assert facts in absent and pressure.comparison.absent.eligible == len(absent)


# --- 2. Controlled changes ----------------------------------------------------------------


def test_a_controlled_outcome_change_changes_the_rate(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    seed_dataset(harness, config=small_dataset_config())
    comparison = dataset_comparison_workflow_version()
    before = workflow_rate(read(harness), COMPARED)

    # A generated schema-v2 observation (the canonical outcome is v1 and is not replaced).
    target = next(
        facts
        for facts in all_decision_facts(harness).values()
        if facts.workflow_version == COMPARED
        and facts.eligible
        and not facts.positive
        and [o.schema_version for o in facts.qualifying_observations] == ["2"]
    )
    (predecessor,) = target.qualifying_observations
    row = outcome_row(harness, predecessor.outcome_event_id)
    assert (row.schema_version, row.evaluation_state, row.opportunity) == ("2", "closed", False)
    claims = {
        name: getattr(row, name)
        for name in ("source_action_event_id", "source_decision_event_id")
        if getattr(row, name) is not None
    }
    post_created(
        harness,
        outcome_v2_envelope(
            "evt-test-o-controlled",
            observed_at=row.observed_at,
            window_opened_at=row.window_opened_at,
            window_closes_at=row.window_closes_at,
            evaluation_state="closed",
            account_ref=row.account_ref,
            reply=True,
            meeting=True,
            opportunity=True,
            supersedes_outcome_event_id=predecessor.outcome_event_id,
            **claims,
        ),
    )
    submit_attribution(harness, "evt-test-o-controlled")

    def refuse(*_args, **_kwargs):
        raise AssertionError("the analytics engine opened a fixture file")

    monkeypatch.setattr(fixtures_module, "load_json", refuse)
    after_result = read(harness, signals=SIGNALS, comparison=comparison)
    monkeypatch.undo()

    after = workflow_rate(after_result, COMPARED)
    assert after.positives == before.positives + 1
    assert after.eligible == before.eligible
    assert after.value == Fraction(before.positives + 1, before.eligible)


def test_a_controlled_cohort_change_changes_the_rate(tmp_path):
    config = small_dataset_config()
    comparison = dataset_comparison_workflow_version()
    first = Harness(tmp_path)
    schedule, _ = seed_dataset(first, config=config)
    before = read(first)
    compared, other = workflow_rate(before, COMPARED), workflow_rate(before, comparison)
    # Neither rate is 0 or 1 (a property of the small config, asserted here).
    assert compared.value not in (0, 1) and other.value not in (0, 1)

    moved = next(
        facts
        for facts in all_decision_facts(first).values()
        if facts.workflow_version == comparison and facts.positive
    )
    stage_1 = copy.deepcopy(list(schedule.stage_1))
    (envelope,) = [e for e in stage_1 if e["event_id"] == moved.decision_event_id]
    envelope["payload"]["workflow_version"] = COMPARED
    changed = build_schedule(
        canonical=schedule.canonical,
        stage_1=stage_1,
        stage_2=schedule.stage_2,
        attribution_instant=schedule.attribution_instant,
    )
    second = Harness(tmp_path)
    report = run_schedule(
        second.app, changed, signals=SIGNALS, comparison_workflow_version=comparison
    )
    assert report.fresh
    after = read(second)

    p, e = compared.positives, compared.eligible
    compared_after = workflow_rate(after, COMPARED)
    assert (compared_after.cohort_total, compared_after.eligible, compared_after.positives) == (
        compared.cohort_total + 1,
        e + 1,
        p + 1,
    )
    assert compared_after.value == Fraction(p + 1, e + 1)

    q, f = other.positives, other.eligible
    other_after = workflow_rate(after, comparison)
    assert (other_after.cohort_total, other_after.eligible, other_after.positives) == (
        other.cohort_total - 1,
        f - 1,
        q - 1,
    )
    assert other_after.value == Fraction(q - 1, f - 1)

    # (p + 1) / (e + 1) equals p / e only when p == e, a rate of 1; (q - 1) / (f - 1)
    # equals q / f only when q == f, again a rate of 1, and f - 1 > 0 because the moved
    # decision is positive (q >= 1) and q < f. Neither rate was 1, so both change.
    assert compared_after.value != compared.value
    assert other_after.value != other.value
    assert (
        after.workflow_comparison.comparison.difference_points
        != before.workflow_comparison.comparison.difference_points
    )
    # The decision moved between cohorts, not out of the population.
    assert after.overall == before.overall


# --- 3. A hand-computed ledger -------------------------------------------------------------

PRESSURE_LOW = next(
    item["value"]
    for envelope in canonical_envelopes()
    if envelope["event_type"] == "evidence.recorded"
    for item in envelope["payload"]["items"]
    if item["evidence_type"] == PRESSURE.input_key
)


def hand_id(account_ref: str, evidence_type: str) -> str:
    return f"ev-{account_ref}-{evidence_type}"


def hand_evidence(account_ref: str, *, pressure, stale_funding_observed: str | None = None):
    """The canonical evidence items re-minted for another account, with its pressure."""
    items = []
    for envelope in canonical_envelopes():
        if envelope["event_type"] != "evidence.recorded":
            continue
        for item in envelope["payload"]["items"]:
            copied = copy.deepcopy(item)
            copied["evidence_version_id"] = hand_id(account_ref, item["evidence_type"])
            if copied["evidence_type"] == PRESSURE.input_key:
                copied["value"] = pressure
            items.append(copied)
    if stale_funding_observed is not None:
        funding = next(item for item in items if item["evidence_type"] == FUNDED.input_key)
        items.append(
            dict(
                funding,
                evidence_version_id=f"ev-{account_ref}-funding-event-stale",
                observed_at=stale_funding_observed,
            )
        )
    return evidence_envelope(
        f"evt-{account_ref}-evidence",
        items,
        occurred_at="2026-04-19T00:00:00Z",
        account_ref=account_ref,
    )


def hand_decision(event_id, account_ref, boundary, *, workflow, pressure, stale_funding=False):
    """The canonical decision re-recorded for another account's re-minted evidence."""
    envelope = decision_copy_envelope(event_id, boundary=boundary, account_ref=account_ref)
    payload = envelope["payload"]
    payload["workflow_version"] = workflow
    payload["explanation"] = None
    entries = [*payload["historical_context"], *payload["consumed_inputs"]]
    for entry in entries:
        if entry.get("evidence_version_id"):
            entry["evidence_version_id"] = hand_id(account_ref, entry["input_key"])
        if entry["input_key"] == PRESSURE.input_key:
            entry["value"] = pressure
    if stale_funding:
        for entry in entries:
            if entry["input_key"] == FUNDED.input_key:
                entry["evidence_version_id"] = f"ev-{account_ref}-funding-event-stale"
        consumed = next(u for u in payload["consumed_inputs"] if u["input_key"] == FUNDED.input_key)
        # The funding factor no longer matches: it contributes 0, so 86 - 18 = 68 < 75.
        payload["result"]["score"] -= consumed["contribution"]
        consumed["contribution"] = 0
        payload["result"]["output"] = logic_artifact("v3.2")["output_mapping"]["below_threshold"]
    return envelope


def decision_observation(
    event_id, account_ref, boundary, *, days, opportunity, state="closed", reference=None
):
    opened = datetime.fromisoformat(boundary)
    closes = opened + timedelta(days=days)
    observed = closes if state == "closed" else opened + timedelta(days=10)
    claims = {"source_decision_event_id": reference} if reference is not None else {}
    return outcome_v2_envelope(
        event_id,
        observed_at=format_utc(observed),
        window_opened_at=format_utc(opened),
        window_closes_at=format_utc(closes),
        evaluation_state=state,
        account_ref=account_ref,
        opportunity=opportunity,
        **claims,
    )


def test_hand_computed_small_cases(harness):
    seed_through_decision(harness)
    comparison = dataset_comparison_workflow_version()
    canonical_boundary = canonical_by_type("decision.recorded")["payload"]["decision_boundary"]
    high = PRESSURE.equals
    d1, d2, d3 = "evt-hand-d1", "evt-hand-d2", "evt-hand-d3"
    b1, b2, b3 = "2026-04-20T09:00:00Z", "2026-04-20T10:00:00Z", "2026-04-21T10:00:00Z"
    post_created(
        harness,
        discovery_envelope("hand-a"),
        hand_evidence("hand-a", pressure=PRESSURE_LOW),
        hand_decision(d1, "hand-a", b1, workflow=COMPARED, pressure=PRESSURE_LOW),
        discovery_envelope("hand-b"),
        hand_evidence("hand-b", pressure=high, stale_funding_observed="2025-06-01"),
        hand_decision(d2, "hand-b", b2, workflow=comparison, pressure=high),
        hand_decision(d3, "hand-b", b3, workflow=comparison, pressure=high, stale_funding=True),
        decision_observation(
            "evt-hand-o-canonical",
            ACCOUNT_REF,
            canonical_boundary,
            days=90,
            opportunity=False,
            reference=DECISION_EVENT_ID,
        ),
        decision_observation(
            "evt-hand-o-d1", "hand-a", b1, days=90, opportunity=True, reference=d1
        ),
        decision_observation(
            "evt-hand-o-d1-open",
            "hand-a",
            b1,
            days=90,
            opportunity=True,
            state="open",
            reference=d1,
        ),
        decision_observation(
            "evt-hand-o-d2", "hand-b", b2, days=90, opportunity=True, reference=d2
        ),
        decision_observation(
            "evt-hand-o-d2-short", "hand-b", b2, days=30, opportunity=False, reference=d2
        ),
        # No reference, and hand-b records no action: unresolved.
        decision_observation("evt-hand-o-d3", "hand-b", b3, days=90, opportunity=True),
    )
    for outcome in (
        "evt-hand-o-canonical",
        "evt-hand-o-d1",
        "evt-hand-o-d2",
        "evt-hand-o-d2-short",
        "evt-hand-o-d3",
    ):
        submit_attribution(harness, outcome)  # the open d1 observation stays awaiting
    assert {row.outcome_event_id: row.status for row in attribution_rows(harness)} == {
        "evt-hand-o-canonical": STATUS_DIRECT,
        "evt-hand-o-d1": STATUS_DIRECT,
        "evt-hand-o-d2": STATUS_DIRECT,
        "evt-hand-o-d2-short": STATUS_DIRECT,
        "evt-hand-o-d3": STATUS_UNRESOLVED,
    }

    v32 = logic_artifact("v3.2")
    v32_version, v32_hash = v32["logic_version"], canonical_hash(v32)
    # Qualifying (attributed, closed known, 90 days): canonical (false), d1 (true), d2 (true).
    # d2's 30-day observation is another period; d3's observation is unresolved, so d3 is
    # unattributed. Eligible = {canonical, d1, d2} = 3; positives = {d1, d2} = 2.
    overall = Rate(4, 3, 2, 0, 0, 1)
    # recently_funded: known true = {canonical (18 days), d1 (21), d2 (21)}; known false =
    # {d3 (324 days)}. Present 2 of 3; absent 0 eligible of 1, not evaluated 1: no difference.
    funded_row = SignalRow(
        id=FUNDED.id,
        kind=FUNDED.kind,
        input_key=FUNDED.input_key,
        predicate=FUNDED.predicate,
        known_true=3,
        known_false=1,
        unavailable=0,
        absent=0,
        not_applicable=0,
        reconstruction_failed=0,
        input_available=4,
        input_consumed=4,
        historical_rule_matched=(RuleMatch(v32_version, v32_hash, FUNDED.rule, 4, 3),),
        comparison=Comparison(Rate(3, 3, 2, 0, 0, 0), Rate(1, 0, 0, 0, 0, 1)),
    )
    # HIGH pressure: known true = {d2, d3}: d2 eligible and positive, d3 not evaluated
    # (1 of 1); known false = {canonical, d1}: both eligible, d1 positive (1 of 2).
    # Difference (1 - 1/2) x 100 = 50 points. v3.2 has no pressure factor: consumed 0.
    pressure_row = SignalRow(
        id=PRESSURE.id,
        kind=PRESSURE.kind,
        input_key=PRESSURE.input_key,
        predicate=PRESSURE.predicate,
        known_true=2,
        known_false=2,
        unavailable=0,
        absent=0,
        not_applicable=0,
        reconstruction_failed=0,
        input_available=4,
        input_consumed=0,
        historical_rule_matched=(RuleMatch(v32_version, v32_hash, None, 4, 0),),
        comparison=Comparison(Rate(2, 1, 1, 0, 0, 1), Rate(2, 2, 1, 0, 0, 0)),
    )
    # Workflows: the compared cohort {canonical, d1} is 1 of 2; the comparison cohort
    # {d2, d3} is 1 of 1 with d3 not evaluated. Difference (1/2 - 1) x 100 = -50 points.
    compared_rate, comparison_rate = Rate(2, 2, 1, 0, 0, 0), Rate(2, 1, 1, 0, 0, 1)
    rows = {
        COMPARED: WorkflowRow(COMPARED, 2, DecisionStandings(2, 0, 0, 0), compared_rate),
        comparison: WorkflowRow(comparison, 2, DecisionStandings(1, 0, 0, 1), comparison_rate),
    }
    expected = Insights(
        cutoff=max_sequence(harness),
        population=4,
        reconstructed=4,
        reconstruction_failures=(),
        # Six effective observations: four direct, one unresolved, one awaiting (open).
        observations=ObservationCoverage(
            awaiting_attribution=1,
            unresolved=1,
            direct=4,
            inferred=0,
            open=1,
            closed_known=5,
            closed_unknown=0,
            other_period=1,
            qualifying_90_day=3,
            total=6,
        ),
        standings=DecisionStandings(evaluated=3, unknown=0, open=0, unattributed=1),
        overall=overall,
        signals=(funded_row, pressure_row),
        workflows=tuple(rows[version] for version in sorted(rows)),
        workflow_comparison=WorkflowComparison(
            COMPARED, comparison, Comparison(compared_rate, comparison_rate)
        ),
    )
    result = read(harness)
    assert result == expected
    assert result.overall.value == Fraction(2, 3)
    assert funded_row.comparison.difference_points is None
    assert result.signals[1].comparison.difference_points == 50
    assert result.workflow_comparison.comparison.difference_points == -50
