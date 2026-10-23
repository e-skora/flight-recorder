"""Read-only views of verified CRM originals, archived source facts and offline comparisons."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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
from flight_recorder.logic.rules import ObservedWithinRule, RuleError, parse_rule
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
from flight_recorder.web.crm_operation_view import load_crm_operations
from flight_recorder.web.decision_view import positive_weight_bound_view
from flight_recorder.web.hubspot_decision_view import Fact, InputRow

FAILURES = (ReconstructionError, RuleError, EvaluationError)


@dataclass(frozen=True)
class ReasonRow:
    key: str
    label: str
    state: str
    contribution: int | None
    detail: str


def _result_label(output):
    return {"PRIORITIZE": "Prioritize", "DO_NOT_PRIORITIZE": "Do not prioritize"}.get(
        output, output
    )


def _reason_rows(original, context, rows):
    factors = {factor.key: factor for factor in original.result.factors}
    reasons = []
    for row in rows:
        factor, entry = factors.get(row.key), context.get(row.key)
        if row.state == "available and consumed":
            detail = "Rule matched." if factor.matched else "Rule did not match."
            detail += f" Preserved value: {row.value}."
            # Parsing describes the stored window only. Eligibility and contribution
            # come exclusively from the already verified FactorResult.
            rule = parse_rule(factor.key, factor.rule)
            if isinstance(rule, ObservedWithinRule) and entry.observed_at is not None:
                observed = datetime.combine(entry.observed_at, datetime.min.time(), tzinfo=UTC)
                elapsed = original.decision_boundary - observed
                detail += (
                    f" Observed {entry.observed_at.isoformat()}; {elapsed.days} full days old "
                    f"at the saved decision boundary. Stored rule: {rule.days}-day window. "
                    f"Exact elapsed: {elapsed}; "
                    f"boundary: {original.decision_boundary.isoformat()}. "
                    "Full days are descriptive; the verified rule result uses the exact interval."
                )
        elif row.state == "available but ignored":
            detail = f"Preserved value: {row.value}. The original rules did not consume this input."
        elif row.state == "explicitly unavailable":
            detail = (
                "Explicitly unavailable at the decision boundary; no current value substituted."
            )
        else:
            detail = "Absent from the preserved context; no current value substituted."
        reasons.append(
            ReasonRow(
                row.key, row.key.replace("_", " ").capitalize(), row.state, row.contribution, detail
            )
        )
    return tuple(reasons)


def _comparison_reasons(comparison):
    reasons = []
    for row in comparison.contributions:
        if row.change == "unchanged":
            continue

        def state(value):
            return "not present" if value is None else value.value

        detail = (
            f"{row.key.replace('_', ' ').capitalize()}: {row.change}; "
            f"{row.original_contribution} → {row.counterfactual_contribution} points. "
            f"Original: {state(row.original_state)}; current: {state(row.counterfactual_state)}."
        )
        if row.original is not None and row.counterfactual is not None:
            detail += f" Weight {row.original.weight} → {row.counterfactual.weight}."
        if row.counterfactual is not None and row.counterfactual.input_state.value == "consumed":
            detail += (
                " Current rule matched."
                if row.counterfactual.matched
                else " Current rule did not match."
            )
        reasons.append(detail)
    return tuple(reasons)


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
    operations: tuple = ()
    operation_failure: str | None = None
    result_label: str | None = None
    reason_rows: tuple = ()
    comparison_reasons: tuple = ()


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
        result_label=_result_label(original.result.output),
        reason_rows=_reason_rows(original, context, rows),
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
        return replace(
            page,
            original_failure=f"{type(error).__name__}: {error}",
            operation_failure="original_history_unavailable",
        )
    operations, operation_failure = load_crm_operations(conn, account_ref, decision_event_id)
    page = replace(page, operations=operations, operation_failure=operation_failure)
    if current_artifact_hash is not None:
        try:
            comparison = compare(replay(conn, decision_event_id, current_artifact_hash))
            page = replace(
                page, comparison=comparison, comparison_reasons=_comparison_reasons(comparison)
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
