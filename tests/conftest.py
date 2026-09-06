"""Shared fixtures: temp SQLite per test, in-process client, canonical fixture access."""

import json
import os
import uuid
import warnings
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import event, func, select

from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.fixtures import canonical_envelope_paths, load_json, logic_artifact_path
from flight_recorder.ledger.database import reset_database
from flight_recorder.ledger.schema import (
    PROJECTION_TABLES,
    SYSTEM_ACCOUNT_REF,
    accounts,
    events,
)
from flight_recorder.logic.evaluator import ContextInput
from flight_recorder.logic.rules import parse_boundary
from flight_recorder.replay.reconstruct import ConsumedInputRow

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated",
)
from fastapi.testclient import TestClient  # noqa: E402

from flight_recorder.app import create_app  # noqa: E402

# Hypothesis profiles: modest locally, larger in CI (HYPOTHESIS_PROFILE=ci).
settings.register_profile(
    "default",
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

COLLECTOR_URL = "/api/v1/decision-events"
JSON_HEADERS = {"content-type": "application/json"}


class Harness:
    """A fresh SQLite file, its engine, and an in-process client."""

    def __init__(self, directory: Path, raise_server_exceptions: bool = True):
        self.db_path = directory / f"{uuid.uuid4().hex}.db"
        self.engine = reset_database(self.db_path)
        self.app = create_app(self.db_path)
        self.collector = self.app.state.collector
        self.client = TestClient(self.app, raise_server_exceptions=raise_server_exceptions)

    def post_raw(self, body: bytes | str):
        if isinstance(body, str):
            body = body.encode("utf-8")
        return self.client.post(COLLECTOR_URL, content=body, headers=JSON_HEADERS)

    def post(self, envelope: dict):
        return self.post_raw(json.dumps(envelope))

    def event_count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(events)).scalar_one()

    def account_rows(self) -> list[tuple]:
        with self.engine.connect() as conn:
            query = select(accounts).order_by(accounts.c.account_ref)
            return [tuple(r) for r in conn.execute(query)]

    def projection_rows(self) -> dict[str, list[tuple]]:
        """Every row of every projection table, keyed by table name."""
        with self.engine.connect() as conn:
            return {
                table.name: [
                    tuple(r)
                    for r in conn.execute(select(table).order_by(*table.primary_key.columns))
                ]
                for table in PROJECTION_TABLES
            }

    def snapshot(self) -> tuple[int, list[tuple], dict[str, list[tuple]]]:
        return self.event_count(), self.account_rows(), self.projection_rows()

    def is_empty(self) -> bool:
        """No event, no account, and no projected row anywhere."""
        count, account_rows, projections = self.snapshot()
        return count == 0 and account_rows == [] and not any(projections.values())


@pytest.fixture
def harness(tmp_path) -> Harness:
    return Harness(tmp_path)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return harness.client


def canonical_envelopes() -> list[dict]:
    """All canonical envelopes, including the two `_system` registrations."""
    return [load_json(p) for p in canonical_envelope_paths()]


def account_envelope_paths() -> list[Path]:
    """The canonical envelopes belonging to the NovaSignal AI account."""
    return [
        p for p in canonical_envelope_paths() if load_json(p)["account_ref"] != SYSTEM_ACCOUNT_REF
    ]


def system_envelope_paths() -> list[Path]:
    """The canonical envelopes submitted under the `_system` principal."""
    return [
        p for p in canonical_envelope_paths() if load_json(p)["account_ref"] == SYSTEM_ACCOUNT_REF
    ]


def account_envelopes() -> list[dict]:
    return [load_json(p) for p in account_envelope_paths()]


def canonical_by_type(event_type: str) -> dict:
    return next(e for e in canonical_envelopes() if e["event_type"] == event_type)


def canonical_raw(index: int) -> bytes:
    """Raw bytes of the index-th *account* envelope (0 = account.discovered)."""
    return account_envelope_paths()[index].read_bytes()


def system_raw(index: int) -> bytes:
    """Raw bytes of the index-th `_system` envelope (0 = v3.2, 1 = v5.1)."""
    return system_envelope_paths()[index].read_bytes()


def register_artifacts(harness: "Harness") -> None:
    """Submit both logic-artifact registrations; prerequisite for any decision."""
    for path in system_envelope_paths():
        assert harness.post_raw(path.read_bytes()).status_code == 201


TEST_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def local_fixture(name: str) -> dict:
    """A non-canonical fixture used by one test file, not part of the demo seed."""
    return load_json(TEST_FIXTURES_DIR / name)


def logic_artifact(version: str) -> dict:
    return load_json(logic_artifact_path(version))


def seed_all(harness: Harness) -> list:
    return [harness.post_raw(p.read_bytes()) for p in canonical_envelope_paths()]


def reversed_keys(obj):
    """Recursively reverse dict key order; a genuinely different raw layout."""
    if isinstance(obj, dict):
        return {k: reversed_keys(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [reversed_keys(v) for v in obj]
    return obj


def reformatted(envelope: dict) -> bytes:
    """Same content, reversed keys, indented with tabs, trailing newline."""
    return (json.dumps(reversed_keys(envelope), indent="\t") + "\n").encode("utf-8")


# --- Phase 2B: evaluator, reconstruction ------------------------------------

DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"


def logic_artifact_model(version: str = "v3.2") -> LogicArtifact:
    """The canonical artifact of `version`, through the strict schema-v1 model."""
    return LogicArtifact.model_validate(logic_artifact(version))


def canonical_boundary() -> datetime:
    """`T(d)` of the canonical decision, from the fixture."""
    return parse_boundary(canonical_by_type("decision.recorded")["payload"]["decision_boundary"])


def canonical_observed_at() -> dict[str, date]:
    """`observed_at` per evidence version id, from the canonical evidence events.

    The dates a temporal rule reads come from the evidence versions, never from
    the decision payload, so the tests source them the same way.
    """
    return {
        item["evidence_version_id"]: date.fromisoformat(item["observed_at"])
        for envelope in canonical_envelopes()
        if envelope["event_type"] == "evidence.recorded"
        for item in envelope["payload"]["items"]
        if "observed_at" in item
    }


def canonical_context() -> tuple[ContextInput, ...]:
    """`H(d)` of the canonical decision, hand-built from the fixtures.

    Ordered by input key, matching how the reconstruction loads it.
    """
    observed = canonical_observed_at()
    entries = canonical_by_type("decision.recorded")["payload"]["historical_context"]
    return tuple(
        ContextInput(
            key=entry["input_key"],
            availability=entry["availability"],
            value=entry["value"],
            evidence_version_id=entry.get("evidence_version_id"),
            observed_at=observed.get(entry.get("evidence_version_id")),
        )
        for entry in sorted(entries, key=lambda e: e["input_key"])
    )


def canonical_consumed_rows() -> tuple[ConsumedInputRow, ...]:
    """`U(d)` of the canonical decision as stored comparison rows."""
    consumed = canonical_by_type("decision.recorded")["payload"]["consumed_inputs"]
    return tuple(
        ConsumedInputRow(
            input_key=used["input_key"],
            evidence_version_id=used["evidence_version_id"],
            contribution=used["contribution"],
        )
        for used in sorted(consumed, key=lambda u: u["input_key"])
    )


def replace_context(
    context: tuple[ContextInput, ...], key: str, replacement: ContextInput | None
) -> tuple[ContextInput, ...]:
    """`context` with `key` replaced, or dropped entirely when `replacement` is None."""
    return tuple(
        replacement if entry.key == key else entry
        for entry in context
        if replacement is not None or entry.key != key
    )


def evidence_envelope(
    event_id: str,
    items: list[dict],
    *,
    occurred_at: str,
    account_ref: str = "novasignal-ai",
    source: str = "clay-sim-later",
) -> dict:
    """A well-formed `evidence.recorded` envelope. Test-only, never canonical."""
    return {
        "schema_version": "1",
        "event_id": event_id,
        "event_type": "evidence.recorded",
        "source": source,
        "account_ref": account_ref,
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "payload": {"items": items},
    }


@contextmanager
def captured_statements(engine):
    """Every SQL statement the engine executes inside the block.

    Used to prove that reconstruction writes nothing and reads no `events` or
    `accounts` row (INV-01, INV-02).
    """
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)
