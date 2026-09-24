"""The public, read-only demo application (D-018).

A separate factory, never a mode of `create_app`: the local application and
its collector are not gated, flagged or weakened by anything here.

    FLIGHT_RECORDER_DEMO_DB=/path/demo.db \\
        uvicorn flight_recorder.public_demo:create_public_demo --factory

The snapshot is built beforehand by `flight-recorder build-demo-snapshot`,
through the ordinary collector, and served read-only. Read-only is enforced
twice and each layer stands on its own:

- **Before routing**, every method other than `GET` and `HEAD` answers `405`
  with `Allow: GET, HEAD` (`ReadOnlyMethods`).
- **At SQLite**, the file is opened with URI `mode=ro`, and every connection
  also sets `PRAGMA query_only = ON`. `mode=ro` makes the database file
  unwritable; `query_only` additionally refuses statements that would write
  elsewhere, such as a `TEMP` table, which `mode=ro` alone admits.
  `immutable=1` is deliberately not used: it tells SQLite to skip locking and
  change detection, so a file altered underneath the process would be read
  without complaint.

No collector router is included and no collector instance is created, so the
public application has no write path at any layer.

**Admission.** The factory refuses to start unless the snapshot is exactly the
release snapshot: the canonical decision is present, exactly one registered
artifact is labelled `v5.2` and carries its pinned hash, and the snapshot's
content identity equals `PINNED_CONTENT_IDENTITY`. Each failure is a named
`SnapshotRefused` subclass. Admission only reads.

**Two identities, never conflated.** The *content identity* defined here is a
SHA-256 over every row and column of the eleven domain tables, SQLite's
`sqlite_sequence`, and the schema recorded in `sqlite_master`; it is what
admission and `/healthz` rely on. The *schedule digest* is the dataset's
existing `ordered_digest` (event ids, stored canonical hashes and Insights
aggregates): reproducibility evidence, reported beside it and always labelled
separately, never used in its place.
"""

import hashlib
import os
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError

from flight_recorder.app import STATIC_DIR
from flight_recorder.ledger.database import make_engine
from flight_recorder.web.routes import router as web_router

#: The only place the public snapshot path comes from, apart from an explicit
#: argument. `FLIGHT_RECORDER_DB` and the local default are never consulted.
DEMO_DB_ENV_VAR = "FLIGHT_RECORDER_DEMO_DB"

SOURCE_REPOSITORY_URL = "https://github.com/e-skora/flight-recorder"

#: The canonical decision the demo is built around.
CANONICAL_DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"

#: The demo's default replay logic (D-017) and its registered identity.
CURRENT_LOGIC_VERSION = "v5.2"
CURRENT_LOGIC_HASH = "cbefd0508d9c999de299b8ed7a5d60f38b746dfc81520e798bd6c25515e0b889"

#: The content identity of the release snapshot, pinned after two independent
#: clean builds of `flight-recorder build-demo-snapshot` agreed on it.
PINNED_CONTENT_IDENTITY = "ad3e8f376182421baf81f8fdc24d91c33c17fbef3e9e57ec8c0dd3cff2f42217"

ALLOWED_METHODS = ("GET", "HEAD")

# --- The content identity -------------------------------------------------------------

#: Every table the identity covers, in a fixed order, each with its columns in a
#: fixed order and the columns its rows are ordered by (the primary key, or
#: every column where the table has none). The tables are listed here rather
#: than read from the ledger's metadata, so a later schema change fails
#: admission loudly instead of silently changing what is hashed.
IDENTITY_TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "accounts",
        ("account_ref", "name", "domain", "first_seen_event_id"),
        ("account_ref",),
    ),
    (
        "events",
        (
            "ingest_sequence",
            "event_id",
            "schema_version",
            "event_type",
            "source",
            "account_ref",
            "occurred_at",
            "recorded_at",
            "canonical_hash",
            "payload",
        ),
        ("ingest_sequence",),
    ),
    (
        "evidence_versions",
        (
            "evidence_version_id",
            "account_ref",
            "evidence_type",
            "value_json",
            "source",
            "observed_at",
            "available_at",
            "source_event_id",
            "supersedes_evidence_version_id",
        ),
        ("evidence_version_id",),
    ),
    (
        "logic_artifacts",
        (
            "artifact_hash",
            "artifact_id",
            "logic_version",
            "decision_class",
            "artifact_schema_version",
            "evaluator_version",
            "artifact_json",
            "source_event_id",
        ),
        ("artifact_hash",),
    ),
    (
        "decisions",
        (
            "decision_event_id",
            "account_ref",
            "decision_class",
            "decision_boundary",
            "workflow_version",
            "artifact_hash",
            "evaluator_version",
            "logic_version",
            "score",
            "threshold",
            "output",
            "explanation",
            "ingest_sequence",
        ),
        ("decision_event_id",),
    ),
    (
        "decision_context",
        ("decision_event_id", "input_key", "availability", "value_text", "evidence_version_id"),
        ("decision_event_id", "input_key"),
    ),
    (
        "decision_consumed_inputs",
        ("decision_event_id", "input_key", "value_text", "evidence_version_id", "contribution"),
        ("decision_event_id", "input_key"),
    ),
    (
        "persona_selections",
        ("event_id", "account_ref", "decision_event_id", "persona", "explanation"),
        ("event_id",),
    ),
    (
        "actions",
        (
            "action_event_id",
            "account_ref",
            "decision_event_id",
            "action_type",
            "play_id",
            "target_persona",
            "status",
            "cost",
            "currency",
            "occurred_at",
        ),
        ("action_event_id",),
    ),
    (
        "outcomes",
        (
            "outcome_event_id",
            "account_ref",
            "action_event_id",
            "window_days",
            "reply",
            "meeting",
            "opportunity",
            "occurred_at",
            "recorded_at",
            "schema_version",
            "window_opened_at",
            "window_closes_at",
            "evaluation_state",
            "observed_at",
            "source_action_event_id",
            "source_action_unusable_reason",
            "source_decision_event_id",
            "source_decision_unusable_reason",
            "supersedes_outcome_event_id",
        ),
        ("outcome_event_id",),
    ),
    (
        "outcome_attributions",
        (
            "attribution_event_id",
            "account_ref",
            "source_event_id",
            "outcome_event_id",
            "policy_version",
            "method",
            "window_days",
            "resolved_action_event_id",
            "resolved_decision_event_id",
            "status",
            "reason",
            "attributed_at",
            "ingest_cutoff",
            "supersedes_attribution_event_id",
        ),
        ("attribution_event_id",),
    ),
    # SQLite's own AUTOINCREMENT bookkeeping: `events` is declared with
    # `sqlite_autoincrement=True`, so every snapshot carries this table. It
    # has no primary key; its rows are ordered by every column.
    ("sqlite_sequence", ("name", "seq"), ("name", "seq")),
)

IDENTITY_TABLE_NAMES = frozenset(name for name, _, _ in IDENTITY_TABLES)

#: The schema definitions recorded in `sqlite_master`, hashed after the rows.
SCHEMA_COLUMNS = ("type", "name", "tbl_name", "sql")


def _encode(value) -> bytes:
    """One stored value, typed and length-prefixed, so `NULL`, integer, real,
    text and blob stay distinct and no two different rows serialize alike."""
    if value is None:
        return b"N;"
    if isinstance(value, bool):  # never produced by the driver; refused, not coerced
        raise TypeError("a boolean is not a stored SQLite value")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b";"
    if isinstance(value, float):
        return b"r" + value.hex().encode("ascii") + b";"
    if isinstance(value, str):
        data = value.encode("utf-8")
        return b"t" + str(len(data)).encode("ascii") + b":" + data + b";"
    if isinstance(value, bytes):
        return b"b" + str(len(value)).encode("ascii") + b":" + value + b";"
    raise TypeError(f"unexpected stored value type {type(value).__name__}")


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _hash_rows(digest, label: str, rows: Iterable[tuple]) -> None:
    digest.update(_encode(label))
    count = 0
    for row in rows:
        digest.update(b"(")
        for value in row:
            digest.update(_encode(value))
        digest.update(b")")
        count += 1
    digest.update(b"#" + str(count).encode("ascii") + b";")


def _table_names(conn: Connection) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def content_identity(conn: Connection) -> str:
    """The snapshot's content identity. Reads only.

    Raises `SnapshotTablesMismatch` when the database holds a table outside
    `IDENTITY_TABLES` or lacks one in it, and `SnapshotColumnsMismatch` when a
    table's columns differ from the declared list, so nothing stored is ever
    silently left out of the hash. Values are read as the driver returns them
    and hashed as stored; nothing is recomputed.
    """
    present = _table_names(conn)
    unexpected = sorted(present - IDENTITY_TABLE_NAMES)
    missing = sorted(IDENTITY_TABLE_NAMES - present)
    if unexpected or missing:
        raise SnapshotTablesMismatch(unexpected=unexpected, missing=missing)

    digest = hashlib.sha256()
    digest.update(b"flight-recorder public demo content identity v1;")
    for name, columns, order_by in IDENTITY_TABLES:
        stored = tuple(
            row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({_quoted(name)})")
        )
        if stored != columns:
            raise SnapshotColumnsMismatch(name, declared=columns, stored=stored)
        rows = conn.exec_driver_sql(
            f"SELECT {', '.join(map(_quoted, columns))} FROM {_quoted(name)} "
            f"ORDER BY {', '.join(map(_quoted, order_by))}"
        )
        _hash_rows(digest, f"table {name} ({', '.join(columns)})", rows)
    schema = conn.exec_driver_sql(
        f"SELECT {', '.join(SCHEMA_COLUMNS)} FROM sqlite_master "
        f"ORDER BY {', '.join(SCHEMA_COLUMNS)}"
    )
    _hash_rows(digest, f"sqlite_master ({', '.join(SCHEMA_COLUMNS)})", schema)
    return digest.hexdigest()


def schedule_digest(conn: Connection) -> str:
    """The dataset's existing `ordered_digest` over this database, unchanged:
    reproducibility evidence, never the admission identity."""
    from flight_recorder.dataset.schedule import ordered_digest
    from flight_recorder.fixtures import dataset_comparison_workflow_version, dataset_signals

    return ordered_digest(
        conn,
        signals=dataset_signals(),
        comparison_workflow_version=dataset_comparison_workflow_version(),
    )


def event_count(conn: Connection) -> int:
    return conn.exec_driver_sql("SELECT count(*) FROM events").scalar_one()


# --- Named refusals -------------------------------------------------------------------


class SnapshotRefused(RuntimeError):
    """The public demo refuses to start. Every subclass names one reason."""


class SnapshotPathUnset(SnapshotRefused):
    def __init__(self):
        super().__init__(
            f"{DEMO_DB_ENV_VAR} is not set; the public demo serves only a snapshot built by "
            "`flight-recorder build-demo-snapshot` and never falls back to FLIGHT_RECORDER_DB "
            "or the local default"
        )


class SnapshotMissing(SnapshotRefused):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"snapshot file not found: {path}")


class SnapshotNotSQLite(SnapshotRefused):
    def __init__(self, path: Path, detail: str):
        self.path = path
        super().__init__(f"snapshot is not a SQLite database: {path} ({detail})")


class SnapshotEmpty(SnapshotRefused):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"snapshot is an empty database with no tables: {path}")


class CanonicalDecisionMissing(SnapshotRefused):
    def __init__(self, detail: str):
        super().__init__(
            f"snapshot lacks the canonical decision {CANONICAL_DECISION_EVENT_ID}: {detail}"
        )


class CurrentLogicMismatch(SnapshotRefused):
    def __init__(self, detail: str):
        super().__init__(
            f"snapshot does not hold exactly one {CURRENT_LOGIC_VERSION} artifact with hash "
            f"{CURRENT_LOGIC_HASH}: {detail}"
        )


class ContentIdentityMismatch(SnapshotRefused):
    """The snapshot's content is not the pinned release snapshot's content."""

    def __init__(self, message: str | None = None, *, computed: str | None = None):
        self.computed = computed
        super().__init__(
            message
            or f"snapshot content identity {computed} is not the pinned release identity "
            f"{PINNED_CONTENT_IDENTITY}"
        )


class SnapshotTablesMismatch(ContentIdentityMismatch):
    def __init__(self, *, unexpected: list[str], missing: list[str]):
        self.unexpected = unexpected
        self.missing = missing
        super().__init__(
            "snapshot content identity refused: the table set is not the declared set "
            f"(unexpected {unexpected or 'none'}; missing {missing or 'none'})"
        )


class SnapshotColumnsMismatch(ContentIdentityMismatch):
    def __init__(self, table: str, *, declared: tuple[str, ...], stored: tuple[str, ...]):
        self.table = table
        super().__init__(
            f"snapshot content identity refused: table {table} has columns {list(stored)}, "
            f"declared {list(declared)}"
        )


# --- Opening and admission ------------------------------------------------------------


def _snapshot_path(snapshot_path: Path | str | None) -> Path:
    raw = snapshot_path if snapshot_path is not None else os.environ.get(DEMO_DB_ENV_VAR)
    if raw is None or str(raw) == "":
        raise SnapshotPathUnset()
    path = Path(raw)
    if not path.is_file():
        raise SnapshotMissing(path)
    return path.resolve(strict=True)


def open_read_only(path: Path) -> Engine:
    """An engine over the existing file that cannot write through SQLite.

    `mode=ro` also means a missing file is an error rather than a new empty
    database.
    """
    engine = make_engine(f"{path.as_uri()}?mode=ro&uri=true")

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only = ON")
        cursor.close()

    return engine


def admit(conn: Connection, path: Path) -> str:
    """Admit the snapshot or raise a named refusal; returns its content identity.

    Order: a SQLite database that holds tables, then the canonical decision,
    then exactly one `v5.2` artifact at its pinned hash, then the content
    identity. Reads only.
    """
    try:
        tables = _table_names(conn)
    except DatabaseError as error:
        if isinstance(error.orig, sqlite3.DatabaseError):
            raise SnapshotNotSQLite(path, str(error.orig)) from error
        raise
    if not tables:
        raise SnapshotEmpty(path)

    if "decisions" not in tables:
        raise CanonicalDecisionMissing("the database has no decisions table")
    found = conn.exec_driver_sql(
        "SELECT count(*) FROM decisions WHERE decision_event_id = ?",
        (CANONICAL_DECISION_EVENT_ID,),
    ).scalar_one()
    if found != 1:
        raise CanonicalDecisionMissing("no decision row carries that id")

    if "logic_artifacts" not in tables:
        raise CurrentLogicMismatch("the database has no logic_artifacts table")
    hashes = [
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT artifact_hash FROM logic_artifacts WHERE logic_version = ? "
            "ORDER BY artifact_hash",
            (CURRENT_LOGIC_VERSION,),
        )
    ]
    if hashes != [CURRENT_LOGIC_HASH]:
        raise CurrentLogicMismatch(f"found {hashes or 'none'}")

    identity = content_identity(conn)
    if identity != PINNED_CONTENT_IDENTITY:
        raise ContentIdentityMismatch(computed=identity)
    return identity


# --- The method guard -----------------------------------------------------------------


class ReadOnlyMethods:
    """Pure ASGI middleware: anything but GET and HEAD is refused before routing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ALLOWED_METHODS:
            response = JSONResponse(
                {
                    "detail": (
                        "This public demo is read-only. Only GET and HEAD are served; the "
                        "writable collector runs locally from the source repository."
                    ),
                    "source": SOURCE_REPOSITORY_URL,
                },
                status_code=405,
                headers={"Allow": ", ".join(ALLOWED_METHODS)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --- The factory ----------------------------------------------------------------------


def create_public_demo(snapshot_path: Path | str | None = None) -> FastAPI:
    """The public read-only application over an admitted release snapshot."""
    path = _snapshot_path(snapshot_path)
    engine = open_read_only(path)
    try:
        with engine.connect() as conn:
            identity = admit(conn, path)
            health = {
                "status": "ok",
                "read_only": True,
                "synthetic": True,
                "event_count": event_count(conn),
                "content_identity": identity,
                "schedule_digest": schedule_digest(conn),
                "schedule_digest_note": (
                    "reproducibility evidence over event ids, stored hashes and Insights "
                    "aggregates; not the admission identity"
                ),
            }
    except BaseException:
        engine.dispose()
        raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="GTM Flight Recorder (public read-only demo, synthetic data)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.public_demo = True
    app.state.source_repository_url = SOURCE_REPOSITORY_URL

    def healthz() -> JSONResponse:
        return JSONResponse(health)

    app.add_api_route("/healthz", healthz, methods=list(ALLOWED_METHODS), include_in_schema=False)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(ReadOnlyMethods)
    return app
