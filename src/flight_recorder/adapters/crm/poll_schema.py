"""New-only format4 adds bounded poll work without altering formats1/2/3."""


def poll_schema(base):
    result = dict(base)
    result[("table", "crm_store_meta")] = """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=4))"""
    tables = {
        "crm_runtime_binding": (
            (
                "singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1),body "
                "BLOB NOT NULL,body_sha256 TEXT NOT NULL"
            ),
            [("singleton",)],
        ),
        "crm_poll_ticks": (
            (
                "connection_id TEXT NOT NULL,tick_id TEXT NOT NULL,ordinal "
                "INTEGER NOT NULL CHECK(ordinal>0),body BLOB NOT NULL,body_sha256 "
                "TEXT NOT NULL,PRIMARY KEY(connection_id,tick_id),UNIQUE(connectio"
                "n_id,ordinal),FOREIGN KEY(connection_id,tick_id) REFERENCES "
                "crm_receipts(connection_id,receipt_id)"
            ),
            [("connection_id", "tick_id"), ("connection_id", "ordinal")],
        ),
        "crm_poll_terminals": (
            (
                "terminal_id TEXT PRIMARY KEY NOT NULL,connection_id TEXT NOT "
                "NULL,tick_id TEXT NOT NULL,target_key TEXT NOT NULL,body BLOB "
                "NOT NULL,body_sha256 TEXT NOT NULL,UNIQUE(connection_id,tick_id,t"
                "arget_key),FOREIGN KEY(connection_id,tick_id) REFERENCES "
                "crm_poll_ticks(connection_id,tick_id)"
            ),
            [("terminal_id",), ("connection_id", "tick_id", "target_key")],
        ),
        "crm_poll_recoveries": (
            (
                "recovery_id TEXT PRIMARY KEY NOT NULL,target_key TEXT NOT "
                "NULL,claim_id TEXT NOT NULL,generation INTEGER NOT NULL,body "
                "BLOB NOT NULL,body_sha256 TEXT NOT "
                "NULL,UNIQUE(target_key,claim_id,generation)"
            ),
            [("recovery_id",), ("target_key", "claim_id", "generation")],
        ),
    }
    for table, (columns, keys) in tables.items():
        result[("table", table)] = f"CREATE TABLE {table} ({columns}) WITHOUT ROWID"
        for op in ["UPDATE", "DELETE"]:
            name = f"{table}_no_{op.lower()}"
            result[("trigger", name)] = (
                f"CREATE TRIGGER {name} BEFORE {op} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'immutable CRM poll fact'); END"
            )
        condition = " OR ".join(
            "(" + " AND ".join(f"{k}=NEW.{k}" for k in group) + ")" for group in keys
        )
        name = f"{table}_no_replace"
        result[("trigger", name)] = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition}) "
            "BEGIN SELECT RAISE(ABORT,'immutable CRM poll fact'); END"
        )
    result[("table", "crm_poll_progress")] = (
        "CREATE TABLE crm_poll_progress (\n        connection_id TEXT NOT "
        "NULL,tick_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN "
        "('active','waiting','coalesced','complete')),\n        PRIMARY "
        "KEY(connection_id,tick_id),FOREIGN KEY(connection_id,tick_id) "
        "REFERENCES crm_poll_ticks(connection_id,tick_id)) WITHOUT ROWID"
    )
    result[("table", "crm_poll_members")] = (
        "CREATE TABLE crm_poll_members (\n        connection_id TEXT NOT "
        "NULL,tick_id TEXT NOT NULL,target_key TEXT NOT NULL,claim_id "
        "TEXT NOT NULL,\n        generation INTEGER,state TEXT NOT NULL "
        "CHECK(state IN ('queued','claimed','read_started','blocked','term"
        "inal')),\n        reason TEXT,terminal_id TEXT REFERENCES "
        "crm_poll_terminals(terminal_id),\n        PRIMARY "
        "KEY(connection_id,tick_id,target_key),FOREIGN "
        "KEY(connection_id,tick_id) REFERENCES "
        "crm_poll_ticks(connection_id,tick_id)) WITHOUT ROWID"
    )
    return result
