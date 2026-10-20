"""INV-01/02/04/06/08/11: presentation cannot rewrite or import history."""

import re
import socket
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import event

from flight_recorder.adapters.hubspot.contracts import CaptureRefused
from tests.acceptance.test_decision_detail_page import element
from tests.acceptance.test_hubspot_v2_presentation import captured, get, page_api
from tests.adapters.hubspot.domain_cases import admit, make_case
from tests.conftest import Harness, canonical_by_type, register_current_logic, seed_all
from tests.public_demo.conftest import full_state

pytestmark = pytest.mark.invariant


def test_page_original_and_counterfactual_are_offline_and_read_only(tmp_path, harness, monkeypatch):
    case = captured(tmp_path, harness)
    current = register_current_logic(harness)
    before = full_state(harness.db_path)
    operational = (tmp_path / "capture.sqlite").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("GET attempted network or collector write")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(harness.collector, "ingest_json", forbidden)

    def check(conn, cursor, statement, parameters, context, executemany):
        assert not re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)", statement, re.I)

    event.listen(harness.engine, "before_cursor_execute", check)
    event.listen(harness.app.state.engine, "before_cursor_execute", check)
    try:
        # Direct reader avoids TestClient's internal socketpair, while testing the same model.
        api = page_api()
        with harness.engine.connect() as conn:
            page = api.load_hubspot_decision_page(
                conn, case["decision"]["account_ref"], case["decision"]["event_id"], current
            )
        assert page.original is not None and page.comparison is not None
        body = get(harness, case, "?current=" + current)
        assert "Verified original" in element(body, "v2-original")
        assert "Ephemeral counterfactual" in element(body, "v2-replay")
    finally:
        event.remove(harness.engine, "before_cursor_execute", check)
        event.remove(harness.app.state.engine, "before_cursor_execute", check)
    assert full_state(harness.db_path) == before
    assert (tmp_path / "capture.sqlite").read_bytes() == operational


@given(st.integers(min_value=1, max_value=10000))
def test_current_data_and_appended_history_do_not_rewrite_original_page(
    tmp_path_factory, employees
):
    directory = tmp_path_factory.mktemp("v2-page-history")
    h = Harness(directory)
    try:
        case = captured(directory, h)
        body = get(h, case)
        prior = {
            n: element(body, n)
            for n in ("v2-original", "v2-historical-ruleset", "v2-context-table")
        }
        later = captured(
            directory,
            h,
            selection="later",
            reprocesses=case["unit"].unit_id,
            properties={"numberofemployees": employees},
        )
        assert later["unit"].unit_id != case["unit"].unit_id
        current = register_current_logic(h)
        changed = get(h, case, "?current=" + current)
        assert {n: element(changed, n) for n in prior} == prior
    finally:
        h.client.close()
        h.engine.dispose()


@given(st.sampled_from([-1, 0, 1]), st.integers(min_value=-12, max_value=14))
def test_before_at_after_availability_never_imports_later_values(tmp_path_factory, delta, offset):
    directory = tmp_path_factory.mktemp("v2-page-time")
    boundary = datetime.fromisoformat(
        canonical_by_type("decision.recorded")["payload"]["decision_boundary"]
    )
    h = Harness(directory)
    try:
        original = captured(directory, h)
        before = element(get(h, original), "v2-original")
        source_at = (boundary + timedelta(microseconds=delta)).astimezone(
            timezone(timedelta(hours=offset))
        )
        other = directory / "other"
        other.mkdir()
        if delta > 0:
            with pytest.raises(CaptureRefused):
                make_case(other, selection="time", available_at={"numberofemployees": source_at})
        else:
            case = make_case(other, selection="time", available_at={"numberofemployees": source_at})
            # Separate ledger avoids sequence overlap with the retained original.
            fresh = Harness(other)
            try:
                admit(fresh, case)
                case["url"] = (
                    f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
                )
                assert "Verified original" in element(get(fresh, case), "v2-original")
            finally:
                fresh.client.close()
                fresh.engine.dispose()
        assert element(get(h, original), "v2-original") == before
    finally:
        h.client.close()
        h.engine.dispose()


def test_later_legacy_outcomes_and_counterfactual_views_do_not_change_actual_metrics(
    tmp_path, harness
):
    case = captured(tmp_path, harness)
    before = element(get(harness, case), "v2-original")
    seed_all(harness)
    assert element(get(harness, case), "v2-original") == before
    current = register_current_logic(harness)
    metrics = harness.client.get("/insights").content
    state = harness.snapshot()
    get(harness, case, "?current=" + current)
    assert harness.snapshot() == state
    assert harness.client.get("/insights").content == metrics
