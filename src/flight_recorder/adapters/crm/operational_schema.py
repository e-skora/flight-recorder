"""Exact new-file operational format2; no migration or domain tables."""


def capture_schema(base):
    result = dict(base)
    result[("table", "crm_store_meta")] = """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=2))"""
    tables = {
        "crm_frozen_captures": """unit_id TEXT PRIMARY KEY NOT NULL,
            capture_hash TEXT NOT NULL UNIQUE, target_key TEXT NOT NULL, claim_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>0),
            capture_sequence INTEGER NOT NULL CHECK(capture_sequence>0),
            config_binding TEXT NOT NULL, observation_bytes BLOB NOT NULL, body BLOB NOT NULL,
            UNIQUE(target_key,claim_id,generation), UNIQUE(target_key,capture_sequence)""",
        "crm_read_observations": """fact_id TEXT PRIMARY KEY NOT NULL,
            target_key TEXT NOT NULL, claim_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>0),
            body BLOB NOT NULL, body_sha256 TEXT NOT NULL,
            UNIQUE(target_key,claim_id,generation)""",
        "crm_source_versions": """target_key TEXT NOT NULL, precondition TEXT NOT NULL,
            semantic_hash TEXT NOT NULL, PRIMARY KEY(target_key,precondition)""",
    }
    keys = {
        "crm_frozen_captures": [
            ("unit_id",),
            ("capture_hash",),
            ("target_key", "claim_id", "generation"),
            ("target_key", "capture_sequence"),
        ],
        "crm_read_observations": [("fact_id",), ("target_key", "claim_id", "generation")],
        "crm_source_versions": [("target_key", "precondition")],
    }
    for table, columns in tables.items():
        result[("table", table)] = f"CREATE TABLE {table} ({columns}) WITHOUT ROWID"
        for operation in ["UPDATE", "DELETE"]:
            name = f"{table}_no_{operation.lower()}"
            result[("trigger", name)] = f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable CRM operational record'); END"""
        condition = " OR ".join(
            "(" + " AND ".join(f"{k}=NEW.{k}" for k in group) + ")" for group in keys[table]
        )
        name = f"{table}_no_replace"
        result[("trigger", name)] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT,'immutable CRM operational record'); END"""
    result[("table", "crm_polling_state")] = """CREATE TABLE crm_polling_state (
        target_key TEXT PRIMARY KEY NOT NULL,
        capture_sequence INTEGER NOT NULL CHECK(capture_sequence>=0),
        last_unit_id TEXT, input_digest TEXT, processing_version TEXT,
        last_fact_id TEXT NOT NULL) WITHOUT ROWID"""
    return result
