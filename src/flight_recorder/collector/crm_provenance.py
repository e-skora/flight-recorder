"""Atomic CRM projections and full preserved-observation verification."""

import json
from datetime import date, datetime

from sqlalchemy import or_, select

from flight_recorder.adapters.crm.contracts import CRMTarget
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.crm_schema import (
    CaptureDocument,
    available_keys,
    bound_item,
    expected_context,
    observation_body,
)
from flight_recorder.collector.errors import ConflictError, RejectedError
from flight_recorder.ledger.crm_schema import (
    crm_captures,
    crm_decision_consumed_inputs,
    crm_decision_context,
    crm_decisions,
    crm_evidence_versions,
    schema_ready,
)
from flight_recorder.ledger.schema import events


def is_crm(n):
    return n["schema_version"] == "3" and n["event_type"] in (
        "evidence.recorded",
        "decision.recorded",
    )


def _reject(detail):
    raise RejectedError(dict(status="rejected", reason="invalid_crm_provenance", detail=detail))


def _conflict(detail):
    raise ConflictError(dict(status="conflict", reason="crm_identity_conflict", detail=detail))


def ensure_crm_schema(conn):
    if not schema_ready(conn):
        raise RejectedError(dict(status="rejected", reason="crm_schema_uninitialized"))


def _capture_row(conn, n):
    p = n["payload"]
    cap = p["capture"]
    rows = conn.execute(
        select(crm_captures).where(
            or_(
                crm_captures.c.capture_hash == p["capture_hash"],
                crm_captures.c.unit_id == cap["unit_id"],
                (crm_captures.c.account_ref == n["account_ref"])
                & (crm_captures.c.capture_sequence == cap["capture_sequence"]),
            )
        )
    ).all()
    for row in rows:
        expected = (
            p["capture_hash"],
            n["account_ref"],
            cap["unit_id"],
            cap["capture_sequence"],
            CRMTarget(**cap["target"]).key,
            canonical_text(cap),
        )
        if (
            row.capture_hash,
            row.account_ref,
            row.unit_id,
            row.capture_sequence,
            row.target_key,
            row.body_json,
        ) != expected:
            _conflict("capture hash/unit/account-sequence already bound to other bytes")
    return rows[0] if rows else None


def _verified_event(conn, event_id):
    from flight_recorder.collector.schema import validate_envelope_json

    row = conn.execute(select(events).where(events.c.event_id == event_id)).first()
    if row is None:
        raise ValueError("missing minting event")
    n = {
        key: getattr(row, key)
        for key in (
            "schema_version",
            "event_id",
            "event_type",
            "source",
            "account_ref",
            "occurred_at",
            "recorded_at",
        )
    }
    n["payload"] = json.loads(row.payload)
    validated = validate_envelope_json(canonical_text(n)).model_dump(mode="json")
    if canonical_text(validated) != canonical_text(n) or canonical_hash(n) != row.canonical_hash:
        raise ValueError("minting event content/hash mismatch")
    return n


def _original_capture(conn, capture_hash):
    row = conn.execute(
        select(crm_captures).where(crm_captures.c.capture_hash == capture_hash)
    ).first()
    if row is None:
        raise ValueError("missing capture projection")
    cap = CaptureDocument.model_validate_json(row.body_json, strict=True).model_dump(mode="json")
    if (
        canonical_text(cap) != row.body_json
        or canonical_hash(cap) != row.capture_hash
        or CRMTarget(**cap["target"]).account_ref != row.account_ref
        or CRMTarget(**cap["target"]).key != row.target_key
        or cap["unit_id"] != row.unit_id
        or cap["capture_sequence"] != row.capture_sequence
    ):
        raise ValueError("capture projection identity mismatch")
    event = _verified_event(conn, row.source_event_id)
    if (
        not is_crm(event)
        or event["account_ref"] != row.account_ref
        or (event["payload"]["capture_hash"], canonical_text(event["payload"]["capture"]))
        != (row.capture_hash, row.body_json)
    ):
        raise ValueError("capture minting event/projection mismatch")
    return cap


def _evidence_row(conn, version_id):
    return conn.execute(
        select(crm_evidence_versions).where(
            crm_evidence_versions.c.evidence_version_id == version_id
        )
    ).first()


def _verify_evidence(row, account_ref, cap, item):
    if row is None:
        raise ValueError("missing CRM evidence version")
    o = observation_body(account_ref, cap, item)
    expected = dict(
        account_ref=account_ref,
        evidence_type=item["evidence_type"],
        value_json=canonical_text(o["semantic"]),
        source="salesforce-test-account",
        observed_at=item.get("observed_at"),
        available_at=o["eligible_at"],
        supersedes_evidence_version_id=item["supersedes_evidence_version_id"],
        observation_json=canonical_text(o),
    )
    if row.evidence_version_id != item["evidence_version_id"] or any(
        getattr(row, key) != value for key, value in expected.items()
    ):
        raise ValueError("evidence differs from complete captured observation")
    if row.available_at > cap["capture_boundary"]:
        raise ValueError("evidence available after decision boundary")


def _verify_origin(conn, row):
    cap = _original_capture(conn, row.capture_hash)
    account = CRMTarget(**cap["target"]).account_ref
    item = bound_item(account, cap, row.evidence_type, row.supersedes_evidence_version_id)
    _verify_evidence(row, account, cap, item)
    event = _verified_event(conn, row.source_event_id)
    if (
        event["event_type"] != "evidence.recorded"
        or not is_crm(event)
        or event["account_ref"] != account
        or event["payload"]["capture_hash"] != row.capture_hash
        or item not in event["payload"]["items"]
    ):
        raise ValueError("evidence minting event/projection mismatch")
    return cap


def _lineage(conn, account_ref, cap):
    if cap["reprocesses"] is None:
        prior = conn.execute(
            select(crm_decisions.c.unit_id).where(
                crm_decisions.c.account_ref == account_ref,
                crm_decisions.c.logical_key == cap["logical_key"],
            )
        ).first()
        if prior:
            raise ValueError("same observation requires explicit reprocessing lineage")
        return
    prior = conn.execute(
        select(crm_decisions).where(crm_decisions.c.unit_id == cap["reprocesses"])
    ).first()
    if (
        prior is None
        or prior.account_ref != account_ref
        or prior.logical_key != cap["logical_key"]
        or prior.processing_version == cap["processing_version"]
    ):
        raise ValueError(
            "reprocessing requires accepted same-target observation under different processing"
        )
    old = _original_capture(conn, prior.capture_hash)
    event = _verified_event(conn, prior.decision_event_id)
    p = event["payload"]
    if (
        event["event_type"] != "decision.recorded"
        or not is_crm(event)
        or event["account_ref"] != account_ref
        or p["capture_hash"] != prior.capture_hash
        or old["target"] != cap["target"]
        or old["observation_id"] != cap["observation_id"]
        or old["capture_sequence"] >= cap["capture_sequence"]
        or old["capture_boundary"] > cap["capture_boundary"]
        or old["unit_id"] != prior.unit_id
        or old["logical_key"] != prior.logical_key
        or old["processing_version"] != prior.processing_version
    ):
        raise ValueError("invalid preserved reprocessing lineage")


def _context(conn, account_ref, cap, submitted):
    supplied = {x["input_key"]: x for x in submitted}
    supersessions = {}
    for key in available_keys(cap):
        entry = supplied.get(key)
        if entry is None or entry["evidence_version_id"] is None:
            raise ValueError("available captured input missing from context")
        row = _evidence_row(conn, entry["evidence_version_id"])
        if row is None:
            raise ValueError("context references missing CRM evidence")
        supersessions[key] = row.supersedes_evidence_version_id
        expected = bound_item(account_ref, cap, key, row.supersedes_evidence_version_id)
        _verify_evidence(row, account_ref, cap, expected)
        original = _verify_origin(conn, row)
        if original["target"] != cap["target"]:
            raise ValueError("cross-target evidence reuse")
    expected = expected_context(account_ref, cap, supersessions)
    if canonical_text(sorted(submitted, key=lambda x: x["input_key"])) != canonical_text(expected):
        raise ValueError("context differs from captured non-absent inputs")
    return expected


def _evaluate(conn, p, cap, context):
    from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
    from flight_recorder.replay.reconstruct import load_artifact_row, verify_artifact_row

    ref = p["logic_artifact"]
    artifact = verify_artifact_row(
        load_artifact_row(conn, ref["artifact_hash"]), ref["artifact_hash"]
    ).artifact
    if (
        any(
            getattr(artifact, key) != ref[key]
            for key in ("artifact_id", "logic_version", "evaluator_version")
        )
        or artifact.decision_class != p["decision_class"]
    ):
        raise ValueError("registered artifact identity mismatch")
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
    by_key = {entry["input_key"]: entry for entry in context}
    used = [
        dict(
            input_key=f.key,
            value=by_key[f.key]["value"],
            evidence_version_id=f.evidence_version_id,
            contribution=f.contribution,
        )
        for f in result.factors
        if f.input_state is InputState.CONSUMED
    ]
    if not used:
        raise ValueError("no usable consumed inputs")
    if canonical_text(sorted(p["consumed_inputs"], key=lambda x: x["input_key"])) != canonical_text(
        sorted(used, key=lambda x: x["input_key"])
    ):
        raise ValueError("consumed values/contributions differ from pinned evaluation")
    if canonical_text(p["result"]) != canonical_text(
        dict(score=result.score, threshold=result.threshold, output=result.output)
    ):
        raise ValueError("result differs from pinned evaluation")


def validate_crm(conn, n):
    ensure_crm_schema(conn)
    p = n["payload"]
    cap = p["capture"]
    account = n["account_ref"]
    _capture_row(conn, n)
    try:
        if n["event_type"] == "evidence.recorded":
            for item in p["items"]:
                if _evidence_row(conn, item["evidence_version_id"]) is not None:
                    raise ValueError("evidence already minted; reference verified version instead")
                if item["supersedes_evidence_version_id"] is not None:
                    prior = _evidence_row(conn, item["supersedes_evidence_version_id"])
                    if prior is None:
                        raise ValueError("missing superseded evidence")
                    old = _verify_origin(conn, prior)
                    eligible = observation_body(account, cap, item)["eligible_at"]
                    if (
                        prior.evidence_version_id == item["evidence_version_id"]
                        or prior.account_ref != account
                        or prior.evidence_type != item["evidence_type"]
                        or old["target"] != cap["target"]
                        or old["mapping_hash"] != cap["mapping_hash"]
                        or old["capture_sequence"] >= cap["capture_sequence"]
                        or prior.available_at > eligible
                    ):
                        raise ValueError(
                            "supersession requires earlier same-target/type/mapping evidence"
                        )
        else:
            if conn.execute(
                select(crm_decisions.c.decision_event_id).where(
                    crm_decisions.c.unit_id == cap["unit_id"]
                )
            ).first():
                _conflict("unit already has accepted original decision")
            _lineage(conn, account, cap)
            context = _context(conn, account, cap, p["historical_context"])
            _evaluate(conn, p, cap, context)
    except (ValueError, KeyError, TypeError) as exc:
        _reject(str(exc))
    except Exception as exc:
        from flight_recorder.logic.evaluator import EvaluationError
        from flight_recorder.logic.rules import RuleError
        from flight_recorder.replay.reconstruct import ReconstructionError

        if isinstance(exc, (EvaluationError, RuleError, ReconstructionError)):
            _reject(str(exc))
        raise


def project_crm(conn, n, sequence):
    p = n["payload"]
    cap = p["capture"]
    account = n["account_ref"]
    event_id = n["event_id"]
    if _capture_row(conn, n) is None:
        conn.execute(
            crm_captures.insert().values(
                capture_hash=p["capture_hash"],
                account_ref=account,
                unit_id=cap["unit_id"],
                capture_sequence=cap["capture_sequence"],
                target_key=CRMTarget(**cap["target"]).key,
                body_json=canonical_text(cap),
                source_event_id=event_id,
            )
        )
    if n["event_type"] == "evidence.recorded":
        for item in p["items"]:
            o = observation_body(account, cap, item)
            conn.execute(
                crm_evidence_versions.insert().values(
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
        crm_decisions.insert().values(
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
        (crm_decision_context, p["historical_context"]),
        (crm_decision_consumed_inputs, p["consumed_inputs"]),
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
            if table is crm_decision_context:
                values["availability"] = entry["availability"]
            else:
                values["contribution"] = entry["contribution"]
            conn.execute(table.insert().values(**values))
