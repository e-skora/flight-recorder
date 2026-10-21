"""Default-off, one-attempt local fake writer. No live client or implicit retry."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from flight_recorder.adapters.hubspot.contracts import strict_json_loads, typed_equal
from flight_recorder.adapters.hubspot.fake_crm import FakeCRM, FakeRejected
from flight_recorder.adapters.hubspot.submit import _target, _verify_domain
from flight_recorder.adapters.hubspot.writer_contracts import (
    FakeObservation,
    WriteIntent,
    WritePolicy,
    WriteRefused,
    require,
)
from flight_recorder.adapters.hubspot.writer_store import TERMINAL
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.capture_contract import validate_capture
from flight_recorder.collector.operation_contract import (
    event_document,
    intent_digest,
    operation_history,
)
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import capture_contexts, events
from flight_recorder.replay.reconstruct import load_artifact_row, verify_artifact_row


def _time(clock):
    now = clock()
    require(
        isinstance(now, datetime) and now.utcoffset() is not None, "aware_writer_clock_required"
    )
    return format_utc(now.astimezone(UTC))


def _context(store, collector, unit):
    target = Path(_target(collector))
    require(target.exists() and not target.samefile(store.path), "invalid_writer_domain_target")
    store.submission.target(target)
    frozen = store.submission.load(unit)
    require(frozen is not None, "missing_frozen_submission")
    accepted, failure, _ = store.submission.history(frozen)
    require(
        failure is None and accepted == {x["stage"] for x in frozen.document["stages"]},
        "original_submission_not_complete",
    )
    _verify_domain(collector, frozen, accepted)
    decision = frozen.document["stages"][-1]["envelope"]
    cap = decision["payload"]["capture"]
    with collector.engine.connect() as conn:
        artifact = verify_artifact_row(
            load_artifact_row(conn, cap["rule_hash"]), cap["rule_hash"]
        ).artifact
    return {
        "unit": unit,
        "frozen": frozen,
        "accepted": accepted,
        "decision": decision,
        "cap": cap,
        "factor_keys": {f.key for f in artifact.factors},
    }


def _context_for_intent(store, collector, intent):
    d = intent.document
    with store.transaction() as conn:
        actual = store.load(d["operation_id"], conn)
        require(actual.canonical_bytes == intent.canonical_bytes, "write_intent_not_stored_binding")
        row = conn.execute(
            "SELECT body_json FROM captures WHERE unit_id=?", (d["unit_id"],)
        ).fetchone()
        require(row is not None, "missing_frozen_capture")
        cap = strict_json_loads(row[0].encode())
        unit = validate_capture(cap, canonical_hash(cap))
    context = _context(store, collector, unit)
    require(context["decision"]["event_id"] == d["decision_event_id"], "writer_original_mismatch")
    return context


def _history(collector, account_ref):
    with collector.engine.connect() as conn:
        return operation_history(conn, account_ref)


def _freshness(conn, store, collector, context, *, own_intent=None):
    unit, cap = context["unit"], context["cap"]
    current = store.submission._load(conn, unit)
    require(
        current is not None and current.canonical_bytes == context["frozen"].canonical_bytes,
        "writer_submission_drift",
    )
    _verify_domain(collector, current, context["accepted"])
    maximum = conn.execute(
        "SELECT max(capture_sequence) FROM captures WHERE company_key=?", (unit.company_key,)
    ).fetchone()[0]
    require(type(maximum) is int and maximum == cap["capture_sequence"], "stale_capture")
    with collector.engine.connect() as domain:
        maximum = domain.execute(
            select(func.max(capture_contexts.c.capture_sequence)).where(
                capture_contexts.c.account_ref == context["decision"]["account_ref"]
            )
        ).scalar_one()
    require(maximum == cap["capture_sequence"], "stale_domain_capture")
    owner = conn.execute(
        "SELECT * FROM company_ownership WHERE company_key=?", (unit.company_key,)
    ).fetchone()
    if own_intent is None:
        require(owner is None or owner["state"] != "held", "company_capture_or_writer_busy")
    else:
        store.assert_fence(conn, own_intent)
    require(
        all(cap["inputs"][key]["state"] != "absent" for key in context["factor_keys"]),
        "ambiguous_selected_input_coverage",
    )
    original = cap["notification"]
    rows = conn.execute(
        (
            "SELECT s.semantic_json FROM processing_units u JOIN "
            "source_identities s ON u.logical_key=s.logical_key WHERE "
            "u.company_key=?"
        ),
        (unit.company_key,),
    ).fetchall()
    for row in rows:
        n = strict_json_loads(row[0].encode())
        if n["property_name"] == original["property_name"]:
            require(n["occurred_at"] <= original["occurred_at"], "known_newer_trigger")
            require(
                n["occurred_at"] != original["occurred_at"]
                or typed_equal(n["property_value"], original["property_value"]),
                "conflicting_equal_notification_time",
            )
    for row in conn.execute(
        "SELECT * FROM captures WHERE company_key=?", (unit.company_key,)
    ).fetchall():
        prior = strict_json_loads(row["body_json"].encode())
        prior_unit = validate_capture(prior, canonical_hash(prior))
        require(
            prior_unit.company_key == unit.company_key
            and prior["capture_sequence"] == row["capture_sequence"],
            "writer_capture_order_integrity",
        )
        for key, provenance in cap["input_provenance"].items():
            other = prior["input_provenance"].get(key)
            if (
                other
                and provenance["available_at"] is not None
                and provenance["available_at"] == other["available_at"]
                and cap["inputs"][key]["state"] in {"consumed", "ignored"}
                and prior["inputs"][key]["state"] in {"consumed", "ignored"}
            ):
                require(
                    typed_equal(cap["inputs"][key]["value"], prior["inputs"][key]["value"]),
                    "conflicting_equal_source_time",
                )


def flush_observations(store, collector):
    require(
        Path(_target(collector)).resolve() != store.path, "domain_and_operational_store_must_differ"
    )
    store.submission.target(_target(collector))
    results = {}
    for document in store.pending():
        result = collector.ingest_json(canonical_bytes(document))
        require(
            getattr(result, "status", None) in {"created", "duplicate"}
            and type(getattr(result, "http_status", None)) is int
            and result.http_status == (201 if result.status == "created" else 200)
            and getattr(result, "event_id", None) == document["event_id"]
            and getattr(result, "canonical_hash", None) == canonical_hash(document),
            "invalid_operation_collector_ack",
        )
        with collector.engine.connect() as conn:
            row = conn.execute(
                select(events).where(events.c.event_id == document["event_id"])
            ).first()
            require(
                row is not None and typed_equal(event_document(row), document),
                "acknowledged_operation_missing_or_changed",
            )
        store.acknowledge(document, result)
        results[document["event_id"]] = result.status
    return results


def prepare_intent(store, collector, unit, *, clock):
    flush_observations(store, collector)
    context = _context(store, collector, unit)
    decision = context["decision"]
    history = _history(collector, decision["account_ref"])
    bound = [
        n["payload"]["intent"]
        for n in history
        if n["payload"]["intent"]["decision_event_id"] == decision["event_id"]
    ]
    with store.transaction() as conn:
        old = conn.execute(
            "SELECT operation_id FROM writer_intents WHERE decision_event_id=?",
            (decision["event_id"],),
        ).fetchone()
        if old:
            intent = store.load(old[0], conn)
            require(
                bool(bound) and all(typed_equal(x, intent.document) for x in bound),
                "writer_domain_intent_binding_mismatch",
            )
            return intent
        require(not bound, "original_already_bound_to_intent")
        _freshness(conn, store, collector, context)
        at = _time(clock)
        revision = max((n["payload"]["intent"]["revision"] for n in history), default=0) + 1
        p, cap = decision["payload"], context["cap"]
        d = {
            "format_version": 1,
            "company_key": unit.company_key,
            "unit_id": unit.unit_id,
            "capture_hash": p["capture_hash"],
            "capture_sequence": cap["capture_sequence"],
            "decision_event_id": decision["event_id"],
            "artifact_hash": p["logic_artifact"]["artifact_hash"],
            "evaluator_version": p["logic_artifact"]["evaluator_version"],
            "revision": revision,
            "created_at": at,
            "intended": {
                "fr_score": p["result"]["score"],
                "fr_output": p["result"]["output"],
                "fr_logic_version": p["logic_artifact"]["logic_version"],
                "fr_decision_ref": decision["event_id"],
            },
        }
        op = intent_digest(d)
        d["operation_id"] = op
        d["intended"].update(fr_op_id=op, fr_op_rev=revision)
        intent = WriteIntent.from_document(d)
        latest = {n["payload"]["intent"]["operation_id"]: n["payload"]["state"] for n in history}
        for row in conn.execute(
            (
                "SELECT i.operation_id FROM writer_intents i JOIN "
                "writer_progress p ON i.operation_id=p.operation_id WHERE "
                "i.company_key=? AND p.attempted=0 AND p.state IN "
                "('prepared','refused')"
            ),
            (unit.company_key,),
        ).fetchall():
            if latest.get(row[0]) in {"prepared", "refused"}:
                prior = store.load(row[0], conn)
                store.append(conn, prior, "superseded", "newer_unsent_intent", at)
                store.update_progress(conn, row[0], "superseded", "newer_unsent_intent")
        store.insert_intent(conn, intent, decision["account_ref"])
        store.append(conn, intent, "prepared", "intent_prepared", at)
    flush_observations(store, collector)
    return intent


@contextmanager
def _domain_reservation(collector):
    # A reservation, not a domain write. Only Collector writes event rows.
    path = Path(_target(collector)).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise WriteRefused("domain_reservation_unavailable") from exc
        yield
    finally:
        conn.rollback()
        conn.close()


def _classify(intent, proof):
    require(isinstance(proof, FakeObservation), "invalid_fake_observation")
    d = intent.document
    op = d["operation_id"]
    if op in proof.settled_operation_ids and not proof.pending_operation_ids:
        if op in proof.not_applied_operation_ids:
            return "observed_not_applied_at_t", "definite_fake_rejection"
        if typed_equal(proof.properties, d["intended"]):
            return "observed_applied_at_t", "full_tuple_observed"
    return "unknown", "tuple_or_disposition_unresolved"


def _record_observation(conn, store, intent, fake, clock):
    try:
        proof = fake.observe(intent.document["company_key"])
    except Exception:
        proof = None
        state, reason = "unknown", "fake_observation_unknown"
    else:
        state, reason = _classify(intent, proof)
    store.append(conn, intent, state, reason, _time(clock), proof)
    store.update_progress(conn, intent.document["operation_id"], "unknown", reason)


def _terminal_domain_proof(store, collector, intent, conn):
    latest = store.latest(conn, intent.document["operation_id"])
    history = _history(collector, latest["account_ref"])
    own = [
        n
        for n in history
        if n["payload"]["intent"]["operation_id"] == intent.document["operation_id"]
    ]
    require(own and typed_equal(own[-1], latest), "writer_terminal_domain_proof_changed")


def _finalize(store, collector, intent):
    d = intent.document
    with store.transaction() as conn:
        progress = store.progress(d["operation_id"], conn)
        if progress.state in TERMINAL:
            _terminal_domain_proof(store, collector, intent, conn)
            return progress
        if progress.state != "unknown":
            return progress
        latest = store.latest(conn, d["operation_id"])
        if latest["payload"]["state"] not in {"observed_applied_at_t", "observed_not_applied_at_t"}:
            return progress
        receipt = conn.execute(
            "SELECT canonical_hash FROM writer_receipts WHERE event_id=?", (latest["event_id"],)
        ).fetchone()
        require(
            receipt is not None and receipt[0] == canonical_hash(latest),
            "terminal_observation_unacknowledged",
        )
        history = _history(collector, latest["account_ref"])
        own = [n for n in history if n["payload"]["intent"]["operation_id"] == d["operation_id"]]
        require(own and typed_equal(own[-1], latest), "terminal_domain_observation_changed")
        store.reserve(conn, intent, reconcile=True)
        store.release(conn, intent)
        store.update_progress(
            conn, d["operation_id"], latest["payload"]["state"], latest["payload"]["reason"]
        )
        return store.progress(d["operation_id"], conn)


DISABLED_POLICY = WritePolicy()


def send_intent(store, collector, intent, fake, *, policy=DISABLED_POLICY, clock):
    require(
        isinstance(intent, WriteIntent)
        and isinstance(fake, FakeCRM)
        and isinstance(policy, WritePolicy),
        "local_fake_writer_arguments_required",
    )
    d = intent.document
    with store.transaction() as conn:
        require(
            store.load(d["operation_id"], conn).canonical_bytes == intent.canonical_bytes,
            "write_intent_not_stored_binding",
        )
        progress = store.progress(d["operation_id"], conn)
        if progress.state in TERMINAL:
            _terminal_domain_proof(store, collector, intent, conn)
            return progress
        if progress.state == "unknown":
            return progress
    context = _context_for_intent(store, collector, intent)
    if not policy.enabled or d["company_key"] not in policy.allowlist:
        reason = "disabled" if not policy.enabled else "unallowlisted"
        with store.transaction() as conn:
            # The operation may advance while original verification runs outside this lock.
            progress = store.progress(d["operation_id"], conn)
            if progress.state in TERMINAL:
                _terminal_domain_proof(store, collector, intent, conn)
                return progress
            if progress.state == "unknown":
                return progress
            store.append(conn, intent, "refused", reason, _time(clock))
            store.update_progress(conn, d["operation_id"], "refused", reason)
        flush_observations(store, collector)
        return store.progress(d["operation_id"])
    flush_observations(store, collector)
    with store.transaction() as conn:
        # Recheck after potentially concurrent callers, not only before preparation.
        progress = store.progress(d["operation_id"], conn)
        if progress.state in TERMINAL:
            _terminal_domain_proof(store, collector, intent, conn)
            return progress
        if progress.state == "unknown":
            return progress
        store.reserve(conn, intent)
        _freshness(conn, store, collector, context, own_intent=intent)
        marker = store.append(conn, intent, "unknown", "send_started", _time(clock))
        store.update_progress(conn, d["operation_id"], "unknown", "send_started", attempted=1)
    grants = flush_observations(store, collector)
    if grants.get(marker) != "created":
        with store.transaction() as conn:
            store.update_progress(conn, d["operation_id"], "unknown", "send_grant_not_created")
        return store.progress(d["operation_id"])
    with store.transaction() as conn, _domain_reservation(collector):
        store.assert_fence(conn, intent)
        _freshness(conn, store, collector, context, own_intent=intent)
        try:
            fake.send(d["company_key"], d["operation_id"], d["intended"])
        except FakeRejected:
            _record_observation(conn, store, intent, fake, clock)
        except Exception as exc:
            reason = "fake_timeout" if isinstance(exc, TimeoutError) else "fake_exception"
            store.append(conn, intent, "unknown", reason, _time(clock))
            store.update_progress(conn, d["operation_id"], "unknown", reason)
        else:
            _record_observation(conn, store, intent, fake, clock)
    flush_observations(store, collector)
    return _finalize(store, collector, intent)


def reconcile_intent(store, collector, intent, fake, *, clock):
    require(
        isinstance(intent, WriteIntent) and isinstance(fake, FakeCRM),
        "local_fake_writer_arguments_required",
    )
    _context_for_intent(store, collector, intent)
    flush_observations(store, collector)
    progress = _finalize(store, collector, intent)
    if progress.state != "unknown":
        return progress
    with store.transaction() as conn, _domain_reservation(collector):
        store.reserve(conn, intent, reconcile=True)
        store.assert_fence(conn, intent)
        _record_observation(conn, store, intent, fake, clock)
    flush_observations(store, collector)
    return _finalize(store, collector, intent)
