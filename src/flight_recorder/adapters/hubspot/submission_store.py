"""Append-only operational requests/receipts, separate from the domain ledger."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flight_recorder.adapters.hubspot.contracts import (
    FrozenCapture,
    ProcessingUnit,
    SourceNotification,
    strict_json_loads,
    typed_equal,
)
from flight_recorder.adapters.hubspot.submission_contracts import (
    FrozenSubmission,
    SubmissionError,
    digest,
    require,
)
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.capture_contract import account_identity, validate_capture

TABLES = (
    "submission_target",
    "frozen_submissions",
    "submission_attempts",
    "submission_observations",
)
DDL = (
    (
        "CREATE TABLE IF NOT EXISTS submission_target (singleton INTEGER "
        "PRIMARY KEY CHECK(singleton=1), ledger_path TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS frozen_submissions (unit_id TEXT "
        "PRIMARY KEY REFERENCES processing_units(unit_id), capture_hash "
        "TEXT NOT NULL, submission_hash TEXT NOT NULL, body_bytes BLOB NOT "
        "NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS submission_attempts (attempt_id "
        "INTEGER PRIMARY KEY AUTOINCREMENT, unit_id TEXT NOT NULL "
        "REFERENCES frozen_submissions(unit_id), submission_hash TEXT NOT "
        "NULL, stage TEXT NOT NULL, event_id TEXT NOT NULL, canonical_hash "
        "TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS submission_observations "
        "(observation_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id "
        "INTEGER NOT NULL UNIQUE REFERENCES "
        "submission_attempts(attempt_id), disposition TEXT NOT NULL, "
        "result_json TEXT NOT NULL)"
    ),
)


class SubmissionStore:
    def __init__(self, capture_store):
        self.path = Path(capture_store.path).resolve()
        self._closed = False
        with self.transaction() as conn:
            for ddl in DDL:
                conn.execute(ddl)
            for table in TABLES:
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, "
                        f"'append-only submission record'); END"
                    )

    @contextmanager
    def transaction(self):
        require(not self._closed, "submission_store_closed")
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self):
        self._closed = True

    def _capture(self, conn, unit):
        require(isinstance(unit, ProcessingUnit), "processing_unit_required")
        row = conn.execute(
            "SELECT * FROM processing_units WHERE unit_id=?", (unit.unit_id,)
        ).fetchone()
        require(row is not None, "missing_processing_unit")
        for key in (
            "logical_key",
            "processing_version",
            "company_key",
            "mapping_identity",
            "selection_identity",
            "adapter_identity",
            "reprocesses",
        ):
            require(typed_equal(row[key], getattr(unit, key)), "processing_identity_mismatch")
        semantic = conn.execute(
            "SELECT semantic_json FROM source_identities WHERE logical_key=?", (unit.logical_key,)
        ).fetchone()
        require(
            semantic is not None
            and typed_equal(
                strict_json_loads(semantic[0].encode()), unit.notification.semantic_body()
            ),
            "source_identity_mismatch",
        )
        row = conn.execute("SELECT * FROM captures WHERE unit_id=?", (unit.unit_id,)).fetchone()
        require(row is not None, "missing_frozen_capture")
        frozen = FrozenCapture(row["body_json"].encode())
        cap = frozen.document
        try:
            checked = validate_capture(cap, canonical_hash(cap))
        except ValueError as exc:
            raise SubmissionError("invalid_capture") from exc
        require(
            checked.unit_id == unit.unit_id
            and cap["company"]
            == {
                "portal_id": unit.notification.portal_id,
                "company_id": unit.notification.company_id,
            },
            "capture_unit_mismatch",
        )
        require(
            row["capture_sequence"] == cap["capture_sequence"]
            and row["company_key"] == unit.company_key,
            "capture_projection_mismatch",
        )
        raw = conn.execute(
            "SELECT raw_body FROM raw_notifications WHERE raw_ref=?", (cap["raw_notification_ref"],)
        ).fetchone()
        require(
            raw is not None and "sha256:" + digest(bytes(raw[0])) == cap["raw_notification_ref"],
            "missing_or_corrupt_raw",
        )
        try:
            notification = SourceNotification.from_payload(
                strict_json_loads(bytes(raw[0])), raw_body=bytes(raw[0])
            )
        except ValueError as exc:
            raise SubmissionError("invalid_original_raw") from exc
        require(
            typed_equal(notification.semantic_body(), cap["notification"]), "raw_semantic_mismatch"
        )
        return frozen

    def capture(self, unit):
        with self.transaction() as conn:
            return self._capture(conn, unit)

    def _load(self, conn, unit):
        capture = self._capture(conn, unit)
        row = conn.execute(
            "SELECT * FROM frozen_submissions WHERE unit_id=?", (unit.unit_id,)
        ).fetchone()
        if row is None:
            return None
        frozen = FrozenSubmission(bytes(row["body_bytes"]))
        d = frozen.document
        require(
            frozen.submission_hash == row["submission_hash"]
            and d["unit_id"] == unit.unit_id
            and d["capture_hash"] == row["capture_hash"] == canonical_hash(capture.document)
            and d["capture_bytes_sha256"] == digest(capture.canonical_bytes)
            and d["account_ref"] == account_identity(capture.document),
            "frozen_binding_mismatch",
        )
        for stage in d["stages"]:
            if stage["stage"] in ("evidence", "decision"):
                require(
                    typed_equal(stage["envelope"]["payload"]["capture"], capture.document),
                    "frozen_capture_bytes_mismatch",
                )
        return frozen

    def load(self, unit):
        with self.transaction() as conn:
            return self._load(conn, unit)

    def target(self, ledger_path, *, bind=False, conn=None):
        if conn is None:
            with self.transaction() as own:
                return self.target(ledger_path, bind=bind, conn=own)
        ledger_path = str(Path(ledger_path).resolve())
        require(ledger_path != str(self.path), "domain_and_operational_store_must_differ")
        row = conn.execute("SELECT * FROM submission_target").fetchall()
        require(
            not row
            or (
                len(row) == 1 and row[0]["singleton"] == 1 and row[0]["ledger_path"] == ledger_path
            ),
            "submission_target_mismatch",
        )
        if not row and bind:
            conn.execute("INSERT INTO submission_target VALUES(1,?)", (ledger_path,))
        elif not row:
            require(
                conn.execute("SELECT 1 FROM frozen_submissions LIMIT 1").fetchone() is None,
                "missing_target_binding",
            )

    def freeze(self, unit, candidate, ledger_path):
        with self.transaction() as conn:
            self.target(ledger_path, bind=True, conn=conn)
            winner = self._load(conn, unit)
            if winner is not None:
                require(
                    winner.submission_hash != candidate.submission_hash
                    or winner.canonical_bytes == candidate.canonical_bytes,
                    "submission_digest_collision",
                )
                return winner
            d = candidate.document
            conn.execute(
                "INSERT INTO frozen_submissions VALUES(?,?,?,?)",
                (
                    unit.unit_id,
                    d["capture_hash"],
                    candidate.submission_hash,
                    candidate.canonical_bytes,
                ),
            )
            return self._load(conn, unit)

    def attempt(self, frozen, stage):
        with self.transaction() as conn:
            return conn.execute(
                (
                    "INSERT INTO submission_attempts "
                    "(unit_id,submission_hash,stage,event_id,canonical_hash) "
                    "VALUES(?,?,?,?,?)"
                ),
                (
                    frozen.document["unit_id"],
                    frozen.submission_hash,
                    stage["stage"],
                    stage["event_id"],
                    stage["canonical_hash"],
                ),
            ).lastrowid

    def observe(self, attempt, disposition, result):
        require(
            disposition
            in ("accepted_created", "accepted_duplicate", "retryable", "rejected", "unknown"),
            "invalid_observation_disposition",
        )
        raw = canonical_text(result)
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM submission_observations WHERE attempt_id=?", (attempt,)
            ).fetchone()
            if old:
                require(
                    old["disposition"] == disposition and old["result_json"] == raw,
                    "contradictory_terminal_observation",
                )
                return
            conn.execute(
                (
                    "INSERT INTO submission_observations "
                    "(attempt_id,disposition,result_json) VALUES(?,?,?)"
                ),
                (attempt, disposition, raw),
            )

    def history(self, frozen):
        d = frozen.document
        stages = {s["stage"]: s for s in d["stages"]}
        accepted = set()
        failure = None
        latest = None
        with self.transaction() as conn:
            attempts = conn.execute(
                (
                    "SELECT a.*,o.observation_id,o.disposition,o.result_json FROM "
                    "submission_attempts a LEFT JOIN submission_observations o ON "
                    "a.attempt_id=o.attempt_id WHERE a.unit_id=? ORDER BY "
                    "o.observation_id, a.attempt_id"
                ),
                (d["unit_id"],),
            ).fetchall()
        for row in attempts:
            stage = stages.get(row["stage"])
            require(
                stage is not None
                and row["submission_hash"] == frozen.submission_hash
                and row["event_id"] == stage["event_id"]
                and row["canonical_hash"] == stage["canonical_hash"],
                "attempt_binding_mismatch",
            )
            if row["observation_id"] is None:
                continue
            try:
                result = strict_json_loads(row["result_json"].encode())
            except ValueError as exc:
                raise SubmissionError("invalid_receipt_json") from exc
            require(
                type(result) is dict and canonical_text(result) == row["result_json"],
                "invalid_receipt_shape",
            )
            disposition = row["disposition"]
            require(
                disposition
                in ("accepted_created", "accepted_duplicate", "retryable", "rejected", "unknown"),
                "invalid_receipt_disposition",
            )
            latest = (row["stage"], disposition, result)
            predecessors = list(stages)[: list(stages).index(row["stage"])]
            require(all(s in accepted for s in predecessors), "receipt_skips_predecessor")
            if disposition.startswith("accepted_"):
                require(
                    typed_equal(
                        result,
                        {
                            "status": disposition.removeprefix("accepted_"),
                            "event_id": stage["event_id"],
                            "canonical_hash": stage["canonical_hash"],
                            "http_status": 201 if disposition == "accepted_created" else 200,
                        },
                    ),
                    "accepted_receipt_mismatch",
                )
                require(
                    all(s in accepted for s in predecessors) and failure is None,
                    "receipt_chain_contradiction",
                )
                accepted.add(row["stage"])
            elif disposition == "rejected":
                failure = (row["stage"], result)
        return accepted, failure, latest
