"""Original-bound CRM operation observations, never execution or provider attestation."""

import copy
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.crm.contracts import CRMTarget
from flight_recorder.adapters.crm.mapping import FIELDS, OUTPUTS
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.crm_schema import (
    Closed,
    Hash,
    Positive,
    SQLiteInteger,
    TargetDocument,
    Text,
    Timestamp,
)
from flight_recorder.collector.errors import RejectedError

TERMINAL = frozenset({"refused", "refused_no_effect", "observed_applied_at_t"})
Origin = Literal["fixture", "injected_transport", "provider_http"]
BoundedText = Annotated[str, Field(min_length=1, max_length=256)]


def require(condition, reason="crm_operation_integrity"):
    if not condition:
        raise RejectedError({"status": "rejected", "reason": reason})


def intent_digest(intent):
    binding = copy.deepcopy(intent)
    binding.pop("operation_id", None)
    binding["intended"].pop("fr_op_id", None)
    return canonical_hash(binding)


def request_digest(intent):
    """Exact logical request identity; does not assert that a request was sent."""
    return canonical_hash(
        dict(
            method="PATCH",
            target=intent["target"],
            connection_id=intent["connection_id"],
            operation_id=intent["operation_id"],
            headers={"If-Match": intent["source_precondition"]},
            body={remote: intent["intended"][local] for local, remote in OUTPUTS.items()},
        )
    )


class WriteTuple(Closed):
    fr_score: SQLiteInteger
    fr_output: Literal["PRIORITIZE", "DO_NOT_PRIORITIZE"]
    fr_logic_version: Annotated[str, Field(min_length=1, max_length=80)]
    fr_decision_ref: BoundedText
    fr_op_id: Hash
    fr_op_rev: Positive


class CRMWriteIntent(Closed):
    format_version: Annotated[int, Field(ge=1, le=1)]
    target: TargetDocument
    connection_id: BoundedText
    decision_event_id: BoundedText
    capture_hash: Hash
    capture_sequence: Positive
    artifact_hash: Hash
    evaluator_version: BoundedText
    mapping_hash: Hash
    selection_identity: BoundedText
    policy_generation: Positive
    revision: Positive
    source_precondition: Text
    created_at: Timestamp
    intended: WriteTuple
    operation_id: Hash

    @model_validator(mode="after")
    def _identity(self):
        d = self.model_dump(mode="json")
        if (
            self.target.provider != "salesforce"
            or not re.fullmatch(r'"[\x21\x23-\x7e]{1,1022}"', self.source_precondition)
            or self.operation_id != intent_digest(d)
            or self.intended.fr_op_id != self.operation_id
            or self.intended.fr_op_rev != self.revision
        ):
            raise ValueError("invalid CRM write intent identity/precondition")
        return self


class ResponseReceipt(Closed):
    request_hash: Hash
    started_at: Timestamp
    finished_at: Timestamp
    status: Annotated[int, Field(ge=100, le=599)] | None
    complete: bool
    body_sha256: Hash | None

    @model_validator(mode="after")
    def _complete(self):
        if self.started_at > self.finished_at or (
            self.complete and (self.status is None or self.body_sha256 is None)
        ):
            raise ValueError("invalid CRM response receipt")
        return self


class OutputRead(Closed):
    target: TargetDocument
    fields: list[Text]
    values: dict[str, SQLiteInteger | str | None]
    started_at: Timestamp
    finished_at: Timestamp
    body_sha256: Hash

    @model_validator(mode="after")
    def _selection(self):
        if (
            self.fields != sorted(OUTPUTS)
            or not set(self.values) <= set(OUTPUTS)
            or self.started_at > self.finished_at
        ):
            raise ValueError("invalid CRM output read")
        return self


class OperationPayload(Closed):
    data_origin: Literal["fictional_records_real_crm"]
    observation_origin: Origin
    intent: CRMWriteIntent
    observation_sequence: Positive
    phase: Literal["prepared", "send_started", "response", "output_observed", "refused"]
    state: Literal[
        "prepared",
        "refused",
        "unknown",
        "awaiting_observation",
        "observed_applied_at_t",
        "refused_no_effect",
    ]
    observed_at: Timestamp
    response: ResponseReceipt | None
    output_read: OutputRead | None
    reason: (
        Literal[
            "policy_disabled",
            "policy_changed",
            "authorization_unavailable",
            "newer_capture",
            "target_unallowlisted",
        ]
        | None
    )


def build_operation_envelope():
    from flight_recorder.collector.schema import _EnvelopeBase

    class CRMOperationEnvelope(_EnvelopeBase):
        schema_version: Literal["2"]
        event_type: Literal["external_operation.observed"]
        source: Literal["salesforce-test-account"]
        payload: OperationPayload

        @model_validator(mode="after")
        def _identity(self):
            p = self.payload
            if (
                self.event_id
                != f"fr-crm-write-observation-v1-{p.intent.operation_id}-{p.observation_sequence}"
                or self.occurred_at != p.observed_at
                or self.recorded_at < self.occurred_at
                or p.observed_at < p.intent.created_at
                or self.account_ref != CRMTarget(**p.intent.target.model_dump()).account_ref
            ):
                raise ValueError("invalid CRM operation envelope binding")
            return self

    return CRMOperationEnvelope


def is_crm_operation(n):
    return n["event_type"] == "external_operation.observed" and n["schema_version"] == "2"


def ensure_operation_schema(conn):
    from flight_recorder.ledger.crm_operations import schema_ready

    require(schema_ready(conn), "crm_operation_schema_uninitialized")


def verify_original(conn, account_ref, intent, *, before_sequence=None):
    """Shared verified reconstruction, plus every immutable request binding."""
    from flight_recorder.ledger.schema import events
    from flight_recorder.replay.crm_context import verified_crm_history
    from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct

    try:
        row, cap, _, _, _ = verified_crm_history(conn, intent["decision_event_id"])
        original = reconstruct(conn, row.decision_event_id)
        ev = conn.execute(select(events).where(events.c.event_id == row.decision_event_id)).one()
        expected = dict(
            target=cap["target"],
            connection_id=cap["connection_id"],
            capture_hash=row.capture_hash,
            capture_sequence=cap["capture_sequence"],
            artifact_hash=cap["rule_hash"],
            evaluator_version=cap["evaluator_version"],
            mapping_hash=cap["mapping_hash"],
            selection_identity=cap["selection_identity"],
            source_precondition=cap["precondition"],
            intended=dict(
                fr_score=original.result.score,
                fr_output=original.result.output,
                fr_logic_version=original.logic_version,
                fr_decision_ref=row.decision_event_id,
                fr_op_id=intent["operation_id"],
                fr_op_rev=intent["revision"],
            ),
        )
        require(
            row.account_ref == account_ref
            and (before_sequence is None or row.ingest_sequence < before_sequence)
            and all(canonical_text(intent[k]) == canonical_text(v) for k, v in expected.items())
            and datetime.fromisoformat(intent["created_at"])
            >= datetime.fromisoformat(ev.recorded_at)
            and datetime.fromisoformat(intent["created_at"])
            >= datetime.fromisoformat(cap["capture_boundary"])
            and all(cap["inputs"][key]["state"] == "available" for key in FIELDS),
            "crm_operation_original_binding",
        )
        return cap
    except (ReconstructionError, SQLAlchemyError, ValueError, KeyError, TypeError) as exc:
        raise RejectedError(
            {"status": "rejected", "reason": "crm_operation_original_integrity"}
        ) from exc


def _latest(conn, account_ref, intent, cutoff):
    from flight_recorder.ledger.crm_schema import crm_captures, crm_decisions
    from flight_recorder.ledger.schema import events

    capture = conn.execute(
        select(crm_captures.c.capture_hash)
        .join(events, events.c.event_id == crm_captures.c.source_event_id)
        .where(crm_captures.c.account_ref == account_ref, events.c.ingest_sequence < cutoff)
        .order_by(crm_captures.c.capture_sequence.desc())
        .limit(1)
    ).scalar()
    decision = conn.execute(
        select(crm_decisions.c.decision_event_id)
        .where(crm_decisions.c.account_ref == account_ref, crm_decisions.c.ingest_sequence < cutoff)
        .order_by(crm_decisions.c.ingest_sequence.desc())
        .limit(1)
    ).scalar()
    require(
        capture == intent["capture_hash"] and decision == intent["decision_event_id"],
        "crm_operation_newer_capture",
    )


def _step(conn, doc, prior, cutoff):
    p = doc["payload"]
    i = p["intent"]
    own = [d for d in prior if d["payload"]["intent"]["operation_id"] == i["operation_id"]]
    cap = verify_original(conn, doc["account_ref"], i, before_sequence=cutoff)
    require(p["observation_origin"] == cap["observation_origin"])
    require(p["observed_at"] >= i["created_at"] and p["observed_at"] <= doc["recorded_at"])
    if not own:
        require(
            p["observation_sequence"] == 1 and p["phase"] == "prepared" and p["state"] == "prepared"
        )
        require(p["response"] is None and p["output_read"] is None and p["reason"] is None)
        target = canonical_text(i["target"])
        same = [d for d in prior if canonical_text(d["payload"]["intent"]["target"]) == target]
        last = {d["payload"]["intent"]["operation_id"]: d for d in same}
        require(
            all(d["payload"]["state"] in TERMINAL for d in last.values()),
            "crm_operation_target_held",
        )
        require(
            all(
                datetime.fromisoformat(p["observed_at"])
                >= datetime.fromisoformat(d["payload"]["observed_at"])
                for d in last.values()
            ),
            "crm_operation_time_order",
        )
        require(
            i["revision"] == max([d["payload"]["intent"]["revision"] for d in same], default=0) + 1
        )
        require(
            all(
                d["payload"]["intent"]["decision_event_id"] != i["decision_event_id"] for d in same
            ),
            "crm_operation_original_already_used",
        )
        _latest(conn, doc["account_ref"], i, cutoff)
        return
    previous = own[-1]["payload"]
    require(canonical_text(previous["intent"]) == canonical_text(i))
    require(
        p["observation_origin"] == previous["observation_origin"]
        and p["observation_sequence"] == previous["observation_sequence"] + 1
    )
    require(
        datetime.fromisoformat(p["observed_at"]) >= datetime.fromisoformat(previous["observed_at"])
    )
    require(previous["state"] not in TERMINAL, "crm_operation_already_terminal")
    phase = p["phase"]
    response = p["response"]
    read = p["output_read"]
    if phase == "send_started":
        require(previous["phase"] == "prepared" and p["state"] == "unknown")
        require(response is None and read is None and p["reason"] is None)
        _latest(conn, doc["account_ref"], i, cutoff)
    elif phase == "refused":
        require(
            previous["phase"] == "prepared" and p["state"] == "refused" and p["reason"] is not None
        )
        require(response is None and read is None)
    elif phase == "response":
        require(
            previous["phase"] == "send_started"
            and response is not None
            and read is None
            and p["reason"] is None
        )
        require(response["request_hash"] == request_digest(i))
        require(
            datetime.fromisoformat(previous["observed_at"])
            <= datetime.fromisoformat(response["started_at"])
            <= datetime.fromisoformat(response["finished_at"])
            <= datetime.fromisoformat(p["observed_at"])
        )
        expected = "unknown"
        if response["complete"]:
            status = response["status"]
            if 200 <= status < 300:
                expected = "awaiting_observation"
            elif status == 412:
                expected = "refused_no_effect"
        require(p["state"] == expected)
    elif phase == "output_observed":
        require(
            previous["state"] in ("unknown", "awaiting_observation")
            and previous["phase"] in ("send_started", "response", "output_observed")
        )
        require(response is None and read is not None and p["reason"] is None)
        require(canonical_text(read["target"]) == canonical_text(i["target"]))
        marker = next(d["payload"] for d in own if d["payload"]["phase"] == "send_started")
        require(
            datetime.fromisoformat(marker["observed_at"])
            <= datetime.fromisoformat(read["started_at"])
            <= datetime.fromisoformat(read["finished_at"])
            <= datetime.fromisoformat(p["observed_at"])
        )
        complete = next(
            (
                d["payload"]["response"]
                for d in own
                if d["payload"]["phase"] == "response"
                and d["payload"]["state"] == "awaiting_observation"
            ),
            None,
        )
        if complete:
            require(
                datetime.fromisoformat(read["started_at"])
                >= datetime.fromisoformat(complete["finished_at"])
            )
        matches = canonical_text(read["values"]) == canonical_text(i["intended"])
        expected = "observed_applied_at_t" if complete and matches else previous["state"]
        require(p["state"] == expected)
    else:
        require(False)


def _projection(doc, ingest_sequence):
    p = doc["payload"]
    i = p["intent"]
    return dict(
        event_id=doc["event_id"],
        account_ref=doc["account_ref"],
        operation_id=i["operation_id"],
        observation_sequence=p["observation_sequence"],
        target_key=canonical_text(i["target"]),
        revision=i["revision"],
        decision_event_id=i["decision_event_id"],
        capture_hash=i["capture_hash"],
        state=p["state"],
        phase=p["phase"],
        observed_at=p["observed_at"],
        intent_json=canonical_text(i),
        ingest_sequence=ingest_sequence,
    )


def operation_history(conn, account_ref):
    """Verify all preserved events, originals, states and exact projection coverage."""
    from flight_recorder.collector.operation_contract import event_document
    from flight_recorder.ledger.crm_operations import crm_operation_observations
    from flight_recorder.ledger.schema import events

    ensure_operation_schema(conn)
    prior = []
    rows = conn.execute(
        select(events)
        .where(
            events.c.account_ref == account_ref,
            events.c.event_type == "external_operation.observed",
            events.c.schema_version == "2",
        )
        .order_by(events.c.ingest_sequence)
    ).all()
    projected = (
        conn.execute(
            select(crm_operation_observations).where(
                crm_operation_observations.c.account_ref == account_ref
            )
        )
        .mappings()
        .all()
    )
    require({r.event_id for r in rows} == {r["event_id"] for r in projected})
    by_id = {r["event_id"]: dict(r) for r in projected}
    for row in rows:
        try:
            doc = event_document(row)
            require(by_id[row.event_id] == _projection(doc, row.ingest_sequence))
            _step(conn, doc, prior, row.ingest_sequence)
            prior.append(doc)
        except (ValueError, TypeError, KeyError, SQLAlchemyError, StopIteration) as exc:
            raise RejectedError(
                {"status": "rejected", "reason": "crm_operation_history_integrity"}
            ) from exc
    return prior


def validate_operation(conn, normalized):
    from flight_recorder.ledger.schema import events

    prior = operation_history(conn, normalized["account_ref"])
    cutoff = (
        conn.execute(
            select(events.c.ingest_sequence).order_by(events.c.ingest_sequence.desc()).limit(1)
        ).scalar()
        or 0
    )
    _step(conn, normalized, prior, cutoff + 1)


def project_operation(conn, normalized, ingest_sequence):
    from flight_recorder.ledger.crm_operations import crm_operation_observations

    conn.execute(
        crm_operation_observations.insert().values(**_projection(normalized, ingest_sequence))
    )
