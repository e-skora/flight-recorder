"""INV-08 / INV-02 for attribution: the lookback's boundaries, ties, and the snapshot.

1. **Boundaries and ties.** An action is eligible immediately inside 90 days
   and exactly at 90 days, and not immediately outside; an action at the
   observation instant is not before it; `Z` and offset spellings of the same
   instants decide identically. Two eligible actions at the identical instant
   resolve to the higher ingestion sequence -- through the collector in both
   ingestion orders, generatively over random ingestion orders, and as a pure
   property of the selection function under shuffled candidate order.
2. **The snapshot holds.** A result is unchanged by an action recorded after
   its cutoff, recomputing at the recorded cutoff still reproduces it, reason
   included, and the reasons for unusable claims are judged inside the
   snapshot too. A cutoff before the outcome and a cutoff that names no
   sequence are named failures, never `unresolved`. One command run keeps one
   cutoff for every outcome it processes.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.attribution import policy
from flight_recorder.collector.schema import format_utc
from tests.conftest import (
    ACCOUNT_REF,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    FixedClock,
    Harness,
    action_envelope,
    append_unrelated,
    attribute_at,
    attribute_ledger,
    attribution_envelope,
    attribution_rows,
    max_sequence,
    outcome_v2_envelope,
    post_created,
    seed_all,
    seed_through_decision,
)

pytestmark = pytest.mark.invariant

ACTION_AT = "2026-04-20T00:00:00Z"
ACTION_ID = "evt-test-boundary-action"
CLAIMED = "evt-test-o-claimed"
UNCLAIMED = "evt-test-o-unclaimed"


def shifted(text: str, **delta) -> str:
    return format_utc(datetime.fromisoformat(text) + timedelta(**delta))


def spelled(text: str, hours: int) -> str:
    """The same instant in another UTC offset."""
    return datetime.fromisoformat(text).astimezone(timezone(timedelta(hours=hours))).isoformat()


def observation(event_id: str, observed_at: str, **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at=observed_at,
        window_opened_at=ACTION_AT,
        window_closes_at=shifted(ACTION_AT, days=365),
        evaluation_state="open",
        **payload,
    )


def fallback(claims: str, resolution: str) -> str:
    return f"{claims}{policy.SEGMENT_SEPARATOR}{resolution}"


def both_outcomes(harness: Harness, *, action_at: str, observed_at: str) -> None:
    """One action, then one outcome claiming it and one claiming nothing."""
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(ACTION_ID, occurred_at=action_at),
        observation(CLAIMED, observed_at, source_action_event_id=ACTION_ID),
        observation(UNCLAIMED, observed_at),
    )


def assert_eligible(harness: Harness) -> None:
    claimed, unclaimed = attribute_at(harness, CLAIMED), attribute_at(harness, UNCLAIMED)
    assert (claimed.status, claimed.reason) == (policy.STATUS_DIRECT, policy.VALID_SOURCE_ACTION)
    assert claimed.resolved_action_event_id == ACTION_ID
    assert claimed.resolved_decision_event_id == DECISION_EVENT_ID
    assert unclaimed.status == policy.STATUS_INFERRED
    assert unclaimed.reason == fallback(
        policy.NO_SOURCE_REFERENCE, policy.MOST_RECENT_ELIGIBLE_ACTION
    )
    assert unclaimed.resolved_action_event_id == ACTION_ID


def assert_ineligible(harness: Harness, why: str) -> None:
    claimed, unclaimed = attribute_at(harness, CLAIMED), attribute_at(harness, UNCLAIMED)
    assert claimed.status == unclaimed.status == policy.STATUS_UNRESOLVED
    assert claimed.reason == fallback(why, policy.NO_ELIGIBLE_ACTION)
    assert unclaimed.reason == fallback(policy.NO_SOURCE_REFERENCE, policy.NO_ELIGIBLE_ACTION)
    assert claimed.resolved_action_event_id is None
    assert unclaimed.resolved_action_event_id is None


# --- 1. The lookback boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "delta,eligible",
    [
        pytest.param(timedelta(milliseconds=-1), True, id="immediately-inside-90-days"),
        pytest.param(timedelta(0), True, id="exactly-90-days-inclusive"),
        pytest.param(timedelta(milliseconds=1), False, id="immediately-outside-90-days"),
    ],
)
def test_the_lookback_is_inclusive_at_exactly_90_days(harness, delta, eligible):
    observed = format_utc(datetime.fromisoformat(ACTION_AT) + policy.LOOKBACK + delta)
    both_outcomes(harness, action_at=ACTION_AT, observed_at=observed)
    if eligible:
        assert_eligible(harness)
    else:
        assert_ineligible(harness, policy.SOURCE_ACTION_OUTSIDE_LOOKBACK)

    # The persisted results, through the collector, agree with the policy.
    run = attribute_ledger(harness)
    assert [s.http_status for s in run.submissions] == [201, 201]
    for row in attribution_rows(harness):
        expected = attribute_at(harness, row.outcome_event_id, row.ingest_cutoff)
        assert policy.same_policy_result(row, expected)


def test_an_action_at_the_observation_instant_is_not_before_it(harness):
    both_outcomes(harness, action_at=ACTION_AT, observed_at=ACTION_AT)
    assert_ineligible(harness, policy.SOURCE_ACTION_NOT_BEFORE_OBSERVATION)


def test_an_action_one_microsecond_before_the_observation_is_before_it(harness):
    both_outcomes(harness, action_at=ACTION_AT, observed_at=shifted(ACTION_AT, microseconds=1))
    assert_eligible(harness)


@pytest.mark.parametrize("action_hours,observed_hours", [(-5, 2), (9, -11), (0, 13)])
@pytest.mark.parametrize(
    "delta,eligible",
    [
        pytest.param(timedelta(0), True, id="exactly-90-days"),
        pytest.param(timedelta(milliseconds=1), False, id="outside"),
    ],
)
def test_offset_spellings_of_the_same_instants_decide_identically(
    harness, action_hours, observed_hours, delta, eligible
):
    observed = format_utc(datetime.fromisoformat(ACTION_AT) + policy.LOOKBACK + delta)
    both_outcomes(
        harness,
        action_at=spelled(ACTION_AT, action_hours),
        observed_at=spelled(observed, observed_hours),
    )
    if eligible:
        assert_eligible(harness)
    else:
        assert_ineligible(harness, policy.SOURCE_ACTION_OUTSIDE_LOOKBACK)


# --- 1. Ties ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [("evt-test-tie-x", "evt-test-tie-y"), ("evt-test-tie-y", "evt-test-tie-x")],
)
def test_two_eligible_actions_at_one_instant_resolve_to_the_higher_ingest_sequence(
    harness, first, second
):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(first, occurred_at=ACTION_AT),
        # The same instant, spelled differently.
        action_envelope(second, occurred_at=spelled(ACTION_AT, -7)),
        observation(UNCLAIMED, shifted(ACTION_AT, days=10)),
    )
    assert attribute_at(harness, UNCLAIMED).resolved_action_event_id == second


@st.composite
def tied_ledgers(draw):
    """Up to five eligible actions over two instants, in a random ingestion order."""
    count = draw(st.integers(min_value=2, max_value=5))
    instants = [draw(st.sampled_from([0, 0, 1])) for _ in range(count)]
    order = draw(st.permutations(range(count)))
    return instants, list(order)


@given(tied_ledgers())
def test_ingestion_sequence_decides_ties_whatever_the_ingestion_order(tmp_path_factory, case):
    instants, order = case
    harness = Harness(tmp_path_factory.mktemp("inv08-ties"))
    seed_through_decision(harness)
    sequence = {}
    for index in order:
        action_id = f"evt-test-tie-{index}"
        post_created(
            harness,
            action_envelope(action_id, occurred_at=shifted(ACTION_AT, seconds=instants[index])),
        )
        sequence[action_id] = max_sequence(harness)
    post_created(harness, observation(UNCLAIMED, shifted(ACTION_AT, days=10)))

    expected = max(sequence, key=lambda a: (instants[int(a.rsplit("-", 1)[1])], sequence[a]))
    result = attribute_at(harness, UNCLAIMED)
    assert result.status == policy.STATUS_INFERRED
    assert result.resolved_action_event_id == expected


def _candidate(action_id: str, second: int, sequence: int) -> policy.ActionCandidate:
    return policy.ActionCandidate(
        action_event_id=action_id,
        account_ref=ACCOUNT_REF,
        decision_event_id=DECISION_EVENT_ID,
        status="sent",
        occurred_at=shifted(ACTION_AT, seconds=second),
        ingest_sequence=sequence,
        decision_class=policy.ATTRIBUTABLE_DECISION_CLASS,
        decision_account_ref=ACCOUNT_REF,
    )


@given(
    st.lists(
        st.tuples(st.integers(0, 3), st.integers(1, 10_000)),
        min_size=1,
        max_size=8,
        unique_by=lambda pair: pair[1],
    ),
    st.randoms(use_true_random=False),
)
def test_candidate_iteration_order_never_changes_the_selection(pairs, rng: random.Random):
    candidates = [_candidate(f"a-{seq}", second, seq) for second, seq in pairs]
    rule = sorted(candidates, key=lambda c: (c.occurred_at, c.ingest_sequence))[-1]
    for _ in range(4):
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        assert policy.most_recent_eligible(shuffled) == rule
        assert policy.most_recent_eligible(iter(shuffled)) == rule


# --- 2. The snapshot holds --------------------------------------------------------


def test_an_action_recorded_after_the_cutoff_changes_no_persisted_result(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(ACTION_ID, occurred_at="2026-04-18T09:00:00Z"),
        observation(UNCLAIMED, "2026-05-01T00:00:00Z"),
    )
    attribute_ledger(harness)
    (before,) = attribution_rows(harness)
    assert before.resolved_action_event_id == ACTION_ID

    # Occurred before the observation and inside the window, recorded afterwards.
    post_created(
        harness,
        action_envelope(
            "evt-test-late-recorded-action",
            occurred_at="2026-04-25T09:00:00Z",
            recorded_at="2026-09-01T00:00:00Z",
        ),
    )
    append_unrelated(harness, 2)

    (after,) = attribution_rows(harness)
    assert tuple(after) == tuple(before)
    reproduced = attribute_at(harness, UNCLAIMED, before.ingest_cutoff)
    assert policy.same_policy_result(reproduced, before)
    assert reproduced.reason == before.reason
    assert reproduced.cutoff == before.ingest_cutoff
    # The present-day ledger would answer differently; the snapshot does not.
    assert attribute_at(harness, UNCLAIMED).resolved_action_event_id == (
        "evt-test-late-recorded-action"
    )


def test_an_unusable_claims_reason_is_judged_inside_the_snapshot(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(ACTION_ID, occurred_at="2026-04-18T09:00:00Z"),
        observation(
            CLAIMED, "2026-05-01T00:00:00Z", source_action_event_id="evt-test-not-yet-recorded"
        ),
    )
    attribute_ledger(harness)
    (before,) = attribution_rows(harness)
    assert before.reason == fallback(
        policy.SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF, policy.MOST_RECENT_ELIGIBLE_ACTION
    )

    # The claimed action is recorded later; it would now resolve directly.
    post_created(
        harness,
        action_envelope("evt-test-not-yet-recorded", occurred_at="2026-04-26T09:00:00Z"),
    )
    now = attribute_at(harness, CLAIMED)
    assert (now.status, now.reason) == (policy.STATUS_DIRECT, policy.VALID_SOURCE_ACTION)

    then = attribute_at(harness, CLAIMED, before.ingest_cutoff)
    assert policy.same_policy_result(then, before)
    assert then.reason == before.reason
    (after,) = attribution_rows(harness)
    assert tuple(after) == tuple(before)


def test_a_cutoff_before_the_outcome_is_a_named_failure_not_unresolved(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    outcome_sequence = max_sequence(harness)

    with pytest.raises(policy.CutoffExcludesOutcome) as failure:
        attribute_at(harness, OUTCOME_EVENT_ID, outcome_sequence - 1)
    assert failure.value.reason == "ingest_cutoff_excludes_the_outcome"
    assert failure.value.outcome_sequence == outcome_sequence

    # At the collector: the same failure, named, and nothing written.
    envelope = attribution_envelope(harness, OUTCOME_EVENT_ID)
    envelope["payload"]["ingest_cutoff"] = outcome_sequence - 1
    envelope["event_id"] = policy.attribution_event_id(
        OUTCOME_EVENT_ID, policy.POLICY_VERSION, outcome_sequence - 1
    )
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422, response.json()
    assert response.json()["reason"] == "ingest_cutoff_excludes_the_outcome"
    assert harness.snapshot() == before


@pytest.mark.parametrize("cutoff", [0, -3])
def test_a_cutoff_naming_no_sequence_is_rejected(harness, cutoff):
    for response in seed_all(harness):
        assert response.status_code == 201
    with pytest.raises(policy.CutoffNotFound):
        attribute_at(harness, OUTCOME_EVENT_ID, cutoff)

    envelope = attribution_envelope(harness, OUTCOME_EVENT_ID)
    envelope["payload"]["ingest_cutoff"] = cutoff
    envelope["event_id"] = policy.attribution_event_id(
        OUTCOME_EVENT_ID, policy.POLICY_VERSION, cutoff
    )
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == 422, response.json()
    assert response.json()["reason"] == "ingest_cutoff_not_found"
    assert harness.snapshot() == before


def test_one_run_keeps_one_cutoff_for_every_outcome_it_processes(harness):
    """The run's own attribution events raise the ledger maximum between
    outcomes; the later outcome is still evaluated at the run's first cutoff."""
    for response in seed_all(harness):
        assert response.status_code == 201
    post_created(
        harness,
        observation("evt-test-o-second", "2026-05-01T00:00:00Z"),
        observation("evt-test-o-third", "2026-05-02T00:00:00Z"),
    )
    cutoff = max_sequence(harness)

    run = attribute_ledger(harness, clock=FixedClock())
    assert run.cutoff == cutoff
    assert [s.http_status for s in run.submissions] == [201, 201, 201]
    assert max_sequence(harness) == cutoff + 3

    rows = attribution_rows(harness)
    assert [row.outcome_event_id for row in rows] == [
        OUTCOME_EVENT_ID,
        "evt-test-o-second",
        "evt-test-o-third",
    ]
    for row in rows:
        assert row.ingest_cutoff == cutoff
        assert row.attribution_event_id == policy.attribution_event_id(
            row.outcome_event_id, policy.POLICY_VERSION, cutoff
        )
        assert policy.same_policy_result(row, attribute_at(harness, row.outcome_event_id, cutoff))
