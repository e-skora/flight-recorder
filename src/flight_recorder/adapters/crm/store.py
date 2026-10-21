"""Operational receipt immutability and durable per-target fencing, with no domain writes."""

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flight_recorder.adapters.crm.contracts import (
    CRMRefused,
    CRMTarget,
    Ownership,
    Receipt,
    require,
    validate_unit_id,
)

_SCHEMA = {
    ("table", "crm_store_meta"): """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=1))""",
    ("table", "crm_receipts"): """CREATE TABLE crm_receipts (
        connection_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
        body BLOB NOT NULL, body_sha256 TEXT NOT NULL,
        PRIMARY KEY(connection_id,receipt_id))""",
    ("table", "crm_target_ownership"): """CREATE TABLE crm_target_ownership (
        target_key TEXT PRIMARY KEY NOT NULL, unit_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation>0),
        state TEXT NOT NULL CHECK(state IN ('held','released','abandoned')))""",
    ("trigger", "crm_receipts_no_update"): """CREATE TRIGGER crm_receipts_no_update
        BEFORE UPDATE ON crm_receipts BEGIN
        SELECT RAISE(ABORT, 'INV-01: CRM receipts are append-only'); END""",
    ("trigger", "crm_receipts_no_delete"): """CREATE TRIGGER crm_receipts_no_delete
        BEFORE DELETE ON crm_receipts BEGIN
        SELECT RAISE(ABORT, 'INV-01: CRM receipts are append-only'); END""",
}


def _sql(text):
    return " ".join(text.split())


class CRMStore:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self._closed = False
        require(
            self.path.parent.is_dir() and not self.path.is_dir(), "operational_store_unrecognized"
        )
        existed = self.path.exists()
        try:
            if not existed:
                # Exclusive creation prevents a race from initializing a foreign existing file.
                with self.path.open("xb"):
                    pass
                with self.transaction() as conn:
                    for statement in _SCHEMA.values():
                        conn.execute(statement)
                    conn.execute("INSERT INTO crm_store_meta VALUES (1)")
            with self._read() as conn:
                actual = {
                    (row[0], row[1]): _sql(row[2])
                    for row in conn.execute(
                        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','trigger') "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                require(
                    actual == {k: _sql(v) for k, v in _SCHEMA.items()},
                    "operational_store_unrecognized",
                )
                require(
                    conn.execute("SELECT format_version FROM crm_store_meta").fetchall() == [(1,)],
                    "operational_store_unrecognized",
                )
        except FileExistsError as exc:
            raise CRMRefused("operational_store_unrecognized") from exc
        except sqlite3.DatabaseError as exc:
            raise CRMRefused("operational_store_unrecognized") from exc

    def _connect(self):
        require(not self._closed, "operational_store_closed")
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5
        )
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, sqlite3.OperationalError):
                code = getattr(exc, "sqlite_errorcode", 0)
                if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise CRMRefused("operational_store_contended") from exc
            raise
        finally:
            conn.close()

    def close(self):
        self._closed = True

    def record_receipt(self, connection_id: str, receipt_id: str, body: bytes) -> Receipt:
        require(
            type(connection_id) is str
            and bool(connection_id)
            and type(receipt_id) is str
            and bool(receipt_id)
            and type(body) is bytes,
            "invalid_receipt",
        )
        digest = hashlib.sha256(body).hexdigest()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT body,body_sha256 FROM crm_receipts WHERE connection_id=? AND receipt_id=?",
                (connection_id, receipt_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO crm_receipts VALUES (?,?,?,?)",
                    (connection_id, receipt_id, body, digest),
                )
            else:
                require(
                    type(row[0]) is bytes and hashlib.sha256(row[0]).hexdigest() == row[1],
                    "operational_store_unrecognized",
                )
                require(row == (body, digest), "receipt_conflict")
        return Receipt(connection_id, receipt_id, body, digest)

    def receipt_count(self) -> int:
        with self._read() as conn:
            return conn.execute("SELECT count(*) FROM crm_receipts").fetchone()[0]

    def acquire(self, target: CRMTarget, unit_id: str) -> Ownership:
        require(type(target) is CRMTarget, "invalid_target")
        validate_unit_id(unit_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT generation,state FROM crm_target_ownership WHERE target_key=?",
                (target.key,),
            ).fetchone()
            require(row is None or row[1] != "held", "target_busy")
            generation = row[0] + 1 if row else 1
            if row is None:
                conn.execute(
                    "INSERT INTO crm_target_ownership VALUES (?,?,?,'held')",
                    (target.key, unit_id, generation),
                )
            else:
                conn.execute(
                    "UPDATE crm_target_ownership SET unit_id=?,generation=?,state='held' "
                    "WHERE target_key=?",
                    (unit_id, generation, target.key),
                )
        return Ownership(target.key, unit_id, generation)

    def assert_owner(self, conn, owner: Ownership) -> None:
        require(not self._closed, "operational_store_closed")
        require(type(owner) is Ownership, "stale_ownership")
        databases = conn.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in databases if row[1] == "main"), None)
        require(main is not None and Path(main).resolve() == self.path, "stale_ownership")
        row = conn.execute(
            "SELECT unit_id,generation,state FROM crm_target_ownership WHERE target_key=?",
            (owner.target_key,),
        ).fetchone()
        require(row == (owner.unit_id, owner.generation, "held"), "stale_ownership")

    def _settle(self, owner, state):
        with self.transaction() as conn:
            self.assert_owner(conn, owner)
            conn.execute(
                "UPDATE crm_target_ownership SET state=? WHERE target_key=?",
                (state, owner.target_key),
            )

    def release(self, owner: Ownership) -> None:
        self._settle(owner, "released")

    def abandon(self, owner: Ownership) -> None:
        self._settle(owner, "abandoned")

    def owner(self, target: CRMTarget) -> Ownership | None:
        require(type(target) is CRMTarget, "invalid_target")
        with self._read() as conn:
            row = conn.execute(
                "SELECT unit_id,generation FROM crm_target_ownership "
                "WHERE target_key=? AND state='held'",
                (target.key,),
            ).fetchone()
        return Ownership(target.key, row[0], row[1]) if row else None
