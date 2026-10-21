"""Explicit live-only projections; absent from default/public metadata creation."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.schema import CreateTable

from flight_recorder.adapters.crm.contracts import require
from flight_recorder.ledger.schema import accounts, events, logic_artifacts

metadata = MetaData()
for _base in (accounts, events, logic_artifacts):
    _base.to_metadata(metadata)

crm_captures = Table(
    "crm_captures",
    metadata,
    Column("capture_hash", String, primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("unit_id", String, nullable=False, unique=True),
    Column("capture_sequence", Integer, nullable=False),
    Column("target_key", String, nullable=False),
    Column("body_json", String, nullable=False),
    Column("source_event_id", String, ForeignKey("events.event_id"), nullable=False),
    UniqueConstraint("account_ref", "capture_sequence"),
    CheckConstraint("capture_sequence>0"),
    sqlite_with_rowid=False,
)
crm_evidence_versions = Table(
    "crm_evidence_versions",
    metadata,
    Column("evidence_version_id", String, primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("evidence_type", String, nullable=False),
    Column("value_json", String, nullable=False),
    Column("source", String, nullable=False),
    Column("observed_at", String),
    Column("available_at", String, nullable=False),
    Column("source_event_id", String, ForeignKey("events.event_id"), nullable=False),
    Column(
        "supersedes_evidence_version_id",
        String,
        ForeignKey("crm_evidence_versions.evidence_version_id"),
    ),
    Column("capture_hash", String, ForeignKey("crm_captures.capture_hash"), nullable=False),
    Column("observation_json", String, nullable=False),
    sqlite_with_rowid=False,
)
crm_decisions = Table(
    "crm_decisions",
    metadata,
    Column("decision_event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("decision_class", String, nullable=False),
    Column("decision_boundary", String, nullable=False),
    Column("workflow_version", String, nullable=False),
    Column("artifact_hash", String, ForeignKey("logic_artifacts.artifact_hash"), nullable=False),
    Column("evaluator_version", String, nullable=False),
    Column("logic_version", String, nullable=False),
    Column("score", Integer, nullable=False),
    Column("threshold", Integer, nullable=False),
    Column("output", String, nullable=False),
    Column("explanation", String),
    Column("ingest_sequence", Integer, nullable=False),
    Column("unit_id", String, nullable=False, unique=True),
    Column("logical_key", String, nullable=False),
    Column("processing_version", String, nullable=False),
    Column("capture_hash", String, ForeignKey("crm_captures.capture_hash"), nullable=False),
    Column("reprocesses", String, ForeignKey("crm_decisions.unit_id")),
    sqlite_with_rowid=False,
)
crm_decision_context = Table(
    "crm_decision_context",
    metadata,
    Column(
        "decision_event_id", String, ForeignKey("crm_decisions.decision_event_id"), primary_key=True
    ),
    Column("input_key", String, primary_key=True),
    Column("value_text", String),
    Column("availability", String, nullable=False),
    Column("evidence_version_id", String, ForeignKey("crm_evidence_versions.evidence_version_id")),
    CheckConstraint("availability IN ('available','unavailable')"),
    sqlite_with_rowid=False,
)
crm_decision_consumed_inputs = Table(
    "crm_decision_consumed_inputs",
    metadata,
    Column(
        "decision_event_id", String, ForeignKey("crm_decisions.decision_event_id"), primary_key=True
    ),
    Column("input_key", String, primary_key=True),
    Column("value_text", String, nullable=False),
    Column(
        "evidence_version_id",
        String,
        ForeignKey("crm_evidence_versions.evidence_version_id"),
        nullable=False,
    ),
    Column("contribution", Integer, nullable=False),
    sqlite_with_rowid=False,
)
CRM_TABLES = (
    crm_captures,
    crm_evidence_versions,
    crm_decisions,
    crm_decision_context,
    crm_decision_consumed_inputs,
)


def _triggers(table):
    name = table.name
    key_groups = {tuple(col.name for col in table.primary_key.columns)}
    key_groups.update(
        tuple(col.name for col in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    matches = " OR ".join(
        "(" + " AND ".join(f"{key}=NEW.{key}" for key in keys) + ")" for keys in sorted(key_groups)
    )
    failure = "SELECT RAISE(ABORT, 'INV-01: CRM history is append-only'); END"
    return {
        f"{name}_no_update": f"CREATE TRIGGER {name}_no_update "
        f"BEFORE UPDATE ON {name} BEGIN {failure}",
        f"{name}_no_delete": f"CREATE TRIGGER {name}_no_delete "
        f"BEFORE DELETE ON {name} BEGIN {failure}",
        f"{name}_no_replace": f"CREATE TRIGGER {name}_no_replace "
        f"BEFORE INSERT ON {name} WHEN EXISTS "
        f"(SELECT 1 FROM {name} WHERE {matches}) BEGIN {failure}",
    }


def _normalized(sql):
    return " ".join(sql.split())


def schema_ready(conn):
    expected = {
        table.name: _normalized(str(CreateTable(table).compile(dialect=conn.dialect)))
        for table in CRM_TABLES
    }
    expected.update(
        {name: _normalized(sql) for table in CRM_TABLES for name, sql in _triggers(table).items()}
    )
    rows = conn.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger')"
    ).all()
    actual = {name: _normalized(sql) for name, sql in rows if name in expected}
    return actual == expected


def create_crm_schema(engine):
    """Explicit empty-domain initialization only; never called during ingestion."""
    with engine.begin() as conn:
        names = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        require(
            not names & {t.name for t in CRM_TABLES}
            and {"events", "accounts", "logic_artifacts"} <= names,
            "crm_schema_initialization_refused",
        )
        require(
            conn.execute(select(events.c.event_id).limit(1)).first() is None,
            "crm_schema_initialization_refused",
        )
        metadata.create_all(conn, tables=list(CRM_TABLES))
        for table in CRM_TABLES:
            for statement in _triggers(table).values():
                conn.exec_driver_sql(statement)
        require(schema_ready(conn), "crm_schema_initialization_refused")
