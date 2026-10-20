"""New local routing must never touch v2 history on legacy/public requests."""

from fastapi.testclient import TestClient

from flight_recorder.public_demo import create_public_demo
from tests.acceptance.test_hubspot_v2_presentation import page_api
from tests.conftest import seed_all
from tests.public_demo.conftest import DECISION_URL, full_state, owned_copy


def test_public_and_local_legacy_routes_never_call_v2_page_loader(
    tmp_path, harness, built_snapshot, monkeypatch
):
    api = page_api()

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy/public request entered v2 reader")

    monkeypatch.setattr(api, "load_hubspot_decision_page", forbidden)
    seed_all(harness)
    raw = harness.engine.raw_connection()
    raw.execute("PRAGMA foreign_keys=OFF")
    for table in (
        "decision_consumed_inputs_v2",
        "decision_context_v2",
        "decisions_v2",
        "evidence_versions_v2",
        "capture_contexts",
    ):
        raw.execute(f"DROP TABLE {table}")
    raw.commit()
    raw.close()
    assert harness.client.get(DECISION_URL).status_code == 200
    assert harness.client.get(DECISION_URL + "missing").status_code == 404
    assert harness.client.get(DECISION_URL + "?current=Bad").status_code == 400
    path = owned_copy(built_snapshot, tmp_path)
    state = full_state(path)
    assert "decisions_v2" not in state
    with TestClient(create_public_demo(path)) as client:
        assert client.get(DECISION_URL).status_code == 200
        assert client.get(DECISION_URL + "missing").status_code == 404
        assert client.get(DECISION_URL + "?current=Bad").status_code == 400
        assert client.post(DECISION_URL).status_code == 405
    assert full_state(path) == state
