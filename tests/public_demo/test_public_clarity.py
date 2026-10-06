"""P-08 to P-11: public summaries render supplied data and fail honestly.

INV-03/06/09/10: no decorative fallback scores, invented rates or hidden states.
"""

from collections import Counter
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from flight_recorder import public_demo
from flight_recorder.analytics.insights import Comparison, Rate, SelectionFailure
from flight_recorder.public_demo import create_public_demo
from flight_recorder.replay.reconstruct import IntegrityFailure
from flight_recorder.web.insights_view import InsightsPage, rate_line
from tests.acceptance.test_readme import element, has_element, visible
from tests.public_demo.conftest import owned_copy
from tests.public_demo.test_public_branding import Tags


@pytest.fixture(scope="module")
def app(built_snapshot, tmp_path_factory):
    return create_public_demo(owned_copy(built_snapshot, tmp_path_factory.mktemp("clarity")))


@pytest.fixture(scope="module")
def public(app):
    with TestClient(app) as client:
        yield client


def test_approved_copy_and_hero_labels(public):
    html = public.get("/").text
    assert visible(element(html, "home-headline")) == "See why an automated decision was made."
    assert visible(element(html, "home-lead")) == (
        "Inspect the evidence and rules behind an account-prioritization decision. "
        "Replay the same historical evidence under different rules, "
        "without rewriting the original decision."
    )
    assert (
        visible(element(html, "home-audience"))
        == "For GTM engineers, RevOps operators, and revenue-systems owners."
    )
    hero = element(html, "hero-comparison")
    assert "Original, recorded" in visible(hero)
    assert "Counterfactual, computed now, not recorded" in visible(hero)
    ids = [a["id"] for _, a in Tags(html).tags if "id" in a]
    assert max(Counter(ids).values()) == 1


def test_hero_uses_each_requests_varied_values_without_an_extra_replay(public, monkeypatch):
    real = public_demo.replay
    calls = []

    def varied(*args):
        original = real(*args)
        score = (61, 39)[len(calls)]
        calls.append(score)
        return replace(original, result=replace(original.result, score=score, output="TEST_OUTPUT"))

    monkeypatch.setattr(public_demo, "replay", varied)
    for score in (61, 39):
        html = public.get("/").text
        assert visible(element(html, "hero-counterfactual-score")) == str(score)
        assert visible(element(html, "hero-counterfactual-output")) == "TEST_OUTPUT"
        assert visible(element(html, "hero-original-score")) == visible(
            element(html, "example-recorded-score")
        )
    assert calls == [61, 39]


def test_hero_fails_honestly_when_replay_fails(public, monkeypatch):
    def fail(*args):
        raise IntegrityFailure("artifact_hash", "test mismatch")

    monkeypatch.setattr(public_demo, "replay", fail)
    html = public.get("/").text
    hero = element(html, "hero-comparison")
    assert not has_element(hero, "hero-counterfactual-score")
    assert "IntegrityFailure" in visible(hero)
    assert "72" not in visible(hero)
    assert has_element(hero, "hero-original-score")


def test_hero_has_no_score_when_the_record_cannot_be_read(public, monkeypatch):
    monkeypatch.setattr(public_demo, "load_decision_page", lambda *args: None)
    hero = element(public.get("/").text, "hero-comparison")
    assert public_demo.EXAMPLE_UNAVAILABLE in visible(hero)
    assert 'class="hero-account"' not in hero
    assert not has_element(hero, "hero-original-score")
    assert not has_element(hero, "hero-counterfactual-score")


def test_insights_summaries_match_the_detailed_comparisons(public, app):
    html = public.get("/insights").text
    summary = element(html, "insights-comparisons")
    assert (
        html.index('id="insights-language"')
        < html.index('id="insights-comparisons"')
        < html.index('id="insights-summary"')
    )
    assert "Signal and workflow comparisons" in visible(summary)
    assert (
        "Observed comparisons in synthetic data. "
        "These are demonstration checks, not causal findings." in visible(summary)
    )
    for row in app.state.public_insights_page.result.signals:
        card = element(summary, f"summary-signal-{row.id}")
        for arm in (row.comparison.present, row.comparison.absent):
            assert rate_line(arm) in visible(card)
        assert visible(element(card, f"summary-signal-{row.id}-difference")) == visible(
            element(html, f"signal-{row.id}-difference")
        )
        assert f'href="#signal-{row.id}-comparison"' in card
    for arm in ("present", "absent"):
        assert visible(element(summary, f"summary-workflow-{arm}-rate")) == visible(
            element(html, f"workflow-comparison-{arm}-rate")
        )


@pytest.mark.parametrize("eligible,positives", [(7, 3), (0, 0)])
def test_summary_uses_varied_and_zero_eligible_arms(app, public, eligible, positives):
    stored = app.state.public_insights_page
    rate = Rate(11, eligible, positives, 0, 0, 11 - eligible)
    changed = Comparison(rate, rate)
    result = replace(
        stored.result,
        signals=tuple(replace(row, comparison=changed) for row in stored.result.signals),
        workflow_comparison=replace(stored.result.workflow_comparison, comparison=changed),
    )
    try:
        app.state.public_insights_page = replace(stored, result=result)
        html = public.get("/insights").text
        summary = visible(element(html, "insights-comparisons"))
        assert summary.count(rate_line(rate)) == 6
        if eligible == 0:
            assert "no comparison:" in summary and "0 eligible decisions" in summary
            assert "%" not in summary
    finally:
        app.state.public_insights_page = stored


@pytest.mark.parametrize("state", ["empty", "selection_failure"])
def test_empty_or_failed_insights_has_no_comparison_summary(app, public, state):
    stored = app.state.public_insights_page
    try:
        app.state.public_insights_page = InsightsPage(
            state=state, cutoff=1597, failure=SelectionFailure("test-outcome", ["a", "b"])
        )
        html = public.get("/insights").text
        assert not has_element(html, "insights-comparisons")
    finally:
        app.state.public_insights_page = stored


def test_contact_footer_link_only_when_configured(app, public):
    assert 'href="/#contact"' in element(public.get("/about").text, "site-footer")
    stored = app.state.public_contact
    try:
        app.state.public_contact = None
        assert 'href="/#contact"' not in element(public.get("/about").text, "site-footer")
        assert not has_element(public.get("/").text, "contact")
    finally:
        app.state.public_contact = stored


def test_hero_recorded_values_come_from_the_supplied_record(public, monkeypatch):
    real = public_demo.load_decision_page

    def changed(*args):
        page = real(*args)
        return replace(
            page,
            decision=replace(
                page.decision,
                score=41,
                threshold=67,
                logic_version="test-recorded-version",
                output="TEST_RECORDED",
            ),
        )

    monkeypatch.setattr(public_demo, "load_decision_page", changed)
    hero = element(public.get("/").text, "hero-comparison")
    assert visible(element(hero, "hero-original-score")) == "41"
    assert "against threshold 67" in visible(hero)
    assert "test-recorded-version" in visible(hero)
    assert "TEST_RECORDED" in visible(hero)
