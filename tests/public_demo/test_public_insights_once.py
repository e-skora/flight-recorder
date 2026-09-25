"""D-018 piece 1, revision 3: public Insights is computed once at startup. Test 11.

In public mode the Insights page model is computed once, after admission, over
the admitted read-only snapshot, and `/insights` renders that stored model.
The local app never provides one and computes per request, so a new ledger
event appears on its next Insights request. Nothing is persisted and no number
changes (D-014's read-model semantics; INV-06 and INV-10: no counterfactual
enters, and only recorded events are counted; INV-11: the collector is still
the only door, and only in the local app).

`load_insights_page` is observed with a spy on the one name both call sites
use, `flight_recorder.web.routes.load_insights_page`; a spy that sees no call
fails, never passes.
"""

import json

import pytest
from fastapi.testclient import TestClient

import flight_recorder.web.routes as routes
from flight_recorder.app import create_app
from flight_recorder.public_demo import InsightsModelFailed, create_public_demo
from flight_recorder.web.insights_view import PAGE_READY, load_insights_page
from tests.acceptance.test_readme import element, visible
from tests.conftest import COLLECTOR_URL, JSON_HEADERS, discovery_envelope
from tests.public_demo.conftest import assert_unchanged, file_sha256, full_state, owned_copy

#: Rendered metric and cutoff elements compared between public and local pages.
METRIC_IDS = (
    "insights-cutoff",
    "insights-population",
    "insights-reconstructed",
    "overall-rate-value",
    "overall-cohort-total",
    "overall-eligible",
    "overall-positives",
    "coverage-total",
    "coverage-awaiting-attribution",
    "signal-recently_funded-difference",
    "signal-verified_integration_pressure_high-difference",
    "workflow-comparison-difference",
)


class Spy:
    def __init__(self, target):
        self.target = target
        self.calls = 0

    def __call__(self, conn):
        self.calls += 1
        return self.target(conn)


@pytest.fixture
def spy(monkeypatch):
    spy = Spy(routes.load_insights_page)
    monkeypatch.setattr(routes, "load_insights_page", spy)
    return spy


@pytest.fixture
def snapshot(built_snapshot, tmp_path):
    return owned_copy(built_snapshot, tmp_path, "public.db")


def metric_values(html: str) -> dict[str, str]:
    return {element_id: visible(element(html, element_id)) for element_id in METRIC_IDS}


# --- (a) equivalent rendering ---------------------------------------------------------


def test_the_stored_public_page_equals_an_uncached_public_rendering(snapshot):
    app = create_public_demo(snapshot)
    stored = app.state.public_insights_page
    assert stored is not None and stored.state == PAGE_READY
    with TestClient(app) as client:
        cached = [client.get("/insights") for _ in range(3)]
        assert all(r.status_code == 200 for r in cached)
        assert cached[0].content == cached[1].content == cached[2].content
        # The same app, templates and snapshot, with the stored model withdrawn:
        # the route computes per request, as the local app does.
        app.state.public_insights_page = None
        uncached = client.get("/insights")
        app.state.public_insights_page = stored
    assert uncached.status_code == 200
    assert uncached.content == cached[0].content


def test_the_stored_model_and_its_numbers_equal_the_local_computation(snapshot, tmp_path):
    public_app = create_public_demo(snapshot)
    local_db = owned_copy(snapshot, tmp_path, "local.db")
    local_app = create_app(local_db)
    with local_app.state.engine.connect() as conn:
        assert load_insights_page(conn) == public_app.state.public_insights_page
    with TestClient(public_app) as public, TestClient(local_app) as local:
        public_values = metric_values(public.get("/insights").text)
        local_values = metric_values(local.get("/insights").text)
    assert public_values == local_values
    assert public_values["insights-cutoff"] == "1597"


# --- (b) computed once, at startup ----------------------------------------------------


def test_public_insights_is_computed_exactly_once_before_the_first_request(spy, snapshot):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    app = create_public_demo(snapshot)
    assert spy.calls == 1, "the model was not computed at startup"
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/insights").status_code == 200
        assert client.get("/").status_code == 200
    assert spy.calls == 1, f"load_insights_page ran {spy.calls} times"
    assert_unchanged(snapshot, state, sha256)


# --- (c) the local app is unchanged ---------------------------------------------------


def test_the_local_app_computes_per_request_and_shows_a_new_event(spy, snapshot, tmp_path):
    local_db = owned_copy(snapshot, tmp_path, "local.db")
    app = create_app(local_db)
    assert not hasattr(app.state, "public_insights_page")
    with TestClient(app) as client:
        first = client.get("/insights").text
        assert spy.calls == 1
        for expected in (2, 3):
            client.get("/insights")
            assert spy.calls == expected
        created = client.post(
            COLLECTOR_URL,
            content=json.dumps(discovery_envelope("insights-refresh-probe")),
            headers=JSON_HEADERS,
        )
        assert created.status_code == 201, created.json()
        after = client.get("/insights").text
        assert spy.calls == 4
    assert visible(element(first, "insights-cutoff")) == "1597"
    assert visible(element(after, "insights-cutoff")) == "1598"


# --- (d) a failure refuses startup ----------------------------------------------------


def test_a_failing_model_computation_refuses_startup_with_a_named_error(snapshot, monkeypatch):
    def boom(conn):
        raise RuntimeError("forced Insights failure")

    monkeypatch.setattr(routes, "load_insights_page", boom)
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    app = None
    with pytest.raises(InsightsModelFailed, match="forced Insights failure"):
        app = create_public_demo(snapshot)
    assert app is None
    assert_unchanged(snapshot, state, sha256)
