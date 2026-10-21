"""Real SQLite proofs for durable receipt and per-target fence behavior."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.adapters.crm.test_identity import api, target

UNIT = "a" * 64
NEXT = "b" * 64


def store(path):
    return api("store").CRMStore(path)


def test_receipt_reuse_compares_all_raw_bytes_and_connection(tmp_path):
    s = store(tmp_path / "operational.sqlite")
    body = b'{"a":1}'
    first = s.record_receipt("sf-test", "tick-1", body)
    assert first.body == body
    assert first.body_sha256 == hashlib.sha256(body).hexdigest()
    assert s.record_receipt("sf-test", "tick-1", body) == first
    with pytest.raises(api().CRMRefused, match="receipt_conflict"):
        s.record_receipt("sf-test", "tick-1", b'{ "a": 1 }')
    assert s.receipt_count() == 1
    s.record_receipt("other-connection", "tick-1", body)
    assert s.receipt_count() == 2
    s.close()


@pytest.mark.parametrize(
    "connection,receipt,body",
    [("", "id", b"a"), ("a", "", b"a"), ("a", "id", "text"), (True, "id", b"a")],
)
def test_invalid_receipt_does_not_write(tmp_path, connection, receipt, body):
    s = store(tmp_path / "ops.sqlite")
    with pytest.raises(api().CRMRefused, match="invalid_receipt"):
        s.record_receipt(connection, receipt, body)
    assert s.receipt_count() == 0
    s.close()


@pytest.mark.parametrize(
    "statement", ["UPDATE crm_receipts SET body=x'62'", "DELETE FROM crm_receipts"]
)
def test_receipts_are_append_only_even_through_raw_sql(tmp_path, statement):
    path = tmp_path / "ops.sqlite"
    s = store(path)
    saved = s.record_receipt("sf", "tick", b"a")
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement)
    assert s.record_receipt("sf", "tick", b"a") == saved
    s.close()


def test_transaction_rolls_back_and_store_reopens_without_rewriting(tmp_path):
    path = tmp_path / "ops.sqlite"
    s = store(path)
    with pytest.raises(LookupError), s.transaction() as conn:
        conn.execute("INSERT INTO crm_receipts VALUES (?,?,?,?)", ("sf", "x", b"a", "0" * 64))
        raise LookupError("interrupt before commit")
    assert s.receipt_count() == 0
    saved = s.record_receipt("sf", "tick", b"a")
    s.close()
    before = path.read_bytes()
    reopened = store(path)
    assert reopened.record_receipt("sf", "tick", b"a") == saved
    assert path.read_bytes() == before
    reopened.close()


def test_two_store_instances_cannot_acquire_one_target(tmp_path):
    path = tmp_path / "ops.sqlite"
    first, second = store(path), store(path)
    barrier = Barrier(2)

    def acquire(s, unit):
        barrier.wait(timeout=5)
        try:
            return s.acquire(target(), unit)
        except api().CRMRefused as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(acquire, first, UNIT)
        b = pool.submit(acquire, second, NEXT)
        values = [a.result(timeout=10), b.result(timeout=10)]
    assert sum(v == "target_busy" for v in values) == 1
    winner = next(v for v in values if v != "target_busy")
    assert first.owner(target()) == second.owner(target()) == winner
    first.close()
    second.close()


def test_explicit_recovery_advances_fence_and_rejects_stale_owner(tmp_path):
    s = store(tmp_path / "ops.sqlite")
    old = s.acquire(target(), UNIT)
    with pytest.raises(api().CRMRefused, match="target_busy"):
        s.acquire(target(), UNIT)
    other = s.acquire(target(record_id="001000000000002AAA"), UNIT)
    assert other.generation == old.generation == 1
    s.abandon(old)
    assert s.owner(target()) is None
    new = s.acquire(target(), NEXT)
    assert new.generation == old.generation + 1
    for mutation in (s.release, s.abandon):
        with pytest.raises(api().CRMRefused, match="stale_ownership"):
            mutation(old)
        assert s.owner(target()) == new
    with s.transaction() as conn, pytest.raises(api().CRMRefused, match="stale_ownership"):
        s.assert_owner(conn, old)
    s.release(new)
    assert s.owner(target()) is None
    assert s.acquire(target(), UNIT).generation == 3
    s.close()


def test_close_and_restart_do_not_release_or_abandon_held_target(tmp_path):
    path = tmp_path / "ops.sqlite"
    first = store(path)
    owned = first.acquire(target(), UNIT)
    first.close()
    first.close()
    with pytest.raises(api().CRMRefused, match="operational_store_closed"):
        first.receipt_count()
    reopened = store(path)
    assert reopened.owner(target()) == owned
    with pytest.raises(api().CRMRefused, match="target_busy"):
        reopened.acquire(target(), NEXT)
    reopened.release(owned)
    reopened.close()


@pytest.mark.parametrize("unit", ["", "A" * 64, "a" * 63, True, None])
def test_invalid_work_identity_never_claims_target(tmp_path, unit):
    s = store(tmp_path / "ops.sqlite")
    with pytest.raises(api().CRMRefused, match="invalid_unit_id"):
        s.acquire(target(), unit)
    assert s.owner(target()) is None
    s.close()


@pytest.mark.parametrize("kind", ["empty", "foreign_sqlite"])
def test_constructor_never_migrates_an_unrecognized_existing_file(tmp_path, kind):
    path = tmp_path / "retained.sqlite"
    if kind == "empty":
        path.touch()
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE retained (value TEXT)")
            conn.execute("INSERT INTO retained VALUES ('preserve')")
    before = path.read_bytes()
    with pytest.raises(api().CRMRefused, match="operational_store_unrecognized"):
        store(path)
    assert path.read_bytes() == before


def test_constructor_does_not_create_missing_parent_tree(tmp_path):
    parent = tmp_path / "not-created"
    with pytest.raises(api().CRMRefused):
        store(parent / "ops.sqlite")
    assert not parent.exists()


def test_changed_receipt_protection_is_not_recognized_or_repaired(tmp_path):
    path = tmp_path / "ops.sqlite"
    s = store(path)
    s.record_receipt("sf", "tick", b"a")
    s.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER crm_receipts_no_update")
    before = path.read_bytes()
    with pytest.raises(api().CRMRefused, match="operational_store_unrecognized"):
        store(path)
    assert path.read_bytes() == before


def test_assert_owner_cannot_use_a_connection_to_another_store(tmp_path):
    first = store(tmp_path / "first.sqlite")
    second = store(tmp_path / "second.sqlite")
    owned = first.acquire(target(), UNIT)
    second.acquire(target(), UNIT)
    with second.transaction() as conn, pytest.raises(api().CRMRefused, match="stale_ownership"):
        first.assert_owner(conn, owned)
    first.close()
    second.close()


def test_closed_store_refuses_owner_checks_with_an_already_open_connection(tmp_path):
    s = store(tmp_path / "ops.sqlite")
    owned = s.acquire(target(), UNIT)
    with s.transaction() as conn:
        s.close()
        with pytest.raises(api().CRMRefused, match="operational_store_closed"):
            s.assert_owner(conn, owned)


def test_sqlite_contention_is_a_named_refusal_without_target_claim(tmp_path):
    path = tmp_path / "ops.sqlite"
    first, second = store(path), store(path)
    with first.transaction(), pytest.raises(api().CRMRefused, match="operational_store_contended"):
        second.acquire(target(), UNIT)
    assert second.owner(target()) is None
    first.close()
    second.close()


@pytest.mark.parametrize("command", ["REPLACE", "INSERT OR REPLACE"])
@pytest.mark.parametrize("through_store", [False, True])
def test_replacement_insert_cannot_overwrite_receipt(tmp_path, command, through_store):
    path = tmp_path / "ops.sqlite"
    s = store(path)
    saved = s.record_receipt("sf", "tick", b"original")
    connection = s.transaction() if through_store else sqlite3.connect(path)
    with connection as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"{command} INTO crm_receipts VALUES (?,?,?,?)",
            ("sf", "tick", b"changed", hashlib.sha256(b"changed").hexdigest()),
        )
    assert s.receipt_count() == 1
    assert s.record_receipt("sf", "tick", b"original") == saved
    s.close()


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
def test_receipt_has_no_replaceable_hidden_row_identifier(tmp_path, alias):
    path = tmp_path / "ops.sqlite"
    s = store(path)
    saved = s.record_receipt("sf", "tick", b"original")
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            f"INSERT OR REPLACE INTO crm_receipts ({alias},"
            "connection_id,receipt_id,body,body_sha256) "
            "VALUES (1,?,?,?,?)",
            ("sf", "other-tick", b"changed", hashlib.sha256(b"changed").hexdigest()),
        )
    assert s.receipt_count() == 1
    assert s.record_receipt("sf", "tick", b"original") == saved
    s.close()
