"""Shared fixtures for the public, read-only demo (D-018 piece 1).

One release snapshot is built per session by `flight-recorder build-demo-snapshot`,
exactly as the host's start command builds it: through the ordinary collector,
in the supported order. No test reads or writes that file directly: every test
works on its own byte copy (`owned_copy`), so a refused or altered copy can
never disturb another test, and nothing here touches `fixtures/`.

`full_state` is the existing `ledger_state` pattern (every account row, every
projection row, every event row, never an event count), widened to every table
the snapshot holds plus `sqlite_master`, and read through a separate read-only
SQLite connection so observing the state can never change it.
"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from flight_recorder.cli import cmd_build_demo_snapshot

DECISION_URL = "/accounts/novasignal-ai/decisions/evt-novasignal-04-decision-recorded"
DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"
V5_1_HASH = "b5a6f33dd592fe804d9b59e081639c6527a9c4ca2727cb6f850c6888b3ea7a3b"
V5_2_HASH = "cbefd0508d9c999de299b8ed7a5d60f38b746dfc81520e798bd6c25515e0b889"
SOURCE_URL = "https://github.com/e-skora/flight-recorder"

#: The content identity two independent clean builds agreed on, pinned here
#: independently of the production constant so a changed pin is caught too.
EXPECTED_CONTENT_IDENTITY = "ad3e8f376182421baf81f8fdc24d91c33c17fbef3e9e57ec8c0dd3cff2f42217"
#: The fresh-seed schedule digest (D-014), unchanged by this work.
FRESH_SEED_DIGEST = "530f992c3f08f2c01f4e1a5bcc84b05fcc47660c14e51543efa40b2fe41c35ea"
#: The dataset's 1,596 scheduled items plus the one `v5.2` overlay event.
SNAPSHOT_EVENT_COUNT = 1597


@pytest.fixture(scope="session")
def built_snapshot(tmp_path_factory) -> Path:
    """The session's release snapshot, built by the command itself. Never
    opened directly by a test; copy it with `owned_copy`."""
    out = tmp_path_factory.mktemp("release-snapshot") / "demo.db"
    assert cmd_build_demo_snapshot(out) == 0
    return out


def owned_copy(source: Path, directory: Path, name: str = "demo.db") -> Path:
    """A byte copy of `source` that belongs to one test (or one module)."""
    target = directory / name
    shutil.copyfile(source, target)
    assert file_sha256(target) == file_sha256(source)
    return target


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full_state(path: Path) -> dict:
    """Every row of every table and every schema entry, read without writing."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        state = {
            name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for name in tables
        }
        state["sqlite_master"] = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()
    return state


def assert_unchanged(path: Path, state: dict, sha256: str) -> None:
    assert full_state(path) == state, "the snapshot's stored state changed"
    assert file_sha256(path) == sha256, "the snapshot file's bytes changed"
