"""Read-only local presentation of verified, synthetic incoming decision history."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.collector.canonical import canonical_text
from flight_recorder.ledger.schema import (
    capture_contexts,
    decision_consumed_inputs_v2,
    decisions_v2,
    events,
    evidence_versions_v2,
)
from flight_recorder.logic.evaluator import EvaluationError
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.counterfactual import Comparison, compare, replay
from flight_recorder.replay.reconstruct import (
    IntegrityFailure,
    Reconstruction,
    ReconstructionError,
    load_artifact_row,
    load_context,
    load_decision_row,
    reconstruct,
    verify_artifact,
)
from flight_recorder.web.decision_view import positive_weight_bound_view

FAILURES = (ReconstructionError, RuleError, EvaluationError)


@dataclass(frozen=True)
class Fact:
    label: str
    value: str


@dataclass(frozen=True)
class InputRow:
    key: str
    value: str
    state: str
    contribution: int | None
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class HubspotDecisionPage:
    event: object
    recorded: object
    original: Reconstruction | None = None
    comparison: Comparison | None = None
    original_failure: str | None = None
    current_failure: str | None = None
    rules: object = None
    bound_note: object = None
    capture_facts: tuple[Fact, ...] = ()
    context_rows: tuple[InputRow, ...] = ()
    predecessor: object = None
    vendor: tuple[Fact, ...] = ()


def _failure(error):
    return f"{type(error).__name__}: {error}"


def _notification_time(milliseconds):
    # Integer arithmetic preserves the source millisecond exactly, without float rounding.
    try:
        return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)).isoformat()
    except (OverflowError, ValueError):
        return "UTC conversion unavailable"


def _facts(values):
    return tuple(Fact(label, canonical_text(value)) for label, value in values)


def _details(conn, row, original):
    artifact = verify_artifact(
        load_artifact_row(conn, row.artifact_hash), load_decision_row(conn, row.decision_event_id)
    ).artifact
    capture = conn.execute(
        select(capture_contexts).where(capture_contexts.c.capture_hash == row.capture_hash)
    ).one()
    cap = json.loads(capture.body_json)
    context = {x.key: x for x in load_context(conn, row.decision_event_id, row.decision_boundary)}
    consumed = {
        x.input_key: x
        for x in conn.execute(
            select(decision_consumed_inputs_v2).where(
                decision_consumed_inputs_v2.c.decision_event_id == row.decision_event_id
            )
        )
    }
    rows = []
    for key in sorted(set(cap["inputs"]) - {"external_vendor_score"}):
        entry = context.get(key)
        used = consumed.get(key)
        state = (
            "absent"
            if entry is None
            else "unavailable"
            if entry.availability == "unavailable"
            else "consumed"
            if used is not None
            else "available but ignored"
        )
        facts = []
        if entry is not None and entry.evidence_version_id is not None:
            ev = conn.execute(
                select(evidence_versions_v2).where(
                    evidence_versions_v2.c.evidence_version_id == entry.evidence_version_id
                )
            ).one()
            observation = json.loads(ev.observation_json)
            origin = conn.execute(
                select(capture_contexts).where(capture_contexts.c.capture_hash == ev.capture_hash)
            ).one()
            facts.extend(
                [
                    ("Evidence version", ev.evidence_version_id),
                    ("Source", ev.source),
                    ("Source property", observation["source_property"]),
                    ("Evidence origin capture hash", ev.capture_hash),
                    ("Evidence origin unit", origin.unit_id),
                    ("Source availability", observation["source_available_at"]),
                    ("Eligible at", ev.available_at),
                    ("Observation date", ev.observed_at),
                    ("Observation read started", observation["read_started_at"]),
                    ("Observation read finished", observation["read_finished_at"]),
                ]
            )
        rows.append(
            InputRow(
                key,
                canonical_text(entry.value) if entry else "No preserved value",
                state,
                used.contribution if used is not None else None,
                _facts(facts),
            )
        )
    capture_values = [
        (key, cap[key])
        for key in (
            "unit_id",
            "logical_key",
            "mapping_identity",
            "selection_identity",
            "adapter_identity",
            "processing_version",
            "capture_sequence",
            "ownership_generation",
            "raw_notification_ref",
            "read_started_at",
            "read_finished_at",
            "capture_boundary",
        )
    ]
    capture_values.insert(0, ("Capture hash", row.capture_hash))
    capture_values.extend(
        ("Notification " + key, value) for key, value in cap["notification"].items()
    )
    capture_values.append(
        ("Notification UTC", _notification_time(cap["notification"]["occurred_at"]))
    )
    predecessor = None
    if row.reprocesses is not None:
        prior = conn.execute(
            select(decisions_v2).where(decisions_v2.c.unit_id == row.reprocesses)
        ).one()
        predecessor = MappingProxyType(dict(prior._mapping))
    vendor = cap["inputs"]["external_vendor_score"]
    return dict(
        rules=artifact,
        bound_note=positive_weight_bound_view(artifact),
        capture_facts=_facts(capture_values),
        context_rows=tuple(rows),
        predecessor=predecessor,
        vendor=_facts(vendor.items()),
    )


def load_hubspot_decision_page(conn, account_ref, decision_event_id, current_artifact_hash):
    """Identify by event metadata, then verify history; never repair from an envelope."""
    event = conn.execute(
        select(events).where(
            events.c.event_id == decision_event_id,
            events.c.account_ref == account_ref,
            events.c.schema_version == "2",
            events.c.event_type == "decision.recorded",
        )
    ).first()
    if event is None:
        return None
    event_view = MappingProxyType(
        {
            key: event._mapping[key]
            for key in (
                "event_id",
                "account_ref",
                "schema_version",
                "event_type",
                "occurred_at",
                "recorded_at",
            )
        }
    )
    page = HubspotDecisionPage(event_view, MappingProxyType({}))
    try:
        row = conn.execute(
            select(decisions_v2).where(decisions_v2.c.decision_event_id == decision_event_id)
        ).first()
    except SQLAlchemyError:
        failure = IntegrityFailure("decisions_v2", "known decision projection is unavailable")
        return replace(page, original_failure=_failure(failure))
    if row is not None:
        page = replace(page, recorded=MappingProxyType(dict(row._mapping)))
    try:
        original = reconstruct(conn, decision_event_id)
        if row is None:
            raise IntegrityFailure("decisions_v2", "known decision projection is missing")
        details = _details(conn, row, original)
    except FAILURES as error:
        return replace(page, original_failure=_failure(error))
    page = replace(page, original=original, **details)
    if current_artifact_hash is not None:
        try:
            page = replace(
                page, comparison=compare(replay(conn, decision_event_id, current_artifact_hash))
            )
        except FAILURES as error:
            page = replace(page, current_failure=_failure(error))
    return page
