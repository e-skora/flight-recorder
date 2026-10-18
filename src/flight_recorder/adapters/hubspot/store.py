"""Operational capture store for FR-V2-01: a separate SQLite file, never the ledger.

Immutable content and mutable coordination live in different tables:

- append-only (UPDATE/DELETE raise at the database, as the ledger does for
  INV-01): `source_identities`, `raw_notifications`, `processing_units`,
  `receipts` and `captures`;
- mutable: `company_ownership` (per-company reservation and fencing
  generation) and `unit_progress` (visible operational state).

Every write runs inside one `BEGIN IMMEDIATE` transaction, so a check and the
write it guards cannot be separated by another store instance on the same file.

Ownership is cooperative and explicit. A company is reserved before any read
and stays reserved through durable freeze. Nothing here infers that an owner
died from elapsed time: a held reservation is released only by its own freeze
or refusal, or by an explicit known abandonment, which advances the fence so
the abandoned generation can never commit.
"""

import hashlib
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import URL, Connection

from flight_recorder.adapters.hubspot.contracts import (
    CaptureOwnership,
    CaptureRefused,
    FrozenCapture,
    ProcessingUnit,
    SourceNotification,
    strict_json_loads,
)

metadata = MetaData()

source_identities = Table(
    "source_identities",
    metadata,
    Column("logical_key", String, primary_key=True),
    # The normalized semantic body behind the key, compared on every receipt so
    # a digest collision is refused rather than merged.
    Column("semantic_json", Text, nullable=False),
)

raw_notifications = Table(
    "raw_notifications",
    metadata,
    Column("raw_ref", String, primary_key=True),
    Column("raw_body", LargeBinary, nullable=False),
)

processing_units = Table(
    "processing_units",
    metadata,
    Column("unit_id", String, primary_key=True),
    Column("logical_key", String, nullable=False),
    Column("processing_version", String, nullable=False),
    Column("company_key", String, nullable=False),
    Column("mapping_identity", String, nullable=False),
    Column("selection_identity", String, nullable=False),
    Column("adapter_identity", String, nullable=False),
    Column("reprocesses", String, nullable=True),
)

receipts = Table(
    "receipts",
    metadata,
    Column("receipt_id", Integer, primary_key=True, autoincrement=True),
    Column("unit_id", String, nullable=False),
    Column("raw_ref", String, nullable=False),
    sqlite_autoincrement=True,
)

captures = Table(
    "captures",
    metadata,
    # One immutable winner per processing unit.
    Column("unit_id", String, primary_key=True),
    Column("company_key", String, nullable=False),
    Column("capture_sequence", Integer, nullable=False),
    Column("body_json", Text, nullable=False),
    UniqueConstraint("company_key", "capture_sequence", name="uq_captures_company_sequence"),
)

company_ownership = Table(
    "company_ownership",
    metadata,
    Column("company_key", String, primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("owner_unit_id", String, nullable=True),
)

unit_progress = Table(
    "unit_progress",
    metadata,
    Column("unit_id", String, primary_key=True),
    Column("state", String, nullable=False),
    Column("detail", Text, nullable=True),
)

APPEND_ONLY_TABLES = (source_identities, raw_notifications, processing_units, receipts, captures)

HELD = "held"
FREE = "free"
ABANDONED = "abandoned"


def _append_only_triggers(table_name: str) -> tuple[str, str]:
    return (
        f"""
        CREATE TRIGGER IF NOT EXISTS {table_name}_no_update
        BEFORE UPDATE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, '{table_name} is append-only; UPDATE is not allowed');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {table_name}_no_delete
        BEFORE DELETE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, '{table_name} is append-only; DELETE is not allowed');
        END
        """,
    )


APPEND_ONLY_TRIGGERS = tuple(
    ddl for table in APPEND_ONLY_TABLES for ddl in _append_only_triggers(table.name)
)


def raw_notification_ref(raw_body: bytes) -> str:
    """Immutable reference to exact notification bytes."""
    return "sha256:" + hashlib.sha256(raw_body).hexdigest()


def _driver_autocommit(dbapi_connection, _record) -> None:
    # Let SQLAlchemy's begin event own transaction start (documented pysqlite recipe).
    dbapi_connection.isolation_level = None


def _begin_immediate(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def _unit_id(unit) -> str:
    if not isinstance(unit, ProcessingUnit):
        raise CaptureRefused("identity conflict: a ProcessingUnit is required")
    return unit.unit_id


class CaptureStore:
    """The adapter's operational SQLite store at `path`."""

    def __init__(self, path):
        self.path = Path(path)
        self._engine = create_engine(
            URL.create("sqlite", database=str(self.path)),
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(self._engine, "connect", _driver_autocommit)
        event.listen(self._engine, "begin", _begin_immediate)
        with self._engine.begin() as conn:
            metadata.create_all(conn)
            for ddl in APPEND_ONLY_TRIGGERS:
                conn.exec_driver_sql(ddl)

    def close(self) -> None:
        self._engine.dispose()

    # --- Reads -----------------------------------------------------------------

    def load_capture(self, unit) -> FrozenCapture | None:
        unit_id = _unit_id(unit)
        with self._engine.connect() as conn:
            return _load_capture(conn, unit_id)

    def load_raw(self, raw_ref: str) -> bytes | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(raw_notifications.c.raw_body).where(raw_notifications.c.raw_ref == raw_ref)
            ).first()
        return None if row is None else bytes(row.raw_body)

    def receipt_count(self, unit) -> int:
        unit_id = _unit_id(unit)
        with self._engine.connect() as conn:
            return conn.execute(
                select(func.count()).select_from(receipts).where(receipts.c.unit_id == unit_id)
            ).scalar_one()

    def progress(self, unit) -> str | None:
        """Visible operational state of a unit, for example `refused: no_usable_inputs ...`."""
        unit_id = _unit_id(unit)
        with self._engine.connect() as conn:
            row = conn.execute(
                select(unit_progress).where(unit_progress.c.unit_id == unit_id)
            ).first()
        if row is None:
            return None
        return row.state if not row.detail else f"{row.state}: {row.detail}"

    # --- Writes ----------------------------------------------------------------

    def record_receipt(self, unit) -> str:
        """Append one receipt attempt; refuse identity collisions. Returns the raw ref."""
        unit_id = _unit_id(unit)
        with self._engine.begin() as conn:
            raw_ref = _register(conn, unit)
            conn.execute(insert(receipts).values(unit_id=unit_id, raw_ref=raw_ref))
            if _progress_row(conn, unit_id) is None:
                _set_progress(conn, unit_id, "received", None)
        return raw_ref

    def acquire_or_load(self, unit) -> tuple[CaptureOwnership | None, FrozenCapture | None]:
        """Reserve the unit's company before any read, or return the existing winner."""
        unit_id = _unit_id(unit)
        with self._engine.begin() as conn:
            _register(conn, unit)
            existing = _load_capture(conn, unit_id)
            if existing is not None:
                return None, existing
            company_key = unit.company_key
            row = _ownership_row(conn, company_key)
            if row is not None and row.state == HELD:
                raise CaptureRefused(
                    f"busy: company {company_key} is owned by generation {row.generation} "
                    f"(unit {row.owner_unit_id}); no read is permitted until that owner "
                    "freezes, is refused or is explicitly abandoned"
                )
            generation = (row.generation if row is not None else 0) + 1
            values = dict(generation=generation, state=HELD, owner_unit_id=unit_id)
            if row is None:
                conn.execute(insert(company_ownership).values(company_key=company_key, **values))
            else:
                conn.execute(
                    update(company_ownership)
                    .where(company_ownership.c.company_key == company_key)
                    .values(**values)
                )
            _set_progress(conn, unit_id, "reserved", f"generation {generation}")
            return CaptureOwnership(company_key, unit_id, generation), None

    def acquire(self, unit) -> CaptureOwnership:
        owner, existing = self.acquire_or_load(unit)
        if existing is not None:
            raise CaptureRefused(f"identity conflict: unit {unit.unit_id} is already captured")
        return owner

    def freeze(self, owner: CaptureOwnership, candidate: FrozenCapture) -> FrozenCapture:
        """Publish the unit's one immutable winner if `owner` is still current."""
        if not isinstance(owner, CaptureOwnership) or not isinstance(candidate, FrozenCapture):
            raise CaptureRefused("identity conflict: freeze needs CaptureOwnership and capture")
        try:
            document = candidate.document
        except (TypeError, ValueError) as error:
            raise CaptureRefused(f"invalid capture: {error}") from error
        if not isinstance(document, dict) or document.get("unit_id") != owner.unit_id:
            raise CaptureRefused("identity conflict: candidate names a different unit")
        with self._engine.begin() as conn:
            if not _is_current(conn, owner):
                raise CaptureRefused(
                    f"stale fence: generation {owner.generation} for company "
                    f"{owner.company_key} is no longer the current owner; nothing committed"
                )
            unit_row = conn.execute(
                select(processing_units).where(processing_units.c.unit_id == owner.unit_id)
            ).first()
            if unit_row is None or unit_row.company_key != owner.company_key:
                raise CaptureRefused("identity conflict: ownership does not match the unit")
            existing = _load_capture(conn, owner.unit_id)
            if existing is not None:
                _release(conn, owner)
                return existing
            # The public freeze API enforces the same complete capture contract
            # as capture_once, inside the ownership/publication transaction.
            from flight_recorder.adapters.hubspot.capture import validate_pending_capture

            try:
                raw_ref = document["raw_notification_ref"]
                if not isinstance(raw_ref, str):
                    raise ValueError("raw notification reference must be text")
                raw = conn.execute(
                    select(raw_notifications.c.raw_body).where(
                        raw_notifications.c.raw_ref == raw_ref
                    )
                ).first()
                if raw is None:
                    raise ValueError("raw notification reference is not registered")
                notification = SourceNotification.from_payload(
                    strict_json_loads(bytes(raw.raw_body)), raw_body=bytes(raw.raw_body)
                )
                unit = ProcessingUnit(
                    notification,
                    unit_row.mapping_identity,
                    unit_row.selection_identity,
                    unit_row.adapter_identity,
                    unit_row.reprocesses,
                )
                semantic = conn.execute(
                    select(source_identities.c.semantic_json).where(
                        source_identities.c.logical_key == unit_row.logical_key
                    )
                ).first()
                if (
                    unit.unit_id != owner.unit_id
                    or unit.company_key != owner.company_key
                    or unit.processing_version != unit_row.processing_version
                    or semantic is None
                    or notification.semantic_json != semantic.semantic_json
                ):
                    raise ValueError("raw notification differs from registered unit")
                validate_pending_capture(unit, owner, raw_ref, candidate)
            except (KeyError, TypeError, ValueError) as error:
                raise CaptureRefused(f"invalid capture: {error}") from error
            previous = conn.execute(
                select(func.max(captures.c.capture_sequence)).where(
                    captures.c.company_key == owner.company_key
                )
            ).scalar_one()
            sequence = (previous or 0) + 1
            document["capture_sequence"] = sequence
            frozen = FrozenCapture.from_document(document)
            conn.execute(
                insert(captures).values(
                    unit_id=owner.unit_id,
                    company_key=owner.company_key,
                    capture_sequence=sequence,
                    body_json=frozen.canonical_bytes.decode("utf-8"),
                )
            )
            _release(conn, owner)
            _set_progress(conn, owner.unit_id, "captured", f"capture_sequence {sequence}")
        return frozen

    def abandon_capture(self, owner: CaptureOwnership, *, known_abandoned: bool = True) -> None:
        """Explicitly abandon a known-dead generation and advance the fence."""
        if known_abandoned is not True:
            raise CaptureRefused(
                "abandonment refused: requires known_abandoned=True; ownership is never "
                "inferred dead from a timeout or lease"
            )
        if not isinstance(owner, CaptureOwnership):
            raise CaptureRefused("stale fence: a CaptureOwnership token is required")
        with self._engine.begin() as conn:
            if not _is_current(conn, owner):
                raise CaptureRefused(
                    f"stale fence: generation {owner.generation} is not the current owner of "
                    f"company {owner.company_key}; successor ownership and progress unchanged"
                )
            conn.execute(
                update(company_ownership)
                .where(company_ownership.c.company_key == owner.company_key)
                .values(generation=owner.generation + 1, state=ABANDONED, owner_unit_id=None)
            )
            _set_progress(conn, owner.unit_id, "abandoned", f"generation {owner.generation}")

    def release_if_current(self, owner: CaptureOwnership, detail: str) -> bool:
        """After a known refusal, release ownership only if it is still current."""
        with self._engine.begin() as conn:
            if not _is_current(conn, owner):
                return False
            _release(conn, owner)
            _set_progress(conn, owner.unit_id, "refused", detail)
            return True

    def record_refusal(self, unit, detail: str) -> None:
        """Record a refusal made before ownership, unless the unit is captured or owned."""
        unit_id = _unit_id(unit)
        with self._engine.begin() as conn:
            if _load_capture(conn, unit_id) is not None:
                return
            row = _ownership_row(conn, unit.company_key)
            if row is not None and row.state == HELD and row.owner_unit_id == unit_id:
                return
            _set_progress(conn, unit_id, "refused", detail)


def acquire_capture(store: CaptureStore, unit: ProcessingUnit) -> CaptureOwnership:
    """Reserve per-company capture ownership before any read."""
    return store.acquire(unit)


def freeze_once(
    store: CaptureStore, owner: CaptureOwnership, candidate: FrozenCapture
) -> FrozenCapture:
    """Atomically publish one immutable winner for the owner's unit."""
    return store.freeze(owner, candidate)


# --- Transaction helpers ---------------------------------------------------------


def _load_capture(conn: Connection, unit_id: str) -> FrozenCapture | None:
    row = conn.execute(select(captures.c.body_json).where(captures.c.unit_id == unit_id)).first()
    return None if row is None else FrozenCapture(row.body_json.encode("utf-8"))


def _ownership_row(conn: Connection, company_key: str):
    return conn.execute(
        select(company_ownership).where(company_ownership.c.company_key == company_key)
    ).first()


def _is_current(conn: Connection, owner: CaptureOwnership) -> bool:
    row = _ownership_row(conn, owner.company_key)
    return (
        row is not None
        and row.state == HELD
        and row.generation == owner.generation
        and row.owner_unit_id == owner.unit_id
    )


def _release(conn: Connection, owner: CaptureOwnership) -> None:
    conn.execute(
        update(company_ownership)
        .where(company_ownership.c.company_key == owner.company_key)
        .values(state=FREE, owner_unit_id=None)
    )


def _progress_row(conn: Connection, unit_id: str):
    return conn.execute(select(unit_progress).where(unit_progress.c.unit_id == unit_id)).first()


def _set_progress(conn: Connection, unit_id: str, state: str, detail: str | None) -> None:
    if _progress_row(conn, unit_id) is None:
        conn.execute(insert(unit_progress).values(unit_id=unit_id, state=state, detail=detail))
    else:
        conn.execute(
            update(unit_progress)
            .where(unit_progress.c.unit_id == unit_id)
            .values(state=state, detail=detail)
        )


def _register(conn: Connection, unit: ProcessingUnit) -> str:
    """Record source identity, raw bytes and the unit; refuse every conflict."""
    notification = unit.notification
    stored = conn.execute(
        select(source_identities.c.semantic_json).where(
            source_identities.c.logical_key == unit.logical_key
        )
    ).first()
    if stored is None:
        conn.execute(
            insert(source_identities).values(
                logical_key=unit.logical_key, semantic_json=notification.semantic_json
            )
        )
    elif stored.semantic_json != notification.semantic_json:
        raise CaptureRefused(
            f"identity collision: logical key {unit.logical_key} already names a different "
            "semantic body; refused rather than merged"
        )

    raw_ref = raw_notification_ref(notification.raw_body)
    raw = conn.execute(
        select(raw_notifications.c.raw_body).where(raw_notifications.c.raw_ref == raw_ref)
    ).first()
    if raw is None:
        conn.execute(
            insert(raw_notifications).values(raw_ref=raw_ref, raw_body=notification.raw_body)
        )
    elif bytes(raw.raw_body) != notification.raw_body:
        raise CaptureRefused(f"identity collision: raw reference {raw_ref} names other bytes")

    fields = dict(
        logical_key=unit.logical_key,
        processing_version=unit.processing_version,
        company_key=unit.company_key,
        mapping_identity=unit.mapping_identity,
        selection_identity=unit.selection_identity,
        adapter_identity=unit.adapter_identity,
        reprocesses=unit.reprocesses,
    )
    existing = conn.execute(
        select(processing_units).where(processing_units.c.unit_id == unit.unit_id)
    ).first()
    if existing is None:
        if unit.reprocesses is not None:
            prior = conn.execute(
                select(processing_units.c.logical_key).where(
                    processing_units.c.unit_id == unit.reprocesses
                )
            ).first()
            if prior is None or prior.logical_key != unit.logical_key:
                raise CaptureRefused(
                    f"identity conflict: reprocesses {unit.reprocesses} is not a recorded unit "
                    "of the same logical source event"
                )
        conn.execute(insert(processing_units).values(unit_id=unit.unit_id, **fields))
    elif any(getattr(existing, name) != value for name, value in fields.items()):
        raise CaptureRefused(
            f"identity conflict: unit {unit.unit_id} is recorded with different processing "
            "identity or lineage"
        )
    return raw_ref
