"""D-014 Q1 closure tests for `analytics.insights`, each on a ledger built through the collector.

Evidence executed: rate eligibility by recorded period and effective version
(item 7); the account-wide policy for observations of decisions with no action
(D-013 Q2, D-015); decision standings and reconstruction failure (items 5 and 6);
signal states (item 8, INV-03, AC-11); cutoff isolation; the AC-16 exclusion at
the engine (INV-08, INV-10); a late attribution to a superseded version; and
read-only analytics beside replay (INV-01, INV-06).

D-016 note: the collector now refuses a fresh `outcome.attributed` write that
names an outcome version already superseded by the time it arrives.
`test_a_late_attribution_to_a_superseded_outcome_is_excluded` below is the one
test in the suite that used to make exactly that write through the public
endpoint; its read-side proof (a stale result *stored* in the ledger stays
excluded from every selection) is preserved by planting that stored row
directly as a labelled legacy row, beside a new assertion that the collector
now refuses the write itself.
"""

import copy
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from flight_recorder.analytics.insights import (
    ABSENT,
    KNOWN_FALSE,
    KNOWN_TRUE,
    NOT_APPLICABLE,
    RECONSTRUCTION_FAILED,
    SIGNAL_STATES,
    STANDING_EVALUATED,
    STANDING_OPEN,
    STANDING_UNATTRIBUTED,
    STANDING_UNKNOWN,
    UNAVAILABLE,
    DecisionStandings,
    SelectionFailure,
    decision_facts,
    insights,
)
from flight_recorder.attribution.policy import (
    MOST_RECENT_ELIGIBLE_ACTION,
    NO_ELIGIBLE_ACTION,
    NO_SOURCE_REFERENCE,
    SEGMENT_SEPARATOR,
    STATUS_DIRECT,
    STATUS_INFERRED,
    STATUS_UNRESOLVED,
    VALID_SOURCE_DECISION,
    ledger_maximum,
)
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact, format_utc
from flight_recorder.fixtures import dataset_comparison_workflow_version, dataset_signals
from flight_recorder.ledger.schema import actions, decision_consumed_inputs, decisions
from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
from flight_recorder.logic.rules import UnsupportedRule
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import ReconstructionMismatch
from tests.conftest import (
    ACCOUNT_REF,
    ACTION_EVENT_ID,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    FixedClock,
    Harness,
    action_envelope,
    append_unrelated,
    attribute_ledger,
    attribution_envelope,
    attribution_rows,
    canonical_by_type,
    canonical_envelopes,
    captured_statements,
    decision_copy_envelope,
    derived_artifact_envelope,
    discovery_envelope,
    evidence_envelope,
    insert_ambiguous_attribution,
    insert_legacy_attribution,
    logic_artifact,
    outcome_v2_envelope,
    post_created,
    register_artifacts,
    register_derived_artifact,
    seed_all,
    seed_and_attribute,
    seed_dataset,
    seed_through_decision,
    small_dataset_config,
    submit_attribution,
)

SIGNALS = dataset_signals()
FUNDED = next(signal for signal in SIGNALS if signal.kind == "rule")
PRESSURE = next(signal for signal in SIGNALS if signal.kind == "context_value")
COMPARED = canonical_by_type("decision.recorded")["payload"]["workflow_version"]

ACCOUNT = "acct-engine"
BOUNDARY = "2026-04-20T09:00:00Z"


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    harness = Harness(tmp_path_factory.mktemp("engine-fresh"))
    _, report = seed_dataset(harness)
    assert report.fresh
    return harness


# --- Construction ---------------------------------------------------------------------------


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def canonical_items() -> dict[str, dict]:
    return {
        item["evidence_type"]: item
        for envelope in canonical_envelopes()
        if envelope["event_type"] == "evidence.recorded"
        for item in envelope["payload"]["items"]
    }


def record_decision(
    harness: Harness,
    event_id: str,
    *,
    account_ref: str = ACCOUNT,
    boundary: str = BOUNDARY,
    values: dict | None = None,
    unavailable: tuple[str, ...] = (),
    omit: tuple[str, ...] = (),
    artifact: dict | None = None,
    workflow: str = COMPARED,
    funding_days_before: int = 10,
):
    """Evidence re-minted from every canonical evidence item (funding observed
    `funding_days_before` days before the boundary; `values` overrides fields),
    minus `omit` and `unavailable`, then a decision `evaluator-v1` evaluates over
    exactly that context under `artifact` (canonical v3.2 by default)."""
    at = instant(boundary)
    items = []
    for key, item in canonical_items().items():
        if key in omit or key in unavailable:
            continue
        copied = copy.deepcopy(item)
        copied["evidence_version_id"] = f"ev-{event_id}-{key}"
        if key == FUNDED.input_key:
            copied["observed_at"] = (at.date() - timedelta(days=funding_days_before)).isoformat()
        copied.update((values or {}).get(key, {}))
        items.append(copied)
    post_created(
        harness,
        evidence_envelope(
            f"{event_id}-evidence",
            items,
            occurred_at=format_utc(at - timedelta(hours=1)),
            account_ref=account_ref,
        ),
    )
    content = artifact if artifact is not None else logic_artifact("v3.2")
    context = sorted(
        [
            ContextInput(
                key=item["evidence_type"],
                availability="available",
                value=item["value"],
                evidence_version_id=item["evidence_version_id"],
                observed_at=date.fromisoformat(item["observed_at"])
                if "observed_at" in item
                else None,
            )
            for item in items
        ]
        + [ContextInput(key=key, availability="unavailable") for key in unavailable],
        key=lambda entry: entry.key,
    )
    result = evaluate(LogicArtifact.model_validate(content), context, at)
    preserved = {entry.key: entry.value for entry in context}
    envelope = decision_copy_envelope(event_id, boundary=boundary, account_ref=account_ref)
    envelope["payload"].update(
        workflow_version=workflow,
        historical_context=[
            {"input_key": entry.key, "value": entry.value, "availability": entry.availability}
            | ({"evidence_version_id": entry.evidence_version_id} if entry.is_available else {})
            for entry in context
        ],
        consumed_inputs=[
            {
                "input_key": factor.key,
                "value": preserved[factor.key],
                "evidence_version_id": factor.evidence_version_id,
                "contribution": factor.contribution,
            }
            for factor in result.factors
            if factor.input_state is InputState.CONSUMED
        ],
        logic_artifact={
            "logic_version": content["logic_version"],
            "artifact_id": content["artifact_id"],
            "artifact_hash": canonical_hash(content),
            "evaluator_version": content["evaluator_version"],
        },
        result={"score": result.score, "threshold": result.threshold, "output": result.output},
        explanation=None,
    )
    post_created(harness, envelope)
    return result


def engine_ledger(harness: Harness) -> None:
    register_artifacts(harness)
    post_created(harness, discovery_envelope(ACCOUNT))


def act(
    harness,
    event_id,
    decision_event_id,
    *,
    hours,
    status="sent",
    account_ref=ACCOUNT,
    boundary=BOUNDARY,
):
    occurred = format_utc(instant(boundary) + timedelta(hours=hours))
    post_created(
        harness,
        action_envelope(
            event_id,
            occurred_at=occurred,
            status=status,
            decision_event_id=decision_event_id,
            account_ref=account_ref,
        ),
    )
    return occurred


def observation(
    event_id, *, opened, days, state="closed", account_ref=ACCOUNT, age_days=10, **payload
):
    opened_at = instant(opened)
    closes = opened_at + timedelta(days=days)
    observed = closes if state == "closed" else opened_at + timedelta(days=age_days)
    return outcome_v2_envelope(
        event_id,
        observed_at=format_utc(observed),
        window_opened_at=format_utc(opened_at),
        window_closes_at=format_utc(closes),
        evaluation_state=state,
        account_ref=account_ref,
        **payload,
    )


def read(harness: Harness, cutoff: int | None = None, signals=SIGNALS):
    with harness.engine.connect() as conn:
        return insights(
            conn,
            cutoff if cutoff is not None else ledger_maximum(conn),
            signals=signals,
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )


def facts(harness: Harness, decision_event_id: str, signals=SIGNALS):
    with harness.engine.connect() as conn:
        return decision_facts(conn, decision_event_id, ledger_maximum(conn), signals=signals)


def statuses(harness: Harness) -> dict[str, str]:
    return {row.outcome_event_id: row.status for row in attribution_rows(harness)}


def row_for(result, signal):
    return next(row for row in result.signals if row.id == signal.id)


def state_counts(row) -> tuple[int, ...]:
    return (
        row.known_true,
        row.known_false,
        row.unavailable,
        row.absent,
        row.not_applicable,
        row.reconstruction_failed,
    )


# --- Rate eligibility (item 7) --------------------------------------------------------------


def test_a_30_day_true_and_a_90_day_false_yield_a_negative_eligible_decision(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    second = act(harness, "evt-test-a-2", "evt-test-d", hours=2)
    post_created(
        harness,
        observation(
            "evt-test-o-30",
            opened=first,
            days=30,
            opportunity=True,
            source_action_event_id="evt-test-a-1",
        ),
        observation(
            "evt-test-o-90",
            opened=second,
            days=90,
            opportunity=False,
            source_action_event_id="evt-test-a-2",
        ),
    )
    submit_attribution(harness, "evt-test-o-30")
    submit_attribution(harness, "evt-test-o-90")
    assert statuses(harness) == {"evt-test-o-30": STATUS_DIRECT, "evt-test-o-90": STATUS_DIRECT}

    result = read(harness)
    assert (result.overall.eligible, result.overall.positives) == (1, 0)
    assert result.observations.other_period == 1


def test_a_90_day_true_and_a_120_day_false_yield_a_positive(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    second = act(harness, "evt-test-a-2", "evt-test-d", hours=2)
    # A 120-day window opened 60 days before its action closes 60 days after it,
    # inside the policy's lookback, so the observation resolves by its action claim.
    opened = format_utc(instant(second) - timedelta(days=60))
    post_created(
        harness,
        observation(
            "evt-test-o-90",
            opened=first,
            days=90,
            opportunity=True,
            source_action_event_id="evt-test-a-1",
        ),
        observation(
            "evt-test-o-120",
            opened=opened,
            days=120,
            opportunity=False,
            source_action_event_id="evt-test-a-2",
        ),
    )
    submit_attribution(harness, "evt-test-o-120")
    assert statuses(harness) == {"evt-test-o-120": STATUS_DIRECT}
    submit_attribution(harness, "evt-test-o-90")

    result = read(harness)
    assert (result.overall.eligible, result.overall.positives) == (1, 1)
    assert result.observations.other_period == 1


def test_no_qualifying_observation_is_not_available(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    post_created(
        harness,
        observation(
            "evt-test-o-30",
            opened=first,
            days=30,
            opportunity=True,
            source_action_event_id="evt-test-a-1",
        ),
    )
    submit_attribution(harness, "evt-test-o-30")

    overall = read(harness).overall
    assert overall.eligible == 0
    assert overall.available is False
    assert overall.value is None
    assert overall.display_note == "not available (0 eligible decisions)"
    assert overall.excluded_other_period_only == 1
    assert facts(harness, "evt-test-d").standing == STANDING_EVALUATED


def test_two_independent_qualifying_chains_one_true_one_false_count_one_positive(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    second = act(harness, "evt-test-a-2", "evt-test-d", hours=2)
    post_created(
        harness,
        observation(
            "evt-test-o-true",
            opened=first,
            days=90,
            opportunity=True,
            source_action_event_id="evt-test-a-1",
        ),
        observation(
            "evt-test-o-false",
            opened=second,
            days=90,
            opportunity=False,
            source_action_event_id="evt-test-a-2",
        ),
    )
    submit_attribution(harness, "evt-test-o-true")
    submit_attribution(harness, "evt-test-o-false")

    result = read(harness)
    assert (result.overall.eligible, result.overall.positives) == (1, 1)
    assert result.observations.qualifying_90_day == 2


@pytest.mark.parametrize(
    "first_value,then_value,positives",
    [
        pytest.param(True, False, 0, id="true-then-false"),
        pytest.param(False, True, 1, id="false-then-true"),
    ],
)
def test_true_superseded_by_false_within_a_chain_counts_the_effective_false(
    harness, first_value, then_value, positives
):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    claim = {"source_action_event_id": "evt-test-a-1"}
    post_created(
        harness,
        observation("evt-test-o-1", opened=first, days=90, opportunity=first_value, **claim),
    )
    submit_attribution(harness, "evt-test-o-1")
    post_created(
        harness,
        observation(
            "evt-test-o-2",
            opened=first,
            days=90,
            opportunity=then_value,
            supersedes_outcome_event_id="evt-test-o-1",
            **claim,
        ),
    )
    submit_attribution(harness, "evt-test-o-2")

    result = read(harness)
    assert (result.overall.eligible, result.overall.positives) == (1, positives)
    assert result.observations.total == 1


def test_a_successor_awaiting_attribution_inherits_nothing(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    claim = {"source_action_event_id": "evt-test-a-1"}
    post_created(
        harness, observation("evt-test-o-1", opened=first, days=90, opportunity=True, **claim)
    )
    submit_attribution(harness, "evt-test-o-1")
    post_created(
        harness,
        observation(
            "evt-test-o-2",
            opened=first,
            days=90,
            opportunity=True,
            supersedes_outcome_event_id="evt-test-o-1",
            **claim,
        ),
    )

    decision = facts(harness, "evt-test-d")
    assert decision.observations == ()
    assert not decision.eligible
    result = read(harness)
    assert result.overall.eligible == 0
    assert result.observations.awaiting_attribution == 1


def test_a_zero_denominator_comparison_has_no_difference(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")  # funding 10 days before: known true
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    post_created(
        harness,
        observation(
            "evt-test-o",
            opened=first,
            days=90,
            opportunity=True,
            source_action_event_id="evt-test-a-1",
        ),
    )
    submit_attribution(harness, "evt-test-o")

    row = row_for(read(harness), FUNDED)
    assert (row.known_true, row.known_false) == (1, 0)
    assert row.comparison.present.available
    assert not row.comparison.absent.available
    assert row.comparison.absent.value is None
    assert row.comparison.difference_points is None


def test_no_action_observations_follow_the_account_wide_policy(tmp_path):
    # (a) An account with no action at all, and a reference-free observation.
    harness = Harness(tmp_path)
    engine_ledger(harness)
    record_decision(harness, "evt-test-d-alone", funding_days_before=200)  # 68: not prioritized
    post_created(
        harness, observation("evt-test-o-alone", opened=BOUNDARY, days=90, opportunity=True)
    )
    submit_attribution(harness, "evt-test-o-alone")
    (row,) = attribution_rows(harness)
    assert (
        row.status,
        row.reason,
        row.resolved_action_event_id,
        row.resolved_decision_event_id,
    ) == (
        STATUS_UNRESOLVED,
        f"{NO_SOURCE_REFERENCE}{SEGMENT_SEPARATOR}{NO_ELIGIBLE_ACTION}",
        None,
        None,
    )
    alone = facts(harness, "evt-test-d-alone")
    assert (alone.standing, alone.eligible) == (STANDING_UNATTRIBUTED, False)

    # (b) and (c): the canonical account, which holds the canonical action. The
    # no-action decision falls 30 seconds before that action, so a 90-day window
    # opened at its boundary closes inside the action's 90-day lookback.
    no_action = "evt-test-d-no-action"
    action_at = instant(canonical_by_type("action.recorded")["occurred_at"])
    boundary = format_utc(action_at - timedelta(seconds=30))
    for case, claims in (("b", {}), ("c", {"source_decision_event_id": no_action})):
        ledger = Harness(tmp_path)
        for response in seed_all(ledger):
            assert response.status_code == 201
        outcome = f"evt-test-o-{case}"
        post_created(
            ledger,
            decision_copy_envelope(no_action, boundary=boundary),
            observation(
                outcome,
                opened=boundary,
                days=90,
                account_ref=ACCOUNT_REF,
                opportunity=True,
                **claims,
            ),
        )
        submit_attribution(ledger, outcome)
        (row,) = [r for r in attribution_rows(ledger) if r.outcome_event_id == outcome]
        undecided, canonical = facts(ledger, no_action), facts(ledger, DECISION_EVENT_ID)
        credited = [o.outcome_event_id for o in canonical.observations]
        if case == "b":
            assert (
                row.status,
                row.reason,
                row.resolved_action_event_id,
                row.resolved_decision_event_id,
            ) == (
                STATUS_INFERRED,
                f"{NO_SOURCE_REFERENCE}{SEGMENT_SEPARATOR}{MOST_RECENT_ELIGIBLE_ACTION}",
                ACTION_EVENT_ID,
                DECISION_EVENT_ID,
            )
            assert undecided.standing == STANDING_UNATTRIBUTED
            assert outcome in credited
        else:
            assert (
                row.status,
                row.reason,
                row.resolved_action_event_id,
                row.resolved_decision_event_id,
            ) == (
                STATUS_DIRECT,
                VALID_SOURCE_DECISION,
                None,
                no_action,
            )
            assert (undecided.standing, undecided.eligible) == (STANDING_EVALUATED, True)
            assert outcome not in credited


# --- Standing and integrity (items 5 and 6) -------------------------------------------------


def test_closed_known_plus_closed_unknown_is_evaluated(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    claim = {"source_action_event_id": "evt-test-a-1"}
    post_created(
        harness,
        observation("evt-test-o-known", opened=first, days=90, opportunity=False, **claim),
        observation("evt-test-o-unknown", opened=first, days=90, reply=True, **claim),
    )
    submit_attribution(harness, "evt-test-o-known")
    submit_attribution(harness, "evt-test-o-unknown")
    assert facts(harness, "evt-test-d").standing == STANDING_EVALUATED


def test_closed_unknown_plus_open_true_and_open_false_is_unknown(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    claim = {"source_action_event_id": "evt-test-a-1"}
    post_created(
        harness,
        observation("evt-test-o-unknown", opened=first, days=90, reply=False, **claim),
        observation(
            "evt-test-o-open-true", opened=first, days=90, state="open", opportunity=True, **claim
        ),
        observation(
            "evt-test-o-open-false", opened=first, days=90, state="open", opportunity=False, **claim
        ),
    )
    for outcome in ("evt-test-o-unknown", "evt-test-o-open-true", "evt-test-o-open-false"):
        submit_attribution(harness, outcome)

    assert facts(harness, "evt-test-d").standing == STANDING_UNKNOWN
    overall = read(harness).overall
    assert (overall.eligible, overall.positives) == (0, 0)


def test_all_open_is_open(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d")
    first = act(harness, "evt-test-a-1", "evt-test-d", hours=1)
    claim = {"source_action_event_id": "evt-test-a-1"}
    post_created(
        harness,
        observation("evt-test-o-1", opened=first, days=90, state="open", opportunity=True, **claim),
        observation(
            "evt-test-o-2", opened=first, days=90, state="open", opportunity=False, **claim
        ),
    )
    submit_attribution(harness, "evt-test-o-1")
    submit_attribution(harness, "evt-test-o-2")
    assert facts(harness, "evt-test-d").standing == STANDING_OPEN


def test_no_attribution_is_unattributed(harness):
    engine_ledger(harness)
    result = record_decision(harness, "evt-test-d-quiet", funding_days_before=200)
    assert result.output == logic_artifact("v3.2")["output_mapping"]["below_threshold"]
    post_created(harness, discovery_envelope("acct-unresolved"))
    record_decision(harness, "evt-test-d-unresolved", account_ref="acct-unresolved")
    post_created(
        harness,
        observation(
            "evt-test-o-unresolved",
            opened=BOUNDARY,
            days=90,
            account_ref="acct-unresolved",
            opportunity=True,
        ),
    )
    submit_attribution(harness, "evt-test-o-unresolved")
    assert statuses(harness) == {"evt-test-o-unresolved": STATUS_UNRESOLVED}

    assert facts(harness, "evt-test-d-quiet").standing == STANDING_UNATTRIBUTED
    assert facts(harness, "evt-test-d-unresolved").standing == STANDING_UNATTRIBUTED


def test_standings_partition_the_population(harness, fresh):
    engine_ledger(harness)
    for decision in ("evt-test-d-e", "evt-test-d-u", "evt-test-d-o", "evt-test-d-n"):
        record_decision(harness, decision)
    first = act(harness, "evt-test-a-1", "evt-test-d-e", hours=1)
    post_created(
        harness,
        observation(
            "evt-test-o-e",
            opened=first,
            days=90,
            opportunity=False,
            source_action_event_id="evt-test-a-1",
        ),
        observation(
            "evt-test-o-u",
            opened=BOUNDARY,
            days=90,
            reply=True,
            source_decision_event_id="evt-test-d-u",
        ),
        observation(
            "evt-test-o-o",
            opened=BOUNDARY,
            days=90,
            state="open",
            opportunity=True,
            source_decision_event_id="evt-test-d-o",
        ),
    )
    for outcome in ("evt-test-o-e", "evt-test-o-u", "evt-test-o-o"):
        submit_attribution(harness, outcome)

    hand = read(harness)
    assert hand.standings == DecisionStandings(evaluated=1, unknown=1, open=1, unattributed=1)
    assert hand.standings.total == hand.population == 4

    seeded = read(fresh)
    assert seeded.standings.total == seeded.population


def _mismatched_score(harness: Harness) -> tuple[str, str]:
    envelope = decision_copy_envelope("evt-test-d-failing", boundary="2026-04-18T00:00:00Z")
    envelope["payload"]["result"]["score"] = 85  # everything else canonical
    post_created(harness, envelope)
    return envelope["event_id"], ReconstructionMismatch.__name__


def _unsupported_rule(harness: Harness) -> tuple[str, str]:
    factors = copy.deepcopy(logic_artifact("v3.2")["factors"])
    funding = next(factor for factor in factors if factor["key"] == FUNDED.input_key)
    funding["rule"] = f"{funding['rule']} soon"  # a trailing word: no grammar shape
    registration = derived_artifact_envelope(
        "test-trailing-word",
        "v3.2-test-trailing-word",
        factors,
        event_id="evt-system-logic-artifact-test-trailing-word",
    )
    artifact_hash = register_derived_artifact(harness, registration)
    content = registration["payload"]["artifact"]
    envelope = decision_copy_envelope("evt-test-d-failing", boundary="2026-04-18T00:00:00Z")
    envelope["payload"]["logic_artifact"] = {
        "logic_version": content["logic_version"],
        "artifact_id": content["artifact_id"],
        "artifact_hash": artifact_hash,
        "evaluator_version": content["evaluator_version"],
    }
    post_created(harness, envelope)
    return envelope["event_id"], UnsupportedRule.__name__


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_mismatched_score, id="reconstruction-mismatch"),
        pytest.param(_unsupported_rule, id="rule-error"),
    ],
)
def test_a_failed_reconstruction_with_a_resolved_positive_observation_is_excluded_from_rates_but_not_from_counts(  # noqa: E501 (the task names the test)
    harness, build
):
    seed_through_decision(harness)
    failing, error = build(harness)
    boundary = "2026-04-18T00:00:00Z"
    post_created(
        harness,
        observation(
            "evt-test-o-failing",
            opened=boundary,
            days=90,
            account_ref=ACCOUNT_REF,
            opportunity=True,
            source_decision_event_id=failing,
        ),
    )
    submit_attribution(harness, "evt-test-o-failing")
    assert statuses(harness) == {"evt-test-o-failing": STATUS_DIRECT}

    result = read(harness)
    (failure,) = result.reconstruction_failures
    assert (failure.decision_event_id, failure.error) == (failing, error)
    assert result.population == 2
    assert result.reconstructed == 1
    assert result.standings.evaluated == 1  # the failing decision's observation
    assert facts(harness, failing).standing == STANDING_EVALUATED
    assert result.overall.eligible == 0
    assert result.overall.excluded_reconstruction_failed == 1
    assert set(facts(harness, failing).signal_states.values()) == {RECONSTRUCTION_FAILED}
    for row in result.signals:
        assert row.reconstruction_failed == 1
        assert sum(state_counts(row)[:5]) == result.population - 1


def test_ambiguous_selection_fails_the_whole_read(harness):
    seed_and_attribute(harness)
    opened = canonical_by_type("action.recorded")["occurred_at"]
    post_created(
        harness,
        observation(
            "evt-test-o-other",
            opened=opened,
            days=90,
            state="open",
            account_ref=ACCOUNT_REF,
            reply=True,
        ),
    )
    attribute_ledger(harness)
    other = attribution_rows(harness)[1]
    insert_ambiguous_attribution(harness, other.attribution_event_id)

    with pytest.raises(SelectionFailure) as failure:
        read(harness)
    assert failure.value.outcome_event_id == OUTCOME_EVENT_ID
    assert len(failure.value.candidates) == 2


# --- Signals (item 8) ---------------------------------------------------------------------


def test_signal_states_are_exhaustive_and_exclusive(fresh):
    result = read(fresh)
    assert result.population > 0
    for row in result.signals:
        assert sum(state_counts(row)) == result.population, row.id


def test_rule_signal_states(harness):
    engine_ledger(harness)
    record_decision(harness, "evt-test-d-recent", funding_days_before=10)
    record_decision(harness, "evt-test-d-old", funding_days_before=200)
    record_decision(harness, "evt-test-d-unavailable", unavailable=(FUNDED.input_key,))
    record_decision(harness, "evt-test-d-absent", omit=(FUNDED.input_key,))
    registration = derived_artifact_envelope(
        "test-no-funding",
        "v5.1-test-no-funding",
        [f for f in logic_artifact("v5.1")["factors"] if f["key"] != FUNDED.input_key],
        event_id="evt-system-logic-artifact-test-no-funding",
    )
    register_derived_artifact(harness, registration)
    record_decision(harness, "evt-test-d-derived", artifact=registration["payload"]["artifact"])

    expected = {
        "evt-test-d-recent": KNOWN_TRUE,
        "evt-test-d-old": KNOWN_FALSE,
        "evt-test-d-unavailable": UNAVAILABLE,
        "evt-test-d-absent": ABSENT,
        "evt-test-d-derived": NOT_APPLICABLE,
    }
    per_decision = {decision: facts(harness, decision) for decision in expected}
    assert {d: f.signal_states[FUNDED.id] for d, f in per_decision.items()} == expected

    row = row_for(read(harness), FUNDED)
    assert state_counts(row) == (1, 1, 1, 1, 1, 0)
    assert row.input_available == 3  # recent, old, derived
    referencing = [
        f
        for f in per_decision.values()
        if FUNDED.input_key in f.available_inputs and f.historical_rules[FUNDED.id] is not None
    ]
    assert row.input_consumed == len(referencing) == 2


def test_context_value_signal_ignores_consumption(harness):
    engine_ledger(harness)
    high, low = PRESSURE.equals, canonical_items()[PRESSURE.input_key]["value"]
    v51 = logic_artifact("v5.1")
    record_decision(harness, "evt-test-d-high-v32", values={PRESSURE.input_key: {"value": high}})
    record_decision(
        harness, "evt-test-d-high-v51", values={PRESSURE.input_key: {"value": high}}, artifact=v51
    )
    record_decision(
        harness, "evt-test-d-low-v51", values={PRESSURE.input_key: {"value": low}}, artifact=v51
    )

    high_v32 = facts(harness, "evt-test-d-high-v32")
    assert high_v32.signal_states[PRESSURE.id] == KNOWN_TRUE
    assert high_v32.context_states[PRESSURE.input_key] == InputState.IGNORED
    assert PRESSURE.input_key not in high_v32.consumed_inputs
    high_v51 = facts(harness, "evt-test-d-high-v51")
    assert high_v51.signal_states[PRESSURE.id] == KNOWN_TRUE
    assert PRESSURE.input_key in high_v51.consumed_inputs
    assert high_v51.historical_rules[PRESSURE.id][1] is False
    low_v51 = facts(harness, "evt-test-d-low-v51")
    assert low_v51.signal_states[PRESSURE.id] == KNOWN_FALSE
    assert low_v51.historical_rules[PRESSURE.id][1] is True

    row = row_for(read(harness), PRESSURE)
    assert (row.known_true, row.known_false) == (2, 1)
    assert (row.input_available, row.input_consumed) == (3, 2)
    v51_rule = next(f["rule"] for f in v51["factors"] if f["key"] == PRESSURE.input_key)
    matches = {match.logic_version: match for match in row.historical_rule_matched}
    v32_version = logic_artifact("v3.2")["logic_version"]
    assert (
        matches[v32_version].rule,
        matches[v32_version].decisions,
        matches[v32_version].matched,
    ) == (None, 1, 0)
    assert (matches[v51["logic_version"]].rule, matches[v51["logic_version"]].decisions) == (
        v51_rule,
        2,
    )
    assert matches[v51["logic_version"]].matched == 1  # the LOW decision, not the HIGH one


def test_a_zero_weight_matched_rule_is_matched(harness):
    engine_ledger(harness)
    factors = copy.deepcopy(logic_artifact("v3.2")["factors"])
    next(f for f in factors if f["key"] == FUNDED.input_key)["weight"] = 0
    registration = derived_artifact_envelope(
        "test-zero-funding",
        "v3.2-test-zero-funding",
        factors,
        event_id="evt-system-logic-artifact-test-zero-funding",
    )
    register_derived_artifact(harness, registration)
    record_decision(harness, "evt-test-d-zero", artifact=registration["payload"]["artifact"])

    decision = facts(harness, "evt-test-d-zero")
    assert decision.signal_states[FUNDED.id] == KNOWN_TRUE
    assert decision.historical_rules[FUNDED.id] == (FUNDED.rule, True)
    with harness.engine.connect() as conn:
        contribution = conn.execute(
            select(decision_consumed_inputs.c.contribution).where(
                decision_consumed_inputs.c.decision_event_id == "evt-test-d-zero",
                decision_consumed_inputs.c.input_key == FUNDED.input_key,
            )
        ).scalar_one()
    assert contribution == 0


def test_later_evidence_and_current_state_leave_signal_states_unchanged(harness):
    seed_and_attribute(harness)
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
    before = read(harness, cutoff)
    funding = canonical_items()[FUNDED.input_key]
    post_created(
        harness,
        # A correction whose observation date would no longer match the 90-day rule.
        evidence_envelope(
            "evt-test-funding-correction",
            [
                dict(
                    funding,
                    evidence_version_id=f"{funding['evidence_version_id']}-corrected",
                    observed_at="2025-01-01",
                    supersedes_evidence_version_id=funding["evidence_version_id"],
                )
            ],
            occurred_at="2026-08-01T00:00:00Z",
        ),
        # Present-day pressure evidence, available after every boundary.
        evidence_envelope(
            "evt-test-pressure-later",
            [
                dict(
                    canonical_items()[PRESSURE.input_key],
                    evidence_version_id="ev-novasignal-pressure-later",
                    value=PRESSURE.equals,
                )
            ],
            occurred_at="2026-08-02T00:00:00Z",
        ),
    )

    assert read(harness, cutoff).as_dict() == before.as_dict()
    latest = read(harness)
    assert latest.population == before.population
    for after_row, before_row in zip(latest.signals, before.signals, strict=True):
        assert state_counts(after_row) == state_counts(before_row), after_row.id
    assert facts(harness, DECISION_EVENT_ID).signal_states[FUNDED.id] == KNOWN_TRUE


# --- Cutoff isolation and the AC-16 boundary -----------------------------------------------


def test_cutoff_isolation(harness):
    seed_dataset(harness, config=small_dataset_config())
    with harness.engine.connect() as conn:
        cutoff = ledger_maximum(conn)
    before = read(harness, cutoff)

    account, decision = "iso-acct", "evt-test-d-iso"
    boundary = "2026-05-10T09:00:00Z"
    post_created(harness, discovery_envelope(account))
    record_decision(harness, decision, account_ref=account, boundary=boundary)
    persona = copy.deepcopy(canonical_by_type("persona.selected"))
    persona_at = format_utc(instant(boundary) + timedelta(minutes=1))
    persona.update(
        event_id="evt-test-persona-iso",
        account_ref=account,
        occurred_at=persona_at,
        recorded_at=persona_at,
    )
    persona["payload"]["decision_event_id"] = decision
    post_created(harness, persona)
    acted = act(
        harness, "evt-test-a-iso", decision, hours=1, account_ref=account, boundary=boundary
    )
    claim = {"source_action_event_id": "evt-test-a-iso"}
    post_created(
        harness,
        observation(
            "evt-test-o-iso", opened=acted, days=90, account_ref=account, opportunity=False, **claim
        ),
    )
    submit_attribution(harness, "evt-test-o-iso")
    post_created(
        harness,
        observation(
            "evt-test-o-iso-corrected",
            opened=acted,
            days=90,
            account_ref=account,
            opportunity=True,
            supersedes_outcome_event_id="evt-test-o-iso",
            **claim,
        ),
    )
    submit_attribution(harness, "evt-test-o-iso-corrected")

    assert read(harness, cutoff).as_dict() == before.as_dict()

    after = read(harness)
    assert after.population == before.population + 1
    assert facts(harness, decision).standing == STANDING_EVALUATED
    assert after.standings.evaluated == before.standings.evaluated + 1
    for name in ("unknown", "open", "unattributed"):
        assert getattr(after.standings, name) == getattr(before.standings, name)
    # One effective observation more (the correction replaces its predecessor).
    b, a = before.observations, after.observations
    assert (a.total, a.direct, a.closed_known, a.qualifying_90_day) == (
        b.total + 1,
        b.direct + 1,
        b.closed_known + 1,
        b.qualifying_90_day + 1,
    )
    for name in (
        "awaiting_attribution",
        "unresolved",
        "inferred",
        "open",
        "closed_unknown",
        "other_period",
    ):
        assert getattr(a, name) == getattr(b, name), name
    o_before, o_after = before.overall, after.overall
    assert (o_after.cohort_total, o_after.eligible, o_after.positives) == (
        o_before.cohort_total + 1,
        o_before.eligible + 1,
        o_before.positives + 1,
    )


def decision_metrics(result) -> tuple:
    """Every decision-level numerator, denominator and standing."""

    def pair(rate):
        return rate.cohort_total, rate.eligible, rate.positives

    return (
        result.population,
        result.standings,
        pair(result.overall),
        tuple((pair(r.comparison.present), pair(r.comparison.absent)) for r in result.signals),
        tuple((r.workflow_version, pair(r.rate), r.standings) for r in result.workflows),
        pair(result.workflow_comparison.comparison.present),
        pair(result.workflow_comparison.comparison.absent),
    )


def test_unresolved_positive_observations_never_enter_decision_metrics(harness):
    seed_dataset(harness, config=small_dataset_config())
    with harness.engine.connect() as conn:
        rows = conn.execute(
            select(actions.c.account_ref, actions.c.occurred_at).order_by(actions.c.account_ref)
        ).all()
    by_account: dict[str, list[str]] = {}
    for row in rows:
        by_account.setdefault(row.account_ref, []).append(row.occurred_at)
    singles = [(account, times[0]) for account, times in by_account.items() if len(times) == 1][:3]
    assert len(singles) == 3
    before = read(harness)

    # Each window opens 100 days after the account's only action, so the observation
    # instant is 190 days after it: outside the lookback, with no reference.
    posted = []
    for position, (account, acted) in enumerate(singles):
        outcome = f"evt-test-o-late-{position}"
        opened = format_utc(instant(acted) + timedelta(days=100))
        post_created(
            harness,
            observation(outcome, opened=opened, days=90, account_ref=account, opportunity=True),
        )
        submit_attribution(harness, outcome)
        posted.append(outcome)
    stored = {row.outcome_event_id: row.status for row in attribution_rows(harness)}
    assert [stored[outcome] for outcome in posted] == [STATUS_UNRESOLVED] * 3

    after = read(harness)
    assert after.observations.unresolved == before.observations.unresolved + 3
    assert after.observations.total == before.observations.total + 3
    assert after.observations.closed_known == before.observations.closed_known + 3
    assert decision_metrics(after) == decision_metrics(before)

    account, acted = singles[0]
    opened = format_utc(instant(acted) + timedelta(days=110))
    post_created(
        harness,
        observation(
            "evt-test-o-late-awaiting",
            opened=opened,
            days=90,
            account_ref=account,
            opportunity=True,
        ),
    )
    awaiting = read(harness)
    assert awaiting.observations.awaiting_attribution == after.observations.awaiting_attribution + 1
    assert awaiting.observations.unresolved == after.observations.unresolved
    assert decision_metrics(awaiting) == decision_metrics(after)


def test_a_late_attribution_to_a_superseded_outcome_is_excluded(harness):
    seed_and_attribute(harness)
    (root,) = attribution_rows(harness)
    canonical = canonical_by_type("outcome.evaluated")
    opened = canonical_by_type("action.recorded")["occurred_at"]
    post_created(
        harness,
        observation(
            "evt-test-o-canonical-closed",
            opened=opened,
            days=canonical["payload"]["window_days"],
            account_ref=ACCOUNT_REF,
            reply=False,
            meeting=False,
            opportunity=False,
            source_action_event_id=ACTION_EVENT_ID,
            supersedes_outcome_event_id=OUTCOME_EVENT_ID,
        ),
    )
    first = read(harness)
    assert facts(harness, DECISION_EVENT_ID).standing == STANDING_UNATTRIBUTED

    append_unrelated(harness)
    with harness.engine.connect() as conn:
        maximum = ledger_maximum(conn)
    replacement = attribution_envelope(
        harness,
        OUTCOME_EVENT_ID,
        cutoff=maximum,
        supersedes=root.attribution_event_id,
        clock=FixedClock(),
    )

    # D-016: this exact write is now refused through the public collector,
    # because OUTCOME_EVENT_ID has already been superseded by
    # evt-test-o-canonical-closed. Proven first, as a fresh submission under
    # this operation's own identity, before anything is stored under it.
    before = ledger_state(harness)
    response = harness.post(replacement)
    assert response.status_code == 422, response.json()
    body = response.json()
    assert body["reason"] == "attributed_outcome_version_is_superseded"
    assert body["outcome_event_id"] == OUTCOME_EVENT_ID
    assert body["effective_outcome_event_id"] == "evt-test-o-canonical-closed"
    assert ledger_state(harness) == before

    # The read-side proof this test exists for: a stale result *stored* in the
    # ledger (as it could be from before D-016, or by a direct correction)
    # stays excluded from every selection and changes no rendered value.
    # Planted as a coherent legacy row, labelled as such, because the
    # collector no longer accepts this write; this is legacy-row setup, not a
    # submission the collector would ever produce today.
    insert_legacy_attribution(harness, replacement)

    second = read(harness)
    without_cutoff = [
        {k: v for k, v in r.as_dict().items() if k != "cutoff"} for r in (first, second)
    ]
    assert without_cutoff[0] == without_cutoff[1]

    submit_attribution(harness, "evt-test-o-canonical-closed")
    decision = facts(harness, DECISION_EVENT_ID)
    assert [o.outcome_event_id for o in decision.observations] == ["evt-test-o-canonical-closed"]


def ledger_state(harness: Harness) -> tuple:
    """`harness.snapshot()` plus every stored event row (duplicated from
    `test_attribution_ingest.py`)."""
    from flight_recorder.ledger.schema import events

    with harness.engine.connect() as conn:
        stored = conn.execute(select(events).order_by(events.c.ingest_sequence)).all()
    return harness.snapshot(), [tuple(row) for row in stored]


def test_analytics_reads_write_nothing_and_replay_changes_no_aggregate(harness):
    seed_dataset(harness, config=small_dataset_config())
    before_state = ledger_state(harness)

    with captured_statements(harness.engine) as statements:
        before = read(harness)
    assert [s for s in statements if s.strip().upper().startswith("SELECT")]
    for statement in statements:
        assert statement.strip().upper().startswith(("SELECT", "BEGIN")), statement

    hashes = {version: canonical_hash(logic_artifact(version)) for version in ("v3.2", "v5.1")}
    other = {
        logic_artifact("v3.2")["logic_version"]: hashes["v5.1"],
        logic_artifact("v5.1")["logic_version"]: hashes["v3.2"],
    }
    with harness.engine.connect() as conn:
        rows = conn.execute(select(decisions.c.decision_event_id, decisions.c.logic_version)).all()
        for row in rows:
            replay(conn, row.decision_event_id, other[row.logic_version])  # raises on failure

    assert read(harness).as_dict() == before.as_dict()
    assert ledger_state(harness) == before_state
    assert set(SIGNAL_STATES) >= {
        state
        for row in rows
        for state in facts(harness, row.decision_event_id).signal_states.values()
    }
