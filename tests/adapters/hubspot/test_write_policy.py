"""S10–S14: local fake calls are single-use, default-off and uncertainty-safe."""

import copy
import importlib
import shutil
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from flight_recorder.adapters.hubspot.store import CaptureStore
from flight_recorder.adapters.hubspot.submission_store import SubmissionStore
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.service import Collector
from tests.adapters.hubspot.test_operation_admission import accepted_case


def api():
    for module in ("writer", "writer_store", "writer_contracts", "fake_crm"):
        assert importlib.util.find_spec("flight_recorder.adapters.hubspot." + module) is not None, (
            "Task4 writer interface missing: " + module
        )
    return tuple(
        importlib.import_module("flight_recorder.adapters.hubspot." + name)
        for name in ("writer", "writer_store", "writer_contracts", "fake_crm")
    )


class Clock:
    def __init__(self, context, seconds=10):
        self.at = datetime.fromisoformat(context["decision"]["recorded_at"]) + timedelta(
            seconds=seconds
        )

    def __call__(self):
        result = self.at
        self.at += timedelta(seconds=1)
        return result


def writer_case(tmp_path, harness):
    writer, stores, contracts, fakes = api()
    context = accepted_case(tmp_path, harness)
    store = stores.WriterStore(context["store"])
    fake = fakes.FakeCRM()
    clock = Clock(context)
    intent = writer.prepare_intent(store, harness.collector, context["case"]["unit"], clock=clock)
    policy = contracts.WritePolicy(
        enabled=True, allowlist=frozenset({context["case"]["unit"].company_key})
    )
    return SimpleNamespace(
        writer=writer,
        stores=stores,
        contracts=contracts,
        fakes=fakes,
        context=context,
        store=store,
        fake=fake,
        clock=clock,
        intent=intent,
        policy=policy,
    )


def send(harness, c, **kwargs):
    return c.writer.send_intent(
        c.store,
        harness.collector,
        c.intent,
        c.fake,
        clock=c.clock,
        **({"policy": c.policy} | kwargs),
    )


def restart(harness, c):
    path = c.store.path
    c.store.close()
    capture = CaptureStore(path)
    c.store = c.stores.WriterStore(SubmissionStore(capture))
    capture.close()
    harness.collector = Collector(harness.engine)


def test_default_disabled_and_unallowlisted_call_neither_send_nor_read(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    result = c.writer.send_intent(c.store, harness.collector, c.intent, c.fake, clock=c.clock)
    assert result.state == "refused" and result.reason == "disabled"
    result = send(harness, c, policy=c.contracts.WritePolicy(enabled=True))
    assert result.state == "refused" and result.reason == "unallowlisted"
    assert c.fake.send_count == c.fake.read_count == 0
    assert send(harness, c).state == "observed_applied_at_t"
    assert c.fake.send_count == 1


def test_prepare_is_immutable_and_intent_is_durable_before_send(tmp_path, harness, monkeypatch):
    c = writer_case(tmp_path, harness)
    original = c.intent.canonical_bytes
    again = c.writer.prepare_intent(
        c.store, harness.collector, c.context["case"]["unit"], clock=Clock(c.context, 100)
    )
    assert again.canonical_bytes == original
    doc = again.document
    doc["intended"]["fr_score"] = -999
    assert again.canonical_bytes == original
    real = c.fake.send

    def inspect(company, operation_id, properties):
        with sqlite3.connect(c.store.path) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM writer_intents WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT state FROM writer_progress WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
                == "unknown"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM writer_observations WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
                >= 2
            )
        return real(company, operation_id, properties)

    monkeypatch.setattr(c.fake, "send", inspect)
    assert send(harness, c).state == "observed_applied_at_t"
    assert send(harness, c).state == "observed_applied_at_t"
    assert c.fake.send_count == 1


@pytest.mark.parametrize(
    "mode,terminal",
    [
        ("apply", "observed_applied_at_t"),
        ("reject", "observed_not_applied_at_t"),
        ("timeout", "unknown"),
        ("raise", "unknown"),
        ("lost_response", "unknown"),
        ("lost_rejection", "unknown"),
    ],
)
def test_send_disposition_restart_and_explicit_reconciliation(tmp_path, harness, mode, terminal):
    c = writer_case(tmp_path, harness)
    c.fake.mode = mode
    result = send(harness, c)
    assert result.state == terminal
    assert c.fake.send_count == 1
    restart(harness, c)
    assert send(harness, c).state == terminal
    assert c.fake.send_count == 1
    if terminal == "unknown":
        result = c.writer.reconcile_intent(
            c.store, harness.collector, c.intent, c.fake, clock=c.clock
        )
        if mode in {"timeout", "raise"}:
            assert result.state == "unknown"
            c.fake.apply_pending(c.intent.document["operation_id"])
            result = c.writer.reconcile_intent(
                c.store, harness.collector, c.intent, c.fake, clock=c.clock
            )
        assert result.state == (
            "observed_not_applied_at_t" if mode == "lost_rejection" else "observed_applied_at_t"
        )
    assert c.fake.send_count == 1
    with sqlite3.connect(c.store.path) as conn:
        assert (
            conn.execute(
                "SELECT outstanding_operation_id FROM writer_targets WHERE company_key=?",
                (c.intent.document["company_key"],),
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("fault", ["empty", "score_only", "partial", "foreign"])
def test_empty_numeric_partial_and_foreign_reads_never_resolve_unknown(tmp_path, harness, fault):
    c = writer_case(tmp_path, harness)
    c.fake.mode = "lost_response"
    assert send(harness, c).state == "unknown"
    properties = {
        "empty": {},
        "score_only": {"fr_score": c.intent.document["intended"]["fr_score"]},
        "partial": {"fr_op_id": "f" * 64},
        "foreign": c.intent.document["intended"],
    }[fault]
    if fault == "foreign":
        properties["fr_op_id"] = "f" * 64
    c.fake.set_properties(c.intent.document["company_key"], properties)
    for _ in range(2):
        assert (
            c.writer.reconcile_intent(
                c.store, harness.collector, c.intent, c.fake, clock=c.clock
            ).state
            == "unknown"
        )
    assert c.fake.send_count == 1


def test_matching_tuple_with_an_earlier_pending_request_stays_blocked(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    d = c.intent.document
    earlier = canonical_hash({"synthetic": "earlier uncertain request"})
    c.fake.queue_pending(d["company_key"], earlier, {"fr_score": -10})
    assert send(harness, c).state == "unknown"
    assert c.fake.observe(d["company_key"]).properties == d["intended"]
    assert (
        c.writer.reconcile_intent(c.store, harness.collector, c.intent, c.fake, clock=c.clock).state
        == "unknown"
    )
    c.fake.reject_pending(earlier)
    assert (
        c.writer.reconcile_intent(c.store, harness.collector, c.intent, c.fake, clock=c.clock).state
        == "observed_applied_at_t"
    )
    assert c.fake.send_count == 1


def test_pre_intent_copy_cannot_remint_completed_original(tmp_path, harness):
    writer, stores, contracts, fakes = api()
    context = accepted_case(tmp_path, harness)
    clone = tmp_path / "pre-intent.sqlite"
    shutil.copy2(context["store"].path, clone)
    store = stores.WriterStore(context["store"])
    clock = Clock(context)
    intent = writer.prepare_intent(store, harness.collector, context["case"]["unit"], clock=clock)
    fake = fakes.FakeCRM()
    policy = contracts.WritePolicy(
        enabled=True, allowlist=frozenset({context["case"]["unit"].company_key})
    )
    assert (
        writer.send_intent(store, harness.collector, intent, fake, policy=policy, clock=clock).state
        == "observed_applied_at_t"
    )
    other = stores.WriterStore(SubmissionStore(CaptureStore(clone)))
    with pytest.raises(contracts.WriteRefused, match="original_already_bound"):
        writer.prepare_intent(
            other, harness.collector, context["case"]["unit"], clock=Clock(context, 1000)
        )
    assert fake.send_count == 1


@pytest.mark.parametrize("shift", [11, 1000])
def test_pre_marker_copy_gets_no_second_send_grant(tmp_path, harness, shift):
    c = writer_case(tmp_path, harness)
    clone = tmp_path / "prepared-copy.sqlite"
    shutil.copy2(c.store.path, clone)
    c.fake.mode = "timeout"
    assert send(harness, c).state == "unknown"
    other = c.stores.WriterStore(SubmissionStore(CaptureStore(clone)))
    try:
        result = c.writer.send_intent(
            other,
            harness.collector,
            c.intent,
            c.fake,
            policy=c.policy,
            clock=Clock(c.context, shift),
        )
    except CollectorError as error:
        # A changed clock conflicts with frozen marker bytes; no repair/remint.
        assert shift == 1000 and error.status_code == 409
    else:
        assert result.state == "unknown"
    assert other.progress(c.intent.document["operation_id"]).state == "unknown"
    assert c.fake.send_count == 1


@pytest.mark.parametrize("gap", ["lost_marker_ack", "bad_marker_ack", "lost_terminal_ack"])
def test_collector_ack_gaps_hold_target_without_repeated_send(tmp_path, harness, monkeypatch, gap):
    c = writer_case(tmp_path, harness)
    real = harness.collector.ingest_json
    fired = False

    def ingest(body):
        nonlocal fired
        document = __import__("json").loads(body)
        p = document["payload"]
        result = real(body)
        selected = (
            p["reason"] == "send_started"
            if gap != "lost_terminal_ack"
            else p["state"] == "observed_applied_at_t"
        )
        if selected and not fired:
            fired = True
            if gap == "bad_marker_ack":
                return SimpleNamespace(
                    status="created",
                    http_status=201,
                    event_id="wrong",
                    canonical_hash=result.canonical_hash,
                )
            raise RuntimeError("lost collector acknowledgment")
        return result

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)
    with pytest.raises((RuntimeError, c.contracts.WriteRefused)):
        send(harness, c)
    assert c.store.progress(c.intent.document["operation_id"]).state == "unknown"
    expected = 1 if gap == "lost_terminal_ack" else 0
    assert c.fake.send_count == expected
    restart(harness, c)
    assert send(harness, c).state == "unknown"
    c.writer.flush_observations(c.store, harness.collector)
    assert c.fake.send_count == expected
    if gap == "lost_terminal_ack":
        assert (
            c.writer.reconcile_intent(
                c.store, harness.collector, c.intent, c.fake, clock=c.clock
            ).state
            == "observed_applied_at_t"
        )
    assert c.fake.send_count == expected


def test_tampered_intent_and_closed_or_unrelated_store_refuse(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    document = c.intent.document
    document["intended"]["fr_score"] += 1
    with pytest.raises((ValueError, c.contracts.WriteRefused)):
        changed = c.contracts.WriteIntent.from_document(document)
        c.writer.send_intent(
            c.store, harness.collector, changed, c.fake, policy=c.policy, clock=c.clock
        )
    c.store.close()
    with pytest.raises((ValueError, c.contracts.WriteRefused)):
        send(harness, c)
    assert c.fake.send_count == 0


def later_original(tmp_path, harness, c, *, properties=None, available_at=None):
    boundary = datetime.fromisoformat(c.context["case"]["capture"]["capture_boundary"])
    return accepted_case(
        tmp_path,
        harness,
        selection="later",
        reprocesses=c.context["case"]["unit"].unit_id,
        properties=properties,
        available_at=available_at,
        clock_times=[boundary + timedelta(seconds=n) for n in (10, 11, 12)],
    )


def test_newer_unsent_intent_supersedes_only_the_unsent_original(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    later = later_original(tmp_path, harness, c)
    with pytest.raises(c.contracts.WriteRefused, match="stale_capture"):
        send(harness, c)
    assert c.fake.send_count == c.fake.read_count == 0
    new = c.writer.prepare_intent(c.store, harness.collector, later["case"]["unit"], clock=c.clock)
    assert new.document["revision"] == c.intent.document["revision"] + 1
    assert c.store.progress(c.intent.document["operation_id"]).state == "superseded"
    assert (
        c.writer.send_intent(
            c.store, harness.collector, new, c.fake, policy=c.policy, clock=c.clock
        ).state
        == "observed_applied_at_t"
    )
    assert send(harness, c).state == "superseded" and c.fake.send_count == 1


@pytest.mark.parametrize("mode", ["reject", "lost_rejection", "timeout"])
def test_only_a_distinct_original_can_proceed_after_settlement(tmp_path, harness, mode):
    from flight_recorder.adapters.hubspot.contracts import CaptureRefused

    c = writer_case(tmp_path, harness)
    c.fake.mode = mode
    result = send(harness, c)
    restart(harness, c)
    if result.state == "unknown":
        with pytest.raises(CaptureRefused, match="busy"):
            later_original(tmp_path, harness, c)
        if mode == "timeout":
            c.fake.apply_pending(c.intent.document["operation_id"])
        result = c.writer.reconcile_intent(
            c.store, harness.collector, c.intent, c.fake, clock=c.clock
        )
    assert result.state == (
        "observed_applied_at_t" if mode == "timeout" else "observed_not_applied_at_t"
    )
    later = later_original(tmp_path, harness, c)
    new = c.writer.prepare_intent(c.store, harness.collector, later["case"]["unit"], clock=c.clock)
    c.fake.mode = "apply"
    assert (
        c.writer.send_intent(
            c.store, harness.collector, new, c.fake, policy=c.policy, clock=c.clock
        ).state
        == "observed_applied_at_t"
    )
    assert send(harness, c).state == result.state
    assert c.fake.send_count == 2


@pytest.mark.parametrize("fault", ["newer", "equal_conflict"])
def test_known_pending_notification_blocks_unsent_original(tmp_path, harness, fault):
    import json

    from flight_recorder.adapters.hubspot.contracts import ProcessingUnit, SourceNotification

    c = writer_case(tmp_path, harness)
    capture = CaptureStore(c.store.path)
    raw = json.loads(capture.load_raw(c.context["case"]["capture"]["raw_notification_ref"]))
    if fault == "newer":
        raw["occurredAt"] += 1
    else:
        raw["propertyValue"] = "different synthetic notification value"
    notification = SourceNotification.from_payload(raw, raw_body=json.dumps(raw).encode())
    original = c.context["case"]["unit"]
    other = ProcessingUnit(
        notification,
        original.mapping_identity,
        original.selection_identity,
        original.adapter_identity,
    )
    capture.record_receipt(other)
    capture.close()
    with pytest.raises(
        c.contracts.WriteRefused, match="known_newer_trigger|conflicting_equal_notification_time"
    ):
        send(harness, c)
    assert c.fake.send_count == c.fake.read_count == 0


def test_explicit_abandonment_fences_the_older_sender_but_does_not_clear_unknown(
    tmp_path, harness, monkeypatch
):
    from flight_recorder.adapters.hubspot.contracts import CaptureOwnership

    c = writer_case(tmp_path, harness)
    real = harness.collector.ingest_json

    def ingest(body):
        result = real(body)
        if __import__("json").loads(body)["payload"]["reason"] == "send_started":
            with sqlite3.connect(c.store.path) as conn:
                row = conn.execute(
                    "SELECT company_key,owner_unit_id,generation FROM "
                    "company_ownership WHERE state='held'"
                ).fetchone()
            capture = CaptureStore(c.store.path)
            capture.abandon_capture(CaptureOwnership(*row))
            capture.close()
        return result

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)
    with pytest.raises(c.contracts.WriteRefused, match="stale_writer_fence"):
        send(harness, c)
    assert c.fake.send_count == 0
    restart(harness, c)
    assert (
        c.writer.reconcile_intent(c.store, harness.collector, c.intent, c.fake, clock=c.clock).state
        == "unknown"
    )
    assert c.fake.send_count == 0


def test_crash_after_fake_effect_preserves_unknown_until_observation(
    tmp_path, harness, monkeypatch
):
    class Crash(BaseException):
        pass

    c = writer_case(tmp_path, harness)
    real = c.store.append

    def append(conn, intent, state, *args, **kwargs):
        if state == "observed_applied_at_t":
            raise Crash("after fake before observation commit")
        return real(conn, intent, state, *args, **kwargs)

    monkeypatch.setattr(c.store, "append", append)
    with pytest.raises(Crash):
        send(harness, c)
    assert c.fake.send_count == 1
    restart(harness, c)
    assert c.store.progress(c.intent.document["operation_id"]).state == "unknown"
    assert (
        c.writer.reconcile_intent(c.store, harness.collector, c.intent, c.fake, clock=c.clock).state
        == "observed_applied_at_t"
    )
    assert c.fake.send_count == 1


def test_mutable_progress_cannot_fabricate_terminal_proof(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    with sqlite3.connect(c.store.path) as conn:
        conn.execute(
            (
                "UPDATE writer_progress SET "
                "state='observed_applied_at_t',attempted=1 WHERE operation_id=?"
            ),
            (c.intent.document["operation_id"],),
        )
    with pytest.raises(c.contracts.WriteRefused, match="writer_.*integrity|proof"):
        send(harness, c)
    assert c.fake.send_count == 0


def test_real_writer_concurrent_pre_intent_copies_have_one_binding(tmp_path, harness, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    writer, stores, contracts, fakes = api()
    context = accepted_case(tmp_path, harness)
    clone = tmp_path / "concurrent-before-intent.sqlite"
    shutil.copy2(context["store"].path, clone)
    candidates = [
        stores.WriterStore(context["store"]),
        stores.WriterStore(SubmissionStore(CaptureStore(clone))),
    ]
    barrier = Barrier(2)
    real = harness.collector.ingest_json

    def ingest(body):
        if __import__("json").loads(body)["payload"]["state"] == "prepared":
            barrier.wait(timeout=10)
        return real(body)

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)

    def prepare(index):
        try:
            return writer.prepare_intent(
                candidates[index],
                harness.collector,
                context["case"]["unit"],
                clock=Clock(context, 10 + index),
            )
        except CollectorError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(prepare, [0, 1]))
    assert sum(isinstance(x, contracts.WriteIntent) for x in answers) == 1
    assert answers.count(409) == 1
    monkeypatch.setattr(harness.collector, "ingest_json", real)
    winner = next(
        i for i, answer in enumerate(answers) if isinstance(answer, contracts.WriteIntent)
    )
    fake = fakes.FakeCRM()
    policy = contracts.WritePolicy(
        enabled=True, allowlist=frozenset({context["case"]["unit"].company_key})
    )
    assert (
        writer.send_intent(
            candidates[winner],
            harness.collector,
            answers[winner],
            fake,
            policy=policy,
            clock=Clock(context, 100),
        ).state
        == "observed_applied_at_t"
    )
    assert fake.send_count == 1


def test_two_writers_share_the_same_reservation_and_one_attempt(tmp_path, harness):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c = writer_case(tmp_path, harness)
    other = c.stores.WriterStore(SubmissionStore(CaptureStore(c.store.path)))
    barrier = Barrier(2)

    def attempt(store):
        barrier.wait(timeout=10)
        try:
            return c.writer.send_intent(
                store, harness.collector, c.intent, c.fake, policy=c.policy, clock=c.clock
            ).state
        except (c.contracts.WriteRefused, CollectorError):
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [c.store, other]))
    assert c.fake.send_count <= 1
    assert all(x in {"unknown", "observed_applied_at_t", "refused"} for x in outcomes)
    with sqlite3.connect(c.store.path) as conn:
        assert (
            sum(
                __import__("json").loads(row[0])["payload"]["reason"] == "send_started"
                for row in conn.execute("SELECT body_bytes FROM writer_observations")
            )
            == 1
        )


def test_selected_absence_refuses_before_any_intent(tmp_path, harness):
    from tests.conftest import canonical_envelopes

    writer, stores, contracts, _ = api()
    employee = next(
        item["value"]
        for env in canonical_envelopes()
        if env["event_type"] == "evidence.recorded"
        for item in env["payload"]["items"]
        if item["evidence_type"] == "employee_count"
    )
    context = accepted_case(tmp_path, harness, properties={"numberofemployees": employee})
    store = stores.WriterStore(context["store"])
    with pytest.raises(contracts.WriteRefused, match="ambiguous_selected_input_coverage"):
        writer.prepare_intent(
            store, harness.collector, context["case"]["unit"], clock=Clock(context)
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM writer_intents").fetchone()[0] == 0


def test_equal_non_null_source_times_with_conflicting_values_refuse(tmp_path, harness):
    from tests.conftest import canonical_by_type

    writer, stores, contracts, _ = api()
    boundary = datetime.fromisoformat(
        canonical_by_type("decision.recorded")["payload"]["decision_boundary"]
    )
    available = {"numberofemployees": boundary - timedelta(seconds=1)}
    context = accepted_case(tmp_path, harness, available_at=available)
    store = stores.WriterStore(context["store"])
    cap = context["case"]["capture"]
    properties = {
        p["property"]: copy.deepcopy(cap["inputs"][key]["value"])
        for key, p in cap["input_provenance"].items()
        if cap["inputs"][key]["state"] in {"consumed", "ignored"}
    }
    properties["industry"] = context["case"]["unit"].notification.semantic_body()["property_value"]
    properties["numberofemployees"] += 1
    c = SimpleNamespace(context=context)
    later = later_original(tmp_path, harness, c, properties=properties, available_at=available)
    with pytest.raises(contracts.WriteRefused, match="conflicting_equal_source_time"):
        writer.prepare_intent(
            store, harness.collector, later["case"]["unit"], clock=Clock(context, 100)
        )


def test_prepared_ack_loss_recovers_only_identical_frozen_intent(tmp_path, harness, monkeypatch):
    writer, stores, contracts, fakes = api()
    context = accepted_case(tmp_path, harness)
    store = stores.WriterStore(context["store"])
    real = harness.collector.ingest_json

    def lost(body):
        real(body)
        raise RuntimeError("prepared ACK lost")

    monkeypatch.setattr(harness.collector, "ingest_json", lost)
    with pytest.raises(RuntimeError, match="ACK lost"):
        writer.prepare_intent(
            store, harness.collector, context["case"]["unit"], clock=Clock(context)
        )
    with sqlite3.connect(store.path) as conn:
        original = conn.execute("SELECT body_bytes FROM writer_intents").fetchone()[0]
    monkeypatch.setattr(harness.collector, "ingest_json", real)
    recovered = writer.prepare_intent(
        store, harness.collector, context["case"]["unit"], clock=Clock(context, 500)
    )
    assert recovered.canonical_bytes == original
    fake = fakes.FakeCRM()
    policy = contracts.WritePolicy(
        enabled=True, allowlist=frozenset({context["case"]["unit"].company_key})
    )
    assert (
        writer.send_intent(
            store, harness.collector, recovered, fake, policy=policy, clock=Clock(context, 1000)
        ).state
        == "observed_applied_at_t"
    )
    assert fake.send_count == 1


@pytest.mark.parametrize("gap", ["before_collector_call", "before_receipt", "before_release"])
def test_durable_marker_or_terminal_receipt_gap_never_repeats_send(
    tmp_path, harness, monkeypatch, gap
):
    c = writer_case(tmp_path, harness)
    if gap == "before_collector_call":
        real = harness.collector.ingest_json

        def ingest(body):
            if __import__("json").loads(body)["payload"]["reason"] == "send_started":
                raise RuntimeError("before collector")
            return real(body)

        monkeypatch.setattr(harness.collector, "ingest_json", ingest)
    elif gap == "before_receipt":
        real = c.store.acknowledge

        def acknowledge(document, result):
            if document["payload"]["reason"] == "send_started":
                raise RuntimeError("before receipt")
            return real(document, result)

        monkeypatch.setattr(c.store, "acknowledge", acknowledge)
    else:

        def release(*args):
            raise RuntimeError("before release")

        monkeypatch.setattr(c.store, "release", release)
    with pytest.raises(RuntimeError):
        send(harness, c)
    assert c.store.progress(c.intent.document["operation_id"]).state == "unknown"
    expected = 1 if gap == "before_release" else 0
    assert c.fake.send_count == expected
    restart(harness, c)
    c.writer.flush_observations(c.store, harness.collector)
    if gap == "before_release":
        assert (
            c.writer.reconcile_intent(
                c.store, harness.collector, c.intent, c.fake, clock=c.clock
            ).state
            == "observed_applied_at_t"
        )
    else:
        assert send(harness, c).state == "unknown"
    assert c.fake.send_count == expected


def test_new_domain_capture_between_marker_and_final_check_refuses_before_fake(
    tmp_path, harness, monkeypatch
):
    c = writer_case(tmp_path, harness)
    directory = tmp_path / "independent-capture"
    directory.mkdir()
    shutil.copy2(c.store.path, directory / "capture.sqlite")
    real = harness.collector.ingest_json

    def ingest(body):
        result = real(body)
        document = __import__("json").loads(body)
        if (
            document["event_type"] == "external_operation.observed"
            and document["payload"]["reason"] == "send_started"
        ):
            later_original(directory, harness, c)
        return result

    monkeypatch.setattr(harness.collector, "ingest_json", ingest)
    with pytest.raises(c.contracts.WriteRefused, match="stale_domain_capture"):
        send(harness, c)
    assert c.fake.send_count == 0
    assert c.store.progress(c.intent.document["operation_id"]).state == "unknown"


def test_immutable_writer_tables_reject_update_and_delete(tmp_path, harness):
    c = writer_case(tmp_path, harness)
    for table in ("writer_intents", "writer_observations", "writer_receipts"):
        for statement in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
            with sqlite3.connect(c.store.path) as conn:
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(statement)
    assert c.fake.send_count == 0


@pytest.mark.parametrize("policy_name", ["disabled", "unallowlisted"])
@pytest.mark.parametrize("initial_state", ["prepared", "refused"])
@pytest.mark.parametrize(
    "advanced_state",
    ["unknown", "observed_applied_at_t", "observed_not_applied_at_t", "superseded"],
)
def test_refusal_does_not_rewind_concurrent_progress(
    tmp_path, harness, monkeypatch, policy_name, initial_state, advanced_state
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c = writer_case(tmp_path, harness)
    other = c.stores.WriterStore(SubmissionStore(CaptureStore(c.store.path)))
    refusal_fake = c.fakes.FakeCRM()
    policy = c.contracts.WritePolicy(enabled=policy_name == "unallowlisted")
    if initial_state == "refused":
        assert (
            c.writer.send_intent(
                c.store, harness.collector, c.intent, refusal_fake, policy=policy, clock=c.clock
            ).state
            == "refused"
        )
    verified, advanced = Barrier(2), Barrier(2)
    real_context = c.writer._context_for_intent

    def pause_after_verification(store, collector, intent):
        context = real_context(store, collector, intent)
        if store is c.store:
            verified.wait(timeout=10)
            advanced.wait(timeout=10)
        return context

    monkeypatch.setattr(c.writer, "_context_for_intent", pause_after_verification)
    advancing_clock = Clock(c.context, 100)

    def refuse():
        return c.writer.send_intent(
            c.store,
            harness.collector,
            c.intent,
            refusal_fake,
            policy=policy,
            clock=Clock(c.context, 1000),
        )

    def local_history():
        with sqlite3.connect(c.store.path) as conn:
            return conn.execute(
                "SELECT body_bytes FROM writer_observations ORDER BY observation_id"
            ).fetchall()

    def durable_state():
        with sqlite3.connect(c.store.path) as conn:
            progress = conn.execute(
                "SELECT * FROM writer_progress ORDER BY operation_id"
            ).fetchall()
            targets = conn.execute("SELECT * FROM writer_targets ORDER BY company_key").fetchall()
            ownership = conn.execute(
                "SELECT * FROM company_ownership ORDER BY company_key"
            ).fetchall()
            return progress, targets, ownership

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(refuse)
        try:
            verified.wait(timeout=10)
            if advanced_state == "superseded":
                later = later_original(tmp_path, harness, c)
                replacement = c.writer.prepare_intent(
                    other, harness.collector, later["case"]["unit"], clock=advancing_clock
                )
            else:
                c.fake.mode = {
                    "unknown": "timeout",
                    "observed_applied_at_t": "apply",
                    "observed_not_applied_at_t": "reject",
                }[advanced_state]
                assert (
                    c.writer.send_intent(
                        other,
                        harness.collector,
                        c.intent,
                        c.fake,
                        policy=c.policy,
                        clock=advancing_clock,
                    ).state
                    == advanced_state
                )
            assert other.progress(c.intent.document["operation_id"]).state == advanced_state
            before_history, before_state = local_history(), durable_state()
            before_domain = c.writer._history(
                harness.collector, c.context["decision"]["account_ref"]
            )
            advanced.wait(timeout=10)
            assert pending.result(timeout=10).state == advanced_state
        finally:
            # Release either waiter on assertion failure without an unbounded executor join.
            verified.abort()
            advanced.abort()
    monkeypatch.setattr(c.writer, "_context_for_intent", real_context)
    assert refusal_fake.send_count == refusal_fake.read_count == 0
    assert local_history() == before_history
    assert durable_state() == before_state
    assert c.writer.flush_observations(c.store, harness.collector) == {}
    assert c.store.pending() == other.pending() == []
    assert (
        c.writer._history(harness.collector, c.context["decision"]["account_ref"]) == before_domain
    )
    operation_id = c.intent.document["operation_id"]
    with sqlite3.connect(c.store.path) as conn:
        assert conn.execute(
            "SELECT attempted FROM writer_progress WHERE operation_id=?", (operation_id,)
        ).fetchone()[0] == (0 if advanced_state == "superseded" else 1)
        owner = conn.execute(
            "SELECT state FROM company_ownership WHERE company_key=?",
            (c.intent.document["company_key"],),
        ).fetchone()[0]
        target = conn.execute(
            "SELECT outstanding_operation_id FROM writer_targets WHERE company_key=?",
            (c.intent.document["company_key"],),
        ).fetchone()[0]
    assert owner == ("held" if advanced_state == "unknown" else "free")
    assert target == (operation_id if advanced_state == "unknown" else None)
    assert (
        c.writer.reconcile_intent(
            other, harness.collector, c.intent, c.fake, clock=advancing_clock
        ).state
        == advanced_state
    )
    if advanced_state == "unknown":
        c.fake.apply_pending(operation_id)
        assert (
            c.writer.reconcile_intent(
                other, harness.collector, c.intent, c.fake, clock=advancing_clock
            ).state
            == "observed_applied_at_t"
        )
        with sqlite3.connect(c.store.path) as conn:
            assert (
                conn.execute(
                    "SELECT state FROM company_ownership WHERE company_key=?",
                    (c.intent.document["company_key"],),
                ).fetchone()[0]
                == "free"
            )
            assert (
                conn.execute(
                    "SELECT outstanding_operation_id FROM writer_targets WHERE company_key=?",
                    (c.intent.document["company_key"],),
                ).fetchone()[0]
                is None
            )
    elif advanced_state == "superseded":
        assert (
            c.writer.send_intent(
                other,
                harness.collector,
                replacement,
                c.fake,
                policy=c.policy,
                clock=advancing_clock,
            ).state
            == "observed_applied_at_t"
        )
    assert c.fake.send_count == 1
    assert refusal_fake.send_count == refusal_fake.read_count == 0
    assert c.store.pending() == other.pending() == []
    other.close()
