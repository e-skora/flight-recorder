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

**Outcome versions (D-013).** `outcomes` holds both `outcome.evaluated` schema
versions, distinguished by its `schema_version` column. The columns that exist
only for schema v2 are `window_opened_at`, `window_closes_at`,
`evaluation_state`, `observed_at`, `source_action_event_id`,
`source_action_unusable_reason`, `source_decision_event_id`,
`source_decision_unusable_reason` and `supersedes_outcome_event_id`; a v1 row
leaves all of them NULL and writes exactly what it wrote before them. A v2 row
leaves the v1-only `action_event_id` and `window_days` NULL. `reply`, `meeting`
and `opportunity` are shared and are NULL only for a v2 observation recorded as
unknown.

**Attribution results.** `outcome_attributions` holds one row per accepted
`outcome.attributed` event. Every row, like every other projected row, is tied
to its producing event, so `events.ingest_sequence` is the snapshot boundary
the attribution policy filters every read on.

SQLAlchemy Core is used (a reversible in-task choice; see DECISIONS.md Open).
The schema is created from this metadata by `flight-recorder reset`.
"""

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
    # --- Schema v1 columns (v2 rows leave `action_event_id` and `window_days` NULL).
    # v1's stored action reference; a validated foreign key, because v1 ingest
    # rejects a reference that does not resolve. v2 claims live in their own
    # columns below and carry no foreign key.
    Column("action_event_id", String, nullable=True),
    # v1's window length. NULL for v2, which records period bounds instead; no
    # duration is ever derived from those bounds.
    Column("window_days", Integer, nullable=True),
    # Shared by both versions. NULL only for a v2 observation recorded as
    # unknown; v1 always writes true or false.
    Column("reply", Boolean, nullable=True),
    Column("meeting", Boolean, nullable=True),
    Column("opportunity", Boolean, nullable=True),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    # The envelope's schema version ("1" or "2"), so a reader never guesses a shape.
    Column("schema_version", String, nullable=False),
    # --- Schema v2-only columns: always NULL on a v1 row.
    Column("window_opened_at", String, nullable=True),
    Column("window_closes_at", String, nullable=True),
    Column("evaluation_state", String, nullable=True),
    Column("observed_at", String, nullable=True),
    # The submitter's source claims, stored unmodified and deliberately without
    # foreign keys: an unusable claim is retained, not rejected (D-013). Each
    # `*_unusable_reason` records why the claim was unusable as of this
    # outcome's own ingestion, or NULL when it was usable then. The attribution
    # policy never reads these reasons; it recomputes its own at its cutoff.
    Column("source_action_event_id", String, nullable=True),
    Column("source_action_unusable_reason", String, nullable=True),
    Column("source_decision_event_id", String, nullable=True),
    Column("source_decision_unusable_reason", String, nullable=True),
    # INV-08: a closing or correcting observation appends and links back. At
    # most one version may supersede a given version (the collector names the
    # rejection first; the constraint is the backstop).
    Column(
        "supersedes_outcome_event_id",
        String,
        ForeignKey("outcomes.outcome_event_id"),
        nullable=True,
        unique=True,
    ),
    ForeignKeyConstraint(["action_event_id"], ["actions.action_event_id"]),
)

outcome_attributions = Table(
    "outcome_attributions",
    metadata,
    # The attribution's own identity: the `outcome.attributed` event id, derived
    # from (outcome_event_id, policy_version, ingest_cutoff).
    Column("attribution_event_id", String, ForeignKey("events.event_id"), primary_key=True),
    Column("account_ref", String, ForeignKey("accounts.account_ref"), nullable=False),
    Column("source_event_id", String, ForeignKey("events.event_id"), nullable=False),
    # The exact outcome version evaluated.
    Column("outcome_event_id", String, ForeignKey("outcomes.outcome_event_id"), nullable=False),
    Column("policy_version", String, nullable=False),
    Column("method", String, nullable=False),
    # The policy's attribution lookback, not the outcome's evaluation period.
    Column("window_days", Integer, nullable=False),
    # Policy-resolved references are validated references, unlike source claims.
    Column(
        "resolved_action_event_id",
        String,
        ForeignKey("actions.action_event_id"),
        nullable=True,
    ),
    Column(
        "resolved_decision_event_id",
        String,
        ForeignKey("decisions.decision_event_id"),
        nullable=True,
    ),
    Column("status", String, nullable=False),
    Column("reason", String, nullable=False),
    # The real instant the evaluation happened.
    Column("attributed_at", String, nullable=False),
    # The collector-owned snapshot boundary: an `events.ingest_sequence`.
    Column("ingest_cutoff", Integer, nullable=False),
    Column(
        "supersedes_attribution_event_id",
        String,
        ForeignKey("outcome_attributions.attribution_event_id"),
        nullable=True,
        unique=True,
    ),
    # One operation, one row; the collector names the rejection first.
    UniqueConstraint(
        "outcome_event_id",
        "policy_version",
        "ingest_cutoff",
        name="uq_outcome_attributions_operation",
    ),
)

# At most one first result (no supersession link) per outcome version and policy.
Index(
    "uq_outcome_attributions_root",
    outcome_attributions.c.outcome_event_id,
    outcome_attributions.c.policy_version,
    unique=True,
    sqlite_where=outcome_attributions.c.supersedes_attribution_event_id.is_(None),
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
    outcome_attributions,
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
