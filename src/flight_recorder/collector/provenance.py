"""Atomic v2 projections and preserved-provenance verification, local synthetic only."""

import json
from datetime import date, datetime

from sqlalchemy import inspect, or_, select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.hubspot.contracts import typed_equal
from flight_recorder.collector.canonical import canonical_text
from flight_recorder.collector.capture_contract import (
    SOURCE,
    account_identity,
    available_keys,
    bound_item,
    expected_context,
    observation_body,
    validate_capture,
)
from flight_recorder.collector.errors import ConflictError, RejectedError
from flight_recorder.ledger.schema import (
    capture_contexts,
    decision_consumed_inputs_v2,
    decision_context_v2,
    decisions_v2,
    evidence_versions_v2,
)


def _reject(detail):
    raise RejectedError(dict(status="rejected", reason="invalid_v2_provenance", detail=detail))


def _conflict(detail):
    raise ConflictError(dict(status="conflict", reason="v2_identity_conflict", detail=detail))


def is_v2(n):
    return n["schema_version"] == "2" and n["event_type"] in (
        "evidence.recorded",
        "decision.recorded",
    )


def v2_decision(conn, event_id):
    # An existing legacy decision needs no new table to remain readable.
    if not inspect(conn).has_table("decisions_v2"):
        return None
    return conn.execute(
        select(decisions_v2).where(decisions_v2.c.decision_event_id == event_id)
    ).first()


def _capture_row(conn, n):
    p = n["payload"]
    cap = p["capture"]
    rows = conn.execute(
        select(capture_contexts).where(
            or_(
                capture_contexts.c.capture_hash == p["capture_hash"],
                capture_contexts.c.unit_id == cap["unit_id"],
                (capture_contexts.c.account_ref == n["account_ref"])
                & (capture_contexts.c.capture_sequence == cap["capture_sequence"]),
            )
        )
    ).all()
    for row in rows:
        if (
            row.capture_hash,
            row.account_ref,
            row.unit_id,
            row.capture_sequence,
            row.body_json,
        ) != (
            p["capture_hash"],
            n["account_ref"],
            cap["unit_id"],
            cap["capture_sequence"],
            canonical_text(cap),
        ):
            _conflict("captured hash/unit/account-sequence already bound to other bytes")
    return rows[0] if rows else None


def _evidence_row(conn, version_id):
    return conn.execute(
        select(evidence_versions_v2).where(evidence_versions_v2.c.evidence_version_id == version_id)
    ).first()


def _verify_evidence(row, account_ref, cap, item):
    if row is None:
        raise ValueError("missing v2 evidence version")
    o = observation_body(account_ref, cap, item)
    expected = dict(
        account_ref=account_ref,
        evidence_type=item["evidence_type"],
        value_json=canonical_text(o["semantic"]),
        source=SOURCE,
        observed_at=item.get("observed_at"),
        available_at=o["eligible_at"],
        supersedes_evidence_version_id=item["supersedes_evidence_version_id"],
        observation_json=canonical_text(o),
    )
    if (
        any(getattr(row, k) != v for k, v in expected.items())
        or row.evidence_version_id != item["evidence_version_id"]
    ):
        raise ValueError("v2 evidence projection differs from complete captured observation")


def _lineage(conn, account_ref, cap):
    if cap["reprocesses"] is None:
        return
    prior = conn.execute(
        select(decisions_v2).where(decisions_v2.c.unit_id == cap["reprocesses"])
    ).first()
    if (
        prior is None
        or prior.account_ref != account_ref
        or prior.logical_key != cap["logical_key"]
        or prior.processing_version == cap["processing_version"]
    ):
        raise ValueError(
            "reprocesses requires a prior accepted same-account/logical-key unit "
            "under different processing"
        )
    prior_capture = conn.execute(
        select(capture_contexts).where(capture_contexts.c.capture_hash == prior.capture_hash)
    ).first()
    if prior_capture is None or prior_capture.capture_sequence >= cap["capture_sequence"]:
        raise ValueError("reprocessing requires earlier capture sequence")


def _context(conn, account_ref, cap, submitted):
    supplied = {x["input_key"]: x for x in submitted}
    supersessions = {}
    for key in available_keys(cap):
        entry = supplied.get(key)
        if entry is None or entry["evidence_version_id"] is None:
            raise ValueError("available captured input missing from historical context")
        row = _evidence_row(conn, entry["evidence_version_id"])
        if row is None:
            raise ValueError("historical context references missing v2 evidence")
        supersessions[key] = row.supersedes_evidence_version_id
        item = bound_item(account_ref, cap, key, row.supersedes_evidence_version_id)
        _verify_evidence(row, account_ref, cap, item)
        # The original capture that minted a reused observation must remain intact.
        original = conn.execute(
            select(capture_contexts).where(capture_contexts.c.capture_hash == row.capture_hash)
        ).first()
        if original is None:
            raise ValueError("missing evidence capture provenance")
        old = json.loads(original.body_json)
        validate_capture(old, original.capture_hash)
        if (
            original.account_ref != account_ref
            or old["unit_id"] != original.unit_id
            or old["capture_sequence"] != original.capture_sequence
        ):
            raise ValueError("evidence capture identity mismatch")
        old_item = bound_item(account_ref, old, key, row.supersedes_evidence_version_id)
        _verify_evidence(row, account_ref, old, old_item)
    expected = expected_context(account_ref, cap, supersessions)
    if not typed_equal(sorted(submitted, key=lambda x: x["input_key"]), expected):
        raise ValueError("historical context differs from captured non-absent owned inputs")
    return expected


def _evaluate(conn, p, cap, context):
    from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
    from flight_recorder.replay.reconstruct import load_artifact_row, verify_artifact_row

    ref = p["logic_artifact"]
    verified = verify_artifact_row(
        load_artifact_row(conn, ref["artifact_hash"]), ref["artifact_hash"]
    )
    artifact = verified.artifact
    for key in ("artifact_id", "logic_version", "evaluator_version"):
        if getattr(artifact, key) != ref[key]:
            raise ValueError("decision artifact identity differs from registered content")
    if artifact.decision_class != p["decision_class"]:
        raise ValueError("decision class differs from registered artifact")
    inputs = []
    for entry in context:
        row = (
            _evidence_row(conn, entry["evidence_version_id"])
            if entry["evidence_version_id"]
            else None
        )
        inputs.append(
            ContextInput(
                key=entry["input_key"],
                availability=entry["availability"],
                value=entry["value"],
                evidence_version_id=entry["evidence_version_id"],
                observed_at=date.fromisoformat(row.observed_at)
                if row and row.observed_at
                else None,
            )
        )
    result = evaluate(artifact, inputs, datetime.fromisoformat(cap["capture_boundary"]))
    used = []
    by_key = {x["input_key"]: x for x in context}
    for f in result.factors:
        if f.input_state is InputState.CONSUMED:
            if cap["inputs"][f.key]["state"] == "ignored":
                raise ValueError("selected artifact consumes explicitly ignored captured input")
            used.append(
                dict(
                    input_key=f.key,
                    value=by_key[f.key]["value"],
                    evidence_version_id=f.evidence_version_id,
                    contribution=f.contribution,
                )
            )
    if not used:
        raise ValueError("no usable consumed inputs; empty decision refused")
    if not typed_equal(
        sorted(p["consumed_inputs"], key=lambda x: x["input_key"]),
        sorted(used, key=lambda x: x["input_key"]),
    ):
        raise ValueError("consumed values/contributions disagree with pinned evaluation")
    if not typed_equal(
        p["result"], dict(score=result.score, threshold=result.threshold, output=result.output)
    ):
        raise ValueError("recorded result disagrees with pinned evaluation")
    return result


def validate_v2(conn, n):
    p = n["payload"]
    cap = p["capture"]
    account_ref = n["account_ref"]
    _capture_row(conn, n)
    try:
        if n["event_type"] == "evidence.recorded":
            for item in p["items"]:
                row = _evidence_row(conn, item["evidence_version_id"])
                if row is not None:
                    _verify_evidence(row, account_ref, cap, item)
                    _reject("evidence version already minted; reference verified ID instead")
                predecessor = item["supersedes_evidence_version_id"]
                if predecessor:
                    prior = _evidence_row(conn, predecessor)
                    o = observation_body(account_ref, cap, item)
                    if (
                        predecessor == item["evidence_version_id"]
                        or prior is None
                        or prior.account_ref != account_ref
                        or prior.evidence_type != item["evidence_type"]
                        or prior.available_at > o["eligible_at"]
                    ):
                        raise ValueError(
                            "supersession must reference earlier same-account/type v2 evidence"
                        )
        else:
            already = conn.execute(
                select(decisions_v2.c.decision_event_id).where(
                    decisions_v2.c.unit_id == cap["unit_id"]
                )
            ).first()
            if already:
                _conflict("unit already has an accepted original decision")
            _lineage(conn, account_ref, cap)
            context = _context(conn, account_ref, cap, p["historical_context"])
            _evaluate(conn, p, cap, context)
    except ValueError as exc:
        _reject(str(exc))
    except Exception as exc:
        from flight_recorder.logic.evaluator import EvaluationError
        from flight_recorder.logic.rules import RuleError
        from flight_recorder.replay.reconstruct import ReconstructionError

        if isinstance(exc, (EvaluationError, RuleError, ReconstructionError)):
            _reject(str(exc))
        raise


def project_v2(conn, n, sequence):
    p = n["payload"]
    cap = p["capture"]
    event_id = n["event_id"]
    account = n["account_ref"]
    if _capture_row(conn, n) is None:
        conn.execute(
            capture_contexts.insert().values(
                capture_hash=p["capture_hash"],
                account_ref=account,
                unit_id=cap["unit_id"],
                capture_sequence=cap["capture_sequence"],
                body_json=canonical_text(cap),
                source_event_id=event_id,
            )
        )
    if n["event_type"] == "evidence.recorded":
        for item in p["items"]:
            o = observation_body(account, cap, item)
            conn.execute(
                evidence_versions_v2.insert().values(
                    evidence_version_id=item["evidence_version_id"],
                    account_ref=account,
                    evidence_type=item["evidence_type"],
                    value_json=canonical_text(o["semantic"]),
                    source=n["source"],
                    observed_at=item.get("observed_at"),
                    available_at=o["eligible_at"],
                    source_event_id=event_id,
                    supersedes_evidence_version_id=item["supersedes_evidence_version_id"],
                    capture_hash=p["capture_hash"],
                    observation_json=canonical_text(o),
                )
            )
        return
    ref = p["logic_artifact"]
    conn.execute(
        decisions_v2.insert().values(
            decision_event_id=event_id,
            account_ref=account,
            decision_class=p["decision_class"],
            decision_boundary=p["decision_boundary"],
            workflow_version=p["workflow_version"],
            artifact_hash=ref["artifact_hash"],
            evaluator_version=ref["evaluator_version"],
            logic_version=ref["logic_version"],
            **p["result"],
            explanation=p["explanation"],
            ingest_sequence=sequence,
            unit_id=cap["unit_id"],
            logical_key=cap["logical_key"],
            processing_version=cap["processing_version"],
            capture_hash=p["capture_hash"],
            reprocesses=cap["reprocesses"],
        )
    )
    for table, entries in (
        (decision_context_v2, p["historical_context"]),
        (decision_consumed_inputs_v2, p["consumed_inputs"]),
    ):
        for entry in entries:
            values = dict(
                decision_event_id=event_id,
                input_key=entry["input_key"],
                value_text=canonical_text(entry["value"])
                if entry.get("availability", "available") == "available"
                else None,
                evidence_version_id=entry["evidence_version_id"],
            )
            if table is decision_context_v2:
                values["availability"] = entry["availability"]
            else:
                values["contribution"] = entry["contribution"]
            conn.execute(table.insert().values(**values))


def load_v2_context(conn, row):
    """Verify complete projections before replay; never repair from capture."""
    from flight_recorder.logic.evaluator import ContextInput
    from flight_recorder.replay.reconstruct import IntegrityFailure

    try:
        capture = conn.execute(
            select(capture_contexts).where(capture_contexts.c.capture_hash == row.capture_hash)
        ).first()
        if capture is None:
            raise ValueError("missing capture projection")
        cap = json.loads(capture.body_json)
        validate_capture(cap, row.capture_hash)
        if capture.body_json != canonical_text(cap) or (
            capture.account_ref,
            capture.unit_id,
            capture.capture_sequence,
        ) != (row.account_ref, cap["unit_id"], cap["capture_sequence"]):
            raise ValueError("capture projection identity mismatch")
        checks = {
            "unit_id": cap["unit_id"],
            "logical_key": cap["logical_key"],
            "processing_version": cap["processing_version"],
            "workflow_version": cap["processing_version"],
            "reprocesses": cap["reprocesses"],
            "artifact_hash": cap["rule_hash"],
            "evaluator_version": cap["evaluator_version"],
            "decision_boundary": cap["capture_boundary"],
            "account_ref": account_identity(cap),
        }
        if any(getattr(row, k) != v for k, v in checks.items()):
            raise ValueError("decision projection differs from captured identity/boundary")
        _lineage(conn, row.account_ref, cap)
        rows = conn.execute(
            select(decision_context_v2)
            .where(decision_context_v2.c.decision_event_id == row.decision_event_id)
            .order_by(decision_context_v2.c.input_key)
        ).all()
        context = [
            dict(
                input_key=x.input_key,
                availability=x.availability,
                value=json.loads(x.value_text) if x.value_text is not None else None,
                evidence_version_id=x.evidence_version_id,
            )
            for x in rows
        ]
        _context(conn, row.account_ref, cap, context)
        consumed = conn.execute(
            select(decision_consumed_inputs_v2).where(
                decision_consumed_inputs_v2.c.decision_event_id == row.decision_event_id
            )
        ).all()
        # Replay independently checks contributions/result. Verify preserved consumed values too.
        by_key = {x["input_key"]: x for x in context}
        for used in consumed:
            x = by_key.get(used.input_key)
            if (
                x is None
                or x["availability"] != "available"
                or used.value_text != canonical_text(x["value"])
                or used.evidence_version_id != x["evidence_version_id"]
            ):
                raise ValueError("consumed projection differs from preserved context")
            if cap["inputs"][used.input_key]["state"] == "ignored":
                raise ValueError("consumed projection contradicts explicit ignored input")
        inputs = []
        for x in context:
            ev = _evidence_row(conn, x["evidence_version_id"]) if x["evidence_version_id"] else None
            inputs.append(
                ContextInput(
                    key=x["input_key"],
                    availability=x["availability"],
                    value=x["value"],
                    evidence_version_id=x["evidence_version_id"],
                    observed_at=date.fromisoformat(ev.observed_at)
                    if ev and ev.observed_at
                    else None,
                )
            )
        return tuple(inputs)
    except (ValueError, KeyError, TypeError, AttributeError, SQLAlchemyError) as exc:
        raise IntegrityFailure("v2_provenance", str(exc)) from exc
