"""The attribution command: compute with the policy, submit through the collector.

Nothing here writes a row. Every result crosses `POST /api/v1/decision-events`
on the in-process application over `httpx.ASGITransport`, the same boundary
`flight-recorder seed` uses, and the collector recomputes the policy before it
accepts one (D-013, INV-11).

**One run, one cutoff.** A run reads the ledger's maximum `ingest_sequence`
once, before anything is submitted, and evaluates every outcome at it. The
attribution events the run writes raise the ledger maximum but never the
snapshot of an outcome evaluated later in the same run.

**Three situations, decided in three places.**

1. *An exact submission retry* resubmits the complete original envelope, so
   the collector answers `duplicate` and nothing is written twice. `submit`
   resubmits the envelope it holds after an uncertain result (a transport
   error or a 5xx), and `retry_operation` recovers a stored envelope by its
   operation identity (`policy.attribution_event_id`) and resubmits it. These
   are the only paths that reuse an envelope.
2. *A fresh ordinary run* (`reevaluate=False`) reads a fresh cutoff, so its
   operation identities differ from the previous run's. In `_run`, an outcome
   whose version already has an effective result is skipped: nothing is
   submitted, so there is no duplicate response either.
3. *A fresh reevaluation* (`reevaluate=True`) recomputes only outcomes that
   already have an effective result. In `_run`, a result equal to it on
   `policy.POLICY_RESULT_FIELDS` is reported unchanged and nothing is
   submitted, leaving the stored cutoff and `attributed_at` untouched; any
   other result is submitted as one replacement linked to it.

`attributed_at` is the real instant each evaluation happened, read from an
injectable clock; it is never derived from the operation's inputs.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    AttributionResult,
    attribute,
    attribution_event_id,
    effective_attribution,
    effective_outcome_versions,
    ledger_maximum,
    load_outcome,
    same_policy_result,
)
from flight_recorder.collector.schema import ATTRIBUTION_SOURCE, SCHEMA_VERSION, format_utc
from flight_recorder.ledger.schema import events

COLLECTOR_PATH = "/api/v1/decision-events"
JSON_HEADERS = {"content-type": "application/json"}

#: How many times `submit` sends one held envelope before reporting.
SUBMIT_ATTEMPTS = 3

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Submission:
    """One envelope the command sent, retained exactly as sent."""

    outcome_event_id: str
    envelope: dict
    http_status: int
    body: dict

    @property
    def event_id(self) -> str:
        return self.envelope["event_id"]

    @property
    def status(self) -> str:
        return self.body.get("status", "error")


@dataclass
class AttributionRun:
    cutoff: int | None
    reevaluate: bool
    submissions: list[Submission] = field(default_factory=list)
    #: Reevaluation only: the recomputed result equals the effective one.
    unchanged: list[str] = field(default_factory=list)
    #: Ordinary run only: the outcome version already has an effective result.
    already_attributed: list[str] = field(default_factory=list)
    #: Reevaluation only: no effective result to reevaluate.
    not_yet_attributed: list[str] = field(default_factory=list)

    @property
    def created(self) -> list[Submission]:
        return [s for s in self.submissions if s.http_status == 201]

    @property
    def failed(self) -> list[Submission]:
        return [s for s in self.submissions if s.http_status >= 400]


def build_envelope(
    result: AttributionResult,
    *,
    account_ref: str,
    attributed_at: datetime,
    recorded_at: datetime,
    supersedes_attribution_event_id: str | None = None,
) -> dict:
    """The `outcome.attributed` envelope for one computed result."""
    attributed = format_utc(attributed_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": attribution_event_id(
            result.outcome_event_id, result.policy_version, result.cutoff
        ),
        "event_type": "outcome.attributed",
        "source": ATTRIBUTION_SOURCE,
        "account_ref": account_ref,
        "occurred_at": attributed,
        "recorded_at": format_utc(max(recorded_at, attributed_at)),
        "payload": {
            "outcome_event_id": result.outcome_event_id,
            "policy_version": result.policy_version,
            "method": result.method,
            "window_days": result.window_days,
            "resolved_action_event_id": result.resolved_action_event_id,
            "resolved_decision_event_id": result.resolved_decision_event_id,
            "status": result.status,
            "reason": result.reason,
            "attributed_at": attributed,
            "ingest_cutoff": result.cutoff,
            "supersedes_attribution_event_id": supersedes_attribution_event_id,
        },
    }


def recover_envelope(
    conn, outcome_event_id: str, *, ingest_cutoff: int, policy_version: str = POLICY_VERSION
) -> dict | None:
    """The complete stored envelope of one operation, or None if never stored.

    Found by the operation identity alone, and rebuilt from the stored event's
    envelope columns and canonical payload, so resubmitting it is a canonically
    identical retry.
    """
    event_id = attribution_event_id(outcome_event_id, policy_version, ingest_cutoff)
    row = conn.execute(select(events).where(events.c.event_id == event_id)).first()
    if row is None:
        return None
    return {
        "schema_version": row.schema_version,
        "event_id": row.event_id,
        "event_type": row.event_type,
        "source": row.source,
        "account_ref": row.account_ref,
        "occurred_at": row.occurred_at,
        "recorded_at": row.recorded_at,
        "payload": json.loads(row.payload),
    }


def _client(app) -> httpx.AsyncClient:
    # A 5xx comes back as a response rather than an exception, so an uncertain
    # submission can be retried with the same envelope.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://attribute")


async def submit(client: httpx.AsyncClient, envelope: dict) -> tuple[int, dict]:
    """Send one envelope; after an uncertain result, send the same envelope again.

    A retry of a submission that did commit is answered `duplicate`, so it has
    no second domain effect.
    """
    body = json.dumps(envelope).encode("utf-8")
    for attempt in range(1, SUBMIT_ATTEMPTS + 1):
        last = attempt == SUBMIT_ATTEMPTS
        try:
            response = await client.post(COLLECTOR_PATH, content=body, headers=JSON_HEADERS)
        except httpx.TransportError:
            if last:
                raise
            continue
        if response.status_code >= 500 and not last:
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {"status": "error", "detail": response.text}
        return response.status_code, payload
    raise AssertionError("unreachable")


async def _run(app, *, reevaluate: bool, clock: Clock, policy_version: str) -> AttributionRun:
    engine = app.state.engine
    with engine.connect() as conn:
        cutoff = ledger_maximum(conn)
        outcome_ids = () if cutoff is None else effective_outcome_versions(conn, cutoff=cutoff)
    run = AttributionRun(cutoff=cutoff, reevaluate=reevaluate)

    async with _client(app) as client:
        for outcome_event_id in outcome_ids:
            # Reads close before the submission, so the collector's write never
            # waits on this connection.
            with engine.connect() as conn:
                existing = effective_attribution(
                    conn, outcome_event_id, policy_version, cutoff=cutoff
                )
                if not reevaluate and existing is not None:
                    run.already_attributed.append(outcome_event_id)  # situation 2
                    continue
                if reevaluate and existing is None:
                    run.not_yet_attributed.append(outcome_event_id)
                    continue
                result = attribute(
                    conn, outcome_event_id, cutoff=cutoff, policy_version=policy_version
                )
                attributed_at = clock()
                account_ref = load_outcome(conn, outcome_event_id, cutoff=cutoff).account_ref

            if reevaluate and same_policy_result(result, existing):
                run.unchanged.append(outcome_event_id)  # situation 3, unchanged
                continue

            envelope = build_envelope(
                result,
                account_ref=account_ref,
                attributed_at=attributed_at,
                recorded_at=clock(),
                supersedes_attribution_event_id=(
                    existing.attribution_event_id if existing is not None else None
                ),
            )
            status, body = await submit(client, envelope)
            run.submissions.append(Submission(outcome_event_id, envelope, status, body))
            if status >= 400:
                break
    return run


def run_attribution(
    app,
    *,
    reevaluate: bool = False,
    clock: Clock = system_clock,
    policy_version: str = POLICY_VERSION,
) -> AttributionRun:
    """One command run: initial attribution, or explicit reevaluation."""
    return asyncio.run(_run(app, reevaluate=reevaluate, clock=clock, policy_version=policy_version))


def resubmit(app, envelope: dict) -> tuple[int, dict]:
    """Submit an envelope the caller holds, unchanged (situation 1)."""

    async def go():
        async with _client(app) as client:
            return await submit(client, envelope)

    return asyncio.run(go())


def retry_operation(
    app, outcome_event_id: str, *, ingest_cutoff: int, policy_version: str = POLICY_VERSION
) -> Submission:
    """Recover one operation's stored envelope by its identity and resubmit it.

    Raises `LookupError` when that operation was never stored, in which case
    there is nothing to retry and a fresh run is the right call.
    """
    with app.state.engine.connect() as conn:
        envelope = recover_envelope(
            conn, outcome_event_id, ingest_cutoff=ingest_cutoff, policy_version=policy_version
        )
    if envelope is None:
        raise LookupError(
            f"no stored attribution of {outcome_event_id!r} under {policy_version!r} at "
            f"ingest_cutoff {ingest_cutoff}"
        )
    status, body = resubmit(app, envelope)
    return Submission(outcome_event_id, envelope, status, body)
