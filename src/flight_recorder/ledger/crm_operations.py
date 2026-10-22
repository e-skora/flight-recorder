"""Explicit empty-domain CRM operation projection; never a retained-file migration."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.schema import CreateTable

from flight_recorder.adapters.crm.contracts import require
from flight_recorder.ledger.crm_schema import _normalized, _triggers, metadata
from flight_recorder.ledger.crm_schema import schema_ready as capture_schema_ready
from flight_recorder.ledger.schema import events

crm_operation_observations = Table(
    "crm_operation_observations",
    metadata,
    Column("event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("operation_id", String, nullable=False),
    Column("observation_sequence", Integer, nullable=False),
    Column("target_key", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column(
        "decision_event_id", String, ForeignKey("crm_decisions.decision_event_id"), nullable=False
    ),
    Column("capture_hash", String, ForeignKey("crm_captures.capture_hash"), nullable=False),
    Column("state", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("observed_at", String, nullable=False),
    Column("intent_json", String, nullable=False),
    Column("ingest_sequence", Integer, nullable=False),
    UniqueConstraint("operation_id", "observation_sequence"),
    CheckConstraint("observation_sequence>0 AND revision>0"),
    sqlite_with_rowid=False,
)


def schema_ready(conn):
    if not capture_schema_ready(conn):
        return False
    table = crm_operation_observations
    expected = {table.name: _normalized(str(CreateTable(table).compile(dialect=conn.dialect)))}
    expected.update({name: _normalized(sql) for name, sql in _triggers(table).items()})
    actual = {
        name: _normalized(sql)
        for name, sql in conn.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger')"
        )
        if name in expected
    }
    return actual == expected


def create_crm_operation_schema(engine):
    """Only explicitly initialized but empty new CRM domains; no ingestion side effect."""
    with engine.begin() as conn:
        names = {
            r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        require(
            capture_schema_ready(conn)
            and crm_operation_observations.name not in names
            and conn.execute(select(events.c.event_id).limit(1)).first() is None,
            "crm_operation_schema_initialization_refused",
        )
        crm_operation_observations.create(conn)
        for statement in _triggers(crm_operation_observations).values():
            conn.exec_driver_sql(statement)
        require(schema_ready(conn), "crm_operation_schema_initialization_refused")
