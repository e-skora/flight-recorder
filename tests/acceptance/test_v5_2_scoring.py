"""D-017: what `v5.2` scores, and the positive-weight bound over any artifact.

`v5.1` cannot output `PRIORITIZE` for any context: its positive factors sum to
72 against a threshold of 75. `v5.2` replaces its negative low-pressure factor
with a positive high-pressure one, so both outputs are reachable. This module
proves both outputs against real contexts, keeps the three missing-evidence
states distinct at the engine, and exercises the two bound helpers over
non-canonical artifacts so they cannot be satisfied by matching version labels
(INV-03, INV-05; AC-01, AC-02).

Every weight here is a synthetic demonstration choice, not a business rule.

Tests 6 to 9 of the task's numbering; test 8's *rendered* half lives in
`tests/acceptance/test_positive_weight_bound.py`.
"""

import copy

import pytest

from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.fixtures import canonical_artifacts, current_logic_artifact
from flight_recorder.logic.evaluator import (
    ContextInput,
    InputState,
    evaluate,
    positive_weight_score_bound,
    threshold_exceeds_positive_weight_bound,
)
from tests.conftest import (
    canonical_boundary,
    canonical_context,
    derived_artifact_envelope,
    logic_artifact,
    replace_context,
)

PRESSURE = "verified_integration_pressure"


def v3_2() -> LogicArtifact:
    return canonical_artifacts()["v3.2"]


def v5_1() -> LogicArtifact:
    return canonical_artifacts()["v5.1"]


def v5_2() -> LogicArtifact:
    return current_logic_artifact()


def score(artifact: LogicArtifact, context) -> tuple[int, str]:
    result = evaluate(artifact, context, canonical_boundary())
    return result.score, result.output


def bound(artifact: LogicArtifact) -> tuple[int, bool]:
    return (
        positive_weight_score_bound(artifact),
        threshold_exceeds_positive_weight_bound(artifact),
    )


def high_pressure(context) -> tuple[ContextInput, ...]:
    """The canonical context with verified integration pressure recorded `HIGH`."""
    return replace_context(
        context,
        PRESSURE,
        ContextInput(key=PRESSURE, availability="available", value="HIGH"),
    )


# --- 6. The canonical context under all three artifacts -------------------------


def test_the_canonical_context_scores_86_51_and_72():
    """Test 6. `v5.2` lands between the two: 72, still below threshold 75."""
    context = canonical_context()

    assert score(v3_2(), context) == (86, "PRIORITIZE")
    assert score(v5_1(), context) == (51, "DO_NOT_PRIORITIZE")
    assert score(v5_2(), context) == (72, "DO_NOT_PRIORITIZE")


# --- 7. Both outputs are reachable under v5.2 -----------------------------------


def test_the_four_required_factors_alone_reach_the_threshold_exactly():
    """Test 7. 25 + 20 + 15 + 15 = 75, with no funding and no HQ input at all."""
    context = (
        ContextInput(key="employee_count", availability="available", value=184),
        ContextInput(key="industry", availability="available", value="B2B AI Software"),
        ContextInput(key="open_platform_engineering_roles", availability="available", value=7),
        ContextInput(key=PRESSURE, availability="available", value="HIGH"),
    )

    assert score(v5_2(), context) == (75, "PRIORITIZE")


def test_the_full_canonical_shaped_context_with_high_pressure_scores_87():
    """Test 7. Every positive factor matching: the positive-weight bound itself."""
    assert score(v5_2(), high_pressure(canonical_context())) == (87, "PRIORITIZE")
    assert positive_weight_score_bound(v5_2()) == 87


def test_the_same_context_without_funding_evidence_still_prioritizes_at_83():
    """Test 7. A missing *optional* bonus input does not block `PRIORITIZE`.

    This is the counter-example test 8 must not contradict: absence alone is
    not what keeps a context below the threshold.
    """
    context = replace_context(high_pressure(canonical_context()), "funding_event", None)

    assert score(v5_2(), context) == (83, "PRIORITIZE")


def test_a_weak_fit_context_scores_only_the_two_factors_that_match():
    """Test 7. Only US headquarters and HIGH pressure match: 8 + 15 = 23."""
    context = (
        ContextInput(key="employee_count", availability="available", value=12_000),
        ContextInput(key="industry", availability="available", value="Industrial Robotics"),
        ContextInput(key="open_platform_engineering_roles", availability="available", value=1),
        ContextInput(key="headquarters_country", availability="available", value="United States"),
        ContextInput(key=PRESSURE, availability="available", value="HIGH"),
    )

    assert score(v5_2(), context) == (23, "DO_NOT_PRIORITIZE")


# --- 8. Missing integration-pressure evidence, at the engine --------------------


@pytest.mark.parametrize(
    ("label", "entry", "expected_state"),
    [
        ("absent", None, InputState.ABSENT),
        (
            "unavailable",
            ContextInput(key=PRESSURE, availability="unavailable"),
            InputState.UNAVAILABLE,
        ),
        (
            "consumed",
            ContextInput(key=PRESSURE, availability="available", value="LOW"),
            InputState.CONSUMED,
        ),
    ],
)
def test_missing_or_negative_pressure_evidence_stays_at_72_and_stays_distinguishable(
    label, entry, expected_state
):
    """Test 8. One score, three states: an equal number never collapses them.

    Under `v5.2` the pressure factor contributes its 15 only on a `HIGH` match,
    so absent, explicitly unavailable and consumed-but-`LOW` all score 72. They
    are three different facts and the engine keeps them apart (INV-03). The
    rendered half of this proof is in the panel module.
    """
    context = replace_context(canonical_context(), PRESSURE, entry)

    result = evaluate(v5_2(), context, canonical_boundary())
    factor = next(f for f in result.factors if f.key == PRESSURE)

    assert (result.score, result.output) == (72, "DO_NOT_PRIORITIZE"), label
    assert factor.input_state is expected_state, label
    assert factor.contribution == 0, label


# --- 9. The bound, over the shipped three and over non-canonical artifacts ------


def test_the_bound_and_its_warning_for_the_three_shipped_artifacts():
    """Test 9. 86/False, 72/True, 87/False."""
    assert bound(v3_2()) == (86, False)
    assert bound(v5_1()) == (72, True)
    assert bound(v5_2()) == (87, False)


def derived(logic_version: str, factors: list[dict], *, threshold: int | None = None):
    """A non-canonical artifact through the same envelope builder the panel uses."""
    envelope = derived_artifact_envelope(
        f"logic-account-prioritization-{logic_version}",
        logic_version,
        factors,
        event_id=f"evt-system-logic-artifact-{logic_version}",
        threshold=threshold,
    )
    return LogicArtifact.model_validate(envelope["payload"]["artifact"])


def test_the_bound_is_a_generic_calculation_not_a_version_lookup():
    """Test 9. Non-canonical artifacts, built through `derived_artifact_envelope`.

    Nothing about these carries a shipped version label, so a helper that
    matched on `v5.1` would answer all of them wrongly.
    """
    factors = copy.deepcopy(logic_artifact("v5.1")["factors"])

    # The `v5.1` weights under a label no shipped artifact uses: still 72/True.
    assert bound(derived("test-bound-unreachable", factors)) == (72, True)

    # The same weights at a threshold of 60: the bound is unchanged, the
    # warning is not. The bound is a property of the weights alone.
    assert bound(derived("test-bound-reachable", factors, threshold=60)) == (72, False)

    # Zero and negative weights raise no score, so neither enters the bound.
    mixed = derived(
        "test-bound-mixed",
        [
            {"key": "employee_count", "rule": "employee_count at least 1", "weight": 40},
            {"key": "industry", "rule": "industry equals 'B2B AI Software'", "weight": 0},
            {"key": PRESSURE, "rule": f"{PRESSURE} equals 'LOW'", "weight": -100},
        ],
        threshold=41,
    )
    assert bound(mixed) == (40, True)


def test_the_bound_is_an_upper_bound_and_not_a_proven_maximum():
    """Test 9. A positive weight on a rule no value satisfies still counts.

    The closed grammar accepts a reversed interval. Nothing can match it, so
    this artifact's real maximum is 40 while its reported bound is 90. The
    helper reports the bound and says so; it does not solve rules.
    """
    unsatisfiable = derived(
        "test-bound-unsatisfiable",
        [
            {"key": "employee_count", "rule": "employee_count at least 1", "weight": 40},
            {
                "key": "open_platform_engineering_roles",
                "rule": "open_platform_engineering_roles between 500 and 50 inclusive",
                "weight": 50,
            },
        ],
        threshold=85,
    )

    # No warning: 90 is not below 85. The absence proves nothing, and in fact
    # this threshold cannot be reached either.
    assert bound(unsatisfiable) == (90, False)

    generous = (
        ContextInput(key="employee_count", availability="available", value=10_000),
        ContextInput(key="open_platform_engineering_roles", availability="available", value=200),
    )
    assert score(unsatisfiable, generous) == (40, "DO_NOT_PRIORITIZE")
