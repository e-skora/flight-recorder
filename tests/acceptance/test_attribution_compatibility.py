"""The existing readers stay truthful with collector-ingested v2 outcomes and results.

The account trace renders an `outcome.attributed` event and a v2 outcome; the
decision page renders a v2 outcome and an attributed outcome with the status,
the heuristic label where it applies, an unknown observation as unknown, an
open window as open, `Attribution not yet evaluated.` only where no result
exists, and superseded and unresolved outcomes still visible; page loads write
nothing; and the published API names both outcome payload versions.
"""

from sqlalchemy import inspect

from flight_recorder.attribution import policy
from flight_recorder.web.summaries import KIND_LABELS
from tests.acceptance.test_decision_detail_page import decision_url, element, page, rows
from tests.acceptance.test_trace_ordering import KIND_ORDER, _kinds
from tests.conftest import (
    ACCOUNT_REF,
    ACTION_EVENT_ID,
    DECISION_EVENT_ID,
    OUTCOME_EVENT_ID,
    action_envelope,
    attribute_ledger,
    canonical_by_type,
    captured_statements,
    decision_copy_envelope,
    outcome_v2_envelope,
    post_created,
    seed_all,
    seed_and_attribute,
    stored_form,
    v5_1_hash,
)

OPENED = canonical_by_type("action.recorded")["occurred_at"]
CLOSES = canonical_by_type("outcome.evaluated")["occurred_at"]
TRACE_URL = f"/accounts/{ACCOUNT_REF}"
NOT_YET_EVALUATED = "Attribution not yet evaluated."


def open_observation(event_id: str, observed_at: str = "2026-05-01T00:00:00Z", **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at=observed_at,
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state="open",
        **payload,
    )


def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


# --- The account trace ---------------------------------------------------------------


def test_the_trace_renders_an_outcome_attributed_event(harness):
    seed_and_attribute(harness)
    response = harness.client.get(TRACE_URL)
    assert response.status_code == 200
    assert KIND_LABELS["outcome.attributed"] == "ATTRIBUTION"
    assert _kinds(response.text) == [*KIND_ORDER, "ATTRIBUTION"]
    assert (
        f"Outcome attribution: {policy.STATUS_DIRECT} under {policy.POLICY_VERSION}"
        in response.text
    )
    assert "heuristic" not in response.text


def test_the_trace_renders_a_v2_outcome_with_unknown_observations_as_unknown(harness):
    seeded(harness)
    post_created(harness, open_observation("evt-test-o-v2", meeting=False, opportunity=True))
    attribute_ledger(harness)
    response = harness.client.get(TRACE_URL)
    assert response.status_code == 200
    kinds = _kinds(response.text)
    assert kinds.count("OUTCOME") == 2 and kinds.count("ATTRIBUTION") == 2
    expected = (
        f"Outcome observation, window open ({stored_form(OPENED)} to {stored_form(CLOSES)}), "
        f"as of {stored_form('2026-05-01T00:00:00Z')}: "
        "reply unknown, no meeting recorded yet, opportunity"
    )
    assert expected in response.text
    assert f"{policy.STATUS_INFERRED} (heuristic) under {policy.POLICY_VERSION}" in response.text


# --- The decision page -----------------------------------------------------------------


def test_the_decision_page_renders_v2_and_attributed_outcomes_truthfully(harness):
    seeded(harness)
    post_created(harness, open_observation("evt-test-o-inferred", meeting=False))
    attribute_ledger(harness)
    post_created(
        harness,
        open_observation(
            "evt-test-o-unattributed",
            observed_at="2026-05-02T00:00:00Z",
            source_decision_event_id=DECISION_EVENT_ID,
        ),
    )

    html = page(harness)
    table = rows(html, "outcomes-table")
    assert len(table) == 3
    canonical = next(r for r in table if r[0] == "90 days")
    inferred = next(r for r in table if stored_form("2026-05-01T00:00:00Z") in r[0])
    unattributed = next(r for r in table if stored_form("2026-05-02T00:00:00Z") in r[0])

    # The canonical v1 outcome: direct, no heuristic label, the decision on this page.
    assert canonical[5].startswith(policy.STATUS_DIRECT)
    assert "heuristic" not in canonical[5]
    assert f"policy-resolved action {ACTION_EVENT_ID}" in canonical[5]
    resolved_here = f"policy-resolved decision {DECISION_EVENT_ID} (the decision on this page)"
    assert resolved_here in canonical[5]
    assert f"policy {policy.POLICY_VERSION}" in canonical[5]
    assert f"method {policy.METHOD_EXPLICIT_REFERENCE}" in canonical[5]
    assert "attribution window 90 days" in canonical[5]
    evaluated_note = (
        "(recorded reference; the attribution column shows how the policy evaluated it)"
    )
    assert evaluated_note in canonical[4]

    # The v2 inferred outcome: open window, unknown shown as unknown, heuristic label.
    assert inferred[0].startswith("window open")
    assert "as of 2026-05-01T00:00:00.000000Z" in inferred[0]
    assert inferred[1] == "reply: unknown meeting: no opportunity: unknown"
    assert inferred[5].startswith(f"{policy.STATUS_INFERRED} heuristic")
    assert "no source-provided reference recorded" in inferred[4]
    assert NOT_YET_EVALUATED not in inferred[5]

    # The unattributed outcome, and only it, reads not yet evaluated.
    assert unattributed[5].startswith(NOT_YET_EVALUATED)
    assert "(the decision on this page, as a recorded claim)" in unattributed[4]
    assert "(source-provided claim, not validated by an attribution policy)" in unattributed[4]
    assert element(html, "outcomes").count(NOT_YET_EVALUATED) == 1


def test_an_unresolved_outcome_stays_visible_on_the_page(harness):
    seeded(harness)
    # Observed before the canonical action occurred: nothing is eligible.
    post_created(harness, open_observation("evt-test-o-early", observed_at="2026-04-17T10:07:00Z"))
    attribute_ledger(harness)
    table = rows(page(harness), "outcomes-table")
    (early,) = [r for r in table if r[0].startswith("window open")]
    assert early[5].startswith(policy.STATUS_UNRESOLVED)
    assert "No action or decision was resolved" in early[5]
    assert "heuristic" not in early[5]
    assert NOT_YET_EVALUATED not in early[5]


def test_a_result_resolving_to_another_decision_says_it_is_not_this_one(harness):
    seeded(harness)
    post_created(
        harness,
        decision_copy_envelope("evt-test-decision-second", boundary="2026-04-18T00:00:00Z"),
        action_envelope(
            "evt-test-action-second",
            occurred_at="2026-04-18T00:01:00Z",
            decision_event_id="evt-test-decision-second",
        ),
        open_observation("evt-test-o-second"),
    )
    attribute_ledger(harness)
    table = rows(page(harness), "outcomes-table")
    (second,) = [r for r in table if r[0].startswith("window open")]
    elsewhere = "policy-resolved decision evt-test-decision-second (not the decision on this page)"
    assert elsewhere in second[5]
    other = rows(page(harness, "evt-test-decision-second"), "outcomes-table")
    (same,) = [r for r in other if r[0].startswith("window open")]
    assert "(the decision on this page)" in same[5]


def test_a_superseded_version_stays_inspectable_and_names_its_effective_version(harness):
    seed_and_attribute(harness)
    post_created(
        harness,
        outcome_v2_envelope(
            "evt-test-o-closing",
            observed_at=CLOSES,
            window_opened_at=OPENED,
            window_closes_at=CLOSES,
            evaluation_state="closed",
            reply=False,
            meeting=False,
            opportunity=False,
            supersedes_outcome_event_id=OUTCOME_EVENT_ID,
        ),
    )
    table = rows(page(harness), "outcomes-table")
    assert len(table) == 2
    original = next(r for r in table if r[0].startswith("90 days"))
    closing = next(r for r in table if r[0].startswith("window closed"))
    assert "superseded; the effective version is evt-test-o-closing" in original[0]
    assert original[5].startswith(policy.STATUS_DIRECT)  # its own retained result
    assert "superseded" not in closing[0]
    assert closing[5].startswith(NOT_YET_EVALUATED)  # nothing inherited


# --- Reads stay read-only ----------------------------------------------------------------


def test_trace_and_decision_pages_write_nothing_with_attributions_and_v2_outcomes(harness):
    seed_and_attribute(harness)
    post_created(harness, open_observation("evt-test-o-v2"))
    attribute_ledger(harness)
    snapshot = harness.snapshot()
    tables = set(inspect(harness.engine).get_table_names())

    with captured_statements(harness.app.state.engine) as statements:
        assert harness.client.get(TRACE_URL).status_code == 200
        for query in ("", f"?current={v5_1_hash()}"):
            assert harness.client.get(decision_url() + query).status_code == 200

    assert [s for s in statements if "outcome_attributions" in s]
    for statement in statements:
        assert not statement.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")), statement
    assert harness.snapshot() == snapshot
    assert set(inspect(harness.engine).get_table_names()) == tables


# --- The published contract ----------------------------------------------------------------


def test_the_api_names_both_outcome_versions_and_publishes_both_payloads(client):
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/v1/decision-events"]["post"]
    assert "schema version 1" in post["summary"]
    assert "outcome.evaluated also accepts schema version 2" in post["summary"]
    components = schema["components"]["schemas"]
    for name in (
        "OutcomeEvaluatedEnvelope",
        "OutcomeEvaluatedPayload",
        "OutcomeEvaluatedV2Envelope",
        "OutcomeEvaluatedV2Payload",
        "OutcomeAttributedEnvelope",
        "OutcomeAttributedPayload",
    ):
        assert name in components, name
    assert components["OutcomeEvaluatedEnvelope"]["properties"]["schema_version"]["const"] == "1"
    assert components["OutcomeEvaluatedV2Envelope"]["properties"]["schema_version"]["const"] == "2"
