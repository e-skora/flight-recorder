"""Shared fixtures: temp SQLite per test, in-process client, canonical fixture access."""

import json
import os
import uuid
import warnings
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import func, select

from flight_recorder.fixtures import canonical_envelope_paths, load_json, logic_artifact_path
from flight_recorder.ledger.database import reset_database
from flight_recorder.ledger.schema import (
    PROJECTION_TABLES,
    SYSTEM_ACCOUNT_REF,
    accounts,
    events,
)

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
