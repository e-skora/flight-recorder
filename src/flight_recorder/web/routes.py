"""Server-rendered pages: account list and account trace."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from flight_recorder.ledger.schema import accounts, events
from flight_recorder.web.summaries import trace_row

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter(tags=["web"])

OPERATING_COMPANY = "RelayBridge"


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
        rows = conn.execute(select(accounts).order_by(accounts.c.name)).all()
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
            select(accounts).where(accounts.c.account_ref == account_ref)
        ).first()
        if account is None:
            raise HTTPException(status_code=404, detail="unknown account")
        rows = [trace_row(r) for r in conn.execute(trace_query(account_ref)).all()]
    return templates.TemplateResponse(
        request,
        "trace.html",
        {"account": account, "rows": rows, "operating_company": OPERATING_COMPANY},
    )
