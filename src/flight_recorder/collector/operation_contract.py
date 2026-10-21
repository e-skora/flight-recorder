"""Append-only local-fake operation observations; no execution or outcome credit."""

import copy
from datetime import datetime

from sqlalchemy import select

from flight_recorder.adapters.hubspot.contracts import strict_json_loads, typed_equal
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.errors import ConflictError, RejectedError
from flight_recorder.collector.schema import validate_envelope_json
from flight_recorder.ledger.schema import capture_contexts, events
from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct

EVENT_TYPE = "external_operation.observed"
TERMINAL = frozenset({"superseded", "observed_applied_at_t", "observed_not_applied_at_t"})


def intent_digest(intent):
    binding = copy.deepcopy(intent)
    binding.pop("operation_id", None)
    binding["intended"].pop("fr_op_id", None)
    binding["intended"].pop("fr_op_rev", None)
    return canonical_hash(binding)


def require(condition, reason, *, conflict=False):
    if not condition:
        error = ConflictError if conflict else RejectedError
        raise error({"status": "conflict" if conflict else "rejected", "reason": reason})


def event_document(row):
    document = {
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
    document["payload"] = strict_json_loads(row.payload.encode())
    normalized = validate_envelope_json(canonical_text(document)).model_dump(mode="json")
    require(
        typed_equal(document, normalized) and canonical_hash(normalized) == row.canonical_hash,
        "operation_event_integrity",
    )
    return normalized


def verify_original(conn, account_ref, intent):
    """Resolve the accepted original and its full provenance. Never repairs it."""
    try:
        row = conn.execute(
            select(events).where(events.c.event_id == intent["decision_event_id"])
        ).first()
        require(
            row is not None
            and row.event_type == "decision.recorded"
            and row.schema_version == "2"
            and row.account_ref == account_ref,
            "operation_original_required",
        )
        original = event_document(row)
        p = original["payload"]
        cap = p["capture"]
        reconstructed = reconstruct(conn, row.event_id)
        from flight_recorder.collector.provenance import v2_decision

        projected = v2_decision(conn, row.event_id)
        capture = conn.execute(
            select(capture_contexts).where(capture_contexts.c.capture_hash == p["capture_hash"])
        ).first()
        expected = {
            "fr_score": reconstructed.result.score,
            "fr_output": reconstructed.result.output,
            "fr_logic_version": reconstructed.logic_version,
            "fr_decision_ref": row.event_id,
            "fr_op_id": intent["operation_id"],
            "fr_op_rev": intent["revision"],
        }
        company = canonical_text([cap["company"]["portal_id"], cap["company"]["company_id"]])
        require(
            projected is not None
            and capture is not None
            and capture.body_json == canonical_text(cap)
            and capture.account_ref == account_ref,
            "operation_capture_integrity",
        )
        require(
            all(
                typed_equal(intent[key], value)
                for key, value in {
                    "company_key": company,
                    "unit_id": cap["unit_id"],
                    "capture_hash": p["capture_hash"],
                    "capture_sequence": cap["capture_sequence"],
                    "artifact_hash": reconstructed.artifact_hash,
                    "evaluator_version": reconstructed.evaluator_version,
                }.items()
            ),
            "operation_original_binding",
        )
        require(
            projected.capture_hash == intent["capture_hash"]
            and projected.unit_id == intent["unit_id"]
            and typed_equal(intent["intended"], expected),
            "operation_intended_tuple_mismatch",
        )
        require(
            typed_equal(
                p["result"],
                {
                    "score": reconstructed.result.score,
                    "threshold": reconstructed.result.threshold,
                    "output": reconstructed.result.output,
                },
            ),
            "operation_original_result_mismatch",
        )
        require(
            datetime.fromisoformat(intent["created_at"])
            >= datetime.fromisoformat(original["occurred_at"]),
            "operation_intent_before_original",
        )
        return original
    except (KeyError, TypeError, ValueError, ReconstructionError) as exc:
        raise RejectedError(
            {"status": "rejected", "reason": "operation_original_integrity", "detail": str(exc)}
        ) from exc


def _step(document, prior):
    p = document["payload"]
    intent = p["intent"]
    op = intent["operation_id"]
    company_prior = [
        n["payload"]
        for n in prior
        if n["payload"]["intent"]["company_key"] == intent["company_key"]
    ]
    own = [x for x in company_prior if x["intent"]["operation_id"] == op]
    if not own:
        require(
            p["state"] == "prepared" and p["observation_sequence"] == 1,
            "operation_prepared_required",
        )
        require(
            not any(
                x["intent"]["decision_event_id"] == intent["decision_event_id"]
                for x in company_prior
            ),
            "operation_original_already_bound",
            conflict=True,
        )
        maximum = max((x["intent"]["revision"] for x in company_prior), default=0)
        require(intent["revision"] == maximum + 1, "operation_revision_conflict", conflict=True)
    else:
        last = own[-1]
        require(typed_equal(intent, own[0]["intent"]), "operation_intent_changed", conflict=True)
        require(
            p["observation_sequence"] == last["observation_sequence"] + 1,
            "operation_observation_sequence",
            conflict=True,
        )
        require(p["observed_at"] >= last["observed_at"], "operation_observation_time_reversed")
        require(last["state"] not in TERMINAL, "operation_terminal", conflict=True)
        allowed = {
            "prepared": {"refused", "superseded", "unknown"},
            "refused": {"refused", "superseded", "unknown"},
            "unknown": {"unknown", "observed_applied_at_t", "observed_not_applied_at_t"},
        }
        require(p["state"] in allowed[last["state"]], "operation_invalid_transition")
    markers = [x for x in own if x["reason"] == "send_started"]
    if p["reason"] == "send_started":
        require(
            p["state"] == "unknown"
            and own
            and own[-1]["state"] in {"prepared", "refused"}
            and not markers,
            "operation_send_grant_already_used",
            conflict=True,
        )
        require(
            intent["revision"] == max(x["intent"]["revision"] for x in company_prior),
            "operation_stale_revision",
            conflict=True,
        )
    elif p["state"] == "unknown":
        require(bool(markers), "operation_send_marker_required")
    latest = {}
    for x in company_prior:
        latest[x["intent"]["operation_id"]] = x
    unresolved = [x for key, x in latest.items() if key != op and x["state"] == "unknown"]
    if p["reason"] == "send_started" or p["state"] in {
        "observed_applied_at_t",
        "observed_not_applied_at_t",
    }:
        require(not unresolved, "operation_earlier_uncertainty", conflict=True)
    if p["state"] in {"prepared", "refused", "superseded"}:
        require(
            p["observed_properties"] is None
            and not p["settled_operation_ids"]
            and not p["pending_operation_ids"]
            and not p["not_applied_operation_ids"],
            "operation_unsent_claim_has_disposition",
        )
    if p["state"] in {"observed_applied_at_t", "observed_not_applied_at_t"}:
        require(
            op in p["settled_operation_ids"] and not p["pending_operation_ids"],
            "operation_quiescence_required",
        )
        if p["state"] == "observed_applied_at_t":
            require(
                op not in p["not_applied_operation_ids"]
                and typed_equal(p["observed_properties"], intent["intended"]),
                "operation_full_tuple_required",
            )
        else:
            require(
                op in p["not_applied_operation_ids"] and p["reason"] == "definite_fake_rejection",
                "operation_no_effect_proof_required",
            )


def operation_history(conn, account_ref):
    documents = []
    try:
        rows = conn.execute(
            select(events)
            .where(events.c.event_type == EVENT_TYPE, events.c.account_ref == account_ref)
            .order_by(events.c.ingest_sequence)
        ).all()
        for row in rows:
            document = event_document(row)
            verify_original(conn, account_ref, document["payload"]["intent"])
            _step(document, documents)
            documents.append(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise RejectedError(
            {"status": "rejected", "reason": "operation_history_integrity"}
        ) from exc
    return documents


def validate_operation(conn, normalized):
    verify_original(conn, normalized["account_ref"], normalized["payload"]["intent"])
    _step(normalized, operation_history(conn, normalized["account_ref"]))
