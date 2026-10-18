"""INV-01/02/03/04/05/11: strict preserved v2 provenance, no current-state repair."""

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError

from flight_recorder.adapters.hubspot.contracts import CaptureRefused
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import validate_envelope_json
from flight_recorder.ledger.schema import (
    capture_contexts,
    decision_consumed_inputs_v2,
    decision_context_v2,
    decisions_v2,
    evidence_versions_v2,
)
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import IntegrityFailure, ReconstructionMismatch, reconstruct
from tests.adapters.hubspot.domain_cases import admit, make_case
from tests.conftest import DECISION_EVENT_ID, Harness, canonical_by_type, seed_all

pytestmark = pytest.mark.invariant
V2_TABLES = (
    capture_contexts,
    evidence_versions_v2,
    decisions_v2,
    decision_context_v2,
    decision_consumed_inputs_v2,
)


@pytest.mark.parametrize("table", V2_TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_new_domain_rows_are_append_only(harness, tmp_path, table, operation):
    case = make_case(tmp_path)
    admit(harness, case)
    before = harness.snapshot()
    with harness.engine.connect() as conn:
        assert conn.execute(select(table)).first() is not None
    statement = (
        delete(table)
        if operation == "delete"
        else update(table).values({next(c.name for c in table.c if not c.primary_key): "tampered"})
    )
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(statement)
    assert harness.snapshot() == before


@pytest.mark.parametrize(
    "fault",
    [
        "capture_missing",
        "capture_json",
        "capture_identity",
        "evidence_missing",
        "evidence_semantic",
        "observation_json",
        "eligible_time",
        "context_missing",
        "context_value",
        "consumed_missing",
        "consumed_value",
        "consumed_contribution",
        "decision_boundary",
        "capture_table_missing",
    ],
)
def test_corrupt_or_missing_history_fails_without_repair(harness, tmp_path, fault):
    case = make_case(tmp_path)
    admit(harness, case)
    table = {
        "capture_missing": capture_contexts,
        "capture_json": capture_contexts,
        "capture_identity": capture_contexts,
        "capture_table_missing": capture_contexts,
        "evidence_missing": evidence_versions_v2,
        "evidence_semantic": evidence_versions_v2,
        "observation_json": evidence_versions_v2,
        "eligible_time": evidence_versions_v2,
        "context_missing": decision_context_v2,
        "context_value": decision_context_v2,
        "consumed_missing": decision_consumed_inputs_v2,
        "consumed_value": decision_consumed_inputs_v2,
        "consumed_contribution": decision_consumed_inputs_v2,
        "decision_boundary": decisions_v2,
    }[fault]
    values = {
        "capture_json": {"body_json": "{}"},
        "capture_identity": {"unit_id": "tampered"},
        "evidence_semantic": {"value_json": '{"value":999}'},
        "observation_json": {"observation_json": "{}"},
        "eligible_time": {"available_at": "2099-01-01T00:00:00.000000Z"},
        "context_value": {"value_text": "999"},
        "consumed_value": {"value_text": "999"},
        "consumed_contribution": {"contribution": 999},
        "decision_boundary": {"decision_boundary": "2099-01-01T00:00:00.000000Z"},
    }
    # Disposable corruption oracle only: remove guards and FKs through raw SQLite,
    # never a production repair/migration helper.
    raw = harness.engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(f"DROP TRIGGER {table.name}_no_update")
        raw.execute(f"DROP TRIGGER {table.name}_no_delete")
        if fault.endswith("_table_missing"):
            raw.execute(f"DROP TABLE {table.name}")
        elif fault.endswith("_missing"):
            raw.execute(f"DELETE FROM {table.name}")
        else:
            for key, value in values[fault].items():
                raw.execute(f"UPDATE {table.name} SET {key}=?", (value,))
        raw.commit()
    finally:
        raw.close()
    writes = []

    def prohibit_writes(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)", statement, re.I):
            writes.append(statement)
            raise AssertionError("reader attempted repair")

    event.listen(harness.engine, "before_cursor_execute", prohibit_writes)
    try:
        with (
            harness.engine.connect() as conn,
            pytest.raises((IntegrityFailure, ReconstructionMismatch)),
        ):
            reconstruct(conn, case["decision"]["event_id"])
    finally:
        event.remove(harness.engine, "before_cursor_execute", prohibit_writes)
    assert not writes


@given(st.integers(min_value=-100000, max_value=100000), st.integers(min_value=0, max_value=172800))
def test_v2_availability_uses_captured_utc_not_delivery_time(tmp_path_factory, delta, delay):
    directory = tmp_path_factory.mktemp("v2-time")
    boundary = datetime.fromisoformat(
        canonical_by_type("decision.recorded")["payload"]["decision_boundary"].replace(
            "Z", "+00:00"
        )
    )
    source_at = boundary + timedelta(microseconds=delta)
    if delta > 0:
        with pytest.raises(CaptureRefused):
            make_case(directory, available_at={"numberofemployees": source_at})
        return
    case = make_case(directory, available_at={"numberofemployees": source_at})
    for name in ("evidence", "decision"):
        case[name]["recorded_at"] = (boundary + timedelta(seconds=delay)).isoformat()
    h = Harness(directory)
    admit(h, case)
    with h.engine.connect() as conn:
        row = conn.execute(
            select(evidence_versions_v2).where(
                evidence_versions_v2.c.evidence_type == "employee_count"
            )
        ).one()
        assert datetime.fromisoformat(row.available_at) == max(
            boundary - timedelta(seconds=1), source_at
        )
        assert reconstruct(conn, case["decision"]["event_id"]).result == case["result"]
    h.client.close()
    h.engine.dispose()


def test_dst_fold_reversed_read_cannot_enter_v2(tmp_path):
    zone = ZoneInfo("America/Los_Angeles")
    start = datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    assert end.astimezone(UTC) > start.astimezone(UTC)
    case = make_case(tmp_path, clock_times=[start, end, end + timedelta(hours=2)])
    case["evidence"]["recorded_at"] = (end + timedelta(days=2)).isoformat()
    validate_envelope_json(json.dumps(case["evidence"]))
    cap = case["evidence"]["payload"]["capture"]
    cap["read_started_at"], cap["read_finished_at"] = (
        cap["read_finished_at"],
        cap["read_started_at"],
    )
    case["evidence"]["payload"]["capture_hash"] = canonical_hash(cap)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        validate_envelope_json(json.dumps(case["evidence"]))


def test_original_and_counterfactual_only_read_preserved_projections(
    harness, tmp_path, monkeypatch
):
    case = make_case(tmp_path)
    admit(harness, case)
    # Newly registered current logic is separate; existing context stays sealed.
    from tests.conftest import canonical_envelopes

    current = next(
        e
        for e in canonical_envelopes()
        if e["event_type"] == "logic_artifact.registered"
        and e["payload"]["artifact"]["logic_version"]
        != case["decision"]["payload"]["logic_artifact"]["logic_version"]
    )
    assert harness.post(current).status_code == 201
    digest = canonical_hash(current["payload"]["artifact"])
    before = harness.snapshot()

    def guard(conn, cursor, statement, parameters, context, executemany):
        assert not re.search(r'\b(?:FROM|JOIN)\s+["`\[]?(?:events|accounts)\b', statement, re.I), (
            statement
        )
        assert not re.match(
            r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)", statement, re.I
        ), statement

    def no_network(*args, **kwargs):
        raise AssertionError("replay attempted network")

    import socket

    monkeypatch.setattr(socket, "create_connection", no_network)
    event.listen(harness.engine, "before_cursor_execute", guard)
    try:
        with harness.engine.connect() as conn:
            assert reconstruct(conn, case["decision"]["event_id"]).result == case["result"]
            assert replay(conn, case["decision"]["event_id"], digest).label == "counterfactual"
    finally:
        event.remove(harness.engine, "before_cursor_execute", guard)
    assert harness.snapshot() == before


def test_retained_legacy_database_needs_no_new_tables(harness):
    seed_all(harness)
    with harness.engine.begin() as conn:
        for table in reversed(V2_TABLES):
            table.drop(conn)
    with harness.engine.connect() as conn:
        assert reconstruct(conn, DECISION_EVENT_ID).result.score == 86


def test_every_projection_has_one_nonempty_mutation_test_group():
    from flight_recorder.ledger.schema import PROJECTION_TABLES
    from tests.invariants.test_inv_01_projections_append_only import LEGACY_TABLE_NAMES

    new = {t.name for t in V2_TABLES}
    assert not new & LEGACY_TABLE_NAMES
    assert new | LEGACY_TABLE_NAMES == {t.name for t in PROJECTION_TABLES}
