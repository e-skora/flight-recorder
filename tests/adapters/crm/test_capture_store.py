"""Durable capture transactions on explicitly new operational format2 files."""

import hashlib
import sqlite3

import pytest

from flight_recorder.adapters.crm.contracts import CRMCapture, CRMRefused, CRMTarget
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_hash
from tests.adapters.crm.cases import make_case, seal

BINDING = "c" * 64


def store2(path):
    try:
        return CRMStore(path, format_version=2)
    except TypeError:
        pytest.fail("missing explicit format2 durable capture store", pytrace=False)


def capture(sequence=1, generation=1, count=184, seconds=0, etag=None):
    from datetime import datetime, timedelta

    from flight_recorder.collector.schema import format_utc

    cap = make_case()["capture"]
    cap["observation_origin"] = "injected_transport"
    cap.update(capture_sequence=sequence, ownership_generation=generation)
    for field in ["read_started_at", "read_finished_at", "capture_boundary", "source_updated_at"]:
        cap[field] = format_utc(datetime.fromisoformat(cap[field]) + timedelta(seconds=seconds))
    cap["properties"]["LastModifiedDate"] = cap["source_updated_at"]
    cap["properties"]["NumberOfEmployees"] = count
    cap["inputs"]["employee_count"]["value"] = count
    cap["response_body_sha256"] = canonical_hash(cap["properties"])
    cap["precondition"] = etag if etag is not None else f'"version-{seconds}"'
    return CRMCapture.from_document(seal(cap))


def target(cap=None):
    return CRMTarget(**(cap or capture()).document["target"])


def freeze(store, owner, cap):
    return store.freeze_capture(owner, cap, config_binding=BINDING)


def test_explicit_new_format2_does_not_change_default_format1(tmp_path):
    old = CRMStore(tmp_path / "old.sqlite")
    before = hashlib.sha256(old.path.read_bytes()).hexdigest()
    new = store2(tmp_path / "new.sqlite")
    with sqlite3.connect(old.path) as conn:
        assert conn.execute("SELECT format_version FROM crm_store_meta").fetchone() == (1,)
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='crm_frozen_captures'"
        ).fetchall()
    with sqlite3.connect(new.path) as conn:
        assert conn.execute("SELECT format_version FROM crm_store_meta").fetchone() == (2,)
    assert hashlib.sha256(old.path.read_bytes()).hexdigest() == before
    old.close()
    new.close()


@pytest.mark.parametrize("created,opened", [(1, 2), (2, 1)])
def test_cross_format_open_refuses_without_modifying_file(tmp_path, created, opened):
    path = tmp_path / "store.sqlite"
    (CRMStore(path) if created == 1 else store2(path)).close()
    before = path.read_bytes()
    with pytest.raises(CRMRefused, match="operational_store_unrecognized"):
        CRMStore(path) if opened == 1 else store2(path)
    assert path.read_bytes() == before


def test_capture_freeze_promotes_sequence_and_digest_atomically_and_reopens(tmp_path):
    path = tmp_path / "store.sqlite"
    store = store2(path)
    cap = capture()
    owner = store.acquire(target(cap), "a" * 64)
    assert store.next_capture_sequence(owner) == 1
    assert freeze(store, owner, cap) == cap
    state = store.polling_state(target(cap))
    assert state["capture_sequence"] == 1
    assert state["last_unit_id"] == cap.unit_id
    assert state["input_digest"] == canonical_hash(
        {k: cap.document[k] for k in ["mapping_hash", "inputs"]}
    )
    assert state["processing_version"] == cap.document["processing_version"]
    assert freeze(store, owner, cap).canonical_bytes == cap.canonical_bytes
    assert store.capture_for_owner(owner) == cap
    assert store.observation_count(target(cap)) == 1
    store.close()
    resumed = store2(path)
    assert resumed.load_capture(cap.unit_id) == cap
    assert resumed.capture_for_owner(owner) == cap
    assert resumed.polling_state(target(cap)) == state
    resumed.close()


@pytest.mark.parametrize(
    "kind", ["target", "generation", "sequence", "changed_claim_read", "binding"]
)
def test_conflicting_capture_never_changes_original_or_promotion(tmp_path, kind):
    store = store2(tmp_path / "store.sqlite")
    cap = capture()
    owner = store.acquire(target(cap), "a" * 64)
    freeze(store, owner, cap)
    before = store.polling_state(target(cap))
    other = capture(count=185)
    if kind == "generation":
        other = capture(generation=2)
    if kind == "sequence":
        other = capture(sequence=2)
    if kind == "target":
        d = cap.document
        d["target"]["record_id"] = d["properties"]["Id"] = "001000000000002AAA"
        other = CRMCapture.from_document(seal(d))
    with pytest.raises(CRMRefused):
        store.freeze_capture(
            owner,
            cap if kind == "binding" else other,
            config_binding="d" * 64 if kind == "binding" else BINDING,
        )
    assert store.load_capture(cap.unit_id) == cap
    assert store.polling_state(target(cap)) == before
    assert store.observation_count(target(cap)) == 1
    store.close()


def test_abandoned_owner_cannot_freeze_after_new_generation(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    stale = store.acquire(target(), "a" * 64)
    store.abandon(stale)
    current = store.acquire(target(), "b" * 64)
    with pytest.raises(CRMRefused, match="stale_ownership"):
        freeze(store, stale, capture())
    assert store.load_capture(capture().unit_id) is None
    assert freeze(store, current, capture(generation=2)).document["ownership_generation"] == 2
    store.close()


def test_failed_capture_insert_cannot_advance_sequence_digest_or_facts(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    # Install a fault only after strict initialization; do not reopen the altered schema.
    with store.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER injected_fault BEFORE INSERT ON crm_frozen_captures "
            "BEGIN SELECT RAISE(ABORT,'fictional-private-value'); END"
        )
    with pytest.raises(CRMRefused) as caught:
        freeze(store, owner, capture())
    assert "fictional-private-value" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert store.polling_state(target())["capture_sequence"] == 0
    assert store.observation_count(target()) == 0
    assert store.load_capture(capture().unit_id) is None
    store.close()


@pytest.mark.parametrize(
    "table", ["crm_frozen_captures", "crm_read_observations", "crm_source_versions"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_frozen_records_cannot_be_changed_through_raw_sql(tmp_path, table, operation):
    store = store2(tmp_path / "store.sqlite")
    cap = capture()
    owner = store.acquire(target(cap), "a" * 64)
    freeze(store, owner, cap)
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        row = conn.execute(f"SELECT * FROM {table}").fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError):
            if operation == "UPDATE":
                conn.execute(f"UPDATE {table} SET {columns[0]}={columns[0]}")
            elif operation == "DELETE":
                conn.execute(f"DELETE FROM {table}")
            else:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' for _ in row)})", row
                )
        assert conn.execute(f"SELECT * FROM {table}").fetchone() == row
    assert store.load_capture(cap.unit_id) == cap
    store.close()


def test_source_version_conflict_is_detected_across_intermediate_versions(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    for index in range(2):
        owner = store.acquire(target(), str(index + 1) * 64)
        freeze(
            store,
            owner,
            capture(
                sequence=index + 1,
                generation=index + 1,
                count=184 + index,
                seconds=index * 10,
                etag=f'"v{index}"',
            ),
        )
        store.release(owner)
    owner = store.acquire(target(), "3" * 64)
    with pytest.raises(CRMRefused, match="source_version_conflict"):
        freeze(store, owner, capture(sequence=3, generation=3, count=190, seconds=20, etag='"v0"'))
    assert store.polling_state(target())["capture_sequence"] == 2
    assert store.observation_count(target()) == 2
    store.close()


@pytest.mark.parametrize("table", ["crm_read_observations", "crm_polling_state"])
def test_late_freeze_fault_rolls_back_capture_observation_version_and_promotion(tmp_path, table):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    with store.transaction() as conn:
        conn.execute(
            f"CREATE TRIGGER late_fault BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fictional-source-value'); END"
        )
    with pytest.raises(CRMRefused):
        freeze(store, owner, capture())
    with sqlite3.connect(store.path) as conn:
        for name in [
            "crm_frozen_captures",
            "crm_read_observations",
            "crm_source_versions",
            "crm_polling_state",
        ]:
            assert conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
    assert store.next_capture_sequence(owner) == 1
    store.close()


@pytest.mark.parametrize("column", ["capture_hash", "claim_id", "capture_sequence"])
def test_alternate_unique_capture_key_cannot_replace_another_primary_key(tmp_path, column):
    store = store2(tmp_path / "store.sqlite")
    cap = capture()
    owner = store.acquire(target(), "a" * 64)
    freeze(store, owner, cap)
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        columns = [r[1] for r in conn.execute("PRAGMA table_info(crm_frozen_captures)")]
        row = dict(
            zip(columns, conn.execute("SELECT * FROM crm_frozen_captures").fetchone(), strict=True)
        )
        changed = dict(
            row, unit_id="f" * 64, capture_hash="e" * 64, claim_id="d" * 64, capture_sequence=10
        )
        changed[column] = row[column]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO crm_frozen_captures "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(changed[c] for c in columns),
            )
    assert store.load_capture(cap.unit_id) == cap
    store.close()


@pytest.mark.parametrize(
    "column,value", [("capture_sequence", 99), ("config_binding", "f" * 64), ("body", b"{}")]
)
def test_capture_load_detects_changed_body_or_indexed_metadata(tmp_path, column, value):
    store = store2(tmp_path / "store.sqlite")
    cap = capture()
    owner = store.acquire(target(), "a" * 64)
    freeze(store, owner, cap)
    with sqlite3.connect(store.path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_frozen_captures_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER crm_frozen_captures_no_update")
        conn.execute(f"UPDATE crm_frozen_captures SET {column}=?", (value,))
        conn.execute(ddl)
    with pytest.raises(CRMRefused):
        store.load_capture(cap.unit_id)
    store.close()


def test_older_read_never_reverses_target_observation_order(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    first = store.acquire(target(), "a" * 64)
    freeze(store, first, capture(seconds=10))
    store.release(first)
    second = store.acquire(target(), "b" * 64)
    with pytest.raises(CRMRefused, match="observation_out_of_order"):
        freeze(store, second, capture(sequence=2, generation=2, count=185, seconds=0))
    assert store.polling_state(target())["capture_sequence"] == 1
    assert store.observation_count(target()) == 1
    store.close()


def test_polling_pointer_cannot_rewind_to_an_older_valid_observation(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    for index in range(2):
        owner = store.acquire(target(), str(index + 1) * 64)
        freeze(
            store,
            owner,
            capture(
                sequence=index + 1, generation=index + 1, count=184 + index, seconds=index * 10
            ),
        )
        store.release(owner)
    with sqlite3.connect(store.path) as conn:
        older = conn.execute(
            "SELECT fact_id FROM crm_read_observations WHERE generation=1"
        ).fetchone()[0]
        conn.execute("UPDATE crm_polling_state SET last_fact_id=?", (older,))
    with pytest.raises(CRMRefused, match="polling_state_integrity"):
        store.polling_state(target())
    store.close()


@pytest.mark.parametrize("value", [True, False, None, "2", 2.0, 0, 5])
def test_invalid_format_never_creates_file(tmp_path, value):
    path = tmp_path / "store.sqlite"
    with pytest.raises(CRMRefused):
        CRMStore(path, format_version=value)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["empty", "foreign"])
def test_format2_never_initializes_existing_unrecognized_file(tmp_path, kind):
    path = tmp_path / "store.sqlite"
    path.write_bytes(b"")
    if kind == "foreign":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(CRMRefused, match="operational_store_unrecognized"):
        store2(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "table", ["crm_frozen_captures", "crm_read_observations", "crm_source_versions"]
)
def test_immutable_capture_records_have_no_hidden_replaceable_rowid(tmp_path, table):
    store = store2(tmp_path / "store.sqlite")
    with sqlite3.connect(store.path) as conn:
        for alias in ["rowid", "_rowid_", "oid"]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"SELECT {alias} FROM {table}")
    store.close()


def test_two_format2_instances_and_concurrent_freezes_retain_one_capture(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "store.sqlite"
    first, second = store2(path), store2(path)
    cap = capture()
    owner = first.acquire(target(), "a" * 64)
    with pytest.raises(CRMRefused, match="target_busy"):
        second.acquire(target(), "b" * 64)
    barrier = threading.Barrier(2)

    def call(store):
        barrier.wait(timeout=5)
        return freeze(store, owner, cap)

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(call, [first, second])) == [cap, cap]
    assert first.observation_count(target()) == 1
    assert second.polling_state(target())["capture_sequence"] == 1
    first.close()
    second.close()
