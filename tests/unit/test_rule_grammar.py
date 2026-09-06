"""The closed rule grammar: exactly the four supported shapes, and nothing looser.

Every rule string in both registered artifacts must parse to a typed rule, each
shape is exercised at its boundary, and anything outside the grammar is an
explicit `UnsupportedRule` rather than a silent non-match (INV-05, INV-09).
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.logic.evaluator import ContextInput, evaluate
from flight_recorder.logic.rules import (
    AtLeastRule,
    BetweenRule,
    EqualsRule,
    ObservedWithinRule,
    RuleKeyMismatch,
    RuleTypeError,
    UnsupportedBoundary,
    UnsupportedRule,
    parse_boundary,
    parse_rule,
)
from tests.conftest import (
    canonical_boundary,
    canonical_context,
    logic_artifact,
    logic_artifact_model,
    replace_context,
)

#: The six unique rule strings across the two registered artifacts, and the
#: typed rule each must parse to. Asserting the whole set keeps a fixture
#: change from silently escaping the grammar.
EXPECTED_RULES = {
    "employee_count between 50 and 500 inclusive": BetweenRule("employee_count", 50, 500),
    "industry equals 'B2B AI Software'": EqualsRule("industry", "B2B AI Software"),
    "funding_event observed within 90 days before the decision boundary": ObservedWithinRule(
        "funding_event", 90
    ),
    "open_platform_engineering_roles at least 3": AtLeastRule("open_platform_engineering_roles", 3),
    "headquarters_country equals 'United States'": EqualsRule(
        "headquarters_country", "United States"
    ),
    "verified_integration_pressure equals 'LOW'": EqualsRule(
        "verified_integration_pressure", "LOW"
    ),
}

MALFORMED = [
    "employee_count between 50 and 500 inclusive strictly",  # extra word
    "employee_count Between 50 and 500 inclusive",  # capitalized keyword
    "employee_count between 50.0 and 500 inclusive",  # float bound
    "industry equals B2B AI Software",  # unquoted literal
    "employee_count exceeds 50",  # unknown verb
]

MIDNIGHT = datetime(2026, 4, 17, tzinfo=UTC)


def canonical_factors() -> list[tuple[str, str]]:
    """(key, rule) for all eleven factor entries across both artifacts."""
    return [
        (factor["key"], factor["rule"])
        for version in ("v3.2", "v5.1")
        for factor in logic_artifact(version)["factors"]
    ]


def test_every_canonical_rule_string_parses_to_the_expected_typed_rule():
    factors = canonical_factors()
    assert len(factors) == 11, "both artifacts together carry eleven factor entries"
    for key, text in factors:
        assert parse_rule(key, text) == EXPECTED_RULES[text], text
    assert {text for _, text in factors} == set(EXPECTED_RULES)


# --- Shape boundaries -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"), [(49, False), (50, True), (500, True), (501, False)]
)
def test_between_matches_inclusively_at_both_bounds(value, expected):
    rule = parse_rule("employee_count", "employee_count between 50 and 500 inclusive")
    assert rule.matches(value, observed_at=None, boundary=MIDNIGHT) is expected


@pytest.mark.parametrize(
    ("value", "expected"), [("B2B AI Software", True), ("b2b ai software", False)]
)
def test_equals_is_exact_and_case_sensitive(value, expected):
    rule = parse_rule("industry", "industry equals 'B2B AI Software'")
    assert rule.matches(value, observed_at=None, boundary=MIDNIGHT) is expected


@pytest.mark.parametrize(("value", "expected"), [(2, False), (3, True)])
def test_at_least_matches_at_its_threshold(value, expected):
    rule = parse_rule(
        "open_platform_engineering_roles", "open_platform_engineering_roles at least 3"
    )
    assert rule.matches(value, observed_at=None, boundary=MIDNIGHT) is expected


def observed_within_rule() -> ObservedWithinRule:
    return parse_rule(
        "funding_event", "funding_event observed within 90 days before the decision boundary"
    )


@pytest.mark.parametrize(("days_before", "expected"), [(90, True), (91, False)])
def test_against_a_midnight_boundary_the_window_is_exactly_n_days(days_before, expected):
    rule = observed_within_rule()
    observed = MIDNIGHT.date() - timedelta(days=days_before)
    assert rule.matches("Series B", observed_at=observed, boundary=MIDNIGHT) is expected


def test_a_non_midnight_boundary_measures_elapsed_time_not_calendar_days():
    """`evaluator-v1` semantics: the window is `n x 24 hours` of elapsed time.

    Against the canonical `10:05:02Z` boundary a date exactly 90 calendar days
    earlier is 90 days and 10 hours ago, which is outside a 90-day window.
    """
    rule = observed_within_rule()
    boundary = canonical_boundary()
    assert boundary.time() != datetime.min.time(), "this test needs a non-midnight boundary"

    assert rule.matches(
        "Series B", observed_at=boundary.date() - timedelta(days=18), boundary=boundary
    )
    assert not rule.matches(
        "Series B", observed_at=boundary.date() - timedelta(days=90), boundary=boundary
    )
    assert not rule.matches(
        "Series B", observed_at=boundary.date() + timedelta(days=1), boundary=boundary
    )


def test_a_naive_boundary_string_is_an_explicit_error():
    with pytest.raises(UnsupportedBoundary) as caught:
        parse_boundary("2026-04-17T10:05:02")
    assert "UTC designator" in str(caught.value)


def test_a_naive_boundary_reaching_a_temporal_rule_is_an_explicit_error():
    with pytest.raises(UnsupportedBoundary):
        observed_within_rule().matches(
            "Series B", observed_at=date(2026, 3, 30), boundary=MIDNIGHT.replace(tzinfo=None)
        )


# --- Refusals ---------------------------------------------------------------


@pytest.mark.parametrize("text", MALFORMED)
def test_a_malformed_rule_string_is_unsupported(text):
    with pytest.raises(UnsupportedRule):
        parse_rule(text.split(" ", 1)[0], text)


@pytest.mark.parametrize("rule_text", sorted(EXPECTED_RULES))
@pytest.mark.parametrize(
    ("label", "decorate"),
    [
        ("trailing newline", lambda text: text + "\n"),
        ("trailing carriage return and newline", lambda text: text + "\r\n"),
        ("leading space", lambda text: " " + text),
        ("trailing space", lambda text: text + " "),
    ],
)
def test_a_canonical_rule_with_anything_around_it_is_unsupported(rule_text, label, decorate):
    """The grammar accepts the exact string and nothing else.

    Anchoring alone would not do it: in Python `$` also matches immediately
    before a trailing newline, so the patterns are applied with `fullmatch`.
    """
    key = rule_text.split(" ", 1)[0]
    assert parse_rule(key, rule_text) == EXPECTED_RULES[rule_text], "the exact string still parses"
    with pytest.raises(UnsupportedRule):
        parse_rule(key, decorate(rule_text)), label


def test_a_rule_naming_a_different_input_than_its_factor_fails():
    with pytest.raises(RuleKeyMismatch) as caught:
        parse_rule("industry", "employee_count at least 3")
    assert caught.value.rule_key == "employee_count"


def artifact_with_extra_factor(key: str, rule: str) -> LogicArtifact:
    content = logic_artifact("v3.2")
    content["factors"] = [*content["factors"], {"key": key, "rule": rule, "weight": 5}]
    return LogicArtifact.model_validate(content)


@pytest.mark.parametrize(
    ("key", "why"),
    [("website_intent", "unavailable in H(d)"), ("absent_signal", "absent from H(d)")],
)
def test_an_unsupported_rule_fails_even_when_its_input_is_never_read(key, why):
    """Every rule is parsed before any factor is evaluated."""
    artifact = artifact_with_extra_factor(key, f"{key} exceeds 5")
    with pytest.raises(UnsupportedRule):
        evaluate(artifact, canonical_context(), canonical_boundary())


# --- Value type mismatches --------------------------------------------------


@pytest.mark.parametrize(
    ("rule_text", "key", "value"),
    [
        ("employee_count between 50 and 500 inclusive", "employee_count", "184"),
        ("employee_count between 50 and 500 inclusive", "employee_count", True),
        ("industry equals 'B2B AI Software'", "industry", 184),
        ("industry equals 'B2B AI Software'", "industry", True),
        ("open_platform_engineering_roles at least 3", "open_platform_engineering_roles", "7"),
    ],
)
def test_a_value_that_does_not_fit_the_shape_is_an_explicit_error(rule_text, key, value):
    rule = parse_rule(key, rule_text)
    with pytest.raises(RuleTypeError):
        rule.matches(value, observed_at=None, boundary=MIDNIGHT)


def test_a_temporal_rule_without_an_observation_date_is_an_explicit_error():
    with pytest.raises(RuleTypeError) as caught:
        observed_within_rule().matches("Series B", observed_at=None, boundary=MIDNIGHT)
    assert "observed_at" in str(caught.value)


def test_a_boolean_value_fits_no_shape_including_the_temporal_one():
    with pytest.raises(RuleTypeError):
        observed_within_rule().matches(True, observed_at=date(2026, 3, 30), boundary=MIDNIGHT)


def test_a_type_mismatch_surfaces_through_evaluation_rather_than_a_silent_non_match():
    context = replace_context(
        canonical_context(),
        "employee_count",
        ContextInput(
            key="employee_count",
            availability="available",
            value="184",
            evidence_version_id="ev-novasignal-employee-count-v1",
        ),
    )
    with pytest.raises(RuleTypeError):
        evaluate(logic_artifact_model(), context, canonical_boundary())
