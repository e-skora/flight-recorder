"""Verified historical v3 projections; no operational store or current provider access."""

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.crm.contracts import CRMTarget
from flight_recorder.collector.canonical import canonical_text
from flight_recorder.collector.crm_provenance import (
    _context,
    _evidence_row,
    _original_capture,
    _verified_event,
)
from flight_recorder.ledger.crm_schema import (
    crm_decision_consumed_inputs,
    crm_decision_context,
    crm_decisions,
)
from flight_recorder.ledger.schema import events
from flight_recorder.logic.evaluator import ContextInput
from flight_recorder.replay.reconstruct import IntegrityFailure


def crm_event(conn, decision_event_id):
    # v3 wire identity requires this exact namespace. This only narrows the
    # metadata lookup; the event's actual version still decides the reader.
    # Ordinary legacy/v2 IDs retain their no-events replay path.
    if type(decision_event_id) is not str or not re.fullmatch(
        r"fr-crm-decision-v1-[0-9a-f]{64}", decision_event_id
    ):
        return None
    return conn.execute(
        select(events).where(
            events.c.event_id == decision_event_id,
            events.c.schema_version == "3",
            events.c.event_type == "decision.recorded",
        )
    ).first()


def crm_decision(conn, decision_event_id):
    # Event metadata is the dispatch boundary, including known-but-damaged projections.
    if crm_event(conn, decision_event_id) is None:
        return None
    try:
        row = conn.execute(
            select(crm_decisions).where(crm_decisions.c.decision_event_id == decision_event_id)
        ).first()
        if row is None:
            raise ValueError("known v3 decision projection is missing")
        return row
    except (ValueError, SQLAlchemyError) as error:
        raise IntegrityFailure(
            "crm_provenance", "known v3 decision projection unavailable"
        ) from error


def _projection(conn, table, decision_id, consumed=False):
    rows = conn.execute(
        select(table).where(table.c.decision_event_id == decision_id).order_by(table.c.input_key)
    ).all()
    result = []
    for row in rows:
        entry = dict(
            input_key=row.input_key,
            value=json.loads(row.value_text) if row.value_text is not None else None,
            evidence_version_id=row.evidence_version_id,
        )
        entry["contribution" if consumed else "availability"] = (
            row.contribution if consumed else row.availability
        )
        result.append(entry)
    return result


def _lineage(conn, row, cap):
    if cap["reprocesses"] is None:
        return None
    prior = conn.execute(
        select(crm_decisions).where(crm_decisions.c.unit_id == cap["reprocesses"])
    ).first()
    if prior is None:
        raise ValueError("missing recorded predecessor")
    old = _original_capture(conn, prior.capture_hash)
    event = _verified_event(conn, prior.decision_event_id)
    if (
        event["schema_version"] != "3"
        or event["event_type"] != "decision.recorded"
        or event["account_ref"] != row.account_ref
        or event["payload"]["capture_hash"] != prior.capture_hash
        or canonical_text(event["payload"]["capture"]) != canonical_text(old)
        or prior.account_ref != row.account_ref
        or prior.unit_id != old["unit_id"]
        or prior.logical_key != old["logical_key"]
        or prior.processing_version != old["processing_version"]
        or old["target"] != cap["target"]
        or old["observation_id"] != cap["observation_id"]
        or old["logical_key"] != cap["logical_key"]
        or old["processing_version"] == cap["processing_version"]
        or old["capture_sequence"] >= cap["capture_sequence"]
        or old["capture_boundary"] > cap["capture_boundary"]
    ):
        raise ValueError("invalid recorded predecessor lineage")
    return prior


def _history_step(conn, decision_event_id):
    """Verify one complete preserved record and its immediate predecessor identity."""
    try:
        row = crm_decision(conn, decision_event_id)
        if row is None:
            raise ValueError("not a v3 decision")
        event = crm_event(conn, decision_event_id)
        minted = _verified_event(conn, decision_event_id)
        p = minted["payload"]
        cap = _original_capture(conn, row.capture_hash)
        ref = p["logic_artifact"]
        expected = dict(
            decision_event_id=decision_event_id,
            account_ref=minted["account_ref"],
            decision_class=p["decision_class"],
            decision_boundary=p["decision_boundary"],
            workflow_version=p["workflow_version"],
            artifact_hash=ref["artifact_hash"],
            evaluator_version=ref["evaluator_version"],
            logic_version=ref["logic_version"],
            **p["result"],
            explanation=p["explanation"],
            ingest_sequence=event.ingest_sequence,
            unit_id=cap["unit_id"],
            logical_key=cap["logical_key"],
            processing_version=cap["processing_version"],
            capture_hash=p["capture_hash"],
            reprocesses=cap["reprocesses"],
        )
        if any(getattr(row, key) != value for key, value in expected.items()):
            raise ValueError("decision projection differs from canonical minting event")
        if (
            canonical_text(p["capture"]) != canonical_text(cap)
            or CRMTarget(**cap["target"]).account_ref != row.account_ref
            or row.decision_boundary != cap["capture_boundary"]
            or row.workflow_version != cap["processing_version"]
            or row.artifact_hash != cap["rule_hash"]
            or row.evaluator_version != cap["evaluator_version"]
        ):
            raise ValueError("decision differs from preserved capture binding")
        context = _projection(conn, crm_decision_context, decision_event_id)
        consumed = _projection(conn, crm_decision_consumed_inputs, decision_event_id, True)
        for actual, preserved in (
            (context, p["historical_context"]),
            (consumed, p["consumed_inputs"]),
        ):
            if canonical_text(actual) != canonical_text(
                sorted(preserved, key=lambda x: x["input_key"])
            ):
                raise ValueError("historical projection differs from canonical minting event")
        _context(conn, row.account_ref, cap, context)
        predecessor = _lineage(conn, row, cap)
        return row, cap, context, consumed, predecessor
    except IntegrityFailure:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        SQLAlchemyError,
    ) as error:
        raise IntegrityFailure(
            "crm_provenance", "preserved CRM history failed verification"
        ) from error


def verified_crm_history(conn, decision_event_id):
    """Verify the complete chain without a call-stack cutoff or history truncation."""
    original = _history_step(conn, decision_event_id)
    visited = {decision_event_id}
    prior = original[-1]
    while prior is not None:
        if prior.decision_event_id in visited:
            raise IntegrityFailure("crm_provenance", "cyclic recorded predecessor lineage")
        visited.add(prior.decision_event_id)
        prior = _history_step(conn, prior.decision_event_id)[-1]
    return original


def load_crm_context(conn, decision_event_id):
    _, _, context, _, _ = verified_crm_history(conn, decision_event_id)
    result = []
    for entry in context:
        evidence = (
            _evidence_row(conn, entry["evidence_version_id"])
            if entry["evidence_version_id"]
            else None
        )
        result.append(
            ContextInput(
                key=entry["input_key"],
                availability=entry["availability"],
                value=entry["value"],
                evidence_version_id=entry["evidence_version_id"],
                observed_at=date.fromisoformat(evidence.observed_at)
                if evidence and evidence.observed_at
                else None,
            )
        )
    return tuple(result)
