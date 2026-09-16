"""The `/insights` formatting layer: display rounding, rate lines, and a computes-nothing guard.

Evidence: executed against `web/insights_view.py` in memory and against the
source text of `insights_view.py` and `insights.html`; no database is touched.
The proof that the page computes nothing is the engine-field mapping in
`tests/acceptance/test_insights_page.py`; the source guard here is supplementary.
"""

from fractions import Fraction
from pathlib import Path

from flight_recorder.analytics.insights import NOT_AVAILABLE_NOTE, Comparison, Rate
from flight_recorder.web import insights_view
from flight_recorder.web.insights_view import difference_text, percent, points, rate_line

WEB_DIR = Path(insights_view.__file__).parent
VIEW_SOURCE = Path(insights_view.__file__)
TEMPLATE_SOURCE = WEB_DIR / "templates" / "insights.html"


def rate(eligible: int, positives: int, cohort_total: int = 10) -> Rate:
    return Rate(
        cohort_total=cohort_total,
        eligible=eligible,
        positives=positives,
        excluded_other_period_only=1,
        excluded_reconstruction_failed=0,
        excluded_not_evaluated=cohort_total - eligible,
    )


def test_percent_and_points_format_fractions_with_one_decimal():
    assert percent(Fraction(58, 149)) == "38.9%"
    assert percent(Fraction(1, 3)) == "33.3%"
    assert percent(Fraction(1, 1)) == "100.0%"
    assert percent(Fraction(0)) == "0.0%"
    assert percent(Fraction(1, 8)) == "12.5%"
    assert percent(Fraction(1, 16)) == "6.3%"  # 6.25: a half rounds away from zero
    assert percent(None) is None

    assert points(Fraction(470, 319)) == "+1.5 percentage points"
    assert points(Fraction(-56450, 2639)) == "-21.4 percentage points"
    assert points(Fraction(0)) == "0.0 percentage points"
    assert points(Fraction(-1, 4)) == "-0.3 percentage points"  # -0.25, away from zero
    assert points(None) is None

    available = rate(eligible=149, positives=58, cohort_total=160)
    assert rate_line(available) == "38.9% observed (58 of 149 eligible decisions; n = 149)"
    unavailable = rate(eligible=0, positives=0)
    assert rate_line(unavailable) == NOT_AVAILABLE_NOTE


def test_difference_text_names_the_unavailable_arms():
    both = Comparison(present=rate(4, 1), absent=rate(3, 2))
    assert difference_text(both, "known true", "known false") == points(both.difference_points)

    one = Comparison(present=rate(4, 1), absent=rate(0, 0))
    assert difference_text(one, "known true", "known false") == (
        "no comparison: known false has 0 eligible decisions"
    )
    neither = Comparison(present=rate(0, 0), absent=rate(0, 0, cohort_total=0))
    assert difference_text(neither, "v4.2", "v4.1") == (
        "no comparison: v4.2 and v4.1 have 0 eligible decisions"
    )


def test_the_view_module_computes_nothing():
    view = VIEW_SOURCE.read_text(encoding="utf-8")
    template = TEMPLATE_SOURCE.read_text(encoding="utf-8")

    # The one permitted length, `len(result.reconstruction_failures)`, is taken by the
    # template (`| length`), not by the view, so the view has no `len(` at all.
    for construct in ("sum(", "len(", "count(", "Counter", "filter(", "groupby"):
        assert construct not in view, construct
    assert template.count("| length") == 1
    assert "result.reconstruction_failures | length" in template

    for literal in (
        "planted-effects",
        "minimum_decisions",
        "direction",
        "max_difference_points",
        "min_difference_points",
        "expected",
        "passed",
    ):
        assert literal not in view, literal
        assert literal not in template, literal
