"""`evaluator-v1` as a pure function: `R(L, H)` with no database in sight.

The canonical `v3.2` artifact over the canonical `H(d)` must produce 86 /
`PRIORITIZE`, and the four input states must stay distinct (INV-03): an
available input that fails its rule is still consumed, an unavailable one is
not, an absent one is neither, and an available input no factor reads stays
visible as ignored.
"""

import pytest

from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.logic.evaluator import (
    ContextInput,
    InputState,
    UnsupportedMissingValueBehavior,
    evaluate,
)
from flight_recorder.logic.rules import UnsupportedBoundary
from tests.conftest import (
    canonical_boundary,
    canonical_context,
    logic_artifact,
    logic_artifact_model,
    replace_context,
)

EXPECTED_CONTRIBUTIONS = {
    "employee_count": 25,
    "industry": 20,
    "funding_event": 18,
    "open_platform_engineering_roles": 15,
    "headquarters_country": 8,
}
IGNORED = {"verified_integration_pressure", "head_of_platform_start_date"}


def evaluate_canonical(context=None):
    return evaluate(logic_artifact_model(), context or canonical_context(), canonical_boundary())


def factor(result, key):
    return next(f for f in result.factors if f.key == key)


def test_the_canonical_context_under_v3_2_reproduces_the_recorded_decision():
    result = evaluate_canonical()

    assert (result.score, result.threshold, result.output) == (86, 75, "PRIORITIZE")
    assert {f.key: f.contribution for f in result.factors} == EXPECTED_CONTRIBUTIONS
    assert all(f.input_state is InputState.CONSUMED and f.matched for f in result.factors)


def test_available_inputs_no_factor_reads_are_ignored_not_consumed():
    result = evaluate_canonical()

    assert set(result.ignored_inputs) == IGNORED
    for key in IGNORED:
        assert result.context_states[key] is InputState.IGNORED


def test_an_unavailable_input_no_factor_reads_stays_visible_in_the_context_states():
    """`website_intent` is neither a factor nor an available input, and must not
    disappear from the result (INV-03, INV-09)."""
    states = evaluate_canonical().context_states

    assert states["website_intent"] is InputState.UNAVAILABLE
    assert set(states) == {entry.key for entry in canonical_context()}
    assert "website_intent" not in {f.key for f in evaluate_canonical().factors}


def test_an_available_input_that_fails_its_rule_is_still_consumed():
    context = replace_context(
        canonical_context(),
        "employee_count",
        ContextInput(
            key="employee_count",
            availability="available",
            value=40,
            evidence_version_id="ev-novasignal-employee-count-v1",
        ),
    )
    result = evaluate_canonical(context)

    assert (result.score, result.output) == (61, "DO_NOT_PRIORITIZE")
    employees = factor(result, "employee_count")
    assert employees.input_state is InputState.CONSUMED
    assert employees.matched is False
    assert employees.contribution == 0
    assert employees.evidence_version_id == "ev-novasignal-employee-count-v1"


def test_an_explicitly_unavailable_input_contributes_zero_and_is_not_consumed():
    context = replace_context(
        canonical_context(),
        "employee_count",
        ContextInput(key="employee_count", availability="unavailable"),
    )
    result = evaluate_canonical(context)

    assert (result.score, result.output) == (61, "DO_NOT_PRIORITIZE")
    employees = factor(result, "employee_count")
    assert employees.input_state is InputState.UNAVAILABLE
    assert employees.contribution == 0
    assert employees.evidence_version_id is None
    assert result.context_states["employee_count"] is InputState.UNAVAILABLE


def test_a_factor_whose_key_is_absent_from_the_context_is_absent_not_unavailable():
    context = replace_context(canonical_context(), "funding_event", None)
    result = evaluate_canonical(context)

    funding = factor(result, "funding_event")
    assert funding.input_state is InputState.ABSENT
    assert funding.matched is False
    assert funding.contribution == 0
    assert result.score == 68
    assert "funding_event" not in result.context_states


def test_an_unknown_missing_value_behavior_is_refused_rather_than_guessed():
    content = logic_artifact("v3.2")
    content["missing_value_behavior"] = "something_else"
    artifact = LogicArtifact.model_validate(content)

    with pytest.raises(UnsupportedMissingValueBehavior) as caught:
        evaluate(artifact, canonical_context(), canonical_boundary())
    assert caught.value.behavior == "something_else"


@pytest.mark.parametrize(
    ("label", "funding"),
    [
        ("unavailable", ContextInput(key="funding_event", availability="unavailable")),
        ("absent", None),
    ],
)
def test_a_naive_boundary_is_rejected_at_entry_even_when_no_temporal_rule_executes(label, funding):
    """INV-02: a naive boundary is never assumed to be UTC, on any path.

    With `funding_event` unavailable or absent, the only rule that reads the
    boundary never runs -- so the check has to happen at the evaluator's
    entry, not inside that rule.
    """
    context = replace_context(canonical_context(), "funding_event", funding)
    naive = canonical_boundary().replace(tzinfo=None)

    with pytest.raises(UnsupportedBoundary):
        evaluate(logic_artifact_model(), context, naive)


def test_evaluation_is_deterministic():
    assert evaluate_canonical() == evaluate_canonical()
