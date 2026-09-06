"""AC-15 in full: canonical ingestion, idempotency, conflicts, rejection, and the
domain records each accepted event creates.

The first half covers the collector boundary itself (INV-11). The second half
covers the projections of `PRODUCT.md` §6: every accepted event writes its
normalized historical records in the same transaction, and a retry, a
conflicting reuse, or an invalid envelope leaves them untouched.
"""

import copy
import json

import pytest

from tests.conftest import (
    Harness,
    canonical_by_type,
    canonical_raw,
    reformatted,
    register_artifacts,
)


def _discover(harness: Harness):
    response = harness.post_raw(canonical_raw(0))
    assert response.status_code == 201
    return response


def _evidence_with_employee_count(value):
    env = copy.deepcopy(canonical_by_type("evidence.recorded"))
    env["payload"]["items"] = [
        {
            "evidence_version_id": "ev-test-employee-count-v1",
            "evidence_type": "employee_count",
            "value": value,
        }
    ]
    return env


def test_valid_canonical_event_creates_one_row(harness):
    response = _discover(harness)
    assert response.json()["status"] == "created"
    assert harness.event_count() == 1
    assert len(harness.account_rows()) == 1


def test_reordered_keys_and_whitespace_retry_is_duplicate(harness):
    _discover(harness)
    raw = reformatted(canonical_by_type("account.discovered"))
    assert raw != canonical_raw(0)
    response = harness.post_raw(raw)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert harness.event_count() == 1


def test_same_event_id_with_changed_valid_field_conflicts(harness):
    _discover(harness)
    before = harness.snapshot()
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["source"] = "apollo-sim-2"
    response = harness.post(env)
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "conflict"
    assert body["stored_hash"] != body["submitted_hash"]
    assert harness.snapshot() == before


def test_same_instant_different_offset_is_duplicate(harness):
    _discover(harness)
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    assert env["occurred_at"].endswith("Z")
    env["occurred_at"] = env["occurred_at"].replace("T10:", "T12:").replace("Z", "+02:00")
    env["recorded_at"] = env["recorded_at"].replace("T10:", "T05:").replace("Z", "-05:00")
    response = harness.post(env)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert harness.event_count() == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["payload"].pop("domain"), id="malformed-payload-missing-field"),
        pytest.param(lambda e: e.update(schema_version="2"), id="unsupported-schema-version"),
        pytest.param(lambda e: e.update(unexpected="x"), id="unknown-envelope-field"),
        pytest.param(lambda e: e["payload"].update(extra="x"), id="unknown-payload-field"),
        pytest.param(lambda e: e.update(event_type="account.renamed"), id="unknown-event-type"),
        pytest.param(lambda e: e.update(occurred_at="2026-04-17T10:04:00"), id="naive-timestamp"),
        pytest.param(
            lambda e: e.update(recorded_at="2026-04-17T10:03:59Z"), id="recorded-before-occurred"
        ),
    ],
)
def test_invalid_envelopes_are_rejected_without_writes(harness, mutate):
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["event_id"] = "evt-test-rejected"
    mutate(env)
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert harness.is_empty()


def test_non_discovery_event_for_unknown_account_is_rejected(harness):
    response = harness.post_raw(canonical_raw(1))
    assert response.status_code == 422
    assert response.json()["reason"] == "unknown_account_ref"
    assert harness.is_empty()


@pytest.mark.parametrize("bad_value", ["184", 184.0], ids=["string-integer", "float-integer"])
def test_string_or_float_employee_count_is_rejected(harness, bad_value):
    _discover(harness)
    before = harness.snapshot()
    response = harness.post(_evidence_with_employee_count(bad_value))
    assert response.status_code == 422
    assert harness.snapshot() == before


def test_integer_employee_count_is_accepted(harness):
    _discover(harness)
    response = harness.post(_evidence_with_employee_count(184))
    assert response.status_code == 201


def test_non_json_body_is_rejected(harness):
    response = harness.post_raw(b"not json")
    assert response.status_code == 422
    assert harness.is_empty()


def test_openapi_publishes_envelope_schema(client):
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/v1/decision-events"]["post"]
    assert "application/json" in post["requestBody"]["content"]
    assert "DecisionRecordedEnvelope" in schema["components"]["schemas"]
    assert json.dumps(schema)  # serializable


def test_full_canonical_seed_and_reseed(harness):
    from tests.conftest import seed_all

    first = seed_all(harness)
    assert [r.status_code for r in first] == [201] * 9
    second = seed_all(harness)
    assert [r.json()["status"] for r in second] == ["duplicate"] * 9
    assert harness.event_count() == 9


# --- Decision-envelope coherence at the collector boundary ------------------
#
# An internally contradictory decision must never reach the immutable ledger.
# Each case posts a raw JSON body and asserts 422 with no event or account write.

UNAVAILABLE_KEY = "website_intent"


def _seed_through_decision_prerequisites(harness: Harness) -> None:
    """Both logic artifacts, discovery, and both evidence events, so only the
    decision is under test."""
    register_artifacts(harness)
    for index in (0, 1, 2):
        assert harness.post_raw(canonical_raw(index)).status_code == 201


def _decision() -> dict:
    return copy.deepcopy(canonical_by_type("decision.recorded"))


def _context(envelope: dict, input_key: str) -> dict:
    return next(e for e in envelope["payload"]["historical_context"] if e["input_key"] == input_key)


def _duplicate_context_entry(env):
    env["payload"]["historical_context"].append(
        copy.deepcopy(env["payload"]["historical_context"][0])
    )


def _duplicate_consumed_input(env):
    env["payload"]["consumed_inputs"].append(copy.deepcopy(env["payload"]["consumed_inputs"][0]))


INCOHERENT_DECISIONS = [
    pytest.param(
        lambda env: _context(env, UNAVAILABLE_KEY).update(value="high"),
        "must have a null value",
        id="rule-1-unavailable-input-with-a-value",
    ),
    pytest.param(
        _duplicate_context_entry,
        "historical_context has more than one entry",
        id="rule-2-duplicate-historical-context-key",
    ),
    pytest.param(
        _duplicate_consumed_input,
        "consumed_inputs has more than one entry",
        id="rule-3-duplicate-consumed-input-key",
    ),
    pytest.param(
        lambda env: env["payload"]["consumed_inputs"][0].update(input_key="no_such_input"),
        "no historical_context entry with that input_key",
        id="rule-4-consumed-input-without-context",
    ),
    pytest.param(
        lambda env: env["payload"]["consumed_inputs"][0].update(input_key=UNAVAILABLE_KEY),
        "cannot have been consumed",
        id="rule-4-consumed-input-marked-unavailable",
    ),
    pytest.param(
        lambda env: env["payload"]["consumed_inputs"][0].update(value="tampered"),
        "does not equal the historical_context value",
        id="rule-5-consumed-value-disagrees",
    ),
    pytest.param(
        lambda env: env["payload"]["consumed_inputs"][0].update(
            evidence_version_id="ev-some-other-version-v1"
        ),
        "does not equal the historical_context evidence_version_id",
        id="rule-5-consumed-evidence-disagrees",
    ),
    pytest.param(
        lambda env: env["payload"].update(decision_boundary="2026-04-17T10:05:03Z"),
        "must be the same instant as occurred_at",
        id="rule-6-boundary-differs-from-occurred-at",
    ),
]


@pytest.mark.parametrize("mutate,expected_message", INCOHERENT_DECISIONS)
def test_incoherent_decision_is_rejected_without_writes(harness, mutate, expected_message):
    _seed_through_decision_prerequisites(harness)
    before = harness.snapshot()
    env = _decision()
    mutate(env)
    response = harness.post_raw(json.dumps(env))
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "rejected"
    assert expected_message in json.dumps(body["errors"])
    assert harness.snapshot() == before


def test_coherent_decision_is_accepted_and_its_retry_is_a_duplicate(harness):
    _seed_through_decision_prerequisites(harness)
    first = harness.post_raw(canonical_raw(3))
    assert first.status_code == 201 and first.json()["status"] == "created"
    assert harness.event_count() == 6

    retry = harness.post_raw(reformatted(_decision()))
    assert retry.status_code == 200
    assert retry.json()["status"] == "duplicate"
    assert retry.json()["canonical_hash"] == first.json()["canonical_hash"]
    assert harness.event_count() == 6


# --- Full AC-15: every accepted event creates its domain records ------------
#
# `PRODUCT.md` §6 requires normalized historical records, written in the same
# transaction as the event. Each case below seeds the canonical envelopes up to
# the event under test and asserts the rows it produced.

from sqlalchemy import select  # noqa: E402

from flight_recorder.ledger.schema import (  # noqa: E402
    actions,
    decision_consumed_inputs,
    decision_context,
    decisions,
    evidence_versions,
    logic_artifacts,
    outcomes,
    persona_selections,
)
from tests.conftest import seed_all  # noqa: E402

ACCOUNT_REF = "novasignal-ai"
DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"
V32_HASH = "db3a8bdebf2befe286ab49a2381dfe6fb931ac6f848923d35e0e732adcc82db0"


def _rows(harness: Harness, table, order_by=None):
    with harness.engine.connect() as conn:
        query = select(table)
        if order_by is not None:
            query = query.order_by(order_by)
        return conn.execute(query).all()


def test_registration_projects_one_logic_artifact_row_per_hash(harness):
    seed_all(harness)
    rows = _rows(harness, logic_artifacts, logic_artifacts.c.logic_version)
    assert [r.logic_version for r in rows] == ["v3.2", "v5.1"]
    assert rows[0].artifact_hash == V32_HASH
    assert rows[0].decision_class == "account_prioritization"
    assert rows[0].evaluator_version == "evaluator-v1"


def test_discovery_projects_the_account(harness):
    seed_all(harness)
    account = [r for r in harness.account_rows() if r[0] == ACCOUNT_REF]
    assert account == [
        (ACCOUNT_REF, "NovaSignal AI", "novasignal.ai", "evt-novasignal-01-account-discovered")
    ]


def test_evidence_projects_one_immutable_version_per_item(harness):
    seed_all(harness)
    rows = _rows(harness, evidence_versions, evidence_versions.c.evidence_version_id)
    assert len(rows) == 7
    by_type = {r.evidence_type: r for r in rows}

    employees = by_type["employee_count"]
    assert employees.evidence_version_id == "ev-novasignal-employee-count-v1"
    assert employees.account_ref == ACCOUNT_REF
    assert employees.value_json == '{"value":184}'
    assert employees.source == "clay-sim"
    assert employees.observed_at is None
    assert employees.available_at == "2026-04-17T10:04:37.000000Z"
    assert employees.source_event_id == "evt-novasignal-02-evidence-enrichment"
    assert employees.supersedes_evidence_version_id is None

    # Types carrying an observation date store it in both places (§2a).
    funding = by_type["funding_event"]
    assert funding.observed_at == "2026-03-30"
    assert funding.value_json == '{"observed_at":"2026-03-30","value":"Series B"}'

    # `basis` is a semantic field of the item, so it belongs in value_json.
    pressure = by_type["verified_integration_pressure"]
    assert json.loads(pressure.value_json)["value"] == "LOW"
    assert json.loads(pressure.value_json)["basis"] == [
        "one documented production integration",
        "no public API",
        "no agent-tool directory",
    ]
    assert pressure.observed_at is None
    assert pressure.source == "relaybridge-research-sim"


def test_decision_projects_one_decision_eight_context_and_five_consumed_rows(harness):
    seed_all(harness)
    (decision,) = _rows(harness, decisions)
    assert decision.decision_event_id == DECISION_EVENT_ID
    assert decision.account_ref == ACCOUNT_REF
    assert decision.decision_class == "account_prioritization"
    assert decision.decision_boundary == "2026-04-17T10:05:02.000000Z"
    assert decision.workflow_version == "v4.2"
    assert decision.artifact_hash == V32_HASH
    assert decision.evaluator_version == "evaluator-v1"
    assert decision.logic_version == "v3.2"
    assert (decision.score, decision.threshold, decision.output) == (86, 75, "PRIORITIZE")
    assert decision.explanation == canonical_by_type("decision.recorded")["payload"]["explanation"]
    assert decision.ingest_sequence > 0

    context = _rows(harness, decision_context, decision_context.c.input_key)
    assert len(context) == 8
    by_key = {r.input_key: r for r in context}
    assert by_key["employee_count"].availability == "available"
    assert by_key["employee_count"].value_text == "184"
    assert by_key["employee_count"].evidence_version_id == "ev-novasignal-employee-count-v1"
    assert by_key["industry"].value_text == '"B2B AI Software"'
    # INV-03: available-but-ignored is preserved as context, not as a consumed input.
    assert by_key["verified_integration_pressure"].availability == "available"
    # INV-09: an explicitly unavailable input keeps no value and no evidence.
    assert by_key["website_intent"].availability == "unavailable"
    assert by_key["website_intent"].value_text is None
    assert by_key["website_intent"].evidence_version_id is None

    consumed = _rows(harness, decision_consumed_inputs, decision_consumed_inputs.c.input_key)
    assert len(consumed) == 5
    assert sum(r.contribution for r in consumed) == decision.score
    assert "verified_integration_pressure" not in {r.input_key for r in consumed}
    assert all(r.decision_event_id == DECISION_EVENT_ID for r in consumed)


def test_persona_selection_projects_the_decision_to_persona_link(harness):
    seed_all(harness)
    (persona,) = _rows(harness, persona_selections)
    assert persona.event_id == "evt-novasignal-05-persona-selected"
    assert persona.account_ref == ACCOUNT_REF
    assert persona.decision_event_id == DECISION_EVENT_ID
    assert persona.persona == "Head of Platform"
    assert persona.explanation.startswith("Explanation:")


def test_action_projects_its_decision_link_cost_and_time(harness):
    seed_all(harness)
    (action,) = _rows(harness, actions)
    assert action.action_event_id == "evt-novasignal-06-action-recorded"
    assert action.account_ref == ACCOUNT_REF
    assert action.decision_event_id == DECISION_EVENT_ID
    assert (action.action_type, action.play_id) == ("outbound_play", 14)
    assert (action.target_persona, action.status) == ("Head of Platform", "sent")
    assert (action.cost, action.currency) == ("1.42", "USD")
    assert action.occurred_at == "2026-04-17T10:07:00.000000Z"


def test_outcome_projects_a_later_separate_observation(harness):
    seed_all(harness)
    (outcome,) = _rows(harness, outcomes)
    assert outcome.outcome_event_id == "evt-novasignal-07-outcome-evaluated"
    assert outcome.account_ref == ACCOUNT_REF
    assert outcome.action_event_id == "evt-novasignal-06-action-recorded"
    assert outcome.window_days == 90
    assert (outcome.reply, outcome.meeting, outcome.opportunity) == (False, False, False)
    assert outcome.occurred_at == "2026-07-16T10:07:00.000000Z"
    assert outcome.recorded_at == "2026-07-16T10:07:00.000000Z"


def test_canonically_identical_retry_creates_no_projection_rows(harness):
    seed_all(harness)
    before = harness.snapshot()
    for envelope in (canonical_by_type(t) for t in ("evidence.recorded", "decision.recorded")):
        assert harness.post_raw(reformatted(envelope)).status_code == 200
    assert harness.snapshot() == before


def test_conflicting_reuse_leaves_the_projections_unchanged(harness):
    seed_all(harness)
    before = harness.snapshot()
    env = _decision()
    env["source"] = "relaybridge-scoring-v2"
    assert harness.post(env).status_code == 409
    assert harness.snapshot() == before


def test_invalid_envelope_leaves_the_projections_unchanged(harness):
    seed_all(harness)
    before = harness.snapshot()
    env = _decision()
    env["event_id"] = "evt-novasignal-04b-decision-recorded"
    env["payload"]["result"]["score"] = 86.0
    assert harness.post(env).status_code == 422
    assert harness.snapshot() == before
