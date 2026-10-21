"""Exact new-file operational format3; retained format1/2 are never extended."""


def submission_schema(base):
    result = dict(base)
    result[("table", "crm_store_meta")] = """CREATE TABLE crm_store_meta (
        format_version INTEGER PRIMARY KEY CHECK(format_version=3))"""
    tables = {
        "crm_submission_domain": (
            """singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1),
            domain_path TEXT NOT NULL""",
            [("singleton",)],
        ),
        "crm_frozen_submissions": (
            """unit_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_frozen_captures(unit_id),
            submission_hash TEXT NOT NULL UNIQUE,target_key TEXT NOT NULL,claim_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>0),body BLOB NOT NULL,
            UNIQUE(target_key,claim_id,generation)""",
            [("unit_id",), ("submission_hash",), ("target_key", "claim_id", "generation")],
        ),
        "crm_submission_attempts": (
            """attempt_id TEXT PRIMARY KEY NOT NULL,
            unit_id TEXT NOT NULL REFERENCES crm_frozen_submissions(unit_id),stage TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal>0),body BLOB NOT NULL,body_sha256 TEXT NOT NULL,
            UNIQUE(unit_id,stage,ordinal)""",
            [("attempt_id",), ("unit_id", "stage", "ordinal")],
        ),
        "crm_submission_observations": (
            """attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES crm_submission_attempts(attempt_id),
            disposition TEXT NOT NULL,body BLOB NOT NULL,body_sha256 TEXT NOT NULL""",
            [("attempt_id",)],
        ),
    }
    for table, (columns, keys) in tables.items():
        result[("table", table)] = f"CREATE TABLE {table} ({columns}) WITHOUT ROWID"
        for operation in ["UPDATE", "DELETE"]:
            name = f"{table}_no_{operation.lower()}"
            result[("trigger", name)] = f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable CRM submission record'); END"""
        condition = " OR ".join(
            "(" + " AND ".join(f"{k}=NEW.{k}" for k in group) + ")" for group in keys
        )
        name = f"{table}_no_replace"
        result[("trigger", name)] = f"""CREATE TRIGGER {name} BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT,'immutable CRM submission record'); END"""
    return result
