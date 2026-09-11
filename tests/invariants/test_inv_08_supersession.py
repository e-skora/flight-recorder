"""INV-08, INV-01, AC-10: corrections append, supersession is linear, selection is exact.

1. Closing or correcting an outcome appends a linked version. Every earlier
   outcome row, attribution row and decision field is unchanged, and so are
   the original reconstruction and the counterfactual replay.
2. A corrected outcome with no attribution of its own does not inherit its
   predecessor's credit; the two selection operations return exactly one row
   each and never count a chain twice.
3. The collector rejects, atomically, every shape that would make selection
   ambiguous: a second root, a duplicate operation under a new event id, an
   underived event id, a stale predecessor, a branching replacement, cross-
   outcome and cross-policy links, and a second replacement of one outcome
   version. A ledger corrupted around the collector makes selection raise
   rather than pick.
"""

import pytest

from flight_recorder.attribution import policy
from flight_recorder.replay.reconstruct import reconstruct
from tests.conftest import (
    ACCOUNT_REF,
    ACTION_EVENT_ID,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    Harness,
    append_unrelated,
    assert_same_counterfactual,
    assert_same_reconstruction,
    attribute_ledger,
    attribution_envelope,
    attribution_rows,
    canonical_by_type,
    decision_rows,
    discovery_envelope,
    insert_ambiguous_attribution,
    outcome_row,
    outcome_v2_envelope,
    post_created,
    replay_under,
    seed_and_attribute,
    v5_1_hash,
)

pytestmark = pytest.mark.invariant

WINDOW_OPENED = canonical_by_type("action.recorded")["occurred_at"]
WINDOW_CLOSES = canonical_by_type("outcome.evaluated")["occurred_at"]
CLOSING_ID = "evt-test-o-closing"
TEST_POLICY = "outcome-attribution-test"


def closing_version(event_id: str = CLOSING_ID, supersedes: str = OUTCOME_EVENT_ID, **extra):
    """A v2 closed observation replacing `supersedes`, as of the window's close."""
    payload = {
        "reply": False,
        "meeting": False,
        "opportunity": False,
        "source_action_event_id": ACTION_EVENT_ID,
        "supersedes_outcome_event_id": supersedes,
        **extra,
    }
    return outcome_v2_envelope(
        event_id,
        observed_at=WINDOW_CLOSES,
        window_opened_at=WINDOW_OPENED,
        window_closes_at=WINDOW_CLOSES,
        evaluation_state="closed",
        **payload,
    )


def open_observation(event_id: str, **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at="2026-05-01T00:00:00Z",
        window_opened_at=WINDOW_OPENED,
        window_closes_at=WINDOW_CLOSES,
        evaluation_state="open",
        **payload,
    )


def select_effective(harness: Harness, outcome_event_id: str, policy_version=None):
    with harness.engine.connect() as conn:
        cutoff = policy.ledger_maximum(conn)
        version = policy.effective_outcome_version(conn, outcome_event_id, cutoff=cutoff)
        result = policy.effective_attribution(
            conn, outcome_event_id, policy_version or policy.POLICY_VERSION, cutoff=cutoff
        )
        versions = policy.effective_outcome_versions(conn, cutoff=cutoff, account_ref=ACCOUNT_REF)
    return version, result, versions


def assert_rejected(harness: Harness, envelope: dict, reason: str, status: int = 422) -> dict:
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == status, response.json()
    body = response.json()
    assert body["reason"] == reason, body
    assert harness.snapshot() == before
    return body


# --- 1 and 2. Appended versions, unchanged history, no inherited credit ---------


def test_closing_an_outcome_appends_a_linked_version_and_rewrites_nothing(harness):
    seed_and_attribute(harness)
    with harness.engine.connect() as conn:
        reconstruction_before = reconstruct(conn, DECISION_EVENT_ID)
    counterfactual_before = replay_under(harness, v5_1_hash())
    decision_before = decision_rows(harness)
    original_outcome = tuple(outcome_row(harness, OUTCOME_EVENT_ID))
    (original_attribution,) = attribution_rows(harness)
    events_before = harness.event_count()

    post_created(harness, closing_version())

    assert harness.event_count() == events_before + 1
    closing = outcome_row(harness, CLOSING_ID)
    assert closing.supersedes_outcome_event_id == OUTCOME_EVENT_ID
    assert (closing.schema_version, closing.evaluation_state) == ("2", "closed")
    assert tuple(outcome_row(harness, OUTCOME_EVENT_ID)) == original_outcome
    assert [tuple(r) for r in attribution_rows(harness)] == [tuple(original_attribution)]
    assert decision_rows(harness) == decision_before
    with harness.engine.connect() as conn:
        assert_same_reconstruction(reconstruct(conn, DECISION_EVENT_ID), reconstruction_before)
    assert_same_counterfactual(replay_under(harness, v5_1_hash()), counterfactual_before)


def test_a_corrected_outcome_without_its_own_result_does_not_inherit_its_predecessors(harness):
    seed_and_attribute(harness)
    (original_attribution,) = attribution_rows(harness)
    post_created(harness, closing_version())

    # The chain resolves to the correction; the correction has no result.
    version, result, versions = select_effective(harness, OUTCOME_EVENT_ID)
    assert version == CLOSING_ID
    assert versions == (CLOSING_ID,)  # never both versions of one chain
    assert result is not None  # the predecessor's result is retained ...
    assert result.attribution_event_id == original_attribution.attribution_event_id
    _, inherited, _ = select_effective(harness, CLOSING_ID)
    assert inherited is None  # ... and is not the correction's result

    # The ordinary command attributes the correction and nothing else.
    run = attribute_ledger(harness)
    assert [s.outcome_event_id for s in run.submissions] == [CLOSING_ID]
    assert [s.http_status for s in run.submissions] == [201]
    rows = attribution_rows(harness)
    assert len(rows) == 2
    assert tuple(rows[0]) == tuple(original_attribution)

    version, own, versions = select_effective(harness, CLOSING_ID)
    assert (version, versions) == (CLOSING_ID, (CLOSING_ID,))
    assert own.outcome_event_id == CLOSING_ID
    assert own.attribution_event_id == rows[1].attribution_event_id
    assert own.supersedes_attribution_event_id is None  # its own first result
    assert own.status == policy.STATUS_DIRECT
    assert own.resolved_action_event_id == ACTION_EVENT_ID


def test_selection_follows_a_chain_from_any_member_to_one_version(harness):
    seed_and_attribute(harness)
    post_created(
        harness,
        open_observation("evt-test-o-v2-a", supersedes_outcome_event_id=OUTCOME_EVENT_ID),
    )
    post_created(
        harness,
        closing_version("evt-test-o-v2-b", supersedes="evt-test-o-v2-a"),
    )
    for member in (OUTCOME_EVENT_ID, "evt-test-o-v2-a", "evt-test-o-v2-b"):
        version, _, versions = select_effective(harness, member)
        assert version == "evt-test-o-v2-b"
        assert versions == ("evt-test-o-v2-b",)

    # As of a cutoff before the last version, the middle version is effective.
    with harness.engine.connect() as conn:
        earlier = policy.ledger_maximum(conn) - 1
        assert (
            policy.effective_outcome_version(conn, OUTCOME_EVENT_ID, cutoff=earlier)
            == "evt-test-o-v2-a"
        )


# --- 3. Rejected outcome-version shapes ------------------------------------------


def test_a_second_replacement_of_one_outcome_version_is_rejected(harness):
    seed_and_attribute(harness)
    post_created(harness, closing_version())
    body = assert_rejected(
        harness,
        closing_version("evt-test-o-closing-again"),
        "superseded_outcome_is_not_effective",
    )
    assert body["effective_outcome_event_id"] == CLOSING_ID


def test_a_stale_outcome_predecessor_is_rejected(harness):
    seed_and_attribute(harness)
    post_created(
        harness,
        open_observation("evt-test-o-v2-a", supersedes_outcome_event_id=OUTCOME_EVENT_ID),
        closing_version("evt-test-o-v2-b", supersedes="evt-test-o-v2-a"),
    )
    assert_rejected(
        harness,
        closing_version("evt-test-o-v2-c", supersedes=OUTCOME_EVENT_ID),
        "superseded_outcome_is_not_effective",
    )


def test_an_unknown_or_cross_account_outcome_predecessor_is_rejected(harness):
    seed_and_attribute(harness)
    assert_rejected(
        harness,
        closing_version("evt-test-o-ghost", supersedes="evt-no-such-outcome"),
        "unknown_superseded_outcome",
    )
    post_created(harness, discovery_envelope("other-account"))
    other = closing_version("evt-test-o-other")
    other["account_ref"] = "other-account"
    assert_rejected(harness, other, "superseded_outcome_belongs_to_another_account")


# --- 3. Rejected attribution shapes ----------------------------------------------


def test_a_second_root_for_one_outcome_and_policy_is_rejected(harness):
    seed_and_attribute(harness)
    append_unrelated(harness)
    body = assert_rejected(
        harness,
        attribution_envelope(harness, OUTCOME_EVENT_ID),
        "attribution_already_recorded",
    )
    (stored,) = attribution_rows(harness)
    assert body["effective_attribution_event_id"] == stored.attribution_event_id


def test_the_same_operation_under_a_new_event_id_is_rejected(harness):
    run = seed_and_attribute(harness)
    forged = dict(run.submissions[0].envelope, event_id="evt-attribution-forged")
    body = assert_rejected(harness, forged, "attribution_operation_already_recorded")
    assert body["stored_event_id"] == run.submissions[0].event_id


def test_an_event_id_that_is_not_the_operation_identity_is_rejected(harness):
    seed_and_attribute(harness)
    append_unrelated(harness)
    (stored,) = attribution_rows(harness)
    envelope = attribution_envelope(
        harness, OUTCOME_EVENT_ID, supersedes=stored.attribution_event_id
    )
    envelope["event_id"] = "evt-attribution-chosen-by-hand"
    assert_rejected(harness, envelope, "attribution_event_id_is_not_the_operation_identity")


def _replacement(harness: Harness, supersedes: str) -> dict:
    append_unrelated(harness)
    return attribution_envelope(harness, OUTCOME_EVENT_ID, supersedes=supersedes)


def test_a_branching_replacement_is_rejected(harness):
    seed_and_attribute(harness)
    (root,) = attribution_rows(harness)
    post_created(harness, _replacement(harness, root.attribution_event_id))
    assert_rejected(
        harness,
        _replacement(harness, root.attribution_event_id),
        "superseded_attribution_is_not_effective",
    )


def test_a_stale_attribution_predecessor_is_rejected(harness):
    seed_and_attribute(harness)
    (root,) = attribution_rows(harness)
    post_created(harness, _replacement(harness, root.attribution_event_id))
    second = attribution_rows(harness)[1]
    post_created(harness, _replacement(harness, second.attribution_event_id))
    assert_rejected(
        harness,
        _replacement(harness, root.attribution_event_id),
        "superseded_attribution_is_not_effective",
    )
    rows = attribution_rows(harness)
    assert len(rows) == 3
    _, effective, _ = select_effective(harness, OUTCOME_EVENT_ID)
    assert effective.attribution_event_id == rows[2].attribution_event_id


def test_an_unknown_attribution_predecessor_is_rejected(harness):
    seed_and_attribute(harness)
    assert_rejected(
        harness,
        _replacement(harness, "evt-attribution-never-recorded"),
        "unknown_superseded_attribution",
    )


def test_a_cross_outcome_link_is_rejected(harness):
    seed_and_attribute(harness)
    (canonical,) = attribution_rows(harness)
    post_created(harness, open_observation("evt-test-o-other"))
    envelope = attribution_envelope(
        harness, "evt-test-o-other", supersedes=canonical.attribution_event_id
    )
    assert_rejected(harness, envelope, "superseded_attribution_is_for_another_outcome")


def test_a_cross_policy_link_is_rejected(harness, monkeypatch):
    """A second policy is implemented for this test only, so its result enters
    through the collector like any other."""
    monkeypatch.setattr(
        policy,
        "IMPLEMENTED_POLICY_VERSIONS",
        frozenset({policy.POLICY_VERSION, TEST_POLICY}),
    )
    seed_and_attribute(harness)
    append_unrelated(harness)
    post_created(
        harness, attribution_envelope(harness, OUTCOME_EVENT_ID, policy_version=TEST_POLICY)
    )
    test_result = attribution_rows(harness)[1]
    assert test_result.policy_version == TEST_POLICY

    # Each policy has its own single effective result for the outcome.
    _, v1_result, _ = select_effective(harness, OUTCOME_EVENT_ID)
    _, other_result, _ = select_effective(harness, OUTCOME_EVENT_ID, TEST_POLICY)
    assert v1_result.policy_version == policy.POLICY_VERSION
    assert other_result.attribution_event_id == test_result.attribution_event_id

    assert_rejected(
        harness,
        _replacement(harness, test_result.attribution_event_id),
        "superseded_attribution_is_for_another_policy",
    )


# --- 3. Selection raises rather than picks ----------------------------------------


def test_selection_raises_on_a_ledger_holding_two_effective_results(harness):
    seed_and_attribute(harness)
    post_created(harness, open_observation("evt-test-o-other"))
    attribute_ledger(harness)
    other = attribution_rows(harness)[1]
    assert other.outcome_event_id == "evt-test-o-other"

    corrupt = insert_ambiguous_attribution(harness, other.attribution_event_id)
    with pytest.raises(policy.AmbiguousSelection) as failure:
        select_effective(harness, OUTCOME_EVENT_ID)
    assert failure.value.reason == "ambiguous_effective_selection"
    original = attribution_rows(harness)[0]
    assert set(failure.value.candidates) == {original.attribution_event_id, corrupt}
