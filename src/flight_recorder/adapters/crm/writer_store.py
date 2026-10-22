"""Same-file immutable write facts and audits derived independently of mutable selectors."""

import hashlib

from flight_recorder.adapters.crm.contracts import CRMTarget, require
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.adapters.crm.writer_contracts import (
    CRMWriteIntent,
    CRMWritePolicy,
    WriteProgress,
)
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.crm_operations import (
    TERMINAL,
    operation_history,
    request_digest,
    verify_original,
)
from flight_recorder.collector.schema import is_stored_timestamp, validate_envelope_json

FACT_TABLES = (
    "crm_write_policies",
    "crm_write_intents",
    "crm_write_outbox",
    "crm_write_receipts",
    "crm_write_attempts",
    "crm_write_terminals",
    "crm_write_local_refusals",
)


def rows(conn, table):
    require(
        table in (*FACT_TABLES, "crm_write_progress", "crm_target_ownership"),
        "write_store_integrity",
    )
    cursor = conn.execute("SELECT * FROM " + table)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def unpack(row):
    body = row["body"]
    require(
        type(body) is bytes and hashlib.sha256(body).hexdigest() == row["body_sha256"],
        "write_fact_integrity",
    )
    document = strict_json(body)
    require(type(document) is dict and canonical_bytes(document) == body, "write_fact_integrity")
    return document


def put(conn, table, document, **projection):
    require(table in FACT_TABLES, "write_store_integrity")
    body = canonical_bytes(document)
    values = {**projection, "body": body, "body_sha256": hashlib.sha256(body).hexdigest()}
    columns = ",".join(values)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def normalized(document):
    parsed = validate_envelope_json(canonical_bytes(document)).model_dump(mode="json")
    require(
        parsed["event_type"] == "external_operation.observed"
        and parsed["schema_version"] == "2"
        and canonical_bytes(parsed) == canonical_bytes(document),
        "write_outbox_integrity",
    )
    return parsed


class WriterStore:
    def __init__(self, runtime):
        require(
            runtime.writer_capable and runtime.store._format_version == 5, "writer_not_initialized"
        )
        self.runtime, self.store = runtime, runtime.store

    def policy_history(self, conn):
        result = {}
        cfg, binding = self.runtime.config, self.runtime.config.connection
        for row in sorted(
            rows(conn, "crm_write_policies"), key=lambda r: (r["connection_id"], r["generation"])
        ):
            d = unpack(row)
            require(
                set(d)
                == {
                    "format_version",
                    "policy",
                    "config_binding",
                    "connection_binding",
                    "artifact_hash",
                    "evaluator_version",
                    "recorded_at",
                }
                and type(d["format_version"]) is int
                and d["format_version"] == 1
                and is_stored_timestamp(d["recorded_at"]),
                "write_policy_integrity",
            )
            policy = CRMWritePolicy.from_document(d["policy"])
            key = (policy.connection_id, policy.generation)
            require(
                key == (row["connection_id"], row["generation"]) and key not in result,
                "write_policy_integrity",
            )
            require(
                policy.generation
                == max([g for c, g in result if c == policy.connection_id], default=0) + 1,
                "write_policy_integrity",
            )
            require(
                all(
                    type(d[k]) is str
                    and len(d[k]) == 64
                    and all(c in "0123456789abcdef" for c in d[k])
                    for k in ("config_binding", "connection_binding", "artifact_hash")
                ),
                "write_policy_integrity",
            )
            require(
                type(d["evaluator_version"]) is str and bool(d["evaluator_version"]),
                "write_policy_integrity",
            )
            # This operational runtime is pinned, including retained poll history.
            # Policy generations may change enablement/subsets, never that binding.
            require(
                d["config_binding"] == cfg.binding
                and d["connection_binding"] == binding.binding
                and d["artifact_hash"] == binding.artifact_hash
                and d["evaluator_version"] == binding.evaluator_version
                and policy.connection_id == binding.connection_id
                and policy.expected_instance_id == binding.expected_instance_id
                and policy.mapping_hash == binding.mapping_hash
                and policy.selection_identity == cfg.document["selection_identity"]
                and set(policy.target_keys) <= {t.key for t in cfg.targets},
                "write_policy_binding",
            )
            prior = result.get((policy.connection_id, policy.generation - 1))
            require(
                prior is None or d["recorded_at"] >= prior["recorded_at"],
                "write_policy_time_order",
            )
            result[key] = d
        return result

    def audit(self):
        self.runtime.authority.assert_held()
        # Qualify every table/trigger again without creating or repairing a file.
        probe = CRMStore(self.store.path, format_version=5, create=False)
        probe.close()
        with self.store._read() as conn:
            conn.execute("BEGIN")
            return self._audit(conn)

    def _audit(self, conn, *, check_progress=True):
        require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "write_store_integrity")
        policies = self.policy_history(conn)
        intents = {}
        for row in rows(conn, "crm_write_intents"):
            d = unpack(row)
            frozen = CRMWriteIntent.from_document(d)
            require(
                frozen.canonical_bytes == row["body"]
                and row["operation_id"] == d["operation_id"]
                and row["target_key"] == CRMTarget(**d["target"]).key
                and row["decision_event_id"] == d["decision_event_id"]
                and row["revision"] == d["revision"],
                "write_intent_integrity",
            )
            policy_doc = policies.get((d["connection_id"], d["policy_generation"]))
            require(policy_doc is not None, "write_policy_missing")
            p = policy_doc["policy"]
            require(
                p["enabled"]
                and p["expected_instance_id"] == d["target"]["instance_id"]
                and row["target_key"] in p["target_keys"]
                and p["mapping_hash"] == d["mapping_hash"]
                and p["selection_identity"] == d["selection_identity"]
                and policy_doc["artifact_hash"] == d["artifact_hash"]
                and policy_doc["evaluator_version"] == d["evaluator_version"],
                "write_policy_binding",
            )
            require(d["created_at"] >= policy_doc["recorded_at"], "write_policy_time_order")
            intents[d["operation_id"]] = frozen
        boxes = {op: [] for op in intents}
        by_event = {}
        for row in rows(conn, "crm_write_outbox"):
            d = normalized(unpack(row))
            p = d["payload"]
            op = p["intent"]["operation_id"]
            require(
                op in intents
                and row["operation_id"] == op
                and row["event_id"] == d["event_id"]
                and row["observation_sequence"] == p["observation_sequence"]
                and canonical_bytes(p["intent"]) == intents[op].canonical_bytes,
                "write_outbox_integrity",
            )
            boxes[op].append(d)
            by_event[d["event_id"]] = d
        receipts = {}
        for row in rows(conn, "crm_write_receipts"):
            d = unpack(row)
            event_id = row["event_id"]
            require(
                event_id in by_event
                and set(d)
                == {"format_version", "event_id", "canonical_hash", "status", "http_status"}
                and type(d["format_version"]) is int
                and d["format_version"] == 1
                and d["event_id"] == event_id
                and d["canonical_hash"] == canonical_hash(by_event[event_id])
                and (
                    (
                        d["status"] == "created"
                        and type(d["http_status"]) is int
                        and d["http_status"] == 201
                    )
                    or (
                        d["status"] == "duplicate"
                        and type(d["http_status"]) is int
                        and d["http_status"] == 200
                    )
                ),
                "write_receipt_integrity",
            )
            receipts[event_id] = d
        attempts = {}
        terminals = {}
        refusals = {}
        for table, destination in [
            ("crm_write_attempts", attempts),
            ("crm_write_terminals", terminals),
            ("crm_write_local_refusals", refusals),
        ]:
            for row in rows(conn, table):
                d = unpack(row)
                op = row["operation_id"]
                require(op in intents and d.get("operation_id") == op, "write_orphan_fact")
                destination[op] = (row, d)
        owners = {r["target_key"]: r for r in rows(conn, "crm_target_ownership")}
        progress = {r["operation_id"]: r for r in rows(conn, "crm_write_progress")}
        require(not set(progress) - set(intents), "write_orphan_progress")
        history = {}
        expected = {}
        accounts = {CRMTarget(**f.document["target"]).account_ref for f in intents.values()}
        accounts.update(t.account_ref for t in self.runtime.config.targets)
        with self.runtime.collector.engine.connect() as domain:
            for account in accounts:
                records = operation_history(domain, account)
                for d in records:
                    op = d["payload"]["intent"]["operation_id"]
                    require(op in intents, "write_unowned_domain_operation")
                    history.setdefault(op, []).append(d)
            for op, frozen in intents.items():
                i = frozen.document
                target = CRMTarget(**i["target"])
                verify_original(domain, target.account_ref, i)
                docs = sorted(boxes[op], key=lambda d: d["payload"]["observation_sequence"])
                require(
                    bool(docs)
                    and [d["payload"]["observation_sequence"] for d in docs]
                    == list(range(1, len(docs) + 1)),
                    "write_outbox_sequence",
                )
                require(
                    docs[0]["payload"]["phase"] == "prepared"
                    and docs[0]["payload"]["state"] == "prepared",
                    "write_outbox_sequence",
                )
                require(
                    all(
                        d["account_ref"] == target.account_ref
                        and d["payload"]["observation_origin"]
                        == docs[0]["payload"]["observation_origin"]
                        for d in docs
                    ),
                    "write_outbox_origin",
                )
                actual = history.get(op, [])
                require(
                    len(actual) <= len(docs)
                    and all(
                        canonical_bytes(a) == canonical_bytes(b)
                        for a, b in zip(actual, docs, strict=False)
                    ),
                    "write_domain_outbox_binding",
                )
                accepted = [n for n, d in enumerate(docs, 1) if d["event_id"] in receipts]
                require(
                    accepted == list(range(1, len(accepted) + 1)) and len(accepted) <= len(actual),
                    "write_receipt_sequence",
                )
                n = len(accepted)
                marker = [d for d in docs if d["payload"]["phase"] == "send_started"]
                require(
                    len(marker) <= 1 and bool(marker) == (op in attempts), "write_attempt_integrity"
                )
                attempted = bool(marker)
                generation = None
                if attempted:
                    row, a = attempts[op]
                    require(
                        set(a)
                        == {
                            "format_version",
                            "operation_id",
                            "marker_event_id",
                            "marker_hash",
                            "target_key",
                            "generation",
                            "request_hash",
                            "credential_generation",
                            "config_binding",
                        }
                        and type(a["format_version"]) is int
                        and a["format_version"] == 1
                        and marker[0]["payload"]["observation_sequence"] == 2
                        and a["marker_event_id"] == marker[0]["event_id"]
                        and a["marker_hash"] == canonical_hash(marker[0])
                        and a["target_key"] == target.key
                        and a["request_hash"] == request_digest(i)
                        and type(a["generation"]) is int
                        and a["generation"] > 0
                        and type(a["credential_generation"]) is int
                        and a["credential_generation"] > 0
                        and a["config_binding"]
                        == policies[(i["connection_id"], i["policy_generation"])]["config_binding"]
                        and row["marker_event_id"] == a["marker_event_id"]
                        and row["target_key"] == target.key
                        and row["generation"] == a["generation"],
                        "write_attempt_integrity",
                    )
                    generation = a["generation"]
                else:
                    require(
                        all(d["payload"]["phase"] in ("prepared", "refused") for d in docs),
                        "write_attempt_missing",
                    )
                owner = owners.get(target.key)
                state = "unknown" if attempted else "prepared"
                reason = "request_result_unknown" if attempted else "prepared"
                for d in docs[:n]:
                    if d["payload"]["state"] not in TERMINAL:
                        state = d["payload"]["state"]
                if attempted and state == "prepared":
                    state = "unknown"
                if state == "awaiting_observation":
                    reason = "awaiting_observation"
                if op in refusals:
                    _, r = refusals[op]
                    require(
                        set(r)
                        == {
                            "format_version",
                            "operation_id",
                            "event_id",
                            "event_hash",
                            "reason",
                            "collector_reason",
                        }
                        and type(r["format_version"]) is int
                        and r["format_version"] == 1
                        and r["event_id"] == docs[0]["event_id"]
                        and r["event_hash"] == canonical_hash(docs[0])
                        and r["reason"] == "collector_preparation_refused"
                        and type(r["collector_reason"]) is str
                        and r["collector_reason"]
                        and all(
                            c in "abcdefghijklmnopqrstuvwxyz0123456789_"
                            for c in r["collector_reason"]
                        )
                        and not attempted
                        and not actual
                        and not n
                        and op not in terminals,
                        "write_nonadmission_integrity",
                    )
                    state, reason = "refused", "collector_preparation_refused"
                if op in terminals:
                    row, t = terminals[op]
                    last = docs[-1]
                    require(
                        set(t)
                        == {
                            "format_version",
                            "operation_id",
                            "terminal_event_id",
                            "terminal_hash",
                            "state",
                            "generation",
                        }
                        and type(t["format_version"]) is int
                        and t["format_version"] == 1
                        and t["terminal_event_id"] == last["event_id"]
                        and row["terminal_event_id"] == last["event_id"]
                        and t["terminal_hash"] == canonical_hash(last)
                        and n == len(docs)
                        and last["payload"]["state"] in TERMINAL
                        and t["state"] == last["payload"]["state"]
                        and (
                            type(t["generation"]) is int
                            if generation is not None
                            else t["generation"] is None
                        )
                        and t["generation"] == generation
                        and op not in refusals,
                        "write_terminal_integrity",
                    )
                    state = t["state"]
                    reason = last["payload"]["reason"] or state
                    if attempted:
                        require(
                            owner is not None
                            and owner["generation"] >= generation
                            and not (
                                owner["unit_id"] == op
                                and owner["generation"] == generation
                                and owner["state"] == "held"
                            ),
                            "write_terminal_owner_integrity",
                        )
                elif attempted:
                    require(
                        owner is not None
                        and (owner["unit_id"], owner["generation"], owner["state"])
                        == (op, generation, "held"),
                        "write_uncertainty_fence_missing",
                    )
                expected[op] = dict(
                    operation_id=op,
                    state=state,
                    reason=reason,
                    attempted=int(attempted),
                    generation=generation,
                    accepted_sequence=n,
                )
                if check_progress:
                    require(progress.get(op) == expected[op], "write_progress_integrity")
        return dict(
            policies=policies,
            intents=intents,
            boxes=boxes,
            receipts=receipts,
            attempts=attempts,
            terminals=terminals,
            refusals=refusals,
            owners=owners,
            expected=expected,
            history=history,
        )

    def sync(self, conn, operation_id, *, new=False):
        expected = self._audit(conn, check_progress=False)["expected"][operation_id]
        current = conn.execute(
            "SELECT 1 FROM crm_write_progress WHERE operation_id=?", (operation_id,)
        ).fetchone()
        require((current is None) == new, "write_progress_integrity")
        if new:
            conn.execute(
                "INSERT INTO crm_write_progress VALUES (?,?,?,?,?,?)", tuple(expected.values())
            )
        else:
            conn.execute(
                "UPDATE crm_write_progress SET state=?,reason=?,attempted=?,generation=?,"
                "accepted_sequence=? WHERE operation_id=?",
                tuple(
                    expected[k]
                    for k in (
                        "state",
                        "reason",
                        "attempted",
                        "generation",
                        "accepted_sequence",
                        "operation_id",
                    )
                ),
            )

    def progress(self, operation_id, snapshot=None):
        snap = snapshot or self.audit()
        require(operation_id in snap["intents"], "write_intent_missing")
        e = snap["expected"][operation_id]
        return WriteProgress(
            operation_id,
            e["state"],
            e["reason"],
            bool(e["accepted_sequence"]),
            bool(e["attempted"]),
            bool(e["attempted"] and operation_id not in snap["terminals"]),
            len(snap["boxes"][operation_id]),
        )

    def append(self, conn, document):
        d = normalized(document)
        p = d["payload"]
        put(
            conn,
            "crm_write_outbox",
            d,
            event_id=d["event_id"],
            operation_id=p["intent"]["operation_id"],
            observation_sequence=p["observation_sequence"],
        )

    def acknowledge(self, conn, document, result):
        event_id = document["event_id"]
        require(
            getattr(result, "event_id", None) == event_id
            and getattr(result, "canonical_hash", None) == canonical_hash(document)
            and (
                (
                    result.status == "created"
                    and type(result.http_status) is int
                    and result.http_status == 201
                )
                or (
                    result.status == "duplicate"
                    and type(result.http_status) is int
                    and result.http_status == 200
                )
            ),
            "invalid_write_collector_ack",
        )
        existing = conn.execute(
            "SELECT event_id FROM crm_write_receipts WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is None:
            put(
                conn,
                "crm_write_receipts",
                dict(
                    format_version=1,
                    event_id=event_id,
                    canonical_hash=result.canonical_hash,
                    status=result.status,
                    http_status=result.http_status,
                ),
                event_id=event_id,
            )
        self.sync(conn, document["payload"]["intent"]["operation_id"])
