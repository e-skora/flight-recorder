"""SQLite engine construction with the connection rules the ledger relies on.

- `PRAGMA foreign_keys = ON` on every connection (foreign keys are enforced).
- Explicit transaction control so deferred foreign-key checks run at COMMIT
  and every collector write is genuinely one transaction.
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

DEFAULT_DB_PATH = "flight_recorder.db"
DB_ENV_VAR = "FLIGHT_RECORDER_DB"


def db_path_from_env() -> Path:
    return Path(os.environ.get(DB_ENV_VAR, DEFAULT_DB_PATH))


def make_engine(db_path: Path | str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        # Let SQLAlchemy own transaction boundaries instead of pysqlite's
        # implicit BEGIN, so PRAGMA and deferred constraints behave.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN")

    return engine


def reset_database(db_path: Path | str) -> Engine:
    """Delete the SQLite file (if any) and recreate the schema."""
    from flight_recorder.ledger.schema import create_schema

    path = Path(db_path)
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    engine = make_engine(path)
    create_schema(engine)
    return engine
