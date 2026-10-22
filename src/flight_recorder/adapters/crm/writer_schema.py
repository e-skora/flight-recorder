"""Explicit format5 adds writer facts to unchanged format4; opening never migrates."""


def writer_schema(base):
    result = dict(base)
    result[("table", "crm_store_meta")] = """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=5))"""
    tables = {
        "crm_write_policies": (
            "connection_id TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),"
            "body BLOB NOT NULL,body_sha256 TEXT NOT NULL,PRIMARY KEY(connection_id,generation)",
            [("connection_id", "generation")],
        ),
        "crm_write_intents": (
            "operation_id TEXT PRIMARY KEY NOT NULL,target_key TEXT NOT NULL,decision_event_id "
            "TEXT NOT NULL UNIQUE,revision INTEGER NOT NULL CHECK(revision>0),body BLOB NOT NULL,"
            "body_sha256 TEXT NOT NULL",
            [("operation_id",), ("decision_event_id",)],
        ),
        "crm_write_outbox": (
            "event_id TEXT PRIMARY KEY NOT NULL,operation_id TEXT NOT NULL REFERENCES "
            "crm_write_intents(operation_id),observation_sequence INTEGER NOT NULL CHECK"
            "(observation_sequence>0),body BLOB NOT NULL,body_sha256 TEXT NOT NULL,UNIQUE"
            "(operation_id,observation_sequence)",
            [("event_id",), ("operation_id", "observation_sequence")],
        ),
        "crm_write_receipts": (
            "event_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_write_outbox(event_id),"
            "body BLOB NOT NULL,body_sha256 TEXT NOT NULL",
            [("event_id",)],
        ),
        "crm_write_attempts": (
            "operation_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_write_intents(operation_id),"
            "marker_event_id TEXT NOT NULL UNIQUE REFERENCES crm_write_outbox(event_id),"
            "target_key TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),"
            "body BLOB NOT NULL,body_sha256 TEXT NOT NULL",
            [("operation_id",), ("marker_event_id",)],
        ),
        "crm_write_terminals": (
            "operation_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_write_intents(operation_id),"
            "terminal_event_id TEXT NOT NULL UNIQUE REFERENCES crm_write_outbox(event_id),"
            "body BLOB NOT NULL,body_sha256 TEXT NOT NULL",
            [("operation_id",), ("terminal_event_id",)],
        ),
        "crm_write_local_refusals": (
            "operation_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_write_intents(operation_id),"
            "body BLOB NOT NULL,body_sha256 TEXT NOT NULL",
            [("operation_id",)],
        ),
    }
    for table, (columns, keys) in tables.items():
        result[("table", table)] = f"CREATE TABLE {table} ({columns}) WITHOUT ROWID"
        for op in ("UPDATE", "DELETE"):
            name = f"{table}_no_{op.lower()}"
            result[("trigger", name)] = (
                f"CREATE TRIGGER {name} BEFORE {op} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'immutable CRM write fact'); END"
            )
        condition = " OR ".join(
            "(" + " AND ".join(f"{k}=NEW.{k}" for k in group) + ")" for group in keys
        )
        name = f"{table}_no_replace"
        result[("trigger", name)] = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition}) "
            "BEGIN SELECT RAISE(ABORT,'immutable CRM write fact'); END"
        )
    result[("table", "crm_write_progress")] = (
        "CREATE TABLE crm_write_progress (operation_id TEXT PRIMARY KEY NOT NULL REFERENCES "
        "crm_write_intents(operation_id),state TEXT NOT NULL CHECK(state IN "
        "('prepared','refused','unknown','awaiting_observation','observed_applied_at_t',"
        "'refused_no_effect')),reason TEXT NOT NULL,attempted INTEGER NOT NULL CHECK"
        "(attempted IN (0,1)),generation INTEGER,accepted_sequence INTEGER NOT NULL "
        "CHECK(accepted_sequence>=0)) WITHOUT ROWID"
    )
    return result
