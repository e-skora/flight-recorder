"""AC-06 / INV-03 / INV-09: an input current logic expects but `H(d)` lacks stays missing.

Canonical `v5.1` references no input that `H(d)` lacks, so this proof uses a
distinct test-only artifact with its own identity: the six `v5.1` factors plus
`website_intent` (a key `H(d)` records as explicitly `unavailable`) and
`partner_referral` (a key absent from `H(d)` entirely), each weighted 30 on
purpose. A builder who substitutes any value, treats a missing input as a
match, or reads current data pushes the score to at least 81 and flips the
output; the assertions below hold it at 51. Canonical `v5.1` and every file
under `fixtures/canonical/` are untouched, and nothing here claims `v5.1`
requires `website_intent`.

The third test takes a decision whose `H(d)` lacks a *supported* input,
`verified_integration_pressure` -- ignored by `v3.2`, consumed by `v5.1`, so
the one input where a filled gap would change the counterfactual -- and proves
that neither a pre-boundary version the decision did not preserve nor a later
version fills it (INV-02, INV-03, INV-09; `PRODUCT.md` §5 "Decision-time
boundary").
"""

import copy

import pytest

from flight_recorder.logic.evaluator import InputState
from flight_recorder.replay.counterfactual import MissingInput, compare
from flight_recorder.replay.reconstruct import load_decision_row, reconstruct
from tests.acceptance.test_ac_02_counterfactual import V51_CONTEXT_STATES, V51_FACTORS
from tests.acceptance.test_ac_04_isolation import SECOND_ACCOUNT
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    assert_same_comparison,
    assert_same_counterfactual,
    assert_same_reconstruction,
    canonical_by_type,
    canonical_evidence_ids,
    canonical_raw,
    consumed_versions,
    decision_rows,
    derived_artifact_envelope,
    evidence_envelope,
    evidence_version_row,
    factor,
    logic_artifact,
    register_artifacts,
    register_derived_artifact,
    replay_under,
    seed_all,
    v5_1_hash,
)

MISSING_INPUTS_ID = "logic-account-prioritization-test-missing-inputs"
MISSING_INPUTS_VERSION = "test-missing-inputs"
MISSING_VIP_DECISION_ID = "evt-test-decision-missing-vip"
VIP = "verified_integration_pressure"
VIP_LATER = "ev-novasignal-verified-integration-pressure-later"


def missing_input_artifact_envelope() -> dict:
    factors = [
        *logic_artifact("v5.1")["factors"],
        {"key": "website_intent", "rule": "website_intent equals 'HIGH'", "weight": 30},
        {"key": "partner_referral", "rule": "partner_referral equals 'YES'", "weight": 30},
    ]
    return derived_artifact_envelope(
        MISSING_INPUTS_ID,
        MISSING_INPUTS_VERSION,
        factors,
        event_id="evt-system-logic-artifact-test-missing-inputs",
    )


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def assert_missing_inputs_result(result) -> None:
    """The two missing factors contribute nothing; the six `v5.1` factors are
    exactly as under canonical `v5.1`; `partner_referral` is a state of the
    factor, never of a preserved input."""
    website_intent = factor(result, "website_intent")
    assert website_intent.input_state is InputState.UNAVAILABLE
    assert website_intent.matched is False
    assert website_intent.contribution == 0
    assert website_intent.evidence_version_id is None

    partner_referral = factor(result, "partner_referral")
    assert partner_referral.input_state is InputState.ABSENT
    assert partner_referral.matched is False
    assert partner_referral.contribution == 0
    assert partner_referral.evidence_version_id is None

    ids = canonical_evidence_ids()
    for key, (matched, contribution) in V51_FACTORS.items():
        evaluated = factor(result, key)
        assert evaluated.input_state is InputState.CONSUMED, key
        assert evaluated.matched is matched, key
        assert evaluated.contribution == contribution, key
        assert evaluated.evidence_version_id == ids[key], key

    assert dict(result.context_states) == V51_CONTEXT_STATES
    assert "partner_referral" not in result.context_states
    assert (result.score, result.threshold, result.output) == (51, 75, "DO_NOT_PRIORITIZE")


def test_missing_inputs_stay_missing_and_contribute_nothing(seeded):
    test_hash = register_derived_artifact(seeded, missing_input_artifact_envelope())
    cf = replay_under(seeded, test_hash)

    assert cf.current_logic_version == MISSING_INPUTS_VERSION
    assert cf.current_artifact_hash == test_hash
    assert_missing_inputs_result(cf.result)
    assert len(cf.result.factors) == 8


def test_the_comparison_names_every_missing_input_with_its_state(seeded):
    test_hash = register_derived_artifact(seeded, missing_input_artifact_envelope())
    cf = replay_under(seeded, test_hash)
    c = compare(cf)

    assert c.missing_inputs == (
        MissingInput("partner_referral", InputState.ABSENT),
        MissingInput("website_intent", InputState.UNAVAILABLE),
    )
    by_key = {change.key: change for change in c.contributions}
    assert len(c.contributions) == 8

    website_intent = by_key["website_intent"]
    assert website_intent.change == "added"
    assert website_intent.original is None
    assert website_intent.original_state is InputState.UNAVAILABLE
    assert website_intent.counterfactual_state is InputState.UNAVAILABLE
    assert website_intent.contribution_delta == 0
    assert website_intent.evidence_version_id is None

    partner_referral = by_key["partner_referral"]
    assert partner_referral.change == "added"
    assert partner_referral.original is None
    assert partner_referral.original_state is None
    assert partner_referral.counterfactual_state is InputState.ABSENT
    assert partner_referral.contribution_delta == 0
    assert partner_referral.evidence_version_id is None

    assert c.score_delta == -35
    assert sum(change.contribution_delta for change in c.contributions) == c.score_delta


# --- A historically missing supported input is never filled ------------------


def missing_vip_decision_envelope(shape: str) -> dict:
    """The canonical decision with its `verified_integration_pressure` entry made
    `unavailable` or removed; everything else canonical (`v3.2`, the five
    consumed inputs, 86 / 75 / `PRIORITIZE`)."""
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = MISSING_VIP_DECISION_ID
    context = envelope["payload"]["historical_context"]
    index = next(i for i, entry in enumerate(context) if entry["input_key"] == VIP)
    if shape == "unavailable":
        context[index] = {"input_key": VIP, "value": None, "availability": "unavailable"}
    else:
        del context[index]
    assert all(used["input_key"] != VIP for used in envelope["payload"]["consumed_inputs"])
    return envelope


def later_vip_envelope() -> dict:
    """A later `LOW` integration-pressure version, no supersession link."""
    return evidence_envelope(
        "evt-test-later-verified-integration-pressure",
        [
            {
                "evidence_version_id": VIP_LATER,
                "evidence_type": VIP,
                "value": "LOW",
                "basis": ["generated for the AC-06 gap test"],
            }
        ],
        occurred_at="2026-05-02T09:00:00Z",
    )


@pytest.mark.parametrize(
    ("shape", "state"),
    [("unavailable", InputState.UNAVAILABLE), ("absent", InputState.ABSENT)],
)
def test_a_historically_missing_supported_input_is_never_filled_from_evidence_outside_the_sealed_context(  # noqa: E501
    harness: Harness, shape, state
):
    vip_v1 = canonical_evidence_ids()[VIP]
    register_artifacts(harness)
    for index in (0, 1):
        assert harness.post_raw(canonical_raw(index)).status_code == 201
    assert evidence_version_row(harness, vip_v1) is None

    response = harness.post(missing_vip_decision_envelope(shape))
    assert response.status_code == 201, response.json()

    before = replay_under(harness, v5_1_hash(), MISSING_VIP_DECISION_ID)
    assert (before.result.score, before.result.output) == (72, "DO_NOT_PRIORITIZE")
    assert before.result.score == 25 + 20 + 4 + 15 + 8
    missing = factor(before.result, VIP)
    assert missing.input_state is state
    assert missing.matched is False
    assert missing.contribution == 0
    assert missing.evidence_version_id is None
    assert compare(before).missing_inputs == (MissingInput(VIP, state),)
    with harness.engine.connect() as conn:
        original_before = reconstruct(conn, MISSING_VIP_DECISION_ID)
    assert original_before.result.score == 86
    rows_before = decision_rows(harness, MISSING_VIP_DECISION_ID)

    # The canonical `-v1` version: value `LOW`, available before the boundary,
    # so it would have been eligible had the decision preserved it.
    response = harness.post_raw(canonical_raw(2))
    assert response.status_code == 201, response.json()
    with harness.engine.connect() as conn:
        boundary_text = load_decision_row(conn, MISSING_VIP_DECISION_ID).decision_boundary
    assert evidence_version_row(harness, vip_v1).available_at < boundary_text
    # And a later version of the same input.
    response = harness.post(later_vip_envelope())
    assert response.status_code == 201, response.json()

    after = replay_under(harness, v5_1_hash(), MISSING_VIP_DECISION_ID)
    assert_same_counterfactual(after, before)
    assert after.result.score == 72
    still_missing = factor(after.result, VIP)
    assert still_missing.input_state is state
    assert still_missing.evidence_version_id is None
    assert still_missing.contribution == 0
    assert_same_comparison(compare(after), compare(before))
    assert compare(after).missing_inputs == (MissingInput(VIP, state),)

    with harness.engine.connect() as conn:
        original_after = reconstruct(conn, MISSING_VIP_DECISION_ID)
    assert_same_reconstruction(original_after, original_before)
    assert original_after.result.score == 86
    if shape == "unavailable":
        assert original_after.result.context_states[VIP] is InputState.UNAVAILABLE
    else:
        assert VIP not in original_after.result.context_states
    assert decision_rows(harness, MISSING_VIP_DECISION_ID) == rows_before


# --- Current data is never substituted -------------------------------------


def later_evidence_for_every_type() -> list[dict]:
    """A later version of every schema-v1 evidence type, each superseding its
    `-v1`, with values under which every `v5.1` rule evaluates differently."""
    ids = canonical_evidence_ids()
    items = [
        ("employee_count", {"value": 4000}),
        ("industry", {"value": "Industrial Automation"}),
        ("headquarters_country", {"value": "Canada"}),
        ("funding_event", {"value": "Series C", "observed_at": "2026-04-30"}),
        ("open_platform_engineering_roles", {"value": 0}),
        ("head_of_platform_start_date", {"value": "2026-05-01", "observed_at": "2026-05-01"}),
        (
            "verified_integration_pressure",
            {"value": "HIGH", "basis": ["generated for the AC-06 substitution test"]},
        ),
    ]
    return [
        evidence_envelope(
            f"evt-test-later-{evidence_type}",
            [
                {
                    "evidence_version_id": f"ev-novasignal-{evidence_type.replace('_', '-')}-later",
                    "evidence_type": evidence_type,
                    **fields,
                    "supersedes_evidence_version_id": ids[evidence_type],
                }
            ],
            occurred_at=f"2026-05-02T09:{index:02d}:00Z",
        )
        for index, (evidence_type, fields) in enumerate(items)
    ]


def test_current_data_is_never_substituted_for_a_missing_input(seeded):
    test_hash = register_derived_artifact(seeded, missing_input_artifact_envelope())
    before_test = replay_under(seeded, test_hash)
    before_v51 = replay_under(seeded, v5_1_hash())
    with seeded.engine.connect() as conn:
        original_before = reconstruct(conn, DECISION_EVENT_ID)
    rows_before = decision_rows(seeded)

    appended = [*later_evidence_for_every_type(), *SECOND_ACCOUNT]
    assert len(later_evidence_for_every_type()) == 7
    for envelope in appended:
        response = seeded.post(envelope)
        assert response.status_code == 201, (envelope["event_id"], response.json())

    after_test = replay_under(seeded, test_hash)
    assert_same_counterfactual(after_test, before_test)
    assert_same_comparison(compare(after_test), compare(before_test))
    assert_missing_inputs_result(after_test.result)

    after_v51 = replay_under(seeded, v5_1_hash())
    assert_same_counterfactual(after_v51, before_v51)
    assert_same_comparison(compare(after_v51), compare(before_v51))
    ids = canonical_evidence_ids()
    assert consumed_versions(after_v51.result) == {key: ids[key] for key in V51_FACTORS}
    assert after_v51.result.score == 51

    with seeded.engine.connect() as conn:
        assert_same_reconstruction(reconstruct(conn, DECISION_EVENT_ID), original_before)
    assert decision_rows(seeded) == rows_before
