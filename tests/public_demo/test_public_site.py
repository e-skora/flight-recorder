"""D-020, public site task 1: Home at `/`, the account list at `/demo`, About at
`/about`, public navigation and footer, and the Home examples computed on every
request and kept nowhere. Checks B4.1 to B4.9 of the task.

Invariants named: INV-01 and INV-11 (every request here is a read; the
snapshot's full state and file bytes are compared before and after); INV-02
and INV-03 (Home's second example shows the recorded `available but ignored`
and `unavailable` states as distinct words, read from the preserved record);
INV-05 (the replay example resolves the default artifact by label and then uses
its hash, exactly as the decision page); INV-06 and INV-10 (the counterfactual
is labelled as computed now, never stored, and never rendered as a decision
that happened); INV-09 (a replay failure is shown as a named unavailable
example, never a substituted value).

The engine spy replaces the module name `replay` that the Home handler looks
up on every request, so a constant typed into the template or a result kept
from an earlier request both fail here; equality with the one real snapshot is
never accepted alone as proof.
"""

import dataclasses
import re
from collections import Counter

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from flight_recorder import public_demo
from flight_recorder.app import create_app
from flight_recorder.public_demo import (
    COMPANY_NAME,
    COMPANY_URL,
    CREATED_BY,
    EXAMPLE_UNAVAILABLE,
    about,
    create_public_demo,
    home,
    open_read_only,
)
from flight_recorder.replay.counterfactual import compare, replay
from flight_recorder.replay.reconstruct import IntegrityFailure, ReconstructionMismatch
from flight_recorder.web.decision_view import AVAILABLE_BUT_IGNORED, UNAVAILABLE, load_decision_page
from flight_recorder.web.routes import account_list
from tests.acceptance.test_readme import element, has_element, page_text, visible
from tests.public_demo.conftest import (
    DECISION_EVENT_ID,
    DECISION_URL,
    SOURCE_URL,
    V5_2_HASH,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)
from tests.public_demo.test_public_writes import registered_routes

ATTRIBUTION = f"Created by {CREATED_BY} · {COMPANY_NAME}"
ATTRIBUTION_LINK = f'<a href="{COMPANY_URL}">{COMPANY_NAME}</a>'
PUBLIC_PAGES = (
    "/",
    "/demo",
    "/demo?q=nova",
    "/about",
    "/accounts/novasignal-ai",
    DECISION_URL,
    "/insights",
)
SITE_PATHS = ("/", "/demo", "/about")
REFUSED_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")
WALKTHROUGH = "https://walkthrough.example.test/flight-recorder"


@pytest.fixture(scope="module")
def snapshot(built_snapshot, tmp_path_factory):
    return owned_copy(built_snapshot, tmp_path_factory.mktemp("public-site"))


@pytest.fixture(scope="module")
def app(snapshot):
    return create_public_demo(snapshot)


@pytest.fixture(scope="module")
def public(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def local(snapshot, tmp_path_factory):
    path = owned_copy(snapshot, tmp_path_factory.mktemp("public-site-local"), "local.db")
    with TestClient(create_app(path)) as client:
        yield client


def text_of(html: str, element_id: str) -> str:
    return visible(element(html, element_id))


def home_scores(html: str) -> dict[str, str]:
    return {
        key: text_of(html, f"example-{key}")
        for key in (
            "recorded-score",
            "recorded-threshold",
            "recorded-logic",
            "recorded-output",
            "counterfactual-score",
            "counterfactual-threshold",
            "counterfactual-output",
            "counterfactual-logic",
            "original-score",
            "original-output",
        )
    }


# --- B4.1: routes ---------------------------------------------------------------------


def test_the_three_site_pages_answer_200_in_public_mode_and_change_nothing(public, snapshot):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    for url in PUBLIC_PAGES:
        response = public.get(url)
        assert response.status_code == 200, (url, response.status_code)
        assert response.headers["content-type"].startswith("text/html")
    assert_unchanged(snapshot, state, sha256)


def test_local_root_is_the_account_list_and_local_demo_and_about_do_not_exist(local):
    root = local.get("/")
    assert root.status_code == 200
    assert has_element(root.text, "account-filter")
    assert '<form method="get" action="/" id="account-filter">' in root.text
    assert not has_element(root.text, "home-headline")
    assert not has_element(root.text, "site-footer")
    assert local.get("/demo").status_code == 404
    assert local.get("/demo?q=nova").status_code == 404
    assert local.get("/about").status_code == 404


def test_exactly_one_get_route_answers_each_public_path(app):
    """From the app's own route table, walked through every included router:
    Home answers `/`, the shared account-list handler answers `/demo`, About
    answers `/about`, and no path is registered twice (the shared router's `/`
    is not registered a second time, so nothing is shadowed)."""
    routes = [
        (path, endpoint)
        for path, endpoint in registered_routes(app.routes)
        if endpoint is not None and path != "/static"
    ]
    counts = Counter(path for path, _ in routes)
    assert all(count == 1 for count in counts.values()), counts
    by_path = dict(routes)
    assert by_path["/"] is home
    assert by_path["/demo"] is account_list
    assert by_path["/about"] is about
    assert set(by_path) == {
        "/healthz",
        "/",
        "/demo",
        "/about",
        "/accounts/{account_ref}",
        "/accounts/{account_ref}/decisions/{decision_event_id}",
        "/insights",
    }
    # Every one of them is a GET route and nothing else.
    methods = {
        route.path: set(route.methods) for route in app.routes if isinstance(route, APIRoute)
    }
    assert methods == {"/healthz": {"GET", "HEAD"}}
    included = [route for route in app.routes if hasattr(route, "original_router")]
    assert len(included) == 1
    site = included[0].original_router
    assert {(r.path, frozenset(r.methods)) for r in site.routes if isinstance(r, APIRoute)} == {
        ("/", frozenset({"GET"})),
        ("/demo", frozenset({"GET"})),
        ("/about", frozenset({"GET"})),
    }
    (shared,) = [route for route in site.routes if hasattr(route, "original_router")]
    assert {(r.path, frozenset(r.methods)) for r in shared.original_router.routes} == {
        ("/accounts/{account_ref}", frozenset({"GET"})),
        ("/accounts/{account_ref}/decisions/{decision_event_id}", frozenset({"GET"})),
        ("/insights", frozenset({"GET"})),
    }


# --- B4.2: /demo is the account list ---------------------------------------------------


def test_demo_renders_the_account_list_with_its_filter_and_start_block(public):
    html = public.get("/demo").text
    assert has_element(html, "start-here")
    assert has_element(html, "account-filter")
    assert text_of(html, "account-count") == "Showing 241 of 241 accounts"
    assert "Canonical demo account: NovaSignal AI" in page_text(html)
    filtered = public.get("/demo?q=nova").text
    assert text_of(filtered, "account-count") == "Showing 1 of 241 accounts matching “nova”"
    assert has_element(filtered, "start-here")
    none = public.get("/demo?q=zzzz-no-such-account").text
    assert has_element(none, "account-no-match")


def test_home_is_not_the_account_list(public):
    html = public.get("/").text
    assert not has_element(html, "start-here")
    assert not has_element(html, "account-filter")
    assert not has_element(html, "account-count")
    assert text_of(html, "home-headline").startswith("See why an automated decision was made")
    assert text_of(html, "try-the-demo") == "Try the demo"
    assert '<a id="try-the-demo" href="/demo" class="button button-primary">' in html


# --- B4.3: Home's examples are live, from the engine, and never retained ---------------


def test_the_real_examples_equal_the_engine_and_the_record_on_the_admitted_snapshot(
    public, snapshot
):
    engine = open_read_only(snapshot)
    try:
        with engine.connect() as conn:
            page = load_decision_page(conn, DECISION_EVENT_ID)
            expected = compare(replay(conn, DECISION_EVENT_ID, V5_2_HASH))
    finally:
        engine.dispose()
    html = public.get("/").text
    assert home_scores(html) == {
        "recorded-score": str(page.decision.score),
        "recorded-threshold": str(page.decision.threshold),
        "recorded-logic": page.decision.logic_version,
        "recorded-output": page.decision.output,
        "counterfactual-score": str(expected.counterfactual_score),
        "counterfactual-threshold": str(expected.counterfactual_threshold),
        "counterfactual-output": expected.counterfactual_output,
        "counterfactual-logic": expected.current_logic.logic_version,
        "original-score": str(expected.original_score),
        "original-output": expected.original_output,
    }
    assert home_scores(html)["recorded-score"] == "86"
    assert home_scores(html)["counterfactual-score"] == "72"
    assert home_scores(html)["counterfactual-logic"] == "v5.2"
    # Example 2: the recorded states, as distinct words, from the preserved record.
    ignored = [row.input_key for row in page.context_rows if row.state == AVAILABLE_BUT_IGNORED]
    unavailable = [row.input_key for row in page.context_rows if row.state == UNAVAILABLE]
    assert "verified_integration_pressure" in ignored and unavailable == ["website_intent"]
    example = element(html, "example-ignored")
    ignored_lines = re.findall(r'<p class="example-ignored-input">(.*?)</p>', example, re.S)
    unavailable_lines = re.findall(r'<p class="example-unavailable-input">(.*?)</p>', example, re.S)
    assert [re.search(r"<code>(.*?)</code>", line).group(1) for line in ignored_lines] == ignored
    assert [re.search(r"<code>(.*?)</code>", line).group(1) for line in unavailable_lines] == (
        unavailable
    )
    for line in ignored_lines:
        assert f'<span class="state-word">{AVAILABLE_BUT_IGNORED}</span>' in line
    for line in unavailable_lines:
        assert f'<span class="state-word">{UNAVAILABLE}</span>' in line
    assert (
        "<code>LOW</code>"
        in [line for line in ignored_lines if "verified_integration_pressure" in line][0]
    )
    # The counterfactual is labelled as computed now, never as recorded.
    replay_text = text_of(html, "example-replay")
    assert "counterfactual" in replay_text and "never stored" in replay_text
    assert f'href="{DECISION_URL}#replay-comparison"' in element(html, "example-replay")


@pytest.fixture
def spied_replay(monkeypatch):
    """Replace the engine call Home makes with a spy that runs the real replay
    and then returns a non-canonical score and output; records every call."""
    calls: list[tuple[str, str]] = []
    scores: list[int] = [61]
    real = replay

    def spy(conn, decision_event_id, current_artifact_hash):
        calls.append((decision_event_id, current_artifact_hash))
        counterfactual = real(conn, decision_event_id, current_artifact_hash)
        score = scores[len(calls) - 1] if len(calls) <= len(scores) else scores[-1]
        result = dataclasses.replace(counterfactual.result, score=score, output="SPY_OUTPUT")
        return dataclasses.replace(counterfactual, result=result)

    monkeypatch.setattr(public_demo, "replay", spy)
    return calls, scores


def test_home_renders_the_spys_values_not_the_canonical_72(public, spied_replay):
    calls, _ = spied_replay
    html = public.get("/").text
    assert calls == [(DECISION_EVENT_ID, V5_2_HASH)]
    scores = home_scores(html)
    assert scores["counterfactual-score"] == "61"
    assert scores["counterfactual-output"] == "SPY_OUTPUT"
    assert scores["recorded-score"] == "86"  # the record is untouched by the spy
    assert "72" not in text_of(html, "example-replay")


def test_two_home_requests_call_the_engine_twice_and_a_changed_result_changes_the_page(
    public, spied_replay, snapshot
):
    calls, scores = spied_replay
    scores[:] = [61, 39]
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    first = public.get("/").text
    second = public.get("/").text
    assert len(calls) == 2
    assert home_scores(first)["counterfactual-score"] == "61"
    assert home_scores(second)["counterfactual-score"] == "39"
    assert_unchanged(snapshot, state, sha256)


def test_nothing_from_home_is_kept_on_the_app_between_requests(app, public):
    """No attribute of the app's state holds a comparison, a score or a rendered
    fragment after Home has been served; Insights stays the only stored model."""
    public.get("/")
    public.get("/")
    stored = app.state._state  # Starlette keeps the attributes here
    kept = {
        name: value
        for name, value in stored.items()
        if name != "public_insights_page" and not isinstance(value, (str, bool, type(None)))
    }
    assert set(kept) == {"engine"}, kept
    for value in stored.values():
        assert not hasattr(value, "counterfactual_score")
        assert not (isinstance(value, str) and "example-counterfactual-score" in value)


# --- B4.4: honest failure --------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        IntegrityFailure("artifact_hash", "spy", stored="a", recomputed="b"),
        ReconstructionMismatch("score", 86, 85),
    ],
    ids=["integrity", "reconstruction"],
)
def test_a_failing_replay_leaves_the_example_unavailable_and_home_still_200(
    public, monkeypatch, snapshot, error
):
    def raising(conn, decision_event_id, current_artifact_hash):
        raise error

    monkeypatch.setattr(public_demo, "replay", raising)
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    response = public.get("/")
    assert response.status_code == 200
    html = response.text
    assert has_element(html, "example-replay-unavailable")
    statement = text_of(html, "example-replay-unavailable")
    assert statement.startswith(EXAMPLE_UNAVAILABLE)
    assert type(error).__name__ in statement
    assert f'href="{DECISION_URL}#replay-comparison"' in element(html, "example-replay")
    for element_id in ("example-counterfactual-score", "example-counterfactual-output"):
        assert not has_element(html, element_id)
    assert "72" not in text_of(html, "example-replay")
    # The recorded examples are unaffected by a replay failure.
    assert text_of(html, "example-recorded-score") == "86"
    assert_unchanged(snapshot, state, sha256)


# --- B4.5: the walkthrough slot ---------------------------------------------------------


def test_the_walkthrough_section_and_action_render_only_with_an_address(snapshot):
    with TestClient(create_public_demo(snapshot)) as without:
        html = without.get("/").text
        assert not has_element(html, "walkthrough")
        assert not has_element(html, "watch-the-walkthrough")
        assert "Watch the walkthrough" not in page_text(html)
        assert "<iframe" not in html and "<script" not in html
    with TestClient(create_public_demo(snapshot, walkthrough_url=WALKTHROUGH)) as with_address:
        html = with_address.get("/").text
        assert has_element(html, "walkthrough")
        assert '<a id="watch-the-walkthrough" href="#walkthrough"' in html
        assert f'<a id="walkthrough-link" href="{WALKTHROUGH}">Watch the walkthrough</a>' in html
        assert "<iframe" not in html and "<script" not in html
        assert "<video" not in html and "<embed" not in html and "<object" not in html
        for url in ("/demo", "/about", DECISION_URL):
            assert "<iframe" not in with_address.get(url).text


def test_an_empty_address_is_no_address(snapshot):
    with TestClient(create_public_demo(snapshot, walkthrough_url="")) as client:
        assert not has_element(client.get("/").text, "walkthrough")


# --- B4.6: attribution ---------------------------------------------------------------


def test_every_public_page_carries_the_attribution_in_its_footer(public):
    for url in PUBLIC_PAGES:
        html = public.get(url).text
        footer = element(html, "site-footer")
        assert text_of(html, "site-attribution") == ATTRIBUTION, url
        assert ATTRIBUTION_LINK in footer, url
        assert 'href="/about"' in footer and f'href="{SOURCE_URL}"' in footer, url
        assert html.count('id="site-footer"') == 1
        assert html.index("</main>") < html.index('id="site-footer"')


def test_about_section_7_matches_the_footer(public):
    html = public.get("/about").text
    assert text_of(html, "about-attribution-line") == ATTRIBUTION
    assert ATTRIBUTION_LINK in element(html, "about-attribution")
    assert text_of(html, "about-what-i-did-heading") == "What I did"
    assert CREATED_BY in text_of(html, "about-what-i-did")
    for heading in (
        "Why this exists",
        "How it works",
        "Four decisions and their tradeoffs",
        "Limitations",
        "Run it yourself",
        "Attribution",
    ):
        assert heading in page_text(html), heading
    assert f'<a id="about-source" href="{SOURCE_URL}">' in html


def test_attribution_is_absent_in_local_mode(local):
    for url in ("/", "/accounts/novasignal-ai", DECISION_URL, "/insights"):
        html = local.get(url).text
        assert not has_element(html, "site-footer"), url
        assert CREATED_BY not in html and COMPANY_NAME not in html, url
        assert COMPANY_URL not in html, url


# --- B4.7: method refusals on the site pages ------------------------------------------


def test_every_mutating_method_on_the_site_pages_is_refused_and_nothing_changes(snapshot, tmp_path):
    copy = owned_copy(snapshot, tmp_path)
    state, sha256 = full_state(copy), file_sha256(copy)
    with TestClient(create_public_demo(copy)) as client:
        for path in SITE_PATHS:
            for method in REFUSED_METHODS:
                response = client.request(method, path, content=b'{"probe": true}')
                assert response.status_code == 405, (method, path, response.status_code)
                assert response.headers["allow"] == "GET, HEAD", (method, path)
                assert "read-only" in response.json()["detail"]
                assert_unchanged(copy, state, sha256)


# --- B4.8: the account-list addresses --------------------------------------------------


def test_the_filter_form_and_both_breadcrumbs_point_to_demo_in_public_and_root_locally(
    public, local
):
    assert '<form method="get" action="/demo" id="account-filter">' in public.get("/demo").text
    assert '<form method="get" action="/" id="account-filter">' in local.get("/").text
    public_trace = public.get("/accounts/novasignal-ai").text
    public_decision = public.get(DECISION_URL).text
    assert '<nav aria-label="Breadcrumb"><a href="/demo">Accounts</a>' in public_trace
    assert '<nav aria-label="Breadcrumb"><a href="/demo">Accounts</a>' in public_decision
    assert '<a href="/">Accounts</a>' not in public_trace
    assert '<a href="/">Accounts</a>' not in public_decision
    local_trace = local.get("/accounts/novasignal-ai").text
    local_decision = local.get(DECISION_URL).text
    assert '<nav aria-label="Breadcrumb"><a href="/">Accounts</a>' in local_trace
    assert '<nav aria-label="Breadcrumb"><a href="/">Accounts</a>' in local_decision
    assert "/demo" not in local_trace and "/demo" not in local_decision


def test_the_public_filter_stays_on_demo(public):
    """Following the form the way a browser does: its action plus its query."""
    form = re.search(
        r'<form method="get" action="([^"]+)" id="account-filter">', public.get("/demo").text
    )
    response = public.get(form.group(1), params={"q": "nova"})
    assert str(response.url).endswith("/demo?q=nova")
    assert response.status_code == 200
    assert text_of(response.text, "account-count") == "Showing 1 of 241 accounts matching “nova”"


# --- B1.6: navigation, and the claim rules on the new copy -----------------------------


def test_the_public_navigation_names_the_five_places_in_order(public):
    for url in PUBLIC_PAGES:
        html = public.get(url).text
        nav = re.search(r'<nav class="site-nav" aria-label="Site">(.*?)</nav>', html, re.S)
        hrefs = re.findall(r'<a[^>]*href="([^"]+)"', nav.group(1))
        assert hrefs == ["/", "/demo", "/insights", "/about", SOURCE_URL], url
        assert [visible(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", nav.group(1))] == [
            "Home",
            "Try the demo",
            "Insights",
            "About",
            "Source code",
        ], url
        assert has_element(html, "public-demo-notice"), url


def test_the_new_copy_stays_inside_the_claim_rules(public):
    for url in ("/", "/about"):
        text = page_text(public.get(url).text).lower()
        for word in ("real-time", "production", "validated", "live customer data"):
            assert word not in text, (url, word)
        # The About page may name "causal" and "lift" only to deny them.
        for word in ("causal", "lift"):
            for match in re.finditer(re.escape(word), text):
                preceding = text[max(0, match.start() - 24) : match.start()]
                assert "not " in preceding, (url, word, preceding)
        assert "synthetic" in text and "read-only" in text
        assert "—" not in text  # no em dash in public-facing copy
