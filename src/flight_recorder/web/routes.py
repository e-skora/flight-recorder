"""Server-rendered pages: account list, account trace, and one decision in detail."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from flight_recorder.ledger.schema import accounts, accounts_query, events, logic_artifacts
from flight_recorder.logic.evaluator import EvaluationError
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.counterfactual import (
    COUNTERFACTUAL_LABEL,
    ORIGINAL_LABEL,
    compare,
    replay,
)
from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct
from flight_recorder.web.decision_view import (
    ORIGIN_RECORDED_LOGIC,
    ORIGIN_SELECTED_ARTIFACT,
    artifact_label,
    artifact_options,
    failure_view,
    load_decision_page,
)
from flight_recorder.web.summaries import trace_row

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter(tags=["web"])

OPERATING_COMPANY = "RelayBridge"

#: The logic version `PRODUCT.md` §7 names as current for the canonical demo.
#: The default selection resolves *by this label* to exactly one registered
#: artifact and then uses that artifact's hash; a label is never identity
#: (INV-05), and a tie is never broken by recency (D-011).
DEMO_CURRENT_LOGIC_VERSION = "v5.1"

#: A `current` query parameter is an artifact hash or it is not a request we
#: can answer; an unregistered but well-formed hash is a different matter and
#: reaches `replay`, which names it `ArtifactMissing`.
ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}")


#: The failures the replay panel names. `RuleError` and `EvaluationError` are
#: siblings of `ReconstructionError`, all three deriving from `Exception`, so
#: all three are listed: a logic artifact the collector accepts can still carry
#: a rule the closed grammar refuses or a missing-value behavior the evaluator
#: does not implement, and neither may crash a page. `Exception` itself is
#: never caught: a programming error must still propagate as a 500.
REPLAY_FAILURES = (ReconstructionError, RuleError, EvaluationError)


def _failure_origin(conn, decision_event_id: str) -> str:
    """Which side of the replay raised the failure, established by re-running.

    `replay` reconstructs the original first, so re-running `reconstruct`
    alone separates the two cases: if it also fails, the origin is the
    decision's own preserved logic; if it succeeds, the failure came from
    evaluating the selected current artifact. The origin is never guessed from
    the exception's type, which is the same class on both sides. The call is
    read-only and writes nothing (INV-06).
    """
    try:
        reconstruct(conn, decision_event_id)
    except REPLAY_FAILURES:
        return ORIGIN_RECORDED_LOGIC
    return ORIGIN_SELECTED_ARTIFACT


def _signed(value: int) -> str:
    """A signed delta, with plain `0` for no change."""
    return f"{value:+d}" if value else "0"


def _state_word(state) -> str:
    """An engine `InputState` as its lowercase word; `not present` for None."""
    return "not present" if state is None else str(state.value)


templates.env.filters["signed"] = _signed
templates.env.filters["state_word"] = _state_word


def trace_query(account_ref: str):
    """Every event for the account ordered by (occurred_at, ingest_sequence)."""
    return (
        select(events)
        .where(events.c.account_ref == account_ref)
        .order_by(events.c.occurred_at, events.c.ingest_sequence)
    )


@router.get("/", response_class=HTMLResponse)
def account_list(request: Request):
    engine = request.app.state.engine
    with engine.connect() as conn:
        # `accounts_query()` excludes the reserved `_system` principal, which is
        # infrastructure metadata rather than an account.
        rows = conn.execute(accounts_query().order_by(accounts.c.name)).all()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"accounts": rows, "operating_company": OPERATING_COMPANY},
    )


@router.get("/accounts/{account_ref}", response_class=HTMLResponse)
def account_trace(request: Request, account_ref: str):
    engine = request.app.state.engine
    with engine.connect() as conn:
        account = conn.execute(
            accounts_query().where(accounts.c.account_ref == account_ref)
        ).first()
        if account is None:
            raise HTTPException(status_code=404, detail="unknown account")
        rows = [trace_row(r) for r in conn.execute(trace_query(account_ref)).all()]
    return templates.TemplateResponse(
        request,
        "trace.html",
        {"account": account, "rows": rows, "operating_company": OPERATING_COMPANY},
    )


def _default_current_artifact(conn, decision_class: str) -> tuple[str | None, str | None]:
    """The default current artifact's hash, or the message naming why there is none.

    Resolution is by logic version and then by exact hash. Zero or several
    registered artifacts carrying that version is a state the panel names; it
    is never resolved by insertion order, ingest sequence, or activation date,
    and it never falls back to another artifact (D-011).
    """
    rows = conn.execute(
        select(logic_artifacts.c.artifact_hash, logic_artifacts.c.evaluator_version)
        .where(logic_artifacts.c.decision_class == decision_class)
        .where(logic_artifacts.c.logic_version == DEMO_CURRENT_LOGIC_VERSION)
        .order_by(logic_artifacts.c.artifact_hash)
    ).all()
    if len(rows) == 1:
        return rows[0].artifact_hash, None
    if not rows:
        return None, (
            f"Default replay logic {DEMO_CURRENT_LOGIC_VERSION} is not registered for this "
            "decision class."
        )
    listed = "; ".join(
        artifact_label(DEMO_CURRENT_LOGIC_VERSION, row.artifact_hash, row.evaluator_version)
        + f" (full hash {row.artifact_hash})"
        for row in rows
    )
    return None, (
        f"More than one registered artifact carries logic version "
        f"{DEMO_CURRENT_LOGIC_VERSION}, so no default replay logic can be resolved and one "
        f"must be selected explicitly: {listed}."
    )


@router.get(
    "/accounts/{account_ref}/decisions/{decision_event_id}",
    response_class=HTMLResponse,
)
def decision_detail(
    request: Request,
    account_ref: str,
    decision_event_id: str,
    current: str | None = None,
):
    """One recorded decision (§4.4) with its replay panel (§4.5).

    The recorded sections come from the projection tables and render whatever
    happens to replay. Only the panel depends on `replay`, and every failure
    from it -- a `ReconstructionError`, a `RuleError` or an `EvaluationError`
    -- becomes a named, visible failure state rather than a fallback, a cached
    result, or a partial comparison (AC-07, INV-09). The page still returns
    200: the page rendered correctly, and the failure is a data condition
    rather than a transport error. Anything outside those three families is a
    programming error and still propagates.
    """
    if current is not None and not ARTIFACT_HASH.fullmatch(current):
        raise HTTPException(
            status_code=400,
            detail="current must be 64 lowercase hexadecimal characters (an artifact hash)",
        )

    engine = request.app.state.engine
    with engine.connect() as conn:
        account = conn.execute(
            accounts_query().where(accounts.c.account_ref == account_ref)
        ).first()
        if account is None:
            raise HTTPException(status_code=404, detail="unknown account")

        page = load_decision_page(conn, decision_event_id)
        if page is None:
            raise HTTPException(status_code=404, detail="unknown decision")
        # A decision is never viewable under another account's URL.
        if page.decision.account_ref != account_ref:
            raise HTTPException(status_code=404, detail="unknown decision")

        no_selection_message = None
        if current is not None:
            current_hash, default_resolved = current, False
        else:
            current_hash, no_selection_message = _default_current_artifact(
                conn, page.decision.decision_class
            )
            default_resolved = current_hash is not None

        options = artifact_options(conn, page.decision.decision_class, current_hash)

        comparison = failure = None
        if current_hash is not None:
            try:
                comparison = compare(replay(conn, decision_event_id, current_hash))
            except REPLAY_FAILURES as error:
                failure = failure_view(error, _failure_origin(conn, decision_event_id))

    selected_option = next((o for o in options if o.artifact_hash == current_hash), None)
    return templates.TemplateResponse(
        request,
        "decision.html",
        {
            "account": account,
            "operating_company": OPERATING_COMPANY,
            "decision": page.decision,
            "ruleset": page.ruleset,
            "context_rows": page.context_rows,
            "context_note": page.context_note,
            "action_rows": page.action_rows,
            "outcome_rows": page.outcome_rows,
            "artifact_options": options,
            "current_hash": current_hash,
            "selected_option": selected_option,
            "default_resolved": default_resolved,
            "default_logic_version": DEMO_CURRENT_LOGIC_VERSION,
            "no_selection_message": no_selection_message,
            "comparison": comparison,
            "failure": failure,
            "original_label": ORIGINAL_LABEL,
            "counterfactual_label": COUNTERFACTUAL_LABEL,
        },
    )
