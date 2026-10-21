"""Same-file durable intents/outbox and shared capture/writer fencing."""

from contextlib import contextmanager
from pathlib import Path

from flight_recorder.adapters.hubspot.writer_contracts import WriteIntent, WriteProgress, require
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash, canonical_text
from flight_recorder.collector.schema import validate_envelope_json

DDL = (
    (
        "CREATE TABLE IF NOT EXISTS writer_intents (operation_id TEXT "
        "PRIMARY KEY, company_key TEXT NOT NULL, decision_event_id "
        "TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL, account_ref "
        "TEXT NOT NULL, body_bytes BLOB NOT NULL, "
        "UNIQUE(company_key,revision))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS writer_observations "
        "(observation_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "operation_id TEXT NOT NULL REFERENCES "
        "writer_intents(operation_id), observation_sequence INTEGER "
        "NOT NULL, event_id TEXT NOT NULL UNIQUE, body_bytes BLOB NOT "
        "NULL, UNIQUE(operation_id,observation_sequence))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS writer_receipts (event_id TEXT "
        "PRIMARY KEY REFERENCES writer_observations(event_id), "
        "canonical_hash TEXT NOT NULL, ack_json TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS writer_progress (operation_id TEXT "
        "PRIMARY KEY REFERENCES writer_intents(operation_id), state "
        "TEXT NOT NULL, reason TEXT NOT NULL, attempted INTEGER NOT "
        "NULL, generation INTEGER)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS writer_targets (company_key TEXT "
        "PRIMARY KEY, outstanding_operation_id TEXT REFERENCES "
        "writer_intents(operation_id))"
    ),
)
TERMINAL = frozenset({"superseded", "observed_applied_at_t", "observed_not_applied_at_t"})


class WriterStore:
    def __init__(self, submission_store):
        self.submission = submission_store
        self.path = Path(submission_store.path).resolve()
        self._closed = False
        with self.transaction() as conn:
            for statement in DDL:
                conn.execute(statement)
            for table in ("writer_intents", "writer_observations", "writer_receipts"):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,"
                        "'append-only writer record'); END"
                    )

    @contextmanager
    def transaction(self):
        require(not self._closed, "writer_store_closed")
        with self.submission.transaction() as conn:
            yield conn

    def close(self):
        self._closed = True
        self.submission.close()

    def load(self, operation_id, conn=None):
        if conn is None:
            with self.transaction() as own:
                return self.load(operation_id, own)
        row = conn.execute(
            "SELECT * FROM writer_intents WHERE operation_id=?", (operation_id,)
        ).fetchone()
        require(row is not None, "missing_write_intent")
        intent = WriteIntent(bytes(row["body_bytes"]))
        d = intent.document
        require(
            all(
                row[key] == d[key]
                for key in ("operation_id", "company_key", "decision_event_id", "revision")
            ),
            "writer_intent_projection_mismatch",
        )
        from flight_recorder.adapters.hubspot.contracts import strict_json_loads

        account = "fr-account-v2-" + canonical_hash(strict_json_loads(d["company_key"].encode()))
        require(row["account_ref"] == account, "writer_intent_account_mismatch")
        return intent

    def progress(self, operation_id, conn=None):
        if conn is None:
            with self.transaction() as own:
                return self.progress(operation_id, own)
        self.load(operation_id, conn)
        row = conn.execute(
            "SELECT * FROM writer_progress WHERE operation_id=?", (operation_id,)
        ).fetchone()
        require(
            row is not None
            and row["state"] in TERMINAL | {"prepared", "refused", "unknown"}
            and row["attempted"] in (0, 1),
            "writer_progress_integrity",
        )
        require(
            (row["state"] in {"unknown", "observed_applied_at_t", "observed_not_applied_at_t"})
            == bool(row["attempted"]),
            "writer_attempt_integrity",
        )
        documents = [
            self._observation(item)
            for item in conn.execute(
                (
                    "SELECT * FROM writer_observations WHERE operation_id=? ORDER "
                    "BY observation_sequence"
                ),
                (operation_id,),
            ).fetchall()
        ]
        require(
            bool(documents) and documents[0]["payload"]["state"] == "prepared",
            "writer_observation_chain_integrity",
        )
        intent = self.load(operation_id, conn).document
        for index, document in enumerate(documents, 1):
            require(
                document["payload"]["observation_sequence"] == index
                and canonical_bytes(document["payload"]["intent"]) == canonical_bytes(intent),
                "writer_observation_chain_integrity",
            )
        markers = [item for item in documents if item["payload"]["reason"] == "send_started"]
        require(
            len(markers) <= 1 and bool(markers) == bool(row["attempted"]),
            "writer_send_proof_integrity",
        )
        latest = documents[-1]
        if row["state"] != "unknown":
            require(
                latest["payload"]["state"] == row["state"], "writer_progress_observation_integrity"
            )
        if row["state"] in TERMINAL:
            require(self.receipted(conn, latest), "writer_terminal_proof_unacknowledged")
        return WriteProgress(operation_id, row["state"], row["reason"])

    def update_progress(self, conn, operation_id, state, reason, *, attempted=None):
        if attempted is None:
            conn.execute(
                "UPDATE writer_progress SET state=?,reason=? WHERE operation_id=?",
                (state, reason, operation_id),
            )
        else:
            conn.execute(
                "UPDATE writer_progress SET state=?,reason=?,attempted=? WHERE operation_id=?",
                (state, reason, attempted, operation_id),
            )

    def insert_intent(self, conn, intent, account_ref):
        d = intent.document
        conn.execute(
            "INSERT INTO writer_intents VALUES(?,?,?,?,?,?)",
            (
                d["operation_id"],
                d["company_key"],
                d["decision_event_id"],
                d["revision"],
                account_ref,
                intent.canonical_bytes,
            ),
        )
        conn.execute(
            "INSERT INTO writer_progress VALUES(?,?,?,0,NULL)",
            (d["operation_id"], "prepared", "intent_prepared"),
        )
        conn.execute("INSERT OR IGNORE INTO writer_targets VALUES(?,NULL)", (d["company_key"],))

    def append(self, conn, intent, state, reason, at, proof=None):
        d = intent.document
        self.load(d["operation_id"], conn)
        row = conn.execute(
            "SELECT account_ref FROM writer_intents WHERE operation_id=?", (d["operation_id"],)
        ).fetchone()
        sequence = conn.execute(
            (
                "SELECT coalesce(max(observation_sequence),0)+1 FROM "
                "writer_observations WHERE operation_id=?"
            ),
            (d["operation_id"],),
        ).fetchone()[0]
        payload = {
            "intent": d,
            "observation_sequence": sequence,
            "observed_at": at,
            "state": state,
            "reason": reason,
            "observed_properties": proof.properties if proof is not None else None,
            "settled_operation_ids": list(proof.settled_operation_ids) if proof is not None else [],
            "pending_operation_ids": list(proof.pending_operation_ids) if proof is not None else [],
            "not_applied_operation_ids": list(proof.not_applied_operation_ids)
            if proof is not None
            else [],
        }
        event_id = f"fr-write-observation-v1-{d['operation_id']}-{sequence}"
        document = {
            "schema_version": "1",
            "event_id": event_id,
            "event_type": "external_operation.observed",
            "source": "hubspot-synthetic-writer",
            "account_ref": row["account_ref"],
            "occurred_at": at,
            "recorded_at": at,
            "payload": payload,
        }
        normalized = validate_envelope_json(canonical_bytes(document)).model_dump(mode="json")
        latest = self.latest(conn, d["operation_id"])
        require(
            latest is None or at >= latest["payload"]["observed_at"],
            "writer_observation_time_reversed",
        )
        conn.execute(
            (
                "INSERT INTO "
                "writer_observations(operation_id,observation_sequence,event_id"
                ",body_bytes) VALUES(?,?,?,?)"
            ),
            (d["operation_id"], sequence, event_id, canonical_bytes(normalized)),
        )
        return event_id

    def _observation(self, row):
        document = validate_envelope_json(bytes(row["body_bytes"])).model_dump(mode="json")
        require(
            canonical_bytes(document) == bytes(row["body_bytes"])
            and document["event_id"] == row["event_id"]
            and document["payload"]["intent"]["operation_id"] == row["operation_id"]
            and document["payload"]["observation_sequence"] == row["observation_sequence"],
            "writer_observation_integrity",
        )
        return document

    def latest(self, conn, operation_id):
        row = conn.execute(
            (
                "SELECT * FROM writer_observations WHERE operation_id=? ORDER "
                "BY observation_sequence DESC LIMIT 1"
            ),
            (operation_id,),
        ).fetchone()
        return self._observation(row) if row else None

    def pending(self):
        with self.transaction() as conn:
            documents = [
                self._observation(row)
                for row in conn.execute(
                    "SELECT * FROM writer_observations ORDER BY observation_id"
                ).fetchall()
            ]
            return [document for document in documents if not self.receipted(conn, document)]

    def receipted(self, conn, document):
        from flight_recorder.adapters.hubspot.contracts import strict_json_loads

        row = conn.execute(
            "SELECT * FROM writer_receipts WHERE event_id=?", (document["event_id"],)
        ).fetchone()
        if row is None:
            return False
        try:
            ack = strict_json_loads(row["ack_json"].encode())
            require(
                set(ack) == {"status", "http_status", "event_id", "canonical_hash"}
                and ack["status"] in {"created", "duplicate"}
                and type(ack["http_status"]) is int
                and ack["http_status"] == (201 if ack["status"] == "created" else 200)
                and ack["event_id"] == document["event_id"]
                and ack["canonical_hash"] == row["canonical_hash"] == canonical_hash(document)
                and canonical_text(ack) == row["ack_json"],
                "writer_receipt_integrity",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("writer_receipt_integrity") from exc
        return True

    def acknowledge(self, document, result):
        digest = canonical_hash(document)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM writer_receipts WHERE event_id=?", (document["event_id"],)
            ).fetchone()
            if existing:
                require(
                    existing["canonical_hash"] == digest and self.receipted(conn, document),
                    "writer_receipt_changed",
                )
                return
            conn.execute(
                "INSERT INTO writer_receipts VALUES(?,?,?)",
                (
                    document["event_id"],
                    digest,
                    canonical_text(
                        {
                            "status": result.status,
                            "http_status": result.http_status,
                            "event_id": result.event_id,
                            "canonical_hash": result.canonical_hash,
                        }
                    ),
                ),
            )

    def reserve(self, conn, intent, *, reconcile=False):
        d = intent.document
        target = conn.execute(
            "SELECT * FROM writer_targets WHERE company_key=?", (d["company_key"],)
        ).fetchone()
        require(target is not None, "missing_writer_target")
        require(
            target["outstanding_operation_id"] in (None, d["operation_id"]),
            "target_uncertainty_blocked",
        )
        if reconcile:
            require(
                target["outstanding_operation_id"] == d["operation_id"],
                "missing_outstanding_operation",
            )
        row = conn.execute(
            "SELECT * FROM company_ownership WHERE company_key=?", (d["company_key"],)
        ).fetchone()
        progress = conn.execute(
            "SELECT * FROM writer_progress WHERE operation_id=?", (d["operation_id"],)
        ).fetchone()
        if row is not None and row["state"] == "held":
            require(
                reconcile
                and row["owner_unit_id"] == d["unit_id"]
                and row["generation"] == progress["generation"],
                "company_writer_busy",
            )
            generation = row["generation"]
        else:
            generation = (row["generation"] if row is not None else 0) + 1
            if row is None:
                conn.execute(
                    "INSERT INTO company_ownership VALUES(?,?,?,?)",
                    (d["company_key"], generation, "held", d["unit_id"]),
                )
            else:
                conn.execute(
                    (
                        "UPDATE company_ownership SET "
                        "generation=?,state='held',owner_unit_id=? WHERE company_key=?"
                    ),
                    (generation, d["unit_id"], d["company_key"]),
                )
            conn.execute(
                "UPDATE writer_progress SET generation=? WHERE operation_id=?",
                (generation, d["operation_id"]),
            )
        conn.execute(
            "UPDATE writer_targets SET outstanding_operation_id=? WHERE company_key=?",
            (d["operation_id"], d["company_key"]),
        )
        return generation

    def assert_fence(self, conn, intent):
        d = intent.document
        row = conn.execute(
            "SELECT * FROM company_ownership WHERE company_key=?", (d["company_key"],)
        ).fetchone()
        progress = conn.execute(
            "SELECT * FROM writer_progress WHERE operation_id=?", (d["operation_id"],)
        ).fetchone()
        target = conn.execute(
            "SELECT * FROM writer_targets WHERE company_key=?", (d["company_key"],)
        ).fetchone()
        require(
            row is not None
            and row["state"] == "held"
            and row["owner_unit_id"] == d["unit_id"]
            and progress["generation"] == row["generation"]
            and target["outstanding_operation_id"] == d["operation_id"],
            "stale_writer_fence",
        )

    def release(self, conn, intent):
        self.assert_fence(conn, intent)
        d = intent.document
        conn.execute(
            "UPDATE company_ownership SET state='free',owner_unit_id=NULL WHERE company_key=?",
            (d["company_key"],),
        )
        conn.execute(
            "UPDATE writer_targets SET outstanding_operation_id=NULL WHERE company_key=?",
            (d["company_key"],),
        )
