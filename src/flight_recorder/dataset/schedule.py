"""The seed schedule for the synthetic dataset and the runner that performs it (D-014 Q4).

**The schedule.** A `Schedule` is plain data derived only from the dataset
config: the nine canonical envelopes exactly as their files hold them, the
stage-1 generated envelopes, one attribution operation per effective outcome
version of that prefix, and the two stage-2 envelopes. `items` flattens them in
submission order and gives each its scheduled 1-based ingest sequence, its
scheduled `event_id` and, for an envelope, its canonical hash computed the way
the collector computes it. An operation's identity is known ahead
(`attribution_event_id` at `cutoff_1`); its content is rebuilt at run time from
the ledger prefix with the policy and the fixed attribution instant `A`, so it
is the same on every run.

**The runner.** `run_schedule` crosses `POST /api/v1/decision-events` on the
in-process application for every item and writes no row itself. Before its
first submission it performs a read-only entry check: the ledger's first
`min(M, S)` sequences must be exactly the schedule's first items, every stored
operation must have its scheduled content, and no outcome the schedule
attributes may hold a result under another identity. Any divergence raises
`ScheduleDiverged` and nothing is submitted. During the run, any answer other
than `201 created` or `200 duplicate` raises `SeedRefused` and stops further
submissions; items accepted before it stay in the ledger, where a later run's
entry check finds them as a matching prefix. The runner never retargets an
operation to a newer cutoff, never sets a supersession link, never deletes and
never reevaluates; only the stage-1 outcome versions and the canonical outcome
are attributed, so the stage-2 outcomes stay awaiting attribution.

**The digest.** SHA-256 over one line `"{event_id} {canonical_hash}\\n"` per
event in ingest order, followed by the canonical JSON of `insights` at the
ledger maximum. The signal definitions and the comparison workflow version
arrive as arguments; this module reads no manifest.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import func, select

from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    AttributionError,
    attribute,
    attribution_event_id,
    ledger_maximum,
)
from flight_recorder.attribution.service import (
    COLLECTOR_PATH,
    JSON_HEADERS,
    _client,
    build_envelope,
    submit,
)
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.schema import EnvelopeAdapter
from flight_recorder.ledger.schema import events, outcome_attributions

__all__ = [
    "Item",
    "ItemResult",
    "Operation",
    "Schedule",
    "ScheduleDiverged",
    "SeedRefused",
    "SeedReport",
    "build_schedule",
    "check_schedule",
    "envelope_hash",
    "operation_envelope",
    "ordered_digest",
    "run_schedule",
]

ITEM_CANONICAL = "canonical"
ITEM_STAGE_1 = "stage_1"
ITEM_OPERATION = "operation"
ITEM_STAGE_2 = "stage_2"

OUTCOME_EVALUATED = "outcome.evaluated"


# --- Plain data -----------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """One scheduled attribution of one outcome version at `cutoff_1`."""

    outcome_event_id: str
    account_ref: str


@dataclass(frozen=True)
class Item:
    """One scheduled submission.

    `canonical_hash` and `body` are None for an operation: its content depends
    on the ledger prefix and is rebuilt when the prefix exists.
    """

    sequence: int
    kind: str
    event_id: str
    canonical_hash: str | None
    body: bytes | None
    operation: Operation | None


@dataclass(frozen=True)
class Schedule:
    canonical: tuple[bytes, ...]
    stage_1: tuple[dict, ...]
    operations: tuple[Operation, ...]
    stage_2: tuple[dict, ...]
    cutoff_1: int
    attribution_instant: datetime
    items: tuple[Item, ...]

    @property
    def scheduled_total(self) -> int:
        return len(self.items)


def envelope_hash(body: bytes) -> str:
    """The canonical hash the collector computes for `body`: strict validation,
    then the hash of the validated model's JSON dump, never of the raw bytes."""
    return canonical_hash(EnvelopeAdapter.validate_json(body, strict=True).model_dump(mode="json"))


def build_schedule(
    *,
    canonical: Sequence[bytes],
    stage_1: Sequence[dict],
    stage_2: Sequence[dict],
    attribution_instant: datetime,
) -> Schedule:
    """Derive operations, `cutoff_1` and the flattened items from the envelopes.

    Operations: the canonical outcome versions first (they precede stage 1 in
    the ledger), then every stage-1 outcome version in `stage_1` order. Stage 1
    holds no outcome supersession, so each of those versions is effective at
    `cutoff_1`.
    """
    canonical = tuple(canonical)
    stage_1 = tuple(stage_1)
    stage_2 = tuple(stage_2)
    cutoff_1 = len(canonical) + len(stage_1)

    operations: list[Operation] = []
    for body in canonical:
        envelope = json.loads(body)
        if envelope["event_type"] == OUTCOME_EVALUATED:
            operations.append(Operation(envelope["event_id"], envelope["account_ref"]))
    for envelope in stage_1:
        if envelope["event_type"] != OUTCOME_EVALUATED:
            continue
        if envelope["payload"].get("supersedes_outcome_event_id") is not None:
            raise ValueError(
                f"stage-1 outcome {envelope['event_id']!r} supersedes another version; stage 1 "
                "holds no supersession, so every stage-1 outcome version is effective at cutoff_1"
            )
        operations.append(Operation(envelope["event_id"], envelope["account_ref"]))

    items: list[Item] = []

    def add_envelope(kind: str, body: bytes, event_id: str) -> None:
        items.append(Item(len(items) + 1, kind, event_id, envelope_hash(body), body, None))

    for body in canonical:
        add_envelope(ITEM_CANONICAL, body, json.loads(body)["event_id"])
    for envelope in stage_1:
        add_envelope(ITEM_STAGE_1, canonical_bytes(envelope), envelope["event_id"])
    for operation in operations:
        identity = attribution_event_id(operation.outcome_event_id, POLICY_VERSION, cutoff_1)
        items.append(Item(len(items) + 1, ITEM_OPERATION, identity, None, None, operation))
    for envelope in stage_2:
        add_envelope(ITEM_STAGE_2, canonical_bytes(envelope), envelope["event_id"])

    return Schedule(
        canonical=canonical,
        stage_1=stage_1,
        operations=tuple(operations),
        stage_2=stage_2,
        cutoff_1=cutoff_1,
        attribution_instant=attribution_instant,
        items=tuple(items),
    )


# --- Failures -------------------------------------------------------------------


class ScheduleDiverged(Exception):
    """The ledger holds something other than the schedule; nothing was submitted.

    `where` is the diverging sequence (an `int`) or the `Operation` concerned;
    `sequence` is also set when an operation diverged at its scheduled sequence.
    """

    def __init__(self, where, expected, found, *, sequence: int | None = None):
        if isinstance(where, Operation):
            place = f"operation {where.outcome_event_id!r}"
            if sequence is not None:
                place += f" at sequence {sequence}"
        else:
            place = f"sequence {where}"
        super().__init__(f"schedule diverged at {place}: expected {expected!r}, found {found!r}")
        self.where = where
        self.expected = expected
        self.found = found
        self.operation = where if isinstance(where, Operation) else None
        self.sequence = where if isinstance(where, int) else sequence


class SeedRefused(Exception):
    """The collector answered an item with neither `201` nor `200 duplicate`."""

    def __init__(self, item: Item, status: int, body: dict, accepted: tuple = ()):
        super().__init__(
            f"collector refused scheduled item {item.sequence} ({item.event_id}): {status} {body}"
        )
        self.item = item
        self.status = status
        self.body = body
        #: The `ItemResult`s this invocation had accepted before the refusal.
        self.accepted = accepted


# --- The entry check (read-only) ----------------------------------------------------


def operation_envelope(conn, schedule: Schedule, operation: Operation) -> dict:
    """The envelope of one operation, rebuilt from the ledger prefix at `cutoff_1`."""
    result = attribute(
        conn, operation.outcome_event_id, cutoff=schedule.cutoff_1, policy_version=POLICY_VERSION
    )
    return build_envelope(
        result,
        account_ref=operation.account_ref,
        attributed_at=schedule.attribution_instant,
        recorded_at=schedule.attribution_instant,
    )


def check_schedule(conn, schedule: Schedule) -> None:
    """Raise `ScheduleDiverged` unless the ledger is a prefix of the schedule, or the
    complete schedule followed by later events. Reads only.

    Envelope items compare `(event_id, canonical_hash)` at their sequence. When the
    ledger holds the prefix through `cutoff_1`, each stored operation is compared
    with its envelope rebuilt over that prefix, and every result recorded for a
    scheduled outcome under the policy must be that operation's own identity.
    """
    maximum = ledger_maximum(conn) or 0
    limit = min(maximum, schedule.scheduled_total)
    stored = {
        row.ingest_sequence: (row.event_id, row.canonical_hash)
        for row in conn.execute(
            select(events.c.ingest_sequence, events.c.event_id, events.c.canonical_hash).where(
                events.c.ingest_sequence <= limit
            )
        )
    }
    for item in schedule.items[:limit]:
        found = stored.get(item.sequence)
        if item.operation is None:
            expected = (item.event_id, item.canonical_hash)
            if found != expected:
                raise ScheduleDiverged(item.sequence, expected, found)
            continue
        if found is None or found[0] != item.event_id:
            raise ScheduleDiverged(
                item.operation, (item.event_id, None), found, sequence=item.sequence
            )
        expected = (item.event_id, _rebuilt_hash(conn, schedule, item.operation))
        if found != expected:
            raise ScheduleDiverged(item.operation, expected, found, sequence=item.sequence)

    if maximum < schedule.cutoff_1:
        return
    for operation in schedule.operations:
        identity = attribution_event_id(
            operation.outcome_event_id, POLICY_VERSION, schedule.cutoff_1
        )
        for row in conn.execute(
            select(outcome_attributions.c.attribution_event_id).where(
                outcome_attributions.c.outcome_event_id == operation.outcome_event_id,
                outcome_attributions.c.policy_version == POLICY_VERSION,
            )
        ):
            if row.attribution_event_id != identity:
                raise ScheduleDiverged(operation, identity, row.attribution_event_id)


def _rebuilt_hash(conn, schedule: Schedule, operation: Operation) -> str:
    try:
        envelope = operation_envelope(conn, schedule, operation)
    except AttributionError as error:  # the prefix cannot produce the scheduled operation
        raise ScheduleDiverged(operation, "a policy result at cutoff_1", str(error)) from error
    return envelope_hash(canonical_bytes(envelope))


# --- The run ------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemResult:
    item: Item
    http_status: int
    status: str


@dataclass(frozen=True)
class SeedReport:
    results: tuple[ItemResult, ...]
    events_total: int
    scheduled_total: int
    digest: str
    #: The ledger holds exactly the schedule, every event at its scheduled sequence.
    fresh: bool

    @property
    def created(self) -> int:
        return sum(1 for result in self.results if result.status == "created")

    @property
    def duplicate(self) -> int:
        return sum(1 for result in self.results if result.status == "duplicate")


def ordered_digest(conn, *, signals, comparison_workflow_version: str) -> str:
    """The ordered logical digest of the ledger (see the module docstring)."""
    # Imported here so that importing the schedule (as the generator does) does
    # not import the analytics engine.
    from flight_recorder.analytics.insights import insights

    digest = hashlib.sha256()
    for row in conn.execute(
        select(events.c.event_id, events.c.canonical_hash).order_by(events.c.ingest_sequence)
    ):
        digest.update(f"{row.event_id} {row.canonical_hash}\n".encode())
    aggregates = insights(
        conn,
        ledger_maximum(conn),
        signals=signals,
        comparison_workflow_version=comparison_workflow_version,
    )
    digest.update(canonical_bytes(aggregates.as_dict()))
    return digest.hexdigest()


async def _post(client: httpx.AsyncClient, body: bytes) -> tuple[int, dict]:
    """Post envelope bytes exactly as scheduled, over the same route `submit` uses."""
    response = await client.post(COLLECTOR_PATH, content=body, headers=JSON_HEADERS)
    try:
        payload = response.json()
    except ValueError:
        payload = {"status": "error", "detail": response.text}
    return response.status_code, payload


def _accepted(status: int, body: dict) -> bool:
    return (status, body.get("status")) in {(201, "created"), (200, "duplicate")}


async def _perform(app, schedule: Schedule, stop_after: int | None) -> list[ItemResult]:
    engine = app.state.engine
    # Reads close before any submission, so a collector write never waits on them.
    with engine.connect() as conn:
        entry_maximum = ledger_maximum(conn) or 0
        check_schedule(conn, schedule)
    operations_verified = entry_maximum >= schedule.cutoff_1

    results: list[ItemResult] = []
    async with _client(app) as client:
        for item in schedule.items:
            if stop_after is not None and len(results) >= stop_after:
                break
            if item.operation is None:
                status, body = await _post(client, item.body)
            else:
                if not operations_verified:
                    with engine.connect() as conn:
                        check_schedule(conn, schedule)
                    operations_verified = True
                with engine.connect() as conn:
                    envelope = operation_envelope(conn, schedule, item.operation)
                status, body = await submit(client, envelope)
            if not _accepted(status, body):
                raise SeedRefused(item, status, body, accepted=tuple(results))
            results.append(ItemResult(item, status, body["status"]))
    return results


def run_schedule(
    app,
    schedule: Schedule,
    *,
    signals,
    comparison_workflow_version: str,
    stop_after: int | None = None,
) -> SeedReport:
    """Perform the schedule's items in order and report.

    `signals` and `comparison_workflow_version` are the descriptive inputs of
    the digest's aggregates, passed in by the caller. `stop_after` is for tests
    only: stop after that many items, each performed normally; the report then
    describes an incomplete run and is never fresh.
    """
    results = asyncio.run(_perform(app, schedule, stop_after))
    with app.state.engine.connect() as conn:
        events_total = conn.execute(select(func.count()).select_from(events)).scalar_one()
        digest = ordered_digest(
            conn, signals=signals, comparison_workflow_version=comparison_workflow_version
        )
        fresh = events_total == schedule.scheduled_total and _matches(conn, schedule)
    return SeedReport(
        results=tuple(results),
        events_total=events_total,
        scheduled_total=schedule.scheduled_total,
        digest=digest,
        fresh=fresh,
    )


def _matches(conn, schedule: Schedule) -> bool:
    try:
        check_schedule(conn, schedule)
    except ScheduleDiverged:
        return False
    return True
