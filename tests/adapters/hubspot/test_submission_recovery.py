"""S05/S06: real Collector effects survive every operational acknowledgment gap."""

import copy
import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from flight_recorder.adapters.hubspot.store import CaptureStore
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.errors import ConflictError, RejectedError
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import decisions_v2, events, evidence_versions_v2
from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct
from tests.adapters.hubspot.domain_cases import make_case


def api():
    # A missing feature is an assertion failure in RED, not an import/collection error.
    name = "flight_recorder.adapters.hubspot.submit"
    assert importlib.util.find_spec(name) is not None, "Task2B submission interface missing"
    submit = importlib.import_module(name)
    contracts = importlib.import_module("flight_recorder.adapters.hubspot.submission_contracts")
    store = importlib.import_module("flight_recorder.adapters.hubspot.submission_store")
    return submit, contracts, store


def setup_case(tmp_path, harness, *, freeze=True, **kwargs):
    submit, contracts, stores = api()
    case = make_case(tmp_path, **kwargs)
    capture = CaptureStore(tmp_path / "capture.sqlite")
    store = stores.SubmissionStore(capture)
    capture.close()
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(case["account"]),
        canonical_bytes(case["artifact"]),
        case["decision"]["recorded_at"],
        b"{}",
    )
    frozen = (
        submit.freeze_submission(store, harness.collector, case["unit"], prerequisites=prereqs)
        if freeze
        else None
    )
    return submit, contracts, store, case, frozen, prereqs


def count(harness, table):
    with harness.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def restart(store, harness):
    _, _, stores = api()
    path = store.path
    store.close()
    return stores.SubmissionStore(CaptureStore(path)), Collector(harness.engine)


def test_freeze_is_pure_and_wire_matches_independent_oracle(tmp_path, harness):
    submit, _, store, case, frozen, prereqs = setup_case(tmp_path, harness)
    assert count(harness, events) == 0
    doc = frozen.document
    expected = copy.deepcopy(case["decision"])
    expected["event_id"] = "fr-submit-decision-v1-" + case["unit"].unit_id
    assert doc["stages"][-1]["envelope"] == expected
    expected = copy.deepcopy(case["evidence"])
    expected["event_id"] = "fr-submit-evidence-v1-" + case["unit"].unit_id
    assert doc["stages"][2]["envelope"] == expected
    changed = replace(prereqs, recorded_at="invalid", account_envelope=b"not JSON")
    assert (
        submit.freeze_submission(
            store, harness.collector, case["unit"], prerequisites=changed
        ).canonical_bytes
        == frozen.canonical_bytes
    )
    doc["stages"].clear()
    assert len(frozen.document["stages"]) == 4
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    assert count(harness, decisions_v2) == 1
    with harness.engine.connect() as conn:
        assert (
            reconstruct(conn, expected["event_id"].replace("evidence", "decision")).result.score
            == case["result"].score
        )


@pytest.mark.parametrize("stage", range(4))
@pytest.mark.parametrize("gap", ["before_call", "lost_return", "before_receipt", "after_receipt"])
def test_each_real_commit_and_receipt_boundary_recovers(tmp_path, harness, monkeypatch, stage, gap):
    submit, _, store, case, frozen, _ = setup_case(tmp_path, harness)
    real_ingest = harness.collector.ingest_json
    real_record = store.observe
    calls = []

    def ingest(body):
        calls.append(body)
        index = next(
            i
            for i, s in enumerate(frozen.document["stages"])
            if s["event_id"] == json.loads(body)["event_id"]
        )
        if index == stage and gap == "before_call":
            raise RuntimeError("stop before collector")
        result = real_ingest(body)
        if index == stage and gap == "lost_return":
            raise RuntimeError("accepted but lost response")
        return result

    def observe(attempt, disposition, result):
        if (
            disposition.startswith("accepted")
            and json.loads(calls[-1])["event_id"] == frozen.document["stages"][stage]["event_id"]
        ):
            if gap == "before_receipt":
                raise RuntimeError("receipt transaction failed")
            answer = real_record(attempt, disposition, result)
            if gap == "after_receipt":
                raise RuntimeError("stopped after receipt")
            return answer
        return real_record(attempt, disposition, result)

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)
    monkeypatch.setattr(store, "observe", observe)
    with pytest.raises(RuntimeError):
        submit.resume_submission(store, harness.collector, case["unit"])
    if stage == 3 and gap != "before_call":
        assert count(harness, decisions_v2) == 1
    if stage == 3:
        assert count(harness, evidence_versions_v2) > 0
    recovered, collector = restart(store, harness)
    retried = []
    actual = collector.ingest_json

    def record(body):
        retried.append(body)
        return actual(body)

    monkeypatch.setattr(collector, "ingest_json", record)
    assert submit.resume_submission(recovered, collector, case["unit"]).state == "complete"
    frozen_calls = {canonical_bytes(s["envelope"]) for s in frozen.document["stages"]}
    assert all(body in frozen_calls for body in calls + retried)
    assert count(harness, events) == 4
    assert count(harness, decisions_v2) == 1
    if gap in ("lost_return", "before_receipt"):
        with sqlite3.connect(recovered.path) as conn:
            duplicates = conn.execute(
                (
                    "SELECT COUNT(*) FROM submission_attempts a JOIN "
                    "submission_observations o USING(attempt_id) WHERE a.event_id=? "
                    "AND o.disposition='accepted_duplicate'"
                ),
                (frozen.document["stages"][stage]["event_id"],),
            ).fetchone()[0]
        assert duplicates >= 1
    assert submit.resume_submission(recovered, collector, case["unit"]).state == "complete"
    assert count(harness, events) == 4


@pytest.mark.parametrize(
    "reason,state", [("permanent", "failed"), ("v2_admission_contended", "retryable")]
)
def test_rejections_stop_without_internal_retry(tmp_path, harness, monkeypatch, reason, state):
    submit, _, store, case, _, _ = setup_case(tmp_path, harness)
    actual = harness.collector.ingest_json
    calls = []

    def fail_decision(body):
        calls.append(body)
        if json.loads(body)["event_type"] == "decision.recorded":
            if state == "failed":
                raise RejectedError({"reason": reason})
            raise ConflictError({"reason": reason})
        return actual(body)

    monkeypatch.setattr(harness.collector, "ingest_json", fail_decision)
    result = submit.resume_submission(store, harness.collector, case["unit"])
    assert result.state == state and result.next_stage == "decision"
    assert len(calls) == 4 and count(harness, decisions_v2) == 0
    assert count(harness, evidence_versions_v2) > 0
    monkeypatch.setattr(harness.collector, "ingest_json", actual)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == (
        "failed" if state == "failed" else "complete"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "wrong"),
        ("canonical_hash", "0" * 64),
        ("http_status", 200),
        ("http_status", 201.0),
        ("status", "bogus"),
    ],
)
def test_bad_acknowledgment_holds_accepted_unit(tmp_path, harness, monkeypatch, field, value):
    submit, _, store, case, _, _ = setup_case(tmp_path, harness)
    actual = harness.collector.ingest_json
    monkeypatch.setattr(
        harness.collector, "ingest_json", lambda body: replace(actual(body), **{field: value})
    )
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "failed"
    assert count(harness, events) == 1
    monkeypatch.setattr(harness.collector, "ingest_json", actual)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "failed"
    assert count(harness, events) == 1


def test_missing_capture_target_and_corrupt_frozen_refuse(tmp_path, harness, monkeypatch):
    submit, contracts, store, case, _, prereqs = setup_case(tmp_path, harness)
    other = Collector(make_engine(tmp_path / "other-ledger.sqlite"))
    with pytest.raises(contracts.SubmissionError):
        submit.resume_submission(store, other, case["unit"])
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER frozen_submissions_no_update")
        conn.execute("UPDATE frozen_submissions SET body_bytes=x'7b7d'")
    with pytest.raises(contracts.SubmissionError):
        submit.resume_submission(store, harness.collector, case["unit"])
    assert count(harness, events) == 0


def test_two_freezers_and_resumers_share_one_winner(tmp_path, harness):
    submit, _, store, case, frozen, prereqs = setup_case(tmp_path, harness)
    # Delete only a disposable unsubmitted winner to stage a truly simultaneous first freeze.
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER frozen_submissions_no_delete")
        conn.execute("DELETE FROM frozen_submissions")
    _, _, stores = api()
    barrier = Barrier(2)
    harness.collector.ingest_json(canonical_bytes(case["account"]))
    harness.collector.ingest_json(canonical_bytes(case["artifact"]))

    def work(i):
        other = stores.SubmissionStore(CaptureStore(store.path))
        collector = Collector(harness.engine)
        real_freeze = other.freeze

        def freeze_after_both_prepared(unit, candidate, target):
            barrier.wait(timeout=15)
            return real_freeze(unit, candidate, target)

        other.freeze = freeze_after_both_prepared
        selected = submit.freeze_submission(
            other,
            collector,
            case["unit"],
            prerequisites=replace(
                prereqs,
                recorded_at=(
                    datetime.fromisoformat(prereqs.recorded_at) + timedelta(seconds=i)
                ).isoformat(),
            ),
        )
        return selected.canonical_bytes, submit.resume_submission(
            other, collector, case["unit"]
        ).state

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(work, range(2)))
    assert results[0][0] == results[1][0]
    assert all(state in ("complete", "retryable") for _, state in results)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    assert count(harness, decisions_v2) == 1 and count(harness, events) == 4


@pytest.mark.parametrize("change", ["all_reuse", "partial", "new_read", "source_time"])
def test_reprocessing_reuses_only_verified_complete_observations(tmp_path, harness, change):
    submit, _, store, first, _, _ = setup_case(tmp_path, harness)
    assert submit.resume_submission(store, harness.collector, first["unit"]).state == "complete"
    properties = copy.deepcopy(first["capture"]["observation"]["properties"])
    kwargs = {}
    if change == "partial":
        properties["numberofemployees"] += 1
        kwargs["properties"] = properties
    if change == "new_read":
        boundary = datetime.fromisoformat(first["capture"]["capture_boundary"])
        kwargs["clock_times"] = [
            boundary,
            boundary + timedelta(seconds=1),
            boundary + timedelta(seconds=2),
        ]
    if change == "source_time":
        kwargs["available_at"] = {
            "numberofemployees": datetime.fromisoformat(first["capture"]["read_started_at"])
        }
    second = make_case(
        tmp_path, selection="reprocessed", reprocesses=first["unit"].unit_id, **kwargs
    )
    _, contracts, _ = api()
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(second["account"]),
        canonical_bytes(second["artifact"]),
        second["decision"]["recorded_at"],
        b"{}",
    )
    frozen = submit.freeze_submission(
        store, harness.collector, second["unit"], prerequisites=prereqs
    )
    stages = frozen.document["stages"]
    total = len(first["evidence"]["payload"]["items"])
    expected_new = {"all_reuse": 0, "partial": 1, "source_time": 1, "new_read": total}[change]
    assert len(frozen.document["reused_evidence"]) == total - expected_new
    evidence = next((s for s in stages if s["stage"] == "evidence"), None)
    assert (len(evidence["envelope"]["payload"]["items"]) if evidence else 0) == expected_new
    assert submit.resume_submission(store, harness.collector, second["unit"]).state == "complete"
    assert count(harness, decisions_v2) == 2
    assert count(harness, evidence_versions_v2) == total + expected_new
    with harness.engine.connect() as conn:
        assert (
            reconstruct(conn, "fr-submit-decision-v1-" + first["unit"].unit_id).result.score
            == first["result"].score
        )


@pytest.mark.parametrize("lineage_change", ["dropped", "added", "substituted"])
def test_capture_lineage_mismatch_refuses_before_freeze(
    tmp_path, harness, monkeypatch, lineage_change
):
    from flight_recorder.collector.capture_contract import validate_capture

    submit, contracts, store, first, _, _ = setup_case(tmp_path, harness)
    assert submit.resume_submission(store, harness.collector, first["unit"]).state == "complete"
    predecessor = first["unit"].unit_id
    if lineage_change == "substituted":
        prior = make_case(tmp_path, selection="other-prior", reprocesses=predecessor)
        prereqs = contracts.SubmissionPrerequisites(
            canonical_bytes(prior["account"]),
            canonical_bytes(prior["artifact"]),
            prior["decision"]["recorded_at"],
            b"{}",
        )
        submit.freeze_submission(store, harness.collector, prior["unit"], prerequisites=prereqs)
        assert submit.resume_submission(store, harness.collector, prior["unit"]).state == "complete"
        predecessor = prior["unit"].unit_id
    case = make_case(
        tmp_path,
        selection="lineage-under-test",
        reprocesses=None if lineage_change == "added" else predecessor,
    )
    unit = case["unit"]
    changed = copy.deepcopy(case["capture"])
    changed["reprocesses"] = None if lineage_change == "dropped" else first["unit"].unit_id
    assert {k for k in changed if changed[k] != case["capture"][k]} == {"reprocesses"}
    checked = validate_capture(changed, canonical_hash(changed))
    assert checked.unit_id == unit.unit_id and checked.reprocesses != unit.reprocesses
    tables = (
        "submission_target",
        "frozen_submissions",
        "submission_attempts",
        "submission_observations",
    )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT reprocesses FROM processing_units WHERE unit_id=?", (unit.unit_id,)
        ).fetchone() == (unit.reprocesses,)
        # Only this disposable capture is corrupted; the registered unit remains intact.
        conn.execute("DROP TRIGGER captures_no_update")
        conn.execute(
            "UPDATE captures SET body_json=? WHERE unit_id=?",
            (canonical_bytes(changed).decode(), unit.unit_id),
        )
        before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    domain_before = harness.snapshot()
    with harness.engine.connect() as conn:
        events_before = conn.execute(select(events).order_by(events.c.event_id)).all()
    calls = []
    ingest = harness.collector.ingest_json

    def record(body):
        calls.append(body)
        return ingest(body)

    monkeypatch.setattr(harness.collector, "ingest_json", record)
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(case["account"]),
        canonical_bytes(case["artifact"]),
        case["decision"]["recorded_at"],
        b"{}",
    )
    with pytest.raises(contracts.SubmissionError, match="capture_unit_mismatch"):
        submit.freeze_submission(store, harness.collector, unit, prerequisites=prereqs)
    assert calls == []
    assert harness.snapshot() == domain_before
    with harness.engine.connect() as conn:
        assert conn.execute(select(events).order_by(events.c.event_id)).all() == events_before
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute(
                "SELECT * FROM frozen_submissions WHERE unit_id=?", (unit.unit_id,)
            ).fetchall()
            == []
        )
        assert {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables
        } == before
        assert conn.execute(
            "SELECT reprocesses FROM processing_units WHERE unit_id=?", (unit.unit_id,)
        ).fetchone() == (unit.reprocesses,)


def test_delivery_attempt_metadata_reuses_frozen_reprocessing(tmp_path, harness):
    from flight_recorder.adapters.hubspot.contracts import SourceNotification

    submit, contracts, store, first, _, _ = setup_case(tmp_path, harness)
    assert submit.resume_submission(store, harness.collector, first["unit"]).state == "complete"
    second = make_case(tmp_path, selection="delivery-retry", reprocesses=first["unit"].unit_id)
    unit = second["unit"]
    payload = json.loads(unit.notification.raw_body)
    payload.update(attemptNumber=9, messageId="different-delivery", messageType="delivery")
    retry = replace(
        unit,
        notification=SourceNotification.from_payload(payload, raw_body=canonical_bytes(payload)),
    )
    assert retry.unit_id == unit.unit_id
    assert retry.notification.raw_body != unit.notification.raw_body
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(second["account"]),
        canonical_bytes(second["artifact"]),
        second["decision"]["recorded_at"],
        b"{}",
    )
    frozen = submit.freeze_submission(store, harness.collector, retry, prerequisites=prereqs)
    assert frozen.document["stages"][-1]["envelope"]["payload"]["capture"] == second["capture"]
    assert {s["stage"] for s in frozen.document["stages"]} == {"account", "artifact", "decision"}
    assert submit.resume_submission(store, harness.collector, retry).state == "complete"
    assert store.load(unit).canonical_bytes == frozen.canonical_bytes
    with harness.engine.connect() as conn:
        assert (
            conn.execute(
                select(decisions_v2.c.reprocesses).where(decisions_v2.c.unit_id == unit.unit_id)
            ).scalar_one()
            == first["unit"].unit_id
        )
    assert count(harness, decisions_v2) == 2
    assert count(harness, evidence_versions_v2) == len(first["evidence"]["payload"]["items"])


def test_post_freeze_mint_race_is_permanent_without_rewriting_plan(tmp_path, harness):
    from tests.adapters.hubspot.domain_cases import admit

    submit, _, store, case, frozen, _ = setup_case(tmp_path, harness)
    admit(harness, case, decision=False)
    result = submit.resume_submission(store, harness.collector, case["unit"])
    assert result.state == "failed" and result.next_stage == "evidence"
    assert store.load(case["unit"]).canonical_bytes == frozen.canonical_bytes
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "failed"
    assert count(harness, decisions_v2) == 0 and count(harness, events) == 3


@pytest.mark.parametrize(
    "bad",
    ["raw_missing", "account", "artifact", "clock", "supersession", "duplicate_json", "ignored"],
)
def test_invalid_preparation_never_writes_a_submission_or_domain_event(tmp_path, harness, bad):
    kwargs = {"ignored": ("numberofemployees",)} if bad == "ignored" else {}
    submit, contracts, store, case, _, prereqs = setup_case(
        tmp_path, harness, freeze=False, **kwargs
    )
    if bad == "raw_missing":
        with sqlite3.connect(store.path) as conn:
            conn.execute("DROP TRIGGER raw_notifications_no_delete")
            conn.execute("DELETE FROM raw_notifications")
    if bad == "account":
        account = copy.deepcopy(case["account"])
        account["account_ref"] = "other"
        prereqs = replace(prereqs, account_envelope=canonical_bytes(account))
    if bad == "artifact":
        artifact = copy.deepcopy(case["artifact"])
        artifact["payload"]["artifact"]["threshold"] += 1
        prereqs = replace(prereqs, artifact_envelope=canonical_bytes(artifact))
    if bad == "clock":
        prereqs = replace(prereqs, recorded_at="2020-01-01T00:00:00")
    if bad == "supersession":
        prereqs = replace(prereqs, supersessions=b'{"unknown":"x"}')
    if bad == "duplicate_json":
        prereqs = replace(prereqs, account_envelope=b'{"event_id":"a","event_id":"b"}')
    with pytest.raises((contracts.SubmissionError, ValueError)):
        submit.freeze_submission(store, harness.collector, case["unit"], prerequisites=prereqs)
    assert count(harness, events) == 0
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM frozen_submissions").fetchone()[0] == 0


def test_all_four_operational_tables_are_append_only(tmp_path, harness):
    submit, _, store, case, _, _ = setup_case(tmp_path, harness)
    submit.resume_submission(store, harness.collector, case["unit"])
    for table in (
        "submission_target",
        "frozen_submissions",
        "submission_attempts",
        "submission_observations",
    ):
        for sql in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
            with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_failed_freeze_rolls_back_target_and_winner_together(tmp_path, harness):
    submit, _, store, case, _, prereqs = setup_case(tmp_path, harness, freeze=False)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_freeze BEFORE INSERT ON frozen_submissions "
            "BEGIN SELECT RAISE(ABORT,'freeze failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        submit.freeze_submission(store, harness.collector, case["unit"], prerequisites=prereqs)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM submission_target").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM frozen_submissions").fetchone()[0] == 0
    assert count(harness, events) == 0


@pytest.mark.parametrize("corrupt", ["attempt", "receipt", "missing_projection"])
def test_corrupt_acknowledged_history_refuses_without_domain_repair(tmp_path, harness, corrupt):
    submit, contracts, store, case, _, _ = setup_case(tmp_path, harness)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    if corrupt == "missing_projection":
        with harness.engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER decisions_v2_no_delete")
            conn.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
            # Keep corrupt fixture local and restore FK-dependent tables in the same transaction.
            conn.exec_driver_sql("DROP TRIGGER decision_consumed_inputs_v2_no_delete")
            conn.exec_driver_sql("DROP TRIGGER decision_context_v2_no_delete")
            conn.exec_driver_sql("DELETE FROM decision_consumed_inputs_v2")
            conn.exec_driver_sql("DELETE FROM decision_context_v2")
            conn.exec_driver_sql("DELETE FROM decisions_v2")
    else:
        with sqlite3.connect(store.path) as conn:
            table = "submission_attempts" if corrupt == "attempt" else "submission_observations"
            conn.execute(f"DROP TRIGGER {table}_no_update")
            conn.execute(
                f"UPDATE {table} SET "
                + ("canonical_hash='wrong'" if corrupt == "attempt" else "result_json='{}'")
            )
    with pytest.raises((contracts.SubmissionError, ReconstructionError)):
        submit.resume_submission(store, harness.collector, case["unit"])
    assert count(harness, events) == 4
    if corrupt == "missing_projection":
        assert count(harness, decisions_v2) == 0


def test_missing_freeze_and_same_file_target_refuse(tmp_path, harness):
    submit, contracts, store, case, _, prereqs = setup_case(tmp_path, harness, freeze=False)
    with pytest.raises(contracts.SubmissionError):
        submit.resume_submission(store, harness.collector, case["unit"])
    same = Collector(make_engine(store.path))
    with pytest.raises(contracts.SubmissionError):
        submit.freeze_submission(store, same, case["unit"], prerequisites=prereqs)
    assert count(harness, events) == 0


def test_frozen_bytes_cannot_be_mutable():
    _, contracts, _ = api()
    with pytest.raises(contracts.SubmissionError):
        contracts.FrozenSubmission(bytearray(b"{}"))


def test_rejected_receipt_cannot_skip_predecessors(tmp_path, harness):
    submit, contracts, store, case, frozen, _ = setup_case(tmp_path, harness)
    attempt = store.attempt(frozen, frozen.document["stages"][-1])
    store.observe(attempt, "rejected", {"reason": "corrupt chain"})
    with pytest.raises(contracts.SubmissionError):
        submit.resume_submission(store, harness.collector, case["unit"])
    assert count(harness, events) == 0


def test_receipt_types_are_part_of_its_frozen_identity(tmp_path, harness):
    submit, contracts, store, case, _, _ = setup_case(tmp_path, harness)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT observation_id,result_json FROM submission_observations LIMIT 1"
        ).fetchone()
        result = json.loads(row[1])
        result["http_status"] = float(result["http_status"])
        conn.execute("DROP TRIGGER submission_observations_no_update")
        conn.execute(
            "UPDATE submission_observations SET result_json=? WHERE observation_id=?",
            (canonical_bytes(result).decode(), row[0]),
        )
    with pytest.raises(contracts.SubmissionError):
        submit.resume_submission(store, harness.collector, case["unit"])
    assert count(harness, events) == 4


def test_unknown_reprocessing_predecessor_is_not_fabricated(tmp_path, harness):
    submit, contracts, store, first, _, _ = setup_case(tmp_path, harness)
    second = make_case(tmp_path, selection="orphan", reprocesses=first["unit"].unit_id)
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(second["account"]),
        canonical_bytes(second["artifact"]),
        second["decision"]["recorded_at"],
        b"{}",
    )
    with pytest.raises(contracts.SubmissionError):
        submit.freeze_submission(store, harness.collector, second["unit"], prerequisites=prereqs)
    assert count(harness, events) == 0


def test_selected_artifact_with_no_actual_consumption_refuses(tmp_path, harness):
    from flight_recorder.adapters.hubspot.capture import capture_once
    from flight_recorder.adapters.hubspot.contracts import ProcessingUnit, ReadResult, RuleIdentity

    submit, contracts, store, case, _, _ = setup_case(tmp_path, harness, freeze=False)
    registration = copy.deepcopy(case["artifact"])
    artifact = registration["payload"]["artifact"]
    artifact["logic_version"] = "zero-consumed-test"
    artifact["factors"] = [f for f in artifact["factors"] if f["key"] == "industry"]
    registration["event_id"] = "zero-consumed-registration"
    old = case["unit"]
    unit = ProcessingUnit(
        old.notification, old.mapping_identity, "zero-selected", old.adapter_identity
    )
    clock = iter(
        datetime.fromisoformat(case["capture"][k])
        for k in ("read_started_at", "read_finished_at", "capture_boundary")
    )
    capture = CaptureStore(store.path)
    employees = case["capture"]["observation"]["properties"]["numberofemployees"]
    capture_once(
        capture,
        unit,
        observe=lambda owner: ReadResult({"numberofemployees": employees}),
        select_logic=lambda: RuleIdentity(canonical_hash(artifact), artifact["evaluator_version"]),
        clock=lambda: next(clock),
    )
    capture.close()
    prereqs = contracts.SubmissionPrerequisites(
        canonical_bytes(case["account"]),
        canonical_bytes(registration),
        case["decision"]["recorded_at"],
        b"{}",
    )
    with pytest.raises(contracts.SubmissionError, match="no_usable_inputs"):
        submit.freeze_submission(store, harness.collector, unit, prerequisites=prereqs)
    assert count(harness, events) == 0
