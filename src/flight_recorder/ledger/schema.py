"""Ledger tables for Phase 1: `accounts` and the append-only `events` table.

SQLAlchemy Core is used (a reversible in-task choice; see DECISIONS.md Open).
The schema is created from this metadata by `flight-recorder reset`.
"""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

accounts = Table(
    "accounts",
    metadata,
    Column("account_ref", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("domain", String, nullable=False),
    # Points at the discovery event, which is written in the same transaction
    # as the account row; the deferred check runs at COMMIT.
    Column(
        "first_seen_event_id",
        String,
        ForeignKey("events.event_id", deferrable=True, initially="DEFERRED"),
        nullable=False,
    ),
)

events = Table(
    "events",
    metadata,
    # INV-02 tie-breaker for same-instant ordering.
    Column("ingest_sequence", Integer, primary_key=True, autoincrement=True),
    # The stable logical identity (INV-11), not the database primary key.
    Column("event_id", String, nullable=False, unique=True),
    Column("schema_version", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("source", String, nullable=False),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("canonical_hash", String, nullable=False),
    Column("payload", Text, nullable=False),
    sqlite_autoincrement=True,
)

# INV-01: the events table is append-only, enforced at the database.
APPEND_ONLY_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'INV-01: events are append-only; UPDATE is not allowed');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'INV-01: events are append-only; DELETE is not allowed');
    END
    """,
)


def create_schema(engine: Engine) -> None:
    """Create both tables and the append-only triggers.

    Tables are created explicitly in a fixed order because the two foreign keys
    form a cycle; SQLite accepts a forward reference in CREATE TABLE.
    """
    conn: Connection
    with engine.begin() as conn:
        accounts.create(conn, checkfirst=True)
        events.create(conn, checkfirst=True)
        for ddl in APPEND_ONLY_TRIGGERS:
            conn.exec_driver_sql(ddl)
