"""Ledger tables: `accounts`, the append-only `events` table, and the domain projections.

Every accepted event is stored raw in `events` and, in the same transaction,
normalized into the projection tables below (`PRODUCT.md` §6). The projections
are the queryable historical record; the raw payload is never the basis for
reconstruction.

Every projection table is append-only at the database (INV-01): `BEFORE UPDATE`
and `BEFORE DELETE` triggers raise, so a mutation fails visibly rather than
silently rewriting the past. Each projected row links back to the `event_id`
that produced it.

**The `_system` principal.** `account_ref = "_system"` is a reserved system
principal, not a prospect or tenant. It exists so logic artifacts can enter
through the same collector boundary as every other record while still
satisfying the `events.account_ref` foreign key. Exactly one event type may use
it (`logic_artifact.registered`) and it may use no other. It is infrastructure
metadata and MUST NOT appear on any account-facing surface: select accounts
through `accounts_query()`, which excludes it, rather than from `accounts`
directly.

SQLAlchemy Core is used (a reversible in-task choice; see DECISIONS.md Open).
The schema is created from this metadata by `flight-recorder reset`.
"""

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

#: The reserved system principal (see the module docstring).
SYSTEM_ACCOUNT_REF = "_system"

#: Fixed sentinel identity for the `_system` row. `.invalid` is reserved by
#: RFC 2606, so this can never collide with a real domain.
SYSTEM_ACCOUNT_NAME = "System (logic artifact registry)"
SYSTEM_ACCOUNT_DOMAIN = "system.invalid"

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


# --- Projections (PRODUCT.md §6) -------------------------------------------

evidence_versions = Table(
    "evidence_versions",
    metadata,
    Column("evidence_version_id", String, primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("evidence_type", String, nullable=False),
    # Canonical JSON of the item's semantic fields: always `value`, plus
    # `observed_at` or `basis` where the evidence type carries them.
    Column("value_json", Text, nullable=False),
    Column("source", String, nullable=False),
    # Effective/observation date where the type carries one, else NULL.
    Column("observed_at", String, nullable=True),
    # When the value became available to a decision: the envelope's recorded_at.
    Column("available_at", String, nullable=False),
    Column("source_event_id", String, ForeignKey("events.event_id"), nullable=False),
    # INV-04: a correction appends and links back; it never mutates the original.
    Column(
        "supersedes_evidence_version_id",
        String,
        ForeignKey("evidence_versions.evidence_version_id"),
        nullable=True,
    ),
)

logic_artifacts = Table(
    "logic_artifacts",
    metadata,
    # INV-05: the canonical content hash is the identity; the label is not.
    Column("artifact_hash", String, primary_key=True),
    Column("artifact_id", String, nullable=False),
    Column("logic_version", String, nullable=False),
    Column("decision_class", String, nullable=False),
    Column("artifact_schema_version", String, nullable=False),
    Column("evaluator_version", String, nullable=False),
    Column("artifact_json", Text, nullable=False),
    Column("source_event_id", String, ForeignKey("events.event_id"), nullable=False),
    UniqueConstraint("artifact_id", "logic_version", name="uq_logic_artifacts_identity"),
)

decisions = Table(
    "decisions",
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
    # The originally persisted explanation text, exactly as submitted (INV-07).
    Column("explanation", Text, nullable=True),
    Column("ingest_sequence", Integer, nullable=False),
)

decision_context = Table(
    "decision_context",
    metadata,
    Column(
        "decision_event_id",
        String,
        ForeignKey("decisions.decision_event_id"),
        primary_key=True,
    ),
    Column("input_key", String, primary_key=True),
    Column("availability", String, nullable=False),
    # Canonical JSON text of the preserved scalar, so `184`, `"184"` and `true`
    # stay distinguishable. NULL when the input was explicitly unavailable.
    Column("value_text", Text, nullable=True),
    Column(
        "evidence_version_id",
        String,
        ForeignKey("evidence_versions.evidence_version_id"),
        nullable=True,
    ),
)

decision_consumed_inputs = Table(
    "decision_consumed_inputs",
    metadata,
    Column(
        "decision_event_id",
        String,
        ForeignKey("decisions.decision_event_id"),
        primary_key=True,
    ),
    Column("input_key", String, primary_key=True),
    Column("value_text", Text, nullable=False),
    Column(
        "evidence_version_id",
        String,
        ForeignKey("evidence_versions.evidence_version_id"),
        nullable=False,
    ),
    Column("contribution", Integer, nullable=False),
)

persona_selections = Table(
    "persona_selections",
    metadata,
    Column("event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("decision_event_id", String, ForeignKey("decisions.decision_event_id"), nullable=False),
    Column("persona", String, nullable=False),
    Column("explanation", Text, nullable=True),
)

actions = Table(
    "actions",
    metadata,
    Column("action_event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("decision_event_id", String, ForeignKey("decisions.decision_event_id"), nullable=False),
    Column("action_type", String, nullable=False),
    Column("play_id", Integer, nullable=False),
    Column("target_persona", String, nullable=False),
    Column("status", String, nullable=False),
    # Exact money as a decimal string; schema v1 has no float fields.
    Column("cost", String, nullable=False),
    Column("currency", String, nullable=False),
    Column("occurred_at", String, nullable=False),
)

outcomes = Table(
    "outcomes",
    metadata,
    Column("outcome_event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    # Nullable in the schema for the unresolved cases later phases record;
    # schema v1's `outcome.evaluated` always supplies it.
    Column("action_event_id", String, nullable=True),
    Column("window_days", Integer, nullable=False),
    Column("reply", Boolean, nullable=False),
    Column("meeting", Boolean, nullable=False),
    Column("opportunity", Boolean, nullable=False),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    ForeignKeyConstraint(["action_event_id"], ["actions.action_event_id"]),
)

#: Every projected historical table, in dependency order (parents first).
PROJECTION_TABLES = (
    evidence_versions,
    logic_artifacts,
    decisions,
    decision_context,
    decision_consumed_inputs,
    persona_selections,
    actions,
    outcomes,
)

#: `events` plus the projections: everything INV-01 protects at the database.
APPEND_ONLY_TABLES = (events, *PROJECTION_TABLES)


def accounts_query():
    """Accounts for any account-facing surface, with `_system` excluded.

    Every list, count, lookup, and future Insights query selects through this
    helper so a later surface cannot forget the exclusion.
    """
    return select(accounts).where(accounts.c.account_ref != SYSTEM_ACCOUNT_REF)


def _append_only_triggers(table_name: str) -> tuple[str, str]:
    return (
        f"""
        CREATE TRIGGER IF NOT EXISTS {table_name}_no_update
        BEFORE UPDATE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, 'INV-01: {table_name} is append-only; UPDATE is not allowed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {table_name}_no_delete
        BEFORE DELETE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, 'INV-01: {table_name} is append-only; DELETE is not allowed');
        END
        """,
    )


# INV-01, enforced at the database on the events table and every projection.
APPEND_ONLY_TRIGGERS = tuple(
    ddl for table in APPEND_ONLY_TABLES for ddl in _append_only_triggers(table.name)
)


def create_schema(engine: Engine) -> None:
    """Create every table and the append-only triggers.

    Tables are created explicitly in a fixed order because `accounts` and
    `events` reference each other; SQLite accepts a forward reference in
    CREATE TABLE.
    """
    conn: Connection
    with engine.begin() as conn:
        accounts.create(conn, checkfirst=True)
        events.create(conn, checkfirst=True)
        for table in PROJECTION_TABLES:
            table.create(conn, checkfirst=True)
        for ddl in APPEND_ONLY_TRIGGERS:
            conn.exec_driver_sql(ddl)
