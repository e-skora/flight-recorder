"""AC-02: the preserved NovaSignal AI context + logic `v5.1` -> score 51, `DO_NOT_PRIORITIZE`.

The counterfactual, `R(Lc, H(d))`, through the application's own replay path:
`replay` re-verifies and reproduces the original, reads the same sealed `H(d)`
through `load_context`, verifies the explicitly selected current artifact, and
evaluates. The pure-evaluator run over the hand-built canonical context is only
a cross-check here; the assertions against the seeded ledger are the acceptance.

The comparison must explain the entire -35 (AC-02, `PRODUCT.md` §4.5): funding
reweighted +18 -> +4 and the historically available, `v3.2`-ignored low
integration pressure consumed at -21. A third test-only artifact exercises the
`removed` and `changed` classifications the canonical pair does not reach.

A current artifact that is unregistered, mislabeled, or written for another
evaluator, and an original that cannot be reproduced exactly, each fail
explicitly before any counterfactual evaluation (AC-07, INV-05, INV-09).
"""

import json

import pytest
from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF, events, logic_artifacts
from flight_recorder.logic import evaluator as evaluator_module
from flight_recorder.logic.evaluator import InputState, evaluate
from flight_recorder.replay import counterfactual as counterfactual_module
from flight_recorder.replay.counterfactual import (
    COUNTERFACTUAL_LABEL,
    ORIGINAL_LABEL,
    LogicIdentity,
    compare,
    replay,
)
from flight_recorder.replay.reconstruct import ArtifactMissing, IntegrityFailure, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    RESULT_FIELDS,
    assert_same_reconstruction,
    canonical_boundary,
    canonical_by_type,
    canonical_context,
    canonical_evidence_ids,
    consumed_versions,
    derived_artifact_envelope,
    factor,
    logic_artifact,
    logic_artifact_model,
    register_derived_artifact,
    replay_under,
    seed_all,
    v5_1_hash,
)
from tests.invariants.test_inv_05_evaluator_integrity import refuse_to_evaluate

V51_HASH = v5_1_hash()

#: What `v5.1` does with each of its six factors over `H(d)`: (matched, contribution).
V51_FACTORS = {
    "employee_count": (True, 25),
    "industry": (True, 20),
    "funding_event": (True, 4),
    "open_platform_engineering_roles": (True, 15),
    "headquarters_country": (True, 8),
    "verified_integration_pressure": (True, -21),
}

#: The state of every preserved input of `H(d)` under `v5.1`.
V51_CONTEXT_STATES = {
    **{key: InputState.CONSUMED for key in V51_FACTORS},
    "head_of_platform_start_date": InputState.IGNORED,
    "website_intent": InputState.UNAVAILABLE,
}


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def decision_fixture_hash() -> str:
    return canonical_by_type("decision.recorded")["payload"]["logic_artifact"]["artifact_hash"]


def test_the_v5_1_hash_is_the_registered_artifacts_hash(seeded):
    with seeded.engine.connect() as conn:
        registered = conn.execute(
            select(logic_artifacts.c.artifact_hash).where(logic_artifacts.c.logic_version == "v5.1")
        ).scalar_one()
    assert V51_HASH == registered


def test_the_canonical_context_under_v5_1_is_51_do_not_prioritize(seeded):
    cf = replay_under(seeded, V51_HASH)

    assert cf.label == COUNTERFACTUAL_LABEL
    assert (cf.result.score, cf.result.threshold, cf.result.output) == (
        51,
        75,
        "DO_NOT_PRIORITIZE",
    )
    assert cf.current_logic_version == "v5.1"
    assert cf.current_artifact_hash == V51_HASH
    assert cf.stored_artifact_hash == V51_HASH
    assert cf.recomputed_artifact_hash == V51_HASH
    assert cf.current_evaluator_version == "evaluator-v1"
    assert cf.runtime_evaluator_version == "evaluator-v1"
    assert cf.decision_boundary == canonical_boundary()
    assert cf.decision_boundary.tzinfo is not None
    assert cf.decision_boundary.utcoffset().total_seconds() == 0

    ids = canonical_evidence_ids()
    for key, (matched, contribution) in V51_FACTORS.items():
        evaluated = factor(cf.result, key)
        assert evaluated.input_state is InputState.CONSUMED, key
        assert evaluated.matched is matched, key
        assert evaluated.contribution == contribution, key
        assert evaluated.evidence_version_id == ids[key], key
    assert (
        factor(cf.result, "verified_integration_pressure").evidence_version_id
        == "ev-novasignal-verified-integration-pressure-v1"
    )
    assert consumed_versions(cf.result) == {key: ids[key] for key in V51_FACTORS}
    assert cf.result.ignored_inputs == ("head_of_platform_start_date",)
    assert cf.result.context_states["website_intent"] is InputState.UNAVAILABLE
    assert dict(cf.result.context_states) == V51_CONTEXT_STATES
    assert len(cf.result.context_states) == 8


def test_the_original_inside_the_counterfactual_is_the_exact_reconstruction(seeded):
    cf = replay_under(seeded, V51_HASH)
    with seeded.engine.connect() as conn:
        original = reconstruct(conn, DECISION_EVENT_ID)

    assert_same_reconstruction(cf.original, original)
    assert cf.original.result.score == 86
    assert cf.original.logic_version == "v3.2"
    assert cf.original.artifact_hash == decision_fixture_hash()


def test_the_comparison_explains_the_entire_difference(seeded):
    c = compare(replay_under(seeded, V51_HASH))
    ids = canonical_evidence_ids()

    assert (c.original_score, c.counterfactual_score, c.score_delta) == (86, 51, -35)
    assert (c.original_threshold, c.counterfactual_threshold) == (75, 75)
    assert (c.original_output, c.counterfactual_output) == ("PRIORITIZE", "DO_NOT_PRIORITIZE")
    assert c.output_changed is True
    assert c.historical_logic == LogicIdentity("v3.2", decision_fixture_hash(), "evaluator-v1")
    assert c.current_logic == LogicIdentity("v5.1", V51_HASH, "evaluator-v1")
    assert (c.original_label, c.counterfactual_label) == ("original", "counterfactual")
    assert c.decision_event_id == DECISION_EVENT_ID
    assert c.decision_boundary == canonical_boundary()

    assert [change.key for change in c.contributions] == sorted(V51_FACTORS)
    by_key = {change.key: change for change in c.contributions}

    funding = by_key["funding_event"]
    assert funding.change == "reweighted"
    assert (funding.original_contribution, funding.counterfactual_contribution) == (18, 4)
    assert funding.contribution_delta == -14
    assert (funding.original.weight, funding.counterfactual.weight) == (18, 4)
    assert funding.original.rule == funding.counterfactual.rule
    assert (funding.original_state, funding.counterfactual_state) == (
        InputState.CONSUMED,
        InputState.CONSUMED,
    )
    assert funding.evidence_version_id == ids["funding_event"] == "ev-novasignal-funding-event-v1"

    pressure = by_key["verified_integration_pressure"]
    assert pressure.change == "added"
    assert pressure.original is None
    assert pressure.original_state is InputState.IGNORED
    assert pressure.counterfactual_state is InputState.CONSUMED
    assert (pressure.original_contribution, pressure.counterfactual_contribution) == (0, -21)
    assert pressure.contribution_delta == -21
    assert (
        pressure.evidence_version_id
        == ids["verified_integration_pressure"]
        == "ev-novasignal-verified-integration-pressure-v1"
    )

    unchanged = (
        "employee_count",
        "industry",
        "open_platform_engineering_roles",
        "headquarters_country",
    )
    for key in unchanged:
        change = by_key[key]
        assert change.change == "unchanged", key
        assert change.contribution_delta == 0, key
        assert change.original == change.counterfactual, key
        assert change.evidence_version_id == ids[key], key

    assert sum(change.contribution_delta for change in c.contributions) == -35 == c.score_delta
    assert c.missing_inputs == ()
    assert c.ignored_by_historical == (
        "head_of_platform_start_date",
        "verified_integration_pressure",
    )
    assert c.ignored_by_current == ("head_of_platform_start_date",)


# --- `removed` and `changed`, on a third test-only artifact -----------------

REMOVED_CHANGED_ID = "logic-account-prioritization-test-removed-changed"
REMOVED_CHANGED_VERSION = "test-removed-changed"


def removed_changed_artifact_envelope() -> dict:
    """`v5.1` with `headquarters_country` dropped, the `employee_count` rule text
    changed (184 still matches), and the `open_platform_engineering_roles` rule
    text changed (7 no longer matches); weights untouched."""
    factors = []
    for original in logic_artifact("v5.1")["factors"]:
        edited = dict(original)
        if edited["key"] == "headquarters_country":
            continue
        if edited["key"] == "employee_count":
            edited["rule"] = "employee_count between 100 and 300 inclusive"
        if edited["key"] == "open_platform_engineering_roles":
            edited["rule"] = "open_platform_engineering_roles at least 10"
        factors.append(edited)
    assert [f["weight"] for f in factors] == [25, 20, 4, 15, -21]
    return derived_artifact_envelope(
        REMOVED_CHANGED_ID,
        REMOVED_CHANGED_VERSION,
        factors,
        event_id="evt-system-logic-artifact-test-removed-changed",
    )


def test_the_comparison_classifies_removed_and_changed_factors(seeded):
    test_rc_hash = register_derived_artifact(seeded, removed_changed_artifact_envelope())
    ids = canonical_evidence_ids()

    cf = replay_under(seeded, test_rc_hash)
    assert cf.current_logic_version == REMOVED_CHANGED_VERSION
    assert (cf.result.score, cf.result.output) == (28, "DO_NOT_PRIORITIZE")
    assert cf.result.score == 25 + 20 + 4 + 0 + 0 - 21

    c = compare(cf)
    assert c.score_delta == -58
    assert [change.key for change in c.contributions] == sorted(V51_FACTORS)
    by_key = {change.key: change for change in c.contributions}

    headquarters = by_key["headquarters_country"]
    assert headquarters.change == "removed"
    assert headquarters.original == factor(cf.original.result, "headquarters_country")
    assert headquarters.original.contribution == 8
    assert headquarters.counterfactual is None
    assert headquarters.original_state is InputState.CONSUMED
    assert headquarters.counterfactual_state is InputState.IGNORED
    assert (headquarters.original_contribution, headquarters.counterfactual_contribution) == (8, 0)
    assert headquarters.contribution_delta == -8
    assert headquarters.evidence_version_id == ids["headquarters_country"]
    assert headquarters.evidence_version_id == "ev-novasignal-headquarters-country-v1"

    employees = by_key["employee_count"]
    assert employees.change == "changed"
    assert employees.original.rule != employees.counterfactual.rule
    assert (employees.original.matched, employees.counterfactual.matched) == (True, True)
    assert (employees.original_contribution, employees.counterfactual_contribution) == (25, 25)
    assert employees.contribution_delta == 0
    assert employees.evidence_version_id == ids["employee_count"]

    roles = by_key["open_platform_engineering_roles"]
    assert roles.change == "changed"
    assert roles.original.rule != roles.counterfactual.rule
    assert (roles.original.matched, roles.counterfactual.matched) == (True, False)
    assert (roles.original_contribution, roles.counterfactual_contribution) == (15, 0)
    assert roles.contribution_delta == -15
    assert roles.original.evidence_version_id == ids["open_platform_engineering_roles"]
    assert roles.counterfactual.evidence_version_id == ids["open_platform_engineering_roles"]
    assert roles.evidence_version_id == ids["open_platform_engineering_roles"]

    assert (by_key["funding_event"].change, by_key["funding_event"].contribution_delta) == (
        "reweighted",
        -14,
    )
    assert (
        by_key["verified_integration_pressure"].change,
        by_key["verified_integration_pressure"].contribution_delta,
    ) == ("added", -21)
    assert (by_key["industry"].change, by_key["industry"].contribution_delta) == ("unchanged", 0)

    assert sum(change.contribution_delta for change in c.contributions) == -58 == c.score_delta
    assert c.missing_inputs == ()
    assert c.ignored_by_current == ("head_of_platform_start_date", "headquarters_country")
    assert c.ignored_by_historical == (
        "head_of_platform_start_date",
        "verified_integration_pressure",
    )
    for change in c.contributions:
        assert change.evidence_version_id == ids[change.key], change.key


# --- Determinism and the pure-evaluator cross-check ---------------------------


def test_replay_and_comparison_are_deterministic(seeded):
    first = replay_under(seeded, V51_HASH)
    second = replay_under(seeded, V51_HASH)

    assert first == second
    for field in RESULT_FIELDS:
        assert getattr(first.result, field) == getattr(second.result, field), field
    first_c, second_c = compare(first), compare(second)
    assert first_c == second_c
    for a, b in zip(first_c.contributions, second_c.contributions, strict=True):
        assert a == b, a.key


def test_replay_through_the_application_path_agrees_with_the_pure_evaluator(seeded):
    cf = replay_under(seeded, V51_HASH)
    pure = evaluate(logic_artifact_model("v5.1"), canonical_context(), canonical_boundary())

    assert (cf.result.score, cf.result.output) == (pure.score, pure.output)
    assert cf.result.factors == pure.factors
    assert dict(cf.result.context_states) == dict(pure.context_states)


# --- Explicit failures for the current artifact (AC-07, INV-05, INV-09) --------


def test_an_unregistered_current_artifact_is_an_explicit_failure(seeded):
    with seeded.engine.connect() as conn, pytest.raises(ArtifactMissing) as caught:
        replay(conn, DECISION_EVENT_ID, "0" * 64)
    assert caught.value.artifact_hash == "0" * 64

    with seeded.engine.connect() as conn:
        assert reconstruct(conn, DECISION_EVENT_ID).result.score == 86


def test_a_current_artifact_for_another_evaluator_fails_before_any_evaluation(seeded, monkeypatch):
    other_hash = register_derived_artifact(
        seeded,
        derived_artifact_envelope(
            "logic-account-prioritization-v5.1-other-evaluator",
            "v5.1-other-evaluator",
            logic_artifact("v5.1")["factors"],
            event_id="evt-system-logic-artifact-v5.1-other-evaluator",
            evaluator_version="evaluator-v2",
        ),
    )

    # Only the counterfactual side is stubbed: the original legitimately
    # evaluates with the real evaluator before the current artifact is checked.
    monkeypatch.setattr(counterfactual_module, "evaluate", refuse_to_evaluate)
    with seeded.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        replay(conn, DECISION_EVENT_ID, other_hash)
    assert caught.value.field == "evaluator_version"
    assert (caught.value.stored, caught.value.recomputed) == ("evaluator-v2", "evaluator-v1")

    monkeypatch.undo()
    assert replay_under(seeded, V51_HASH).result.score == 51


MISLABELED_HASH = "0" * 64
MISLABELED_ID = "logic-account-prioritization-v5.1-mislabeled"
MISLABELED_VERSION = "v5.1-mislabeled"


def register_mislabeled_v5_1_artifact(harness) -> str:
    """The `register_mislabeled_artifact` pattern of `test_inv_05_evaluator_integrity.py`
    for `v5.1`-derived content: a `logic_artifacts` row filed under a hash that
    is not its content's hash. Returns the content's true hash."""
    with harness.engine.connect() as conn:
        real_text = conn.execute(
            select(logic_artifacts.c.artifact_json).where(logic_artifacts.c.logic_version == "v5.1")
        ).scalar_one()

    content = json.loads(real_text)
    content["artifact_id"] = MISLABELED_ID
    content["logic_version"] = MISLABELED_VERSION
    text = canonical_text(content)
    source_event_id = "evt-system-logic-artifact-v5.1-mislabeled"

    with harness.engine.begin() as conn:
        conn.execute(
            events.insert().values(
                event_id=source_event_id,
                schema_version="1",
                event_type="logic_artifact.registered",
                source="direct-insert-for-ac-02",
                account_ref=SYSTEM_ACCOUNT_REF,
                occurred_at="2026-01-12T09:00:02.000000Z",
                recorded_at="2026-01-12T09:00:02.000000Z",
                canonical_hash=canonical_hash({"mislabeled": source_event_id}),
                payload=canonical_text({"artifact": content}),
            )
        )
        conn.execute(
            logic_artifacts.insert().values(
                artifact_hash=MISLABELED_HASH,
                artifact_id=MISLABELED_ID,
                logic_version=MISLABELED_VERSION,
                decision_class=content["decision_class"],
                artifact_schema_version=content["artifact_schema_version"],
                evaluator_version=content["evaluator_version"],
                artifact_json=text,
                source_event_id=source_event_id,
            )
        )
    return canonical_hash(content)


def test_a_mislabeled_current_artifact_hash_fails_before_any_evaluation(seeded, monkeypatch):
    true_hash = register_mislabeled_v5_1_artifact(seeded)
    assert true_hash != MISLABELED_HASH

    monkeypatch.setattr(counterfactual_module, "evaluate", refuse_to_evaluate)
    with seeded.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        replay(conn, DECISION_EVENT_ID, MISLABELED_HASH)
    assert caught.value.field == "artifact_hash"
    assert caught.value.stored == MISLABELED_HASH
    assert caught.value.recomputed == true_hash


def test_a_decision_whose_original_cannot_be_reproduced_gets_no_counterfactual(seeded, monkeypatch):
    monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSION", "evaluator-v2")
    with seeded.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        replay(conn, DECISION_EVENT_ID, V51_HASH)
    assert caught.value.field == "evaluator_version"
    assert (caught.value.stored, caught.value.recomputed) == ("evaluator-v1", "evaluator-v2")

    monkeypatch.undo()
    assert replay_under(seeded, V51_HASH).result.score == 51


def test_replaying_a_decision_under_its_own_artifact_is_still_a_counterfactual(seeded):
    cf = replay_under(seeded, decision_fixture_hash())
    assert cf.label == COUNTERFACTUAL_LABEL
    assert cf.current_logic_version == "v3.2"
    assert cf.result == cf.original.result
    c = compare(cf)
    assert c.score_delta == 0
    assert c.output_changed is False
    assert {change.change for change in c.contributions} == {"unchanged"}
    assert c.original_label == ORIGINAL_LABEL


def test_the_removed_changed_artifact_is_a_distinct_identity(seeded):
    """The test-only artifact never touches `fixtures/canonical/`: it is a copy
    with its own identity, registered through the collector."""
    envelope = removed_changed_artifact_envelope()
    assert logic_artifact("v5.1")["artifact_id"] == "logic-account-prioritization-v5.1"
    assert envelope["payload"]["artifact"]["artifact_id"] == REMOVED_CHANGED_ID
    assert canonical_hash(envelope["payload"]["artifact"]) != V51_HASH
