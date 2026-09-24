"""D-018 piece 1: nothing a visitor sends can change the public demo's data.
Tests 3, 3a and 4 of the task, and test 8 (the local collector is unaffected).

Three layers, each proven by its own assertion and none weakened for another
(settled ruling 9):

- the method guard refuses every method but GET and HEAD before routing
  (test 3, over HTTP, with full state and file bytes compared after every
  refused request);
- the public app registers no collector route and holds no collector (test 3a,
  over the registered route surface, including mounted and included routers);
- SQLite itself refuses writes through the public app's own engine (test 4).

INV-01 (the recorded past is immutable) and INV-11 (the collector is the only
door, and in the public app there is no door): the canonical envelope a local
collector would create is refused, and the snapshot is unchanged.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from flight_recorder.app import create_app
from flight_recorder.cli import cmd_reset
from flight_recorder.collector.api import post_decision_event
from flight_recorder.public_demo import create_public_demo
from tests.conftest import COLLECTOR_URL, JSON_HEADERS, discovery_envelope
from tests.public_demo.conftest import (
    DECISION_URL,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)

pytestmark = pytest.mark.invariant

REFUSED_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")
PATHS = (
    COLLECTOR_URL,
    "/",
    "/accounts/novasignal-ai",
    DECISION_URL,
    "/insights",
    "/healthz",
    "/static/style.css",
)


@pytest.fixture
def snapshot(built_snapshot, tmp_path):
    return owned_copy(built_snapshot, tmp_path)


@pytest.fixture
def public_app(snapshot):
    return create_public_demo(snapshot)


def probe_envelope() -> bytes:
    """An envelope a local collector accepts and creates (proved below)."""
    return json.dumps(discovery_envelope("public-demo-write-probe")).encode()


# --- Test 3: the method guard, over HTTP ---------------------------------------------


def test_every_mutating_method_on_every_route_is_refused_and_nothing_changes(public_app, snapshot):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    with TestClient(public_app) as client:
        for path in PATHS:
            for method in REFUSED_METHODS:
                response = client.request(method, path, content=probe_envelope())
                assert response.status_code == 405, (method, path, response.status_code)
                assert response.headers["allow"] == "GET, HEAD", (method, path)
                assert_unchanged(snapshot, state, sha256)


def test_the_canonical_envelope_a_local_collector_creates_is_refused_in_public(
    public_app, snapshot, tmp_path
):
    local_db = tmp_path / "local.db"
    assert cmd_reset(local_db) == 0
    with TestClient(create_app(local_db)) as local:
        canonical = local.post(COLLECTOR_URL, content=_canonical_discovery(), headers=JSON_HEADERS)
        assert canonical.status_code == 201  # the same bytes are a valid, creating envelope

    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    with TestClient(public_app) as client:
        for body in (_canonical_discovery(), probe_envelope()):
            response = client.post(COLLECTOR_URL, content=body, headers=JSON_HEADERS)
            assert response.status_code == 405
            assert response.headers["allow"] == "GET, HEAD"
            assert "read-only" in response.json()["detail"]
            assert_unchanged(snapshot, state, sha256)


def _canonical_discovery() -> bytes:
    from tests.conftest import canonical_by_type

    return json.dumps(canonical_by_type("account.discovered")).encode()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_api_documentation_is_not_published(public_app, path):
    with TestClient(public_app) as client:
        assert client.get(path).status_code == 404


def test_get_on_the_collector_path_finds_no_route(public_app):
    with TestClient(public_app) as client:
        assert client.get(COLLECTOR_URL).status_code == 404


# --- Test 3a: the route surface ------------------------------------------------------


def registered_routes(routes, prefix: str = ""):
    """(path, endpoint) for every registered route, walking included routers
    and mounted applications recursively."""
    for route in routes:
        path = prefix + getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        yield path, endpoint
        original = getattr(route, "original_router", None)
        if original is not None:  # a router included by reference
            context = getattr(route, "include_context", None)
            yield from registered_routes(original.routes, prefix + getattr(context, "prefix", ""))
        nested = getattr(route, "routes", None)
        if nested is not None and original is None:
            yield from registered_routes(nested, path)
        mounted = getattr(route, "app", None)
        if mounted is not None and getattr(mounted, "routes", None) is not None:
            yield from registered_routes(mounted.routes, path)


def test_the_public_app_registers_no_collector_route_and_holds_no_collector(public_app):
    routes = list(registered_routes(public_app.routes))
    paths = {path for path, _ in routes}
    assert {"/", "/insights", "/healthz", "/static"} <= paths, paths  # the walk sees routes
    forbidden = [p for p in paths if p == COLLECTOR_URL or p.startswith("/api/")]
    assert forbidden == [], f"collector routes registered in the public app: {forbidden}"
    assert all(endpoint is not post_decision_event for _, endpoint in routes)
    assert not hasattr(public_app.state, "collector")


def test_the_walk_does_see_the_collector_route_in_the_local_app(tmp_path):
    """The route-surface assertion can fail: the same walk finds the collector
    endpoint where it is registered."""
    local_db = tmp_path / "local.db"
    assert cmd_reset(local_db) == 0
    routes = list(registered_routes(create_app(local_db).routes))
    assert COLLECTOR_URL in {path for path, _ in routes}
    assert any(endpoint is post_decision_event for _, endpoint in routes)


# --- Test 4: SQLite refuses writes through the public app's own engine ---------------


WRITES = (
    "INSERT INTO accounts (account_ref, name, domain, first_seen_event_id) "
    "VALUES ('x', 'x', 'x.example', 'evt-x')",
    "UPDATE accounts SET name = 'changed' WHERE account_ref = 'novasignal-ai'",
    "DELETE FROM accounts WHERE account_ref = 'novasignal-ai'",
    "CREATE TABLE public_write_probe (x INTEGER)",
    "DROP TRIGGER events_no_update",
    "PRAGMA user_version = 7",
    "PRAGMA journal_mode = WAL",
)


@pytest.mark.parametrize("statement", WRITES)
def test_a_write_through_the_public_engine_raises_in_sqlite(public_app, snapshot, statement):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    engine = public_app.state.engine
    with engine.connect() as conn:
        with pytest.raises(OperationalError):
            conn.exec_driver_sql(statement)
            conn.commit()
            if statement.startswith("PRAGMA"):  # a PRAGMA that did not raise must not have applied
                name = statement.split()[1]
                value = conn.exec_driver_sql(f"PRAGMA {name}").scalar_one()
                raise AssertionError(f"PRAGMA {name} applied: {value}")
    engine.dispose()
    assert_unchanged(snapshot, state, sha256)


def test_the_file_itself_is_read_only_even_with_query_only_switched_off(public_app, snapshot):
    """The `mode=ro` layer on its own: with the per-connection `query_only`
    switched off, a write still fails at the file."""
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    engine = public_app.state.engine
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA query_only = OFF")
        with pytest.raises(OperationalError, match="readonly"):
            conn.exec_driver_sql("UPDATE accounts SET name = 'changed'")
            conn.commit()
        conn.invalidate()
    engine.dispose()
    assert_unchanged(snapshot, state, sha256)


def test_query_only_also_refuses_a_temporary_table(public_app, snapshot):
    """The `query_only` layer on its own: `mode=ro` admits a TEMP table, which
    lives outside the file; `query_only` refuses it too."""
    engine = public_app.state.engine
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        with pytest.raises(OperationalError):
            conn.exec_driver_sql("CREATE TEMP TABLE probe (x INTEGER)")
    engine.dispose()


# --- Test 8: the local collector is unaffected ---------------------------------------


def test_a_local_app_and_a_public_app_in_one_process_keep_their_behaviors(
    public_app, snapshot, tmp_path
):
    local_db = tmp_path / "local.db"
    assert cmd_reset(local_db) == 0
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    with TestClient(create_app(local_db)) as local, TestClient(public_app) as public:
        created = local.post(COLLECTOR_URL, content=probe_envelope(), headers=JSON_HEADERS)
        assert created.status_code == 201, created.json()
        assert created.json()["status"] == "created"
        refused = public.post(COLLECTOR_URL, content=probe_envelope(), headers=JSON_HEADERS)
        assert refused.status_code == 405
        retry = local.post(COLLECTOR_URL, content=probe_envelope(), headers=JSON_HEADERS)
        assert retry.status_code == 200
        assert retry.json()["status"] == "duplicate"
        assert local.get("/docs").status_code == 200
        assert public.get("/docs").status_code == 404
    assert_unchanged(snapshot, state, sha256)
