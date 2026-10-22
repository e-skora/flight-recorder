"""Verified domain-only write history; never operational state, credentials or provider IO."""

from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.crm.mapping import OUTPUTS
from flight_recorder.adapters.crm.status import SOURCE_LABELS
from flight_recorder.collector.canonical import canonical_text
from flight_recorder.collector.crm_operations import operation_history
from flight_recorder.collector.errors import CollectorError
from flight_recorder.ledger.crm_operations import schema_ready
from flight_recorder.ledger.schema import events
from flight_recorder.web.hubspot_decision_view import Fact

STATE_TEXT = {
    "prepared": "Prepared. No send was recorded for this operation.",
    "refused": "Sending was refused before this operation could send a request.",
    "unknown": "The request result is unknown. A matching record does not confirm completion.",
    "awaiting_observation": (
        "A complete request response was saved. All six output values are not yet confirmed."
    ),
    "observed_applied_at_t": (
        "The complete response and all six output values agree at the recorded read. "
        "This is not a claim about the record now."
    ),
    "refused_no_effect": (
        "This request was refused with no effect under the recorded contract. "
        "Capture a new original decision before another write."
    ),
}


@dataclass(frozen=True)
class OperationObservation:
    sequence: int
    phase: str
    state: str
    source_label: str
    observed_at: str
    facts: tuple


@dataclass(frozen=True)
class CRMOperationView:
    operation_id: str
    revision: int
    decision_event_id: str
    state: str
    explanation: str
    source_label: str
    intended: object
    output_facts: tuple
    facts: tuple
    observations: tuple


def facts(document):
    return tuple(
        Fact(str(key).replace("_", " "), value if type(value) is str else canonical_text(value))
        for key, value in document.items()
    )


def load_crm_operations(conn, account_ref, decision_event_id):
    try:
        if not schema_ready(conn):
            known = conn.execute(
                select(events.c.event_id)
                .where(
                    events.c.event_type == "external_operation.observed",
                    events.c.schema_version == "2",
                )
                .limit(1)
            ).first()
            return ((), "saved_operation_schema_unavailable") if known else ((), None)
        documents = operation_history(conn, account_ref)
        groups = {}
        for doc in documents:
            payload = doc["payload"]
            if payload["intent"]["decision_event_id"] == decision_event_id:
                groups.setdefault(payload["intent"]["operation_id"], []).append(doc)
        result = []
        for operation_id, docs in groups.items():
            intent, last = docs[0]["payload"]["intent"], docs[-1]["payload"]
            observations = []
            for doc in docs:
                p = doc["payload"]
                details = dict(
                    event_id=doc["event_id"],
                    occurred_at=doc["occurred_at"],
                    recorded_at=doc["recorded_at"],
                    refusal_reason=p["reason"],
                )
                if p["response"] is not None:
                    details.update(
                        {"response " + key: value for key, value in p["response"].items()}
                    )
                if p["output_read"] is not None:
                    details.update(
                        {"output read " + key: value for key, value in p["output_read"].items()}
                    )
                observations.append(
                    OperationObservation(
                        p["observation_sequence"],
                        p["phase"],
                        p["state"],
                        SOURCE_LABELS[p["observation_origin"]],
                        p["observed_at"],
                        facts(details),
                    )
                )
            result.append(
                CRMOperationView(
                    operation_id,
                    intent["revision"],
                    decision_event_id,
                    last["state"],
                    STATE_TEXT[last["state"]],
                    SOURCE_LABELS[last["observation_origin"]],
                    MappingProxyType(dict(intent["intended"])),
                    tuple(
                        Fact(OUTPUTS[k], v if type(v) is str else canonical_text(v))
                        for k, v in intent["intended"].items()
                    ),
                    facts({k: v for k, v in intent.items() if k != "intended"}),
                    tuple(observations),
                )
            )
        return tuple(sorted(result, key=lambda op: (op.revision, op.operation_id))), None
    except (CollectorError, SQLAlchemyError, ValueError, KeyError, TypeError, RecursionError):
        return (), "saved_operation_history_integrity"
