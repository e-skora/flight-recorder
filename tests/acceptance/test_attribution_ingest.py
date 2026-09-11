"""AC-15, INV-11, INV-01 for attribution results: the collector refuses what it
cannot verify, writes atomically, and the command's three situations differ.

Refusals: a forged result whose recomputation disagrees, an unsupported policy
version, a cutoff above the ledger maximum, an invalid resolved reference, a
wrong account or source, and a conflicting retry. A fault injected before
commit rolls back the event and its projection together, and the new table
refuses UPDATE and DELETE.

The three command situations, each with an injected clock advanced between
runs: an exact submission retry (duplicate, one domain effect, stored time
unchanged); a fresh ordinary invocation (nothing submitted, nothing written);
and a fresh reevaluation, unchanged (nothing written) or changed (exactly one
linked replacement).
"""

import asyncio
import copy
import json

import httpx
import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from flight_recorder.attribution import policy, service
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import outcome_attributions
from tests.conftest import (
    ACTION_EVENT_ID,
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
    discovery_envelope,
    max_sequence,
    outcome_v2_envelope,
    post_created,
    seed_all,
    seed_and_attribute,
    seed_through_decision,
)

OBSERVED = "2026-05-01T00:00:00Z"
FIRST_ACTION = "evt-test-action-first"
LATER_ACTION = "evt-test-action-later"
WATCHED = "evt-test-o-watched"


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def observation(event_id: str, **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at=OBSERVED,
        window_opened_at="2026-04-17T10:07:00Z",
        window_closes_at="2026-07-16T10:07:00Z",
        evaluation_state="open",
        **payload,
    )


def assert_rejected(harness: Harness, envelope: dict, reason: str, status: int = 422) -> dict:
    before = harness.snapshot()
    response = harness.post(envelope)
    assert response.status_code == status, response.json()
    body = response.json()
    assert body["reason"] == reason, body
    assert harness.snapshot() == before
    return body


def effective(harness: Harness, outcome_event_id: str):
    with harness.engine.connect() as conn:
        return policy.effective_attribution(
            conn, outcome_event_id, policy.POLICY_VERSION, cutoff=policy.ledger_maximum(conn)
        )


def forbid_submission(monkeypatch) -> None:
    """Any submission attempt fails the test: 'submits nothing' is proven, not assumed."""

    async def refuse(*_args, **_kwargs):
        raise AssertionError("the command submitted an envelope")

    monkeypatch.setattr(service, "submit", refuse)


# --- Refusals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", policy.STATUS_INFERRED),
        ("method", policy.METHOD_HEURISTIC),
        ("reason", policy.VALID_SOURCE_DECISION),
        ("window_days", 30),
        ("resolved_action_event_id", None),
    ],
)
def test_a_forged_result_that_the_policy_does_not_reproduce_is_rejected(seeded, field, value):
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    envelope["payload"][field] = value
    body = assert_rejected(seeded, envelope, "attribution_disagrees_with_policy")
    assert set(body["disagreements"]) == {field}
    assert body["disagreements"][field]["submitted"] == value


def test_a_forged_direct_claim_for_an_unreferenced_outcome_is_rejected(seeded):
    """Resolving foreign keys alone would accept this; recomputation does not."""
    post_created(seeded, observation("evt-test-o-bare"))
    envelope = attribution_envelope(seeded, "evt-test-o-bare")
    assert envelope["payload"]["status"] == policy.STATUS_INFERRED
    envelope["payload"].update(
        status=policy.STATUS_DIRECT,
        method=policy.METHOD_EXPLICIT_REFERENCE,
        reason=policy.VALID_SOURCE_ACTION,
    )
    body = assert_rejected(seeded, envelope, "attribution_disagrees_with_policy")
    assert set(body["disagreements"]) == {"status", "method", "reason"}


def test_an_unsupported_policy_version_is_rejected(seeded):
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    envelope["payload"]["policy_version"] = "outcome-attribution-v9"
    envelope["event_id"] = policy.attribution_event_id(
        OUTCOME_EVENT_ID, "outcome-attribution-v9", envelope["payload"]["ingest_cutoff"]
    )
    assert_rejected(seeded, envelope, "unsupported_policy_version")


def test_a_cutoff_above_the_ledger_maximum_is_rejected(seeded):
    above = max_sequence(seeded) + 1
    with pytest.raises(policy.CutoffAboveMaximum):
        attribute_at(seeded, OUTCOME_EVENT_ID, above)
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    envelope["payload"]["ingest_cutoff"] = above
    envelope["event_id"] = policy.attribution_event_id(
        OUTCOME_EVENT_ID, policy.POLICY_VERSION, above
    )
    assert_rejected(seeded, envelope, "ingest_cutoff_above_ledger_maximum")


def test_a_resolved_reference_in_another_account_is_rejected(seeded):
    """A resolved reference must be a real record of the same account."""
    other = "other-account"
    post_created(seeded, discovery_envelope(other))
    post_created(seeded, observation("evt-test-o-other", account_ref=other))
    envelope = attribution_envelope(seeded, "evt-test-o-other")
    assert envelope["payload"]["status"] == policy.STATUS_UNRESOLVED
    envelope["payload"].update(
        status=policy.STATUS_INFERRED,
        resolved_action_event_id=ACTION_EVENT_ID,
        resolved_decision_event_id=DECISION_EVENT_ID,
    )
    assert_rejected(seeded, envelope, "resolved_action_belongs_to_another_account")


def test_an_attribution_under_another_account_is_rejected(seeded):
    post_created(seeded, discovery_envelope("other-account"))
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    envelope["account_ref"] = "other-account"
    assert_rejected(seeded, envelope, "attributed_outcome_belongs_to_another_account")


def test_an_attribution_for_an_unknown_outcome_is_rejected(seeded):
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    envelope["payload"]["outcome_event_id"] = "evt-no-such-outcome"
    assert_rejected(seeded, envelope, "unknown_outcome_event_id")


@pytest.mark.parametrize(
    "mutate,message",
    [
        pytest.param(
            lambda e: e.update(account_ref="_system"), "is reserved", id="system-principal"
        ),
        pytest.param(lambda e: e.update(source="crm-sim"), "source", id="vendor-like-source"),
        pytest.param(
            lambda e: e["payload"].update(attributed_at="2026-09-10T00:00:00Z"),
            "must be the same instant as occurred_at",
            id="attributed-at-is-not-occurred-at",
        ),
        pytest.param(
            lambda e: e["payload"].update(ingest_cutoff=True), "ingest_cutoff", id="boolean-cutoff"
        ),
        pytest.param(
            lambda e: e["payload"].update(resolved_decision_event_id=None),
            "status 'direct' requires resolved_decision_event_id",
            id="incoherent-direct",
        ),
        pytest.param(lambda e: e.update(schema_version="2"), "schema_version", id="schema-v2"),
    ],
)
def test_a_structurally_invalid_attribution_envelope_is_rejected(seeded, mutate, message):
    envelope = attribution_envelope(seeded, OUTCOME_EVENT_ID)
    mutate(envelope)
    body = assert_rejected(seeded, envelope, "invalid_envelope")
    assert message in json.dumps(body["errors"])


def test_a_conflicting_retry_under_the_same_event_id_is_a_409(seeded):
    run = attribute_ledger(seeded)
    changed = copy.deepcopy(run.submissions[0].envelope)
    changed["recorded_at"] = "2026-09-12T00:00:00.000000Z"
    body = assert_rejected(seeded, changed, "event_id_reused_with_different_content", status=409)
    assert body["stored_hash"] != body["submitted_hash"]


# --- Atomicity and append-only -------------------------------------------------------


class _Boom(RuntimeError):
    pass


def test_a_failure_before_commit_rolls_back_the_event_and_its_projection(tmp_path):
    harness = Harness(tmp_path, raise_server_exceptions=False)
    for response in seed_all(harness):
        assert response.status_code == 201
    envelope = attribution_envelope(harness, OUTCOME_EVENT_ID)
    before = harness.snapshot()

    def explode(_envelope):
        raise _Boom("injected failure after the attribution's writes")

    harness.collector.before_commit = explode
    assert harness.post(envelope).status_code == 500
    assert harness.snapshot() == before
    assert attribution_rows(harness) == []

    harness.collector.before_commit = None
    assert harness.post(envelope).status_code == 201
    assert len(attribution_rows(harness)) == 1


def test_the_attribution_table_refuses_update_and_delete(harness):
    seed_and_attribute(harness)
    before = harness.projection_rows()
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(update(outcome_attributions).values(status=policy.STATUS_UNRESOLVED))
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(delete(outcome_attributions))
    assert harness.projection_rows() == before


# --- Situation 1: an exact submission retry ------------------------------------------


def test_an_exact_submission_retry_is_a_duplicate_with_one_domain_effect(harness):
    clock = FixedClock()
    first_instant = clock()
    run = seed_and_attribute(harness, clock)
    held = run.submissions[0].envelope
    snapshot = harness.snapshot()
    (stored,) = attribution_rows(harness)
    assert stored.attributed_at == format_utc(first_instant)

    clock.advance(days=3)  # a later clock must not leak into a retry

    # The envelope the command held, resubmitted unchanged.
    status, body = service.resubmit(harness.app, held)
    assert (status, body["status"], body["event_id"]) == (200, "duplicate", held["event_id"])

    # The same envelope, recovered by the retained operation identity alone.
    retry = service.retry_operation(harness.app, OUTCOME_EVENT_ID, ingest_cutoff=run.cutoff)
    assert retry.envelope == held
    assert (retry.http_status, retry.status) == (200, "duplicate")
    assert retry.envelope["payload"]["attributed_at"] == stored.attributed_at
    assert retry.envelope["payload"]["supersedes_attribution_event_id"] is None

    assert harness.snapshot() == snapshot
    (after,) = attribution_rows(harness)
    assert tuple(after) == tuple(stored)
    assert after.attributed_at == format_utc(first_instant)


def test_an_uncertain_submission_is_resent_unchanged_and_answered_duplicate(seeded):
    """The first send commits but its response is lost; `submit` resends the
    envelope it holds, and the collector answers `duplicate`."""
    post_created(seeded, observation("evt-test-o-uncertain"))
    envelope = attribution_envelope(seeded, "evt-test-o-uncertain")
    inner = httpx.ASGITransport(app=seeded.app)
    sent: list[bytes] = []

    class LosesTheFirstResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await inner.handle_async_request(request)
            sent.append(await request.aread())
            if len(sent) == 1:
                await response.aread()
                raise httpx.ReadError("response lost after the collector committed")
            return response

    async def go():
        async with httpx.AsyncClient(
            transport=LosesTheFirstResponse(), base_url="http://attribute"
        ) as client:
            return await service.submit(client, envelope)

    status, body = asyncio.run(go())
    assert (status, body["status"]) == (200, "duplicate")
    assert len(sent) == 2 and sent[0] == sent[1]
    rows = [r for r in attribution_rows(seeded) if r.outcome_event_id == "evt-test-o-uncertain"]
    assert len(rows) == 1


def test_a_retry_of_an_operation_never_stored_is_refused_rather_than_invented(seeded):
    with pytest.raises(LookupError):
        service.retry_operation(seeded.app, OUTCOME_EVENT_ID, ingest_cutoff=max_sequence(seeded))


# --- Situation 2: a fresh ordinary invocation -----------------------------------------


def test_a_fresh_ordinary_run_submits_nothing_and_writes_nothing(harness, monkeypatch):
    clock = FixedClock()
    first = seed_and_attribute(harness, clock)
    (stored,) = attribution_rows(harness)
    append_unrelated(harness, 2)
    clock.advance(days=1)
    snapshot = harness.snapshot()

    forbid_submission(monkeypatch)
    second = attribute_ledger(harness, clock=clock)

    assert second.cutoff > first.cutoff  # a fresh cutoff, so a different operation
    assert (
        policy.attribution_event_id(OUTCOME_EVENT_ID, policy.POLICY_VERSION, second.cutoff)
        != stored.attribution_event_id
    )
    assert second.submissions == []
    assert second.already_attributed == [OUTCOME_EVENT_ID]
    assert harness.snapshot() == snapshot
    (after,) = attribution_rows(harness)
    assert (after.ingest_cutoff, after.attributed_at) == (
        stored.ingest_cutoff,
        stored.attributed_at,
    )


# --- Situation 3: a fresh reevaluation ---------------------------------------------------


def test_an_unchanged_reevaluation_writes_nothing_and_reports_unchanged(harness, monkeypatch):
    clock = FixedClock()
    first = seed_and_attribute(harness, clock)
    (stored,) = attribution_rows(harness)
    # Later events that alter no policy-result field: another account, and a
    # NovaSignal action occurring after the outcome's observation.
    append_unrelated(harness)
    post_created(
        harness, action_envelope("evt-test-action-after", occurred_at="2026-08-01T00:00:00Z")
    )
    clock.advance(days=1)
    snapshot = harness.snapshot()

    forbid_submission(monkeypatch)
    run = attribute_ledger(harness, reevaluate=True, clock=clock)

    assert run.cutoff > first.cutoff
    assert run.unchanged == [OUTCOME_EVENT_ID]
    assert run.submissions == []
    assert harness.snapshot() == snapshot
    (after,) = attribution_rows(harness)
    assert tuple(after) == tuple(stored)
    assert (after.ingest_cutoff, after.attributed_at) == (
        stored.ingest_cutoff,
        stored.attributed_at,
    )


def _watched_ledger(harness: Harness, **claims) -> FixedClock:
    """One eligible action and one observation, attributed once."""
    seed_through_decision(harness)
    post_created(
        harness,
        action_envelope(FIRST_ACTION, occurred_at="2026-04-18T09:00:00Z"),
        observation(WATCHED, **claims),
    )
    clock = FixedClock()
    run = attribute_ledger(harness, clock=clock)
    assert [s.http_status for s in run.submissions] == [201]
    return clock


def test_a_changed_reference_with_the_same_status_appends_one_linked_replacement(harness):
    clock = _watched_ledger(harness)
    (first,) = attribution_rows(harness)
    assert (first.status, first.resolved_action_event_id) == (policy.STATUS_INFERRED, FIRST_ACTION)

    post_created(
        harness,
        action_envelope(
            LATER_ACTION, occurred_at="2026-04-25T09:00:00Z", recorded_at="2026-09-01T00:00:00Z"
        ),
    )
    clock.advance(days=1)
    run = attribute_ledger(harness, reevaluate=True, clock=clock)

    assert [s.http_status for s in run.submissions] == [201]
    rows = attribution_rows(harness)
    assert len(rows) == 2
    assert tuple(rows[0]) == tuple(first)  # the earlier row is retained unchanged
    replacement = rows[1]
    assert replacement.supersedes_attribution_event_id == first.attribution_event_id
    assert replacement.status == first.status == policy.STATUS_INFERRED
    assert replacement.reason == first.reason
    assert replacement.resolved_action_event_id == LATER_ACTION
    assert replacement.ingest_cutoff == run.cutoff
    assert replacement.attributed_at == format_utc(clock())
    assert effective(harness, WATCHED).attribution_event_id == replacement.attribution_event_id


def test_a_changed_reason_with_the_same_status_and_references_appends_one_replacement(harness):
    clock = _watched_ledger(harness, source_action_event_id="evt-test-action-claimed")
    (first,) = attribution_rows(harness)
    assert first.reason == (
        f"{policy.SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF}{policy.SEGMENT_SEPARATOR}"
        f"{policy.MOST_RECENT_ELIGIBLE_ACTION}"
    )

    # The claimed action is recorded later, and it failed: same status, same
    # resolved references, a different reason.
    post_created(
        harness,
        action_envelope(
            "evt-test-action-claimed", occurred_at="2026-04-20T09:00:00Z", status="failed"
        ),
    )
    clock.advance(hours=6)
    run = attribute_ledger(harness, reevaluate=True, clock=clock)

    assert [s.http_status for s in run.submissions] == [201]
    first_again, replacement = attribution_rows(harness)
    assert tuple(first_again) == tuple(first)
    assert replacement.supersedes_attribution_event_id == first.attribution_event_id
    assert (replacement.status, replacement.resolved_action_event_id) == (
        first.status,
        first.resolved_action_event_id,
    )
    assert replacement.reason == (
        f"{policy.SOURCE_ACTION_FAILED}{policy.SEGMENT_SEPARATOR}"
        f"{policy.MOST_RECENT_ELIGIBLE_ACTION}"
    )


def test_reevaluation_leaves_unattributed_outcomes_to_the_ordinary_run(harness, monkeypatch):
    seed_through_decision(harness)
    post_created(harness, observation(WATCHED))
    forbid_submission(monkeypatch)
    run = attribute_ledger(harness, reevaluate=True)
    assert run.not_yet_attributed == [WATCHED]
    assert attribution_rows(harness) == []
