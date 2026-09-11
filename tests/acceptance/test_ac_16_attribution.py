"""AC-16 (the portion 4A closes), INV-08, INV-10: `outcome-attribution-v1`.

Every outcome, action and decision below enters through the collector, and
every persisted result is produced by the attribution command over the same
in-process HTTP boundary. Each case asserts the resolved identities and the
machine-stable reason, not only the status.

1. The seven reference shapes: direct action reference, direct decision-only
   reference, both references agreeing, no reference, an unusable reference,
   the inferred fallback and unresolved; plus precedence of a valid explicit
   reference over a more recent eligible action, and the v1 source-claim
   mapping.
2. Nothing forged earns direct credit.
3. The canonical attribution after `reset && seed && attribute`, driven
   through the console command.
4. Replay separation: the attribution targets the original recorded decision
   and action whatever replay computes, and nothing counterfactual persists.
"""

import pytest
from sqlalchemy import inspect, select

from flight_recorder.attribution import policy
from flight_recorder.attribution.service import build_envelope
from flight_recorder.cli import main
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import ATTRIBUTION_SOURCE
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import actions, events, outcome_attributions, outcomes
from tests.acceptance.test_decision_detail_page import decision_url, element, rows
from tests.conftest import (
    ACCOUNT_REF,
    ACTION_EVENT_ID,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    FixedClock,
    action_envelope,
    attribute_at,
    attribute_ledger,
    attribution_rows,
    canonical_by_type,
    decision_copy_envelope,
    discovery_envelope,
    logic_artifact,
    outcome_row,
    outcome_v1_envelope,
    outcome_v2_envelope,
    post_created,
    replay_under,
    seed_all,
    seed_and_attribute,
    seed_through_decision,
    v5_1_hash,
)

WINDOW_OPENED = canonical_by_type("action.recorded")["occurred_at"]
WINDOW_CLOSES = canonical_by_type("outcome.evaluated")["occurred_at"]
OBSERVED = "2026-05-01T00:00:00Z"

LATER_ACTION_ID = "evt-test-action-later"
FAILED_ACTION_ID = "evt-test-action-failed"
SECOND_DECISION_ID = "evt-test-decision-second"
LATE_DECISION_ID = "evt-test-decision-after-observation"
OTHER_ACCOUNT = "other-account"


def observation(event_id: str, *, observed_at: str = OBSERVED, **payload) -> dict:
    """A v2 open-window observation of the canonical account."""
    return outcome_v2_envelope(
        event_id,
        observed_at=observed_at,
        window_opened_at=WINDOW_OPENED,
        window_closes_at=WINDOW_CLOSES,
        evaluation_state="open",
        **payload,
    )


def effective(harness, outcome_event_id: str):
    with harness.engine.connect() as conn:
        return policy.effective_attribution(
            conn,
            outcome_event_id,
            policy.POLICY_VERSION,
            cutoff=policy.ledger_maximum(conn),
        )


def attributed(harness, outcome_event_id: str):
    """Run the command, then the persisted effective result for one outcome."""
    run = attribute_ledger(harness)
    assert not run.failed, [s.body for s in run.failed]
    stored = effective(harness, outcome_event_id)
    assert stored is not None, outcome_event_id
    return stored


def assert_result(stored, *, status, method, reason, action, decision) -> None:
    assert stored.policy_version == policy.POLICY_VERSION
    assert stored.window_days == policy.LOOKBACK_DAYS == 90
    assert stored.status == status
    assert stored.method == method
    assert stored.reason == reason
    assert stored.resolved_action_event_id == action
    assert stored.resolved_decision_event_id == decision
    assert stored.heuristic is (method in policy.HEURISTIC_METHODS)


def fallback(claims: str, resolution: str) -> str:
    return f"{claims}{policy.SEGMENT_SEPARATOR}{resolution}"


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


# --- 1. The seven reference shapes ---------------------------------------------


def test_a_direct_action_reference_resolves_to_that_action_and_its_decision(seeded):
    post_created(seeded, observation("evt-test-o-action", source_action_event_id=ACTION_EVENT_ID))
    stored = attributed(seeded, "evt-test-o-action")
    assert_result(
        stored,
        status=policy.STATUS_DIRECT,
        method=policy.METHOD_EXPLICIT_REFERENCE,
        reason=policy.VALID_SOURCE_ACTION,
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )
    assert outcome_row(seeded, "evt-test-o-action").source_action_unusable_reason is None


def test_a_decision_only_reference_resolves_directly_without_inventing_an_action(seeded):
    post_created(
        seeded, observation("evt-test-o-decision", source_decision_event_id=DECISION_EVENT_ID)
    )
    stored = attributed(seeded, "evt-test-o-decision")
    assert_result(
        stored,
        status=policy.STATUS_DIRECT,
        method=policy.METHOD_EXPLICIT_REFERENCE,
        reason=policy.VALID_SOURCE_DECISION,
        action=None,
        decision=DECISION_EVENT_ID,
    )


def test_both_references_agreeing_resolve_directly_to_both(seeded):
    post_created(
        seeded,
        observation(
            "evt-test-o-both",
            source_action_event_id=ACTION_EVENT_ID,
            source_decision_event_id=DECISION_EVENT_ID,
        ),
    )
    stored = attributed(seeded, "evt-test-o-both")
    assert_result(
        stored,
        status=policy.STATUS_DIRECT,
        method=policy.METHOD_EXPLICIT_REFERENCE,
        reason=policy.VALID_SOURCE_ACTION_AND_DECISION,
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )


def test_an_observation_with_no_reference_at_all_is_accepted_and_falls_back(seeded):
    post_created(seeded, observation("evt-test-o-none"))
    row = outcome_row(seeded, "evt-test-o-none")
    assert (row.source_action_event_id, row.source_decision_event_id) == (None, None)
    assert (row.action_event_id, row.window_days) == (None, None)

    stored = attributed(seeded, "evt-test-o-none")
    assert_result(
        stored,
        status=policy.STATUS_INFERRED,
        method=policy.METHOD_HEURISTIC,
        reason=fallback(policy.NO_SOURCE_REFERENCE, policy.MOST_RECENT_ELIGIBLE_ACTION),
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )
    assert stored.heuristic


@pytest.mark.parametrize(
    "claims,action_reason,decision_reason",
    [
        pytest.param(
            {"source_action_event_id": "evt-no-such-action"},
            policy.SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF,
            None,
            id="unknown-action",
        ),
        pytest.param(
            {"source_decision_event_id": "evt-no-such-decision"},
            None,
            policy.SOURCE_DECISION_NOT_RECORDED_BY_CUTOFF,
            id="unknown-decision",
        ),
        pytest.param(
            # Well-formed, but an outcome event rather than an action.
            {"source_action_event_id": OUTCOME_EVENT_ID},
            policy.SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF,
            None,
            id="not-an-action",
        ),
    ],
)
def test_an_unusable_reference_is_retained_with_a_reason_and_the_fallback_applies(
    seeded, claims, action_reason, decision_reason
):
    post_created(seeded, observation("evt-test-o-unusable", **claims))
    row = outcome_row(seeded, "evt-test-o-unusable")
    # The claim is stored unmodified, beside the reason it was unusable at ingest.
    assert row.source_action_event_id == claims.get("source_action_event_id")
    assert row.source_decision_event_id == claims.get("source_decision_event_id")
    assert row.source_action_unusable_reason == action_reason
    assert row.source_decision_unusable_reason == decision_reason

    stored = attributed(seeded, "evt-test-o-unusable")
    assert_result(
        stored,
        status=policy.STATUS_INFERRED,
        method=policy.METHOD_HEURISTIC,
        reason=fallback(action_reason or decision_reason, policy.MOST_RECENT_ELIGIBLE_ACTION),
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )


def test_the_inferred_fallback_takes_the_most_recent_eligible_action(seeded):
    post_created(
        seeded,
        action_envelope(LATER_ACTION_ID, occurred_at="2026-04-20T09:00:00Z", status="completed"),
        observation("evt-test-o-fallback"),
    )
    stored = attributed(seeded, "evt-test-o-fallback")
    assert_result(
        stored,
        status=policy.STATUS_INFERRED,
        method=policy.METHOD_HEURISTIC,
        reason=fallback(policy.NO_SOURCE_REFERENCE, policy.MOST_RECENT_ELIGIBLE_ACTION),
        action=LATER_ACTION_ID,
        decision=DECISION_EVENT_ID,
    )


def test_with_no_reference_and_no_eligible_action_the_outcome_is_unresolved(harness):
    seed_through_decision(harness)
    post_created(harness, observation("evt-test-o-unresolved"))
    stored = attributed(harness, "evt-test-o-unresolved")
    assert_result(
        stored,
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.NO_SOURCE_REFERENCE, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )
    assert not stored.heuristic


def test_a_valid_explicit_reference_wins_over_a_more_recent_eligible_action(seeded):
    post_created(
        seeded,
        action_envelope(LATER_ACTION_ID, occurred_at="2026-04-20T09:00:00Z"),
        observation("evt-test-o-explicit", source_action_event_id=ACTION_EVENT_ID),
        observation("evt-test-o-implicit"),
    )
    run = attribute_ledger(seeded)
    assert not run.failed

    # The fallback would take the later action...
    assert effective(seeded, "evt-test-o-implicit").resolved_action_event_id == LATER_ACTION_ID
    # ...but an explicit valid reference to the older one takes precedence.
    assert_result(
        effective(seeded, "evt-test-o-explicit"),
        status=policy.STATUS_DIRECT,
        method=policy.METHOD_EXPLICIT_REFERENCE,
        reason=policy.VALID_SOURCE_ACTION,
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )


def test_a_v1_outcome_naming_a_failed_action_is_ingested_unchanged_and_never_direct(harness):
    """The v1 stored `action_event_id` is mapped into the policy as a source
    action claim. v1 ingest still accepts the reference exactly as before."""
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FAILED_ACTION_ID, occurred_at="2026-04-17T10:08:00Z", status="failed"),
    )
    v1 = outcome_v1_envelope(
        "evt-test-o-v1-failed", action_event_id=FAILED_ACTION_ID, occurred_at=OBSERVED
    )
    response = harness.post(v1)
    assert response.status_code == 201, response.json()
    row = outcome_row(harness, "evt-test-o-v1-failed")
    assert row.schema_version == "1"
    assert row.action_event_id == FAILED_ACTION_ID
    assert row.window_days == v1["payload"]["window_days"]
    assert row.source_action_event_id is None and row.source_action_unusable_reason is None

    # No other eligible action: unresolved, with the failed claim named.
    stored = attributed(harness, "evt-test-o-v1-failed")
    assert_result(
        stored,
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.SOURCE_ACTION_FAILED, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )

    # With an eligible action in the ledger the same claim falls back to it,
    # exactly as the equivalent v2 claim does.
    post_created(
        harness,
        action_envelope(LATER_ACTION_ID, occurred_at="2026-04-18T09:00:00Z"),
        outcome_v1_envelope(
            "evt-test-o-v1-failed-2", action_event_id=FAILED_ACTION_ID, occurred_at=OBSERVED
        ),
        observation("evt-test-o-v2-failed", source_action_event_id=FAILED_ACTION_ID),
    )
    attribute_ledger(harness)
    v1_result = effective(harness, "evt-test-o-v1-failed-2")
    v2_result = effective(harness, "evt-test-o-v2-failed")
    for stored in (v1_result, v2_result):
        assert_result(
            stored,
            status=policy.STATUS_INFERRED,
            method=policy.METHOD_HEURISTIC,
            reason=fallback(policy.SOURCE_ACTION_FAILED, policy.MOST_RECENT_ELIGIBLE_ACTION),
            action=LATER_ACTION_ID,
            decision=DECISION_EVENT_ID,
        )


def test_a_v1_outcome_naming_an_action_older_than_the_lookback_is_not_direct(harness):
    seed_through_decision(harness)
    post_created(harness, action_envelope(LATER_ACTION_ID, occurred_at="2026-04-17T10:07:00Z"))
    long_after = "2026-07-16T10:07:00.001000Z"  # 90 days and one millisecond later
    post_created(
        harness,
        outcome_v1_envelope(
            "evt-test-o-v1-stale", action_event_id=LATER_ACTION_ID, occurred_at=long_after
        ),
    )
    assert_result(
        attributed(harness, "evt-test-o-v1-stale"),
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.SOURCE_ACTION_OUTSIDE_LOOKBACK, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )


# --- 2. Nothing forged earns direct credit -------------------------------------


def test_a_failed_action_reference_never_earns_direct_credit(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FAILED_ACTION_ID, occurred_at="2026-04-17T10:08:00Z", status="failed"),
        observation("evt-test-o-failed", source_action_event_id=FAILED_ACTION_ID),
    )
    assert outcome_row(harness, "evt-test-o-failed").source_action_unusable_reason == (
        policy.SOURCE_ACTION_FAILED
    )
    stored = attributed(harness, "evt-test-o-failed")
    assert stored.status != policy.STATUS_DIRECT
    assert_result(
        stored,
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.SOURCE_ACTION_FAILED, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )


def test_references_into_another_account_never_earn_direct_credit(seeded):
    post_created(
        seeded,
        discovery_envelope(OTHER_ACCOUNT),
        observation(
            "evt-test-o-cross-account",
            account_ref=OTHER_ACCOUNT,
            source_action_event_id=ACTION_EVENT_ID,
            source_decision_event_id=DECISION_EVENT_ID,
        ),
    )
    row = outcome_row(seeded, "evt-test-o-cross-account")
    assert row.source_action_unusable_reason == policy.SOURCE_ACTION_OTHER_ACCOUNT
    assert row.source_decision_unusable_reason == policy.SOURCE_DECISION_OTHER_ACCOUNT

    stored = attributed(seeded, "evt-test-o-cross-account")
    assert stored.account_ref == OTHER_ACCOUNT
    assert_result(
        stored,
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(
            policy.CLAIM_SEPARATOR.join(
                (policy.SOURCE_ACTION_OTHER_ACCOUNT, policy.SOURCE_DECISION_OTHER_ACCOUNT)
            ),
            policy.NO_ELIGIBLE_ACTION,
        ),
        action=None,
        decision=None,
    )


def test_a_nonexistent_decision_target_never_earns_direct_credit(harness):
    seed_through_decision(harness)
    post_created(
        harness, observation("evt-test-o-ghost", source_decision_event_id="evt-no-such-decision")
    )
    assert_result(
        attributed(harness, "evt-test-o-ghost"),
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.SOURCE_DECISION_NOT_RECORDED_BY_CUTOFF, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )


def test_contradictory_references_never_earn_direct_credit(seeded):
    """Both claims resolve individually, but the action records a different decision."""
    post_created(
        seeded,
        decision_copy_envelope(SECOND_DECISION_ID, boundary="2026-04-18T00:00:00Z"),
        observation(
            "evt-test-o-contradictory",
            source_action_event_id=ACTION_EVENT_ID,
            source_decision_event_id=SECOND_DECISION_ID,
        ),
    )
    row = outcome_row(seeded, "evt-test-o-contradictory")
    # Each claim was usable alone; the disagreement is the policy's finding.
    assert row.source_action_unusable_reason is None
    assert row.source_decision_unusable_reason is None

    stored = attributed(seeded, "evt-test-o-contradictory")
    assert_result(
        stored,
        status=policy.STATUS_INFERRED,
        method=policy.METHOD_HEURISTIC,
        reason=fallback(policy.SOURCE_REFERENCES_DISAGREE, policy.MOST_RECENT_ELIGIBLE_ACTION),
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )


def test_a_valid_decision_paired_with_its_failed_action_is_not_salvaged_into_decision_credit(
    harness,
):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FAILED_ACTION_ID, occurred_at="2026-04-17T10:08:00Z", status="failed"),
        observation(
            "evt-test-o-pair-failed",
            source_action_event_id=FAILED_ACTION_ID,
            source_decision_event_id=DECISION_EVENT_ID,
        ),
    )
    row = outcome_row(harness, "evt-test-o-pair-failed")
    assert row.source_decision_unusable_reason is None  # the decision alone was valid

    stored = attributed(harness, "evt-test-o-pair-failed")
    assert stored.status != policy.STATUS_DIRECT
    assert stored.resolved_decision_event_id is None
    assert_result(
        stored,
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.SOURCE_ACTION_FAILED, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )


@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param("2026-05-02T00:00:00Z", id="after-the-observation"),
        pytest.param(OBSERVED, id="at-the-observation-instant"),
    ],
)
def test_a_decision_not_before_the_observation_never_yields_direct_credit(seeded, boundary):
    """Both records exist at the cutoff; the decision still happened too late."""
    post_created(
        seeded,
        decision_copy_envelope(LATE_DECISION_ID, boundary=boundary),
        observation("evt-test-o-late-decision", source_decision_event_id=LATE_DECISION_ID),
    )
    stored = attributed(seeded, "evt-test-o-late-decision")
    with seeded.engine.connect() as conn:
        late_sequence = conn.execute(
            select(events.c.ingest_sequence).where(events.c.event_id == LATE_DECISION_ID)
        ).scalar_one()
    assert late_sequence <= stored.ingest_cutoff
    assert_result(
        stored,
        status=policy.STATUS_INFERRED,
        method=policy.METHOD_HEURISTIC,
        reason=fallback(
            policy.SOURCE_DECISION_NOT_BEFORE_OBSERVATION, policy.MOST_RECENT_ELIGIBLE_ACTION
        ),
        action=ACTION_EVENT_ID,
        decision=DECISION_EVENT_ID,
    )


def test_the_fallback_never_selects_a_failed_action(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(LATER_ACTION_ID, occurred_at="2026-04-18T09:00:00Z"),
        # More recent than the eligible action, and failed.
        action_envelope(FAILED_ACTION_ID, occurred_at="2026-04-25T09:00:00Z", status="failed"),
        observation("evt-test-o-skip-failed"),
    )
    assert attributed(harness, "evt-test-o-skip-failed").resolved_action_event_id == (
        LATER_ACTION_ID
    )


def test_the_fallback_with_only_failed_actions_is_unresolved(harness):
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FAILED_ACTION_ID, occurred_at="2026-04-25T09:00:00Z", status="failed"),
        observation("evt-test-o-only-failed"),
    )
    assert_result(
        attributed(harness, "evt-test-o-only-failed"),
        status=policy.STATUS_UNRESOLVED,
        method=policy.METHOD_UNRESOLVED,
        reason=fallback(policy.NO_SOURCE_REFERENCE, policy.NO_ELIGIBLE_ACTION),
        action=None,
        decision=None,
    )


@pytest.mark.parametrize(
    "field,target,reason",
    [
        pytest.param(
            "resolved_action_event_id", "evt-invented-action", "unknown_resolved_action", id="a"
        ),
        # A recorded event, but an outcome rather than an action.
        pytest.param(
            "resolved_action_event_id", OUTCOME_EVENT_ID, "unknown_resolved_action", id="b"
        ),
        pytest.param(
            "resolved_decision_event_id",
            "evt-invented-decision",
            "unknown_resolved_decision",
            id="c",
        ),
        # A recorded event, but a persona selection rather than a decision.
        pytest.param(
            "resolved_decision_event_id",
            canonical_by_type("persona.selected")["event_id"],
            "unknown_resolved_decision",
            id="d",
        ),
    ],
)
def test_an_attribution_naming_an_unrecorded_target_is_rejected_at_the_collector(
    seeded, field, target, reason
):
    result = attribute_at(seeded, OUTCOME_EVENT_ID)
    clock = FixedClock()
    envelope = build_envelope(
        result, account_ref=ACCOUNT_REF, attributed_at=clock(), recorded_at=clock()
    )
    envelope["payload"][field] = target
    before = seeded.snapshot()
    response = seeded.post(envelope)
    assert response.status_code == 422, response.json()
    assert response.json()["reason"] == reason
    assert target in response.json()["detail"]
    assert seeded.snapshot() == before


# --- 3. The canonical attribution ------------------------------------------------


def test_reset_seed_attribute_records_exactly_the_canonical_direct_result(
    tmp_path, capsys, monkeypatch
):
    """`PRODUCT.md` §7: the canonical outcome resolves `direct` to play #14's
    recorded action and through it to the recorded decision, and the result
    crosses the collector's HTTP route like every other event."""
    from flight_recorder.collector.service import Collector

    crossings: list[bytes] = []
    real_ingest_json = Collector.ingest_json

    def counting_ingest_json(self, body):
        crossings.append(body)
        return real_ingest_json(self, body)

    monkeypatch.setattr(Collector, "ingest_json", counting_ingest_json)
    db = str(tmp_path / "canonical.db")

    assert main(["--db", db, "reset"]) == 0
    assert main(["--db", db, "seed"]) == 0
    assert "seed: 9 created, 0 duplicate" in capsys.readouterr().out
    crossings.clear()

    assert main(["--db", db, "attribute"]) == 0
    out = capsys.readouterr().out
    assert "1 created" in out
    assert len(crossings) == 1  # the one result entered through the HTTP route

    engine = make_engine(db)
    with engine.connect() as conn:
        (row,) = conn.execute(select(outcome_attributions)).all()
        event = conn.execute(
            select(events).where(events.c.event_id == row.attribution_event_id)
        ).one()
        action = conn.execute(
            select(actions).where(actions.c.action_event_id == row.resolved_action_event_id)
        ).one()
        cutoff = conn.execute(
            select(events.c.ingest_sequence).where(events.c.event_id == OUTCOME_EVENT_ID)
        ).scalar_one()
        (outcome,) = conn.execute(select(outcomes)).all()

    assert row.outcome_event_id == OUTCOME_EVENT_ID
    assert row.status == policy.STATUS_DIRECT
    assert row.resolved_action_event_id == ACTION_EVENT_ID
    assert row.resolved_decision_event_id == DECISION_EVENT_ID
    assert action.play_id == canonical_by_type("action.recorded")["payload"]["play_id"]
    assert action.decision_event_id == DECISION_EVENT_ID
    assert (row.policy_version, row.method, row.reason) == (
        policy.POLICY_VERSION,
        policy.METHOD_EXPLICIT_REFERENCE,
        policy.VALID_SOURCE_ACTION,
    )
    assert row.window_days == 90
    # The canonical outcome is the last canonical event, so the run's cutoff is it.
    assert row.ingest_cutoff == cutoff
    assert row.attribution_event_id == policy.attribution_event_id(
        OUTCOME_EVENT_ID, policy.POLICY_VERSION, cutoff
    )
    assert event.event_type == "outcome.attributed"
    assert event.source == ATTRIBUTION_SOURCE
    assert event.account_ref == ACCOUNT_REF
    assert event.occurred_at == row.attributed_at
    assert outcome.schema_version == "1"

    # Seed counts are unchanged, and a second run creates nothing.
    assert main(["--db", db, "seed"]) == 0
    assert "seed: 0 created, 9 duplicate" in capsys.readouterr().out
    assert main(["--db", db, "attribute"]) == 0
    assert "0 created" in capsys.readouterr().out
    with engine.connect() as conn:
        assert len(conn.execute(select(outcome_attributions)).all()) == 1


# --- 4. Replay separation (INV-06, INV-10) ---------------------------------------


def test_replay_under_current_logic_changes_no_attribution_and_persists_nothing(harness):
    """The replay flips the decision's output; the attribution still targets the
    original recorded decision and action, the page's attribution is identical
    with or without the flip, and no counterfactual is stored anywhere. With the
    collector's refusal of an unrecorded resolved target (group 2), this is the
    reachable form of "actual outcomes are never attributed to counterfactual
    decisions or actions"."""
    seed_and_attribute(harness)
    (before,) = attribution_rows(harness)
    snapshot_before = harness.snapshot()

    counterfactual = replay_under(harness, v5_1_hash())
    assert counterfactual.result.output == "DO_NOT_PRIORITIZE"
    assert counterfactual.original.result.output == "PRIORITIZE"

    v3_2_hash = canonical_hash(logic_artifact("v3.2"))
    flipped = harness.client.get(decision_url() + f"?current={v5_1_hash()}")
    unflipped = harness.client.get(decision_url() + f"?current={v3_2_hash}")
    assert flipped.status_code == unflipped.status_code == 200
    assert element(flipped.text, "counterfactual-output") == "DO_NOT_PRIORITIZE"
    assert element(unflipped.text, "counterfactual-output") == "PRIORITIZE"
    assert element(flipped.text, "outcomes") == element(unflipped.text, "outcomes")
    cell = rows(flipped.text, "outcomes-table")[0][5]
    assert cell.startswith(policy.STATUS_DIRECT)
    assert ACTION_EVENT_ID in cell and DECISION_EVENT_ID in cell
    assert "the decision on this page" in cell

    (after,) = attribution_rows(harness)
    assert tuple(after) == tuple(before)
    assert after.resolved_decision_event_id == DECISION_EVENT_ID
    assert after.resolved_action_event_id == ACTION_EVENT_ID
    assert harness.snapshot() == snapshot_before
    names = set(inspect(harness.engine).get_table_names())
    assert not [n for n in names if "counterfactual" in n or "replay" in n]
    with harness.engine.connect() as conn:
        kinds = {row.event_type for row in conn.execute(select(events.c.event_type))}
    assert not [kind for kind in kinds if "counterfactual" in kind or "replay" in kind]
