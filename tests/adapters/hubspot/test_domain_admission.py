"""Real collector admission/replay of versioned synthetic captured observations."""

import copy

import pytest

from flight_recorder.collector.service import Collector
from flight_recorder.replay.reconstruct import reconstruct
from tests.adapters.hubspot.domain_cases import admit, make_case


def test_delayed_v2_submission_reconstructs_from_preserved_input(harness, tmp_path):
    case = make_case(tmp_path)
    admit(harness, case)
    with harness.engine.connect() as conn:
        result = reconstruct(conn, case["decision"]["event_id"])
    assert result.result == case["result"]
    assert harness.post(case["decision"]).status_code == 200
    assert harness.post(case["evidence"]).status_code == 200


@pytest.mark.parametrize(
    "change", ["result", "contribution", "rule", "workflow", "context", "explicit_ignore"]
)
def test_v2_invalid_decision_never_leaves_partial_rows(harness, tmp_path, change):
    case = make_case(
        tmp_path, ignored=("numberofemployees",) if change == "explicit_ignore" else ()
    )
    admit(harness, case, decision=False)
    env = copy.deepcopy(case["decision"])
    if change == "result":
        env["payload"]["result"]["score"] += 1
    elif change == "contribution":
        env["payload"]["consumed_inputs"][0]["contribution"] += 1
    elif change == "rule":
        env["payload"]["logic_artifact"]["artifact_hash"] = "0" * 64
    elif change == "workflow":
        env["payload"]["workflow_version"] = "unbound"
    elif change == "context":
        env["payload"]["historical_context"].pop()
    before = harness.snapshot()
    assert harness.post(env).status_code == 422
    assert harness.snapshot() == before


def test_distinct_event_for_recorded_unit_conflicts(harness, tmp_path):
    case = make_case(tmp_path)
    admit(harness, case)
    env = copy.deepcopy(case["decision"])
    env["event_id"] += "-second"
    before = harness.snapshot()
    assert harness.post(env).status_code == 409
    assert harness.snapshot() == before


def test_reprocessing_reuses_complete_evidence_without_remint(harness, tmp_path):
    first = make_case(tmp_path)
    admit(harness, first)
    second = make_case(tmp_path, selection="reprocess", reprocesses=first["unit"].unit_id)
    assert [i["evidence_version_id"] for i in second["evidence"]["payload"]["items"]] == [
        i["evidence_version_id"] for i in first["evidence"]["payload"]["items"]
    ]
    before = harness.snapshot()
    assert harness.post(second["evidence"]).status_code == 422
    assert harness.snapshot() == before
    assert harness.post(second["decision"]).status_code == 201
    with harness.engine.connect() as conn:
        assert reconstruct(conn, second["decision"]["event_id"]).result == second["result"]


def test_rollback_after_v2_projection_is_complete(harness, tmp_path):
    case = make_case(tmp_path)
    assert harness.post(case["account"]).status_code == 201
    assert harness.post(case["artifact"]).status_code == 201
    collector = Collector(harness.engine)
    before = harness.snapshot()

    def crash(envelope):
        raise RuntimeError("synthetic precommit fault")

    collector.before_commit = crash
    import json

    with pytest.raises(RuntimeError, match="synthetic precommit"):
        collector.ingest_json(json.dumps(case["evidence"]))
    assert harness.snapshot() == before


def test_concurrent_v2_admission_has_one_complete_winner(harness, tmp_path):
    """Both transactions read before either inserts; SQLite upgrade loser is visible."""
    import concurrent.futures
    import json
    import threading

    from sqlalchemy import select

    from flight_recorder.collector.errors import ConflictError
    from flight_recorder.ledger.schema import events

    case = make_case(tmp_path)
    admit(harness, case, decision=False)
    barrier = threading.Barrier(2)
    original = Collector._check_account_rules.__func__

    def check(cls, conn, envelope):
        original(cls, conn, envelope)
        barrier.wait(timeout=10)

    # Only the two real collector calls below use this scoped barrier.
    from unittest.mock import patch

    candidates = [copy.deepcopy(case["decision"]) for _ in range(2)]
    candidates[1]["event_id"] += "-racing"

    def run(env):
        try:
            return Collector(harness.engine).ingest_json(json.dumps(env)).http_status
        except ConflictError as exc:
            assert exc.body["reason"] in ("v2_admission_contended", "v2_identity_conflict")
            return exc.status_code

    with patch.object(Collector, "_check_account_rules", classmethod(check)):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, candidates))
    assert sorted(results) == [201, 409]
    with harness.engine.connect() as conn:
        stored = (
            conn.execute(
                select(events.c.event_id).where(events.c.event_type == "decision.recorded")
            )
            .scalars()
            .all()
        )
        assert len(stored) == 1
        assert reconstruct(conn, stored[0]).result == case["result"]
    assert (
        harness.post(next(e for e in candidates if e["event_id"] == stored[0])).status_code == 200
    )


@pytest.mark.parametrize(
    "fault", ["unknown", "self", "same_processing", "cross_account", "not_earlier"]
)
def test_lineage_requires_same_account_prior_accepted_unit(harness, tmp_path, fault):
    from flight_recorder.collector.canonical import canonical_hash

    first = make_case(tmp_path)
    admit(harness, first)
    prior = first["unit"].unit_id
    second = make_case(tmp_path, selection="different", reprocesses=prior)
    cap = second["capture"]
    # These cases deliberately alter the derived lineage/metadata. Full capture
    # validation must refuse incoherence before any write.
    if fault == "unknown":
        cap["reprocesses"] = "f" * 64
    elif fault == "self":
        cap["reprocesses"] = cap["unit_id"]
    elif fault == "same_processing":
        cap["processing_version"] = first["capture"]["processing_version"]
    elif fault == "cross_account":
        second["decision"]["account_ref"] = "different-account"
    elif fault == "not_earlier":
        cap["capture_sequence"] = first["capture"]["capture_sequence"]
    second["decision"]["payload"]["capture_hash"] = canonical_hash(cap)
    before = harness.snapshot()
    assert harness.post(second["decision"]).status_code in (409, 422)
    assert harness.snapshot() == before


def test_unavailable_absent_and_vendor_observation_do_not_mint_evidence(harness, tmp_path):
    case = make_case(
        tmp_path,
        properties={
            "numberofemployees": None,
            "country": "United States",
            "hubspotscore": 99,
        },
    )
    assert [i["evidence_type"] for i in case["evidence"]["payload"]["items"]] == [
        "headquarters_country"
    ]
    admit(harness, case)
    with harness.engine.connect() as conn:
        result = reconstruct(conn, case["decision"]["event_id"]).result
    assert str(result.context_states["employee_count"]) == "unavailable"
    assert "industry" not in result.context_states
    assert "hubspotscore" not in result.context_states


def test_v2_artifact_corruption_is_visible_before_writes(harness, tmp_path):
    case = make_case(tmp_path)
    admit(harness, case, decision=False)
    raw = harness.engine.raw_connection()
    try:
        raw.execute("DROP TRIGGER logic_artifacts_no_update")
        raw.execute("UPDATE logic_artifacts SET artifact_json='{}'")
        raw.commit()
    finally:
        raw.close()
    before = harness.snapshot()
    assert harness.post(case["decision"]).status_code == 422
    assert harness.snapshot() == before


def test_unrelated_database_errors_propagate_without_duplicate_success(
    harness, tmp_path, monkeypatch
):
    import json
    import sqlite3

    from sqlalchemy.exc import OperationalError

    case = make_case(tmp_path)
    assert harness.post(case["account"]).status_code == 201
    assert harness.post(case["artifact"]).status_code == 201
    collector = Collector(harness.engine)
    before = harness.snapshot()

    def fail(envelope):
        raise OperationalError("synthetic SQL", {}, sqlite3.OperationalError("unrelated"))

    collector.before_commit = fail
    with pytest.raises(OperationalError):
        collector.ingest_json(json.dumps(case["evidence"]))
    assert harness.snapshot() == before
