"""Read-only views of verified CRM originals, archived source facts and offline comparisons."""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from flight_recorder.adapters.crm.status import SOURCE_LABELS, connection_status
from flight_recorder.collector.canonical import canonical_text
from flight_recorder.ledger.crm_schema import crm_evidence_versions
from flight_recorder.logic.evaluator import EvaluationError
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.counterfactual import compare, replay
from flight_recorder.replay.crm_context import crm_decision, crm_event, verified_crm_history
from flight_recorder.replay.reconstruct import (
    ReconstructionError,
    load_artifact_row,
    load_context,
    load_decision_row,
    reconstruct,
    verify_artifact,
)
from flight_recorder.web.decision_view import positive_weight_bound_view
from flight_recorder.web.hubspot_decision_view import Fact, InputRow

FAILURES = (ReconstructionError, RuleError, EvaluationError)


@dataclass(frozen=True)
class CRMDecisionPage:
    event: object
    recorded: object
    original: object = None
    comparison: object = None
    original_failure: str | None = None
    current_failure: str | None = None
    rules: object = None
    bound_note: object = None
    capture_facts: tuple = ()
    context_rows: tuple = ()
    predecessor: object = None
    source_label: str = "Unverified source origin"


def _facts(values):
    return tuple(Fact(label, canonical_text(value)) for label, value in values)


def _details(conn, row, original):
    _, cap, _, _, prior = verified_crm_history(conn, row.decision_event_id)
    artifact = verify_artifact(
        load_artifact_row(conn, row.artifact_hash), load_decision_row(conn, row.decision_event_id)
    ).artifact
    context = {e.key: e for e in load_context(conn, row.decision_event_id, row.decision_boundary)}
    factors = {f.key: f for f in original.result.factors}
    rows = []
    for key in sorted(set(cap["inputs"]) | set(context) | set(factors)):
        entry = context.get(key)
        factor = factors.get(key)
        if entry is None:
            state = "absent"
        elif entry.availability == "unavailable":
            state = "explicitly unavailable"
        elif factor is not None and factor.input_state.value == "consumed":
            state = "available and consumed"
        else:
            state = "available but ignored"
        facts = []
        if entry is not None and entry.evidence_version_id is not None:
            ev = conn.execute(
                select(crm_evidence_versions).where(
                    crm_evidence_versions.c.evidence_version_id == entry.evidence_version_id
                )
            ).one()
            observation = json.loads(ev.observation_json)
            facts = [
                ("Evidence version", ev.evidence_version_id),
                ("Source fields", observation["source_fields"]),
                ("Evidence origin capture hash", ev.capture_hash),
                ("Eligible at", ev.available_at),
                ("Observed date", ev.observed_at),
                ("Source origin", SOURCE_LABELS[observation["observation_origin"]]),
                ("Read started", observation["read_started_at"]),
                ("Read finished", observation["read_finished_at"]),
            ]
        rows.append(
            InputRow(
                key,
                canonical_text(entry.value) if entry else "No preserved value",
                state,
                factor.contribution if state == "available and consumed" else None,
                _facts(facts),
            )
        )
    values = [
        ("Capture hash", row.capture_hash),
        ("Provider / organization / object / record", cap["target"]),
    ]
    values += [
        (label, cap[key])
        for label, key in (
            ("Read started", "read_started_at"),
            ("Read finished", "read_finished_at"),
            ("Source last modified", "source_updated_at"),
            ("Decision capture boundary", "capture_boundary"),
            ("Captured Account ETag", "precondition"),
            ("Provider request ID", "request_id"),
            ("Response body SHA-256", "response_body_sha256"),
            ("Mapping hash", "mapping_hash"),
            ("Rule hash", "rule_hash"),
            ("Evaluator", "evaluator_version"),
            ("Selection identity", "selection_identity"),
            ("Adapter version", "adapter_version"),
            ("Processing version", "processing_version"),
            ("Unit ID", "unit_id"),
            ("Logical key", "logical_key"),
            ("Capture sequence", "capture_sequence"),
            ("Ownership generation", "ownership_generation"),
            ("Connection reference", "connection_id"),
        )
    ]
    return dict(
        rules=artifact,
        bound_note=positive_weight_bound_view(artifact),
        capture_facts=_facts(values),
        context_rows=tuple(rows),
        predecessor=MappingProxyType(dict(prior._mapping)) if prior else None,
        source_label=SOURCE_LABELS[cap["observation_origin"]],
    )


def load_crm_decision_page(conn, account_ref, decision_event_id, current_artifact_hash):
    event = crm_event(conn, decision_event_id)
    if event is None or event.account_ref != account_ref:
        return None
    page = CRMDecisionPage(
        MappingProxyType(
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
        ),
        MappingProxyType({}),
    )
    try:
        row = crm_decision(conn, decision_event_id)
        page = replace(page, recorded=MappingProxyType(dict(row._mapping)))
        original = reconstruct(conn, decision_event_id)
        page = replace(page, original=original, **_details(conn, row, original))
    except FAILURES as error:
        return replace(page, original_failure=f"{type(error).__name__}: {error}")
    if current_artifact_hash is not None:
        try:
            page = replace(
                page, comparison=compare(replay(conn, decision_event_id, current_artifact_hash))
            )
        except FAILURES as error:
            page = replace(page, current_failure=f"{type(error).__name__}: {error}")
    return page


def crm_status_router():
    router = APIRouter()
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    @router.get("/crm/connections", response_class=HTMLResponse)
    def connections(request: Request):
        return templates.TemplateResponse(
            request,
            "crm_status.html",
            {
                "operating_company": "RelayBridge",
                "status": connection_status(request.app.state.crm_runtime),
            },
        )

    return router
