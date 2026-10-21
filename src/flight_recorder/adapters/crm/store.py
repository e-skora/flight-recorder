"""Operational receipt immutability and durable per-target fencing, with no domain writes."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flight_recorder.adapters.crm.contracts import (
    CRMCapture,
    CRMRefused,
    CRMTarget,
    ObservedRead,
    Ownership,
    Receipt,
    require,
    validate_unit_id,
)
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash

_SCHEMA = {
    ("table", "crm_store_meta"): """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=1))""",
    ("table", "crm_receipts"): """CREATE TABLE crm_receipts (
        connection_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
        body BLOB NOT NULL, body_sha256 TEXT NOT NULL,
        PRIMARY KEY(connection_id,receipt_id)) WITHOUT ROWID""",
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
    ("trigger", "crm_receipts_no_replace"): """CREATE TRIGGER crm_receipts_no_replace
        BEFORE INSERT ON crm_receipts WHEN EXISTS (
        SELECT 1 FROM crm_receipts WHERE connection_id=NEW.connection_id
        AND receipt_id=NEW.receipt_id) BEGIN
        SELECT RAISE(ABORT, 'INV-01: CRM receipts are append-only'); END""",
}


def _sql(text):
    return " ".join(text.split())


class CRMStore:
    def __init__(self, path: Path | str, *, format_version=1):
        require(
            type(format_version) is int and format_version in (1, 2),
            "operational_store_unrecognized",
        )
        self._format_version = format_version
        schema = _SCHEMA
        if format_version == 2:
            from flight_recorder.adapters.crm.operational_schema import capture_schema

            schema = capture_schema(_SCHEMA)
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
                    for statement in schema.values():
                        conn.execute(statement)
                    conn.execute("INSERT INTO crm_store_meta VALUES (?)", (format_version,))
            with self._read() as conn:
                actual = {
                    (row[0], row[1]): _sql(row[2])
                    for row in conn.execute(
                        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','trigger') "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                require(
                    actual == {k: _sql(v) for k, v in schema.items()},
                    "operational_store_unrecognized",
                )
                require(
                    conn.execute("SELECT format_version FROM crm_store_meta").fetchall()
                    == [(format_version,)],
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

    @contextmanager
    def _capture_transaction(self):
        require(self._format_version == 2, "capture_store_uninitialized")
        try:
            with self.transaction() as conn:
                yield conn
        except CRMRefused as exc:
            raise CRMRefused(exc.reason) from None
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, OverflowError):
            raise CRMRefused("capture_store_refused") from None

    @staticmethod
    def _read_from_capture(cap):
        d = cap.document
        return ObservedRead.from_document(
            {
                k: d[k]
                for k in (
                    "target",
                    "properties",
                    "unavailable_fields",
                    "read_started_at",
                    "read_finished_at",
                    "source_updated_at",
                    "precondition",
                    "request_id",
                    "response_body_sha256",
                    "observation_origin",
                )
            }
        )

    def _load_capture(self, conn, unit_id):
        row = conn.execute(
            "SELECT unit_id,capture_hash,target_key,claim_id,generation,capture_sequence,"
            "config_binding,observation_bytes,body FROM crm_frozen_captures WHERE unit_id=?",
            (unit_id,),
        ).fetchone()
        if row is None:
            return None
        cap = CRMCapture(row[8])
        d = cap.document
        require(
            (row[0], row[1], row[2], row[4], row[5], row[7])
            == (
                cap.unit_id,
                cap.capture_hash,
                CRMTarget(**d["target"]).key,
                d["ownership_generation"],
                d["capture_sequence"],
                self._read_from_capture(cap).canonical_bytes,
            ),
            "capture_integrity",
        )
        validate_unit_id(row[3])
        validate_unit_id(row[6])
        return cap

    @staticmethod
    def _fact_key(owner):
        return canonical_hash(
            dict(target_key=owner.target_key, claim_id=owner.unit_id, generation=owner.generation)
        )

    def _fact(self, conn, owner):
        row = conn.execute(
            "SELECT fact_id,target_key,claim_id,generation,body,body_sha256 "
            "FROM crm_read_observations "
            "WHERE target_key=? AND claim_id=? AND generation=?",
            (owner.target_key, owner.unit_id, owner.generation),
        ).fetchone()
        if row is None:
            return None
        d = json.loads(row[4])
        require(
            type(row[4]) is bytes
            and canonical_bytes(d) == row[4]
            and hashlib.sha256(row[4]).hexdigest() == row[5],
            "observation_integrity",
        )
        require(
            set(d)
            == {
                "format_version",
                "target_key",
                "claim_id",
                "generation",
                "config_binding",
                "selection_identity",
                "connection_id",
                "processing_version",
                "observation",
                "state",
                "capture_hash",
                "unit_id",
            }
            and d["format_version"] == 1
            and type(d["format_version"]) is int,
            "observation_integrity",
        )
        require(
            (row[0], row[1], row[2], row[3])
            == (self._fact_key(owner), d["target_key"], d["claim_id"], d["generation"]),
            "observation_integrity",
        )
        read = ObservedRead.from_document(d["observation"])
        require(
            read.target.key == owner.target_key and type(d["generation"]) is int,
            "observation_integrity",
        )
        for key in ["config_binding", "processing_version"]:
            validate_unit_id(d[key])
        require(
            type(d["selection_identity"]) is str
            and bool(d["selection_identity"])
            and type(d["connection_id"]) is str
            and bool(d["connection_id"]),
            "observation_integrity",
        )
        if d["state"] == "captured":
            cap = self._load_capture(conn, d["unit_id"])
            require(
                cap is not None and cap.capture_hash == d["capture_hash"], "observation_integrity"
            )
            c = cap.document
            require(
                self._read_from_capture(cap).document == d["observation"]
                and c["ownership_generation"] == owner.generation
                and c["connection_id"] == d["connection_id"]
                and c["processing_version"] == d["processing_version"]
                and c["selection_identity"] == d["selection_identity"],
                "observation_integrity",
            )
            binding = conn.execute(
                "SELECT config_binding,claim_id FROM crm_frozen_captures WHERE unit_id=?",
                (cap.unit_id,),
            ).fetchone()
            require(binding == (d["config_binding"], owner.unit_id), "observation_integrity")
        else:
            require(
                d["state"] == "scoring_inputs_unchanged"
                and d["unit_id"] is None
                and d["capture_hash"] is None,
                "observation_integrity",
            )
        return d

    def _state(self, conn, target):
        row = conn.execute(
            "SELECT capture_sequence,last_unit_id,input_digest,processing_version,last_fact_id "
            "FROM crm_polling_state WHERE target_key=?",
            (target.key,),
        ).fetchone()
        empty = dict(
            capture_sequence=0,
            last_unit_id=None,
            input_digest=None,
            processing_version=None,
            last_fact_id=None,
        )
        if row is None:
            return empty
        state = dict(zip(empty, row, strict=True))
        require(type(row[0]) is int and row[0] > 0, "polling_state_integrity")
        cap = self._load_capture(conn, row[1])
        require(
            cap is not None
            and CRMTarget(**cap.document["target"]) == target
            and cap.document["capture_sequence"] == row[0],
            "polling_state_integrity",
        )
        from flight_recorder.collector.crm_schema import input_digest

        require(
            row[2] == input_digest(cap.document) and row[3] == cap.document["processing_version"],
            "polling_state_integrity",
        )
        fact_owner = conn.execute(
            "SELECT claim_id,generation FROM crm_read_observations "
            "WHERE fact_id=? AND target_key=?",
            (row[4], target.key),
        ).fetchone()
        require(fact_owner is not None, "polling_state_integrity")
        fact = self._fact(conn, Ownership(target.key, *fact_owner))
        require(fact is not None, "polling_state_integrity")
        latest = conn.execute(
            "SELECT max(generation) FROM crm_read_observations WHERE target_key=?", (target.key,)
        ).fetchone()[0]
        require(
            fact["generation"] == latest and fact["processing_version"] == row[3],
            "polling_state_integrity",
        )
        if fact["state"] == "captured":
            require(fact["unit_id"] == row[1], "polling_state_integrity")
        else:
            from flight_recorder.adapters.crm.mapping import MAPPING_HASH, map_inputs

            read = ObservedRead.from_document(fact["observation"])
            require(
                canonical_hash(
                    dict(
                        mapping_hash=MAPPING_HASH,
                        inputs=map_inputs(read.properties, read.unavailable_fields),
                    )
                )
                == row[2],
                "polling_state_integrity",
            )
        return state

    def polling_state(self, target):
        require(type(target) is CRMTarget, "invalid_target")
        with self._capture_transaction() as conn:
            return self._state(conn, target)

    def load_capture(self, unit_id):
        validate_unit_id(unit_id)
        with self._capture_transaction() as conn:
            cap = self._load_capture(conn, unit_id)
            if cap is not None:
                row = conn.execute(
                    "SELECT claim_id,generation FROM crm_frozen_captures WHERE unit_id=?",
                    (unit_id,),
                ).fetchone()
                fact = self._fact(conn, Ownership(CRMTarget(**cap.document["target"]).key, *row))
                require(fact is not None and fact["unit_id"] == unit_id, "capture_integrity")
            return cap

    def observation_for_owner(self, owner):
        with self._capture_transaction() as conn:
            self.assert_owner(conn, owner)
            return self._fact(conn, owner)

    def capture_for_owner(self, owner):
        fact = self.observation_for_owner(owner)
        return (
            self.load_capture(fact["unit_id"])
            if fact is not None and fact["state"] == "captured"
            else None
        )

    def next_capture_sequence(self, owner):
        with self._capture_transaction() as conn:
            self.assert_owner(conn, owner)
            return (
                self._state(conn, CRMTarget(**json.loads(owner.target_key)))["capture_sequence"] + 1
            )

    def observation_count(self, target):
        require(type(target) is CRMTarget, "invalid_target")
        with self._capture_transaction() as conn:
            return conn.execute(
                "SELECT count(*) FROM crm_read_observations WHERE target_key=?", (target.key,)
            ).fetchone()[0]

    def _check_read(self, conn, owner, read, state):
        require(
            type(read) is ObservedRead and read.target.key == owner.target_key,
            "capture_target_mismatch",
        )
        d = read.document
        if state["last_fact_id"] is not None:
            previous = conn.execute(
                "SELECT claim_id,generation FROM crm_read_observations WHERE fact_id=?",
                (state["last_fact_id"],),
            ).fetchone()
            require(previous is not None, "polling_state_integrity")
            old = self._fact(conn, Ownership(owner.target_key, *previous))
            require(
                d["read_started_at"] >= old["observation"]["read_finished_at"],
                "observation_out_of_order",
            )
        if d["precondition"] is not None:
            semantic = canonical_hash(
                {k: d[k] for k in ["properties", "unavailable_fields", "source_updated_at"]}
            )
            old = conn.execute(
                "SELECT semantic_hash FROM crm_source_versions "
                "WHERE target_key=? AND precondition=?",
                (owner.target_key, d["precondition"]),
            ).fetchone()
            require(old is None or old[0] == semantic, "source_version_conflict")
            if old is None:
                conn.execute(
                    "INSERT INTO crm_source_versions VALUES (?,?,?)",
                    (owner.target_key, d["precondition"], semantic),
                )

    def _record_fact(
        self,
        conn,
        owner,
        read,
        *,
        config_binding,
        selection_identity,
        connection_id,
        processing_version,
        cap=None,
    ):
        validate_unit_id(config_binding)
        validate_unit_id(processing_version)
        require(
            type(selection_identity) is str
            and 0 < len(selection_identity) <= 256
            and type(connection_id) is str
            and bool(connection_id),
            "capture_binding_invalid",
        )
        d = dict(
            format_version=1,
            target_key=owner.target_key,
            claim_id=owner.unit_id,
            generation=owner.generation,
            config_binding=config_binding,
            selection_identity=selection_identity,
            connection_id=connection_id,
            processing_version=processing_version,
            observation=read.document,
            state="captured" if cap else "scoring_inputs_unchanged",
            capture_hash=cap.capture_hash if cap else None,
            unit_id=cap.unit_id if cap else None,
        )
        body = canonical_bytes(d)
        conn.execute(
            "INSERT INTO crm_read_observations VALUES (?,?,?,?,?,?)",
            (
                self._fact_key(owner),
                owner.target_key,
                owner.unit_id,
                owner.generation,
                body,
                hashlib.sha256(body).hexdigest(),
            ),
        )
        return d

    def freeze_capture(self, owner, capture, *, config_binding):
        require(type(capture) is CRMCapture, "invalid_crm_capture")
        cap = CRMCapture(capture.canonical_bytes)
        d = cap.document
        read = self._read_from_capture(cap)
        with self._capture_transaction() as conn:
            self.assert_owner(conn, owner)
            require(
                read.target.key == owner.target_key
                and d["ownership_generation"] == owner.generation,
                "capture_target_or_generation_mismatch",
            )
            prior = self._fact(conn, owner)
            if prior is not None:
                require(
                    prior["state"] == "captured" and prior["config_binding"] == config_binding,
                    "capture_claim_conflict",
                )
                saved = self._load_capture(conn, prior["unit_id"])
                require(saved == cap, "capture_claim_conflict")
                return saved
            state = self._state(conn, read.target)
            require(
                d["capture_sequence"] == state["capture_sequence"] + 1 and d["reprocesses"] is None,
                "capture_sequence_mismatch",
            )
            self._check_read(conn, owner, read, state)
            from flight_recorder.collector.crm_schema import input_digest

            digest = input_digest(d)
            require(
                (digest, d["processing_version"])
                != (state["input_digest"], state["processing_version"]),
                "scoring_inputs_unchanged",
            )
            validate_unit_id(config_binding)
            conn.execute(
                "INSERT INTO crm_frozen_captures VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cap.unit_id,
                    cap.capture_hash,
                    owner.target_key,
                    owner.unit_id,
                    owner.generation,
                    d["capture_sequence"],
                    config_binding,
                    read.canonical_bytes,
                    cap.canonical_bytes,
                ),
            )
            self._record_fact(
                conn,
                owner,
                read,
                config_binding=config_binding,
                selection_identity=d["selection_identity"],
                connection_id=d["connection_id"],
                processing_version=d["processing_version"],
                cap=cap,
            )
            conn.execute(
                "INSERT INTO crm_polling_state VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(target_key) DO UPDATE SET "
                "capture_sequence=excluded.capture_sequence,last_unit_id=excluded.last_unit_id,input_digest=excluded.input_digest,processing_version=excluded.processing_version,last_fact_id=excluded.last_fact_id",
                (
                    owner.target_key,
                    d["capture_sequence"],
                    cap.unit_id,
                    digest,
                    d["processing_version"],
                    self._fact_key(owner),
                ),
            )
            return cap

    def record_unchanged(
        self,
        owner,
        observation,
        processing_identity,
        *,
        config_binding,
        selection_identity,
        connection_id,
    ):
        require(type(observation) is ObservedRead, "invalid_observed_read")
        with self._capture_transaction() as conn:
            self.assert_owner(conn, owner)
            prior = self._fact(conn, owner)
            if prior is not None:
                require(
                    prior["state"] == "scoring_inputs_unchanged"
                    and prior["observation"] == observation.document
                    and (
                        prior["config_binding"],
                        prior["selection_identity"],
                        prior["connection_id"],
                        prior["processing_version"],
                    )
                    == (config_binding, selection_identity, connection_id, processing_identity),
                    "capture_claim_conflict",
                )
                return prior
            state = self._state(conn, observation.target)
            from flight_recorder.adapters.crm.mapping import MAPPING_HASH, map_inputs

            digest = canonical_hash(
                dict(
                    mapping_hash=MAPPING_HASH,
                    inputs=map_inputs(observation.properties, observation.unavailable_fields),
                )
            )
            require(
                (digest, processing_identity)
                == (state["input_digest"], state["processing_version"]),
                "inputs_not_unchanged",
            )
            self._check_read(conn, owner, observation, state)
            fact = self._record_fact(
                conn,
                owner,
                observation,
                config_binding=config_binding,
                selection_identity=selection_identity,
                connection_id=connection_id,
                processing_version=processing_identity,
            )
            conn.execute(
                "UPDATE crm_polling_state SET last_fact_id=? WHERE target_key=?",
                (self._fact_key(owner), owner.target_key),
            )
            return fact
