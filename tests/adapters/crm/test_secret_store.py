"""Private local credentials only: all token values in this file are fictional."""

import importlib
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.collector.canonical import canonical_hash

BINDING = "a" * 64
CONNECTION = "fictional-salesforce-connection"


@pytest.fixture
def api():
    class MissingFeatureBoundary:
        def __getattr__(self, name):
            try:
                module = importlib.import_module("flight_recorder.adapters.crm.secrets")
            except ModuleNotFoundError:
                pytest.fail("missing private TokenStore boundary", pytrace=False)
            return getattr(module, name)

    return MissingFeatureBoundary()


@pytest.fixture
def private_dir(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory


def material(api, suffix="one", expiry=None):
    return api.TokenMaterial(f"fictional-access-{suffix}", f"fictional-refresh-{suffix}", expiry)


def filename(directory, connection=CONNECTION):
    return directory / (canonical_hash({"connection_id": connection}) + ".json")


def refuses(reason, action):
    with pytest.raises(CRMRefused) as caught:
        action()
    assert caught.value.reason == reason
    assert str(caught.value) == reason
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def installed(api, directory):
    store = api.TokenStore(directory)
    value = material(api)
    status = store.install(CONNECTION, BINDING, value)
    assert status.generation == 1 and status.state == "ready"
    return store, value


def test_install_reopen_exact_material_and_safe_metadata(api, private_dir):
    expiry = datetime(2026, 10, 21, 14, 0, tzinfo=timezone(timedelta(hours=-7)))
    value = material(api, expiry=expiry)
    store = api.TokenStore(private_dir)
    assert list(private_dir.iterdir()) == []
    status = store.install(CONNECTION, BINDING, value)
    assert vars(status) == dict(
        connection_id=CONNECTION, config_binding=BINDING, generation=1, state="ready"
    )
    assert filename(private_dir).stat().st_mode & 0o777 == 0o600
    saved = json.loads(filename(private_dir).read_text())
    assert saved["material"]["expires_at"] == "2026-10-21T21:00:00.000000Z"
    assert saved["material"]["client_secret"] is None
    store.close()
    other = api.TokenStore(private_dir)
    assert other.load(CONNECTION, BINDING) == value
    assert other.read_ready(CONNECTION, BINDING).generation == 1
    assert other.status(CONNECTION, BINDING) == status
    other.close()


@pytest.mark.parametrize(
    "access,refresh,expiry,secret",
    [
        ("", "ok", None, None),
        (True, "ok", None, None),
        ("ok", None, None, None),
        ("ok\n", "ok", None, None),
        ("ok", "ok\x7f", None, None),
        ("ok", "ok", datetime(2026, 1, 1), None),
        ("ok", "ok", "tomorrow", None),
        ("ok", "ok", None, ""),
        ("ok", "ok", None, 1),
        ("ok", "ok", None, "\ud800"),
        ("x" * 16385, "ok", None, None),
    ],
)
def test_invalid_material_is_safe(api, access, refresh, expiry, secret):
    refuses("invalid_token_material", lambda: api.TokenMaterial(access, refresh, expiry, secret))


def test_material_values_are_frozen_and_redacted(api, private_dir):
    store, value = installed(api, private_dir)
    read = store.read_ready(CONNECTION, BINDING)
    claim = store.begin_refresh(CONNECTION, BINDING, expected_generation=1)
    for obj in [value, read, claim]:
        assert "fictional-access" not in repr(obj)
        assert "fictional-refresh" not in str(obj)
        assert "redacted" in repr(obj).lower()
        with pytest.raises(FrozenInstanceError):
            obj.material = value
    assert not hasattr(value, "document")
    assert not hasattr(value, "model_dump")
    store.close()


@pytest.mark.parametrize(
    "connection,binding",
    [
        ("", BINDING),
        (True, BINDING),
        ("x\n", BINDING),
        ("x" * 257, BINDING),
        (CONNECTION, "A" * 64),
        (CONNECTION, "a" * 63),
        (CONNECTION, 1),
    ],
)
def test_invalid_binding_never_creates_record(api, private_dir, connection, binding):
    store = api.TokenStore(private_dir)
    refuses("invalid_token_binding", lambda: store.install(connection, binding, material(api)))
    assert not list(private_dir.glob("*.json"))
    store.close()


def test_connection_id_is_exact_hashed_filename_not_path(api, private_dir):
    store = api.TokenStore(private_dir)
    identity = "../../fictional connection"
    store.install(identity, BINDING, material(api))
    assert filename(private_dir, identity).is_file()
    assert store.load(identity, BINDING) == material(api)
    store.close()


def test_missing_existing_binding_and_generation_refusals_preserve_file(api, private_dir):
    store = api.TokenStore(private_dir)
    refuses("token_material_missing", lambda: store.load(CONNECTION, BINDING))
    store.install(CONNECTION, BINDING, material(api))
    before = filename(private_dir).read_bytes()
    refuses("token_record_exists", lambda: store.install(CONNECTION, BINDING, material(api)))
    refuses("invalid_token_binding", lambda: store.load(CONNECTION, "b" * 64))
    for generation in [True, 0, -1, 1.0, "1", 2]:
        refuses(
            "stale_token_generation",
            lambda generation=generation: store.replace(
                CONNECTION, material(api, "two"), binding=BINDING, expected_generation=generation
            ),
        )
    assert filename(private_dir).read_bytes() == before
    store.close()


def test_ready_replacement_is_cas_and_preserves_null_expiry(api, private_dir):
    store, _ = installed(api, private_dir)
    newer = material(api, "two")
    assert store.replace(CONNECTION, newer, binding=BINDING, expected_generation=1).generation == 2
    assert store.read_ready(CONNECTION, BINDING).material == newer
    assert store.load(CONNECTION, BINDING).expires_at is None
    store.close()


def test_refresh_claim_persists_blocks_restart_and_finishes_once(api, private_dir):
    store, old = installed(api, private_dir)
    claim = store.begin_refresh(CONNECTION, BINDING, expected_generation=1)
    assert claim.material == old and claim.generation == 2
    assert json.loads(filename(private_dir).read_bytes())["state"] == "refresh_started"
    other = api.TokenStore(private_dir)
    for current in [store, other]:
        refuses("token_state_blocked", lambda current=current: current.load(CONNECTION, BINDING))
        refuses(
            "token_state_blocked",
            lambda current=current: current.begin_refresh(
                CONNECTION, BINDING, expected_generation=2
            ),
        )
        refuses(
            "token_state_blocked",
            lambda current=current: current.replace(
                CONNECTION, old, binding=BINDING, expected_generation=2
            ),
        )
    new = material(api, "two")
    assert other.finish_refresh(claim, new).generation == 3
    assert store.load(CONNECTION, BINDING) == new
    refuses("stale_token_generation", lambda: store.finish_refresh(claim, old))
    refuses("stale_token_generation", lambda: store.fail_refresh(claim))
    store.close()
    other.close()


def test_failed_refresh_retains_material_but_never_recovers_automatically(api, private_dir):
    store, _ = installed(api, private_dir)
    claim = store.begin_refresh(CONNECTION, BINDING, expected_generation=1)
    assert store.fail_refresh(claim).state == "needs_attention"
    assert store.status(CONNECTION, BINDING).generation == 3
    store.close()
    other = api.TokenStore(private_dir)
    refuses("token_state_blocked", lambda: other.load(CONNECTION, BINDING))
    refuses(
        "token_state_blocked",
        lambda: other.replace(
            CONNECTION, material(api, "two"), binding=BINDING, expected_generation=3
        ),
    )
    other.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("connection_id", "other"),
        ("config_binding", "b" * 64),
        ("generation", 1),
        ("generation", True),
        ("material", "forged"),
    ],
)
def test_forged_refresh_claim_cannot_finish_or_fail(api, private_dir, field, value):
    store, _ = installed(api, private_dir)
    claim = store.begin_refresh(CONNECTION, BINDING, expected_generation=1)
    values = dict(
        connection_id=claim.connection_id,
        config_binding=claim.config_binding,
        generation=claim.generation,
        material=claim.material,
    )
    values[field] = material(api, "forged") if field == "material" else value
    # Deliberately bypass value construction for forged local-caller boundary tests.
    forged = object.__new__(api.RefreshClaim)
    for key, part in values.items():
        object.__setattr__(forged, key, part)
    before = filename(private_dir).read_bytes()
    with pytest.raises(CRMRefused):
        store.finish_refresh(forged, material(api, "two"))
    with pytest.raises(CRMRefused):
        store.fail_refresh(forged)
    assert filename(private_dir).read_bytes() == before
    store.close()


@pytest.mark.parametrize(
    "same_instance,operation",
    [
        (True, "replace"),
        (False, "replace"),
        (True, "refresh"),
        (False, "refresh"),
    ],
)
def test_concurrent_callers_have_exactly_one_winner(api, private_dir, same_instance, operation):
    store, _ = installed(api, private_dir)
    other = store if same_instance else api.TokenStore(private_dir)
    barrier = threading.Barrier(2)

    def compete(current):
        barrier.wait(timeout=5)
        try:
            if operation == "refresh":
                current.begin_refresh(CONNECTION, BINDING, expected_generation=1)
            else:
                current.replace(
                    CONNECTION, material(api, "two"), binding=BINDING, expected_generation=1
                )
            return "won"
        except CRMRefused as error:
            return error.reason

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(compete, [store, other]))
    assert results.count("won") == 1
    assert set(results) == {
        "won",
        "token_state_blocked" if operation == "refresh" else "stale_token_generation",
    }
    assert store.status(CONNECTION, BINDING).generation == 2
    store.close()
    other.close()


@pytest.mark.parametrize("kind", ["directory_mode", "symlink", "parent_symlink", "missing"])
def test_unsafe_directory_refused_without_repair(api, private_dir, kind):
    target = private_dir
    if kind == "directory_mode":
        private_dir.chmod(0o755)
    elif kind in {"symlink", "parent_symlink"}:
        alias = private_dir.parent / "alias"
        alias.symlink_to(private_dir, target_is_directory=True)
        target = alias
        if kind == "parent_symlink":
            (private_dir / "child").mkdir(mode=0o700)
            target = alias / "child"
    else:
        target = private_dir / "missing"
    refuses("secret_boundary_invalid", lambda: api.TokenStore(target))
    if kind == "directory_mode":
        assert private_dir.stat().st_mode & 0o777 == 0o755


def test_owner_mismatch_branch_uses_injected_identity_only(api, private_dir, monkeypatch):
    # This is an injected OS identity, not an actual privileged chown proof.
    monkeypatch.setattr(api.os, "geteuid", lambda: os.stat(private_dir).st_uid + 1)
    refuses("secret_boundary_invalid", lambda: api.TokenStore(private_dir))


@pytest.mark.parametrize("kind", ["mode", "hardlink", "symlink", "directory"])
@pytest.mark.parametrize("record", ["token", "lock"])
def test_unsafe_retained_file_refuses_without_mutation(api, private_dir, kind, record):
    store, _ = installed(api, private_dir)
    path = filename(private_dir) if record == "token" else private_dir / ".lock"
    before = path.read_bytes()
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "hardlink":
        os.link(path, private_dir / "extra-link")
    else:
        path.unlink()
        if kind == "symlink":
            safe = private_dir / "retained"
            safe.write_bytes(before)
            safe.chmod(0o600)
            path.symlink_to(safe)
        else:
            path.mkdir(mode=0o700)
    refuses("secret_boundary_invalid", lambda: store.load(CONNECTION, BINDING))
    if kind != "directory":
        assert path.read_bytes() == before
    if kind == "mode":
        assert path.stat().st_mode & 0o777 == 0o644
    store.close()


def test_directory_replacement_never_writes_stale_dirfd(api, private_dir):
    store, _ = installed(api, private_dir)
    moved = private_dir.with_name("moved")
    private_dir.rename(moved)
    private_dir.mkdir(mode=0o700)
    before = filename(moved).read_bytes()
    refuses(
        "secret_boundary_invalid",
        lambda: store.replace(
            CONNECTION, material(api, "two"), binding=BINDING, expected_generation=1
        ),
    )
    assert filename(moved).read_bytes() == before
    assert list(private_dir.iterdir()) == []
    store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "nonfinite",
        "unknown",
        "version_bool",
        "version_float",
        "generation_bool",
        "generation_zero",
        "wrong_connection",
        "bad_state",
        "material_null",
        "material_unknown",
        "naive_expiry",
        "oversized",
        "bad_utf8",
        "bad_json",
    ],
)
def test_malformed_record_never_leaks_or_repairs(api, private_dir, mutation):
    store, _ = installed(api, private_dir)
    path = filename(private_dir)
    record = json.loads(path.read_bytes())
    if mutation == "duplicate":
        content = path.read_bytes()[:-1] + b',"generation":1}'
    elif mutation == "nonfinite":
        content = b'{"secret":NaN}'
    elif mutation == "oversized":
        content = b" " * (1024 * 1024 + 1)
    elif mutation == "bad_utf8":
        content = b"\xff"
    elif mutation == "bad_json":
        content = b'{"fictional-access-one"'
    else:
        if mutation == "unknown":
            record["extra"] = "fictional-access-one"
        elif mutation.startswith("version_"):
            record["format_version"] = True if mutation == "version_bool" else 1.0
        elif mutation.startswith("generation_"):
            record["generation"] = True if mutation == "generation_bool" else 0
        elif mutation == "wrong_connection":
            record["connection_id"] = "foreign"
        elif mutation == "bad_state":
            record["state"] = "recovered"
        elif mutation == "material_null":
            record["material"] = None
        elif mutation == "material_unknown":
            record["material"]["extra"] = "secret"
        elif mutation == "naive_expiry":
            record["material"]["expires_at"] = "2026-10-21T00:00:00"
        content = json.dumps(record).encode()
    path.write_bytes(content)
    refuses("secret_store_unrecognized", lambda: store.load(CONNECTION, BINDING))
    assert path.read_bytes() == content
    store.close()


def test_real_flock_contention_is_bounded(api, private_dir):
    import fcntl

    store, _ = installed(api, private_dir)
    with (private_dir / ".lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refuses("secret_store_contended", lambda: store.load(CONNECTION, BINDING))
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert store.load(CONNECTION, BINDING) == material(api)
    store.close()


@pytest.mark.parametrize("stage", ["file_fsync", "replace", "directory_fsync"])
def test_io_uncertainty_is_paused_and_reopens_complete_old_or_new(
    api, private_dir, monkeypatch, stage
):
    store, _ = installed(api, private_dir)
    before = filename(private_dir).read_bytes()
    original_fsync, original_replace = api.os.fsync, api.os.replace
    calls = 0

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if stage == "file_fsync" else 2) and stage != "replace":
            raise OSError("fictional-access-one must never leak")
        original_fsync(fd)

    def replace(*args, **kwargs):
        if stage == "replace":
            raise OSError("fictional-access-one must never leak")
        return original_replace(*args, **kwargs)

    # Controlled persistence faults, while all successful IO uses the real filesystem.
    with monkeypatch.context() as patch:
        patch.setattr(api.os, "fsync", fsync)
        patch.setattr(api.os, "replace", replace)
        refuses(
            "secret_persistence_uncertain",
            lambda: store.replace(
                CONNECTION, material(api, "two"), binding=BINDING, expected_generation=1
            ),
        )
    refuses("secret_store_paused", lambda: store.load(CONNECTION, BINDING))
    assert not [p for p in private_dir.iterdir() if p.name.endswith(".tmp")]
    store.close()
    reopened = api.TokenStore(private_dir)
    read = reopened.read_ready(CONNECTION, BINDING)
    if stage == "directory_fsync":
        assert read.generation == 2 and read.material == material(api, "two")
    else:
        assert filename(private_dir).read_bytes() == before
        assert read.generation == 1
    reopened.close()


def test_close_waits_for_inflight_operation_then_refuses_without_fd_reuse(
    api, private_dir, monkeypatch
):
    store, _ = installed(api, private_dir)
    entered, release = threading.Event(), threading.Event()
    original = api.os.replace

    def held_replace(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch, ThreadPoolExecutor(2) as pool:
        patch.setattr(api.os, "replace", held_replace)
        replacing = pool.submit(
            store.replace, CONNECTION, material(api, "two"), binding=BINDING, expected_generation=1
        )
        assert entered.wait(timeout=5)
        closing = pool.submit(store.close)
        assert not closing.done()
        release.set()
        assert replacing.result(timeout=5).generation == 2
        closing.result(timeout=5)
    before = filename(private_dir).read_bytes()
    store.close()
    refuses("secret_store_closed", lambda: store.load(CONNECTION, BINDING))
    refuses(
        "secret_store_closed",
        lambda: store.replace(CONNECTION, material(api), binding=BINDING, expected_generation=2),
    )
    assert filename(private_dir).read_bytes() == before


def test_pause_is_visible_to_waiting_same_object_reader(api, private_dir, monkeypatch):
    store, _ = installed(api, private_dir)
    entered, release = threading.Event(), threading.Event()

    def fail_replace(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        raise OSError("fictional token IO failure")

    def result(action):
        try:
            action()
            return "unexpected_success"
        except CRMRefused as error:
            return error.reason

    with monkeypatch.context() as patch, ThreadPoolExecutor(2) as pool:
        patch.setattr(api.os, "replace", fail_replace)
        writing = pool.submit(
            result,
            lambda: store.replace(
                CONNECTION, material(api, "two"), binding=BINDING, expected_generation=1
            ),
        )
        assert entered.wait(timeout=5)
        reading = pool.submit(result, lambda: store.load(CONNECTION, BINDING))
        assert not reading.done()
        release.set()
        assert writing.result(timeout=5) == "secret_persistence_uncertain"
        assert reading.result(timeout=5) == "secret_store_paused"
    store.close()


def test_closed_gate_precedes_all_later_input_checks(api, private_dir):
    store, _ = installed(api, private_dir)
    store.close()
    for action in [
        lambda: store.install("", "bad", None),
        lambda: store.load("", "bad"),
        lambda: store.status("", "bad"),
        lambda: store.replace("", None, binding="bad", expected_generation=True),
        lambda: store.begin_refresh("", "bad", expected_generation=True),
        lambda: store.finish_refresh(None, None),
        lambda: store.fail_refresh(None),
    ]:
        refuses("secret_store_closed", action)


@pytest.mark.parametrize("stage", ["before_replace", "after_replace"])
def test_refresh_io_failure_never_returns_claim_or_replays_pending(
    api, private_dir, monkeypatch, stage
):
    store, old = installed(api, private_dir)
    real_replace = api.os.replace

    def uncertain_replace(*args, **kwargs):
        if stage == "after_replace":
            real_replace(*args, **kwargs)
        raise OSError("fictional-refresh-one suppressed")

    with monkeypatch.context() as patch:
        patch.setattr(api.os, "replace", uncertain_replace)
        refuses(
            "secret_persistence_uncertain",
            lambda: store.begin_refresh(CONNECTION, BINDING, expected_generation=1),
        )
    refuses("secret_store_paused", lambda: store.load(CONNECTION, BINDING))
    store.close()
    reopened = api.TokenStore(private_dir)
    if stage == "after_replace":
        assert reopened.status(CONNECTION, BINDING).state == "refresh_started"
        refuses("token_state_blocked", lambda: reopened.load(CONNECTION, BINDING))
        refuses(
            "token_state_blocked",
            lambda: reopened.begin_refresh(CONNECTION, BINDING, expected_generation=2),
        )
    else:
        assert reopened.load(CONNECTION, BINDING) == old
        assert reopened.status(CONNECTION, BINDING).generation == 1
    reopened.close()


@pytest.mark.parametrize("year", [1, 99, 999, 9999])
def test_all_valid_expiry_years_round_trip_in_fixed_utc_format(api, private_dir, year):
    store = api.TokenStore(private_dir)
    expiry = datetime(year, 1, 1, tzinfo=UTC)
    value = material(api, expiry=expiry)
    store.install(CONNECTION, BINDING, value)
    stored = json.loads(filename(private_dir).read_bytes())["material"]["expires_at"]
    assert stored == f"{year:04d}-01-01T00:00:00.000000Z"
    assert store.load(CONNECTION, BINDING) == value
    store.close()


def test_exception_from_custom_timezone_cannot_expose_secret(api):
    class BrokenTimezone(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("fictional-access-one must never leak")

    refuses(
        "invalid_token_material",
        lambda: material(api, expiry=datetime(2026, 1, 1, tzinfo=BrokenTimezone())),
    )


def test_directory_drift_after_replace_is_uncertain_and_paused(api, tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = api.TokenStore(directory)
    old = api.TokenMaterial("fictional-access-old", "fictional-refresh-old", None)
    new = api.TokenMaterial("fictional-access-new", "fictional-refresh-new", None)
    store.install("fictional", "a" * 64, old)
    original = api.os.fsync

    def drift(fd):
        original(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory.rename(tmp_path / "moved")
            directory.mkdir(mode=0o700)

    with monkeypatch.context() as patch:
        patch.setattr(api.os, "fsync", drift)
        with pytest.raises(CRMRefused, match="^secret_persistence_uncertain$"):
            store.replace("fictional", new, binding="a" * 64, expected_generation=1)
    with pytest.raises(CRMRefused, match="^secret_store_paused$"):
        store.load("fictional", "a" * 64)
    store.close()


def test_utf16_token_record_is_unrecognized(api, tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = api.TokenStore(directory)
    store.install(
        "fictional", "a" * 64, api.TokenMaterial("fictional-access", "fictional-refresh", None)
    )
    path = next(directory.glob("*.json"))
    path.write_bytes(json.dumps(json.loads(path.read_bytes())).encode("utf-16"))
    with pytest.raises(CRMRefused, match="^secret_store_unrecognized$"):
        store.load("fictional", "a" * 64)
    store.close()


@pytest.mark.parametrize("stage", ["lock_close", "store_close"])
def test_close_io_error_is_named_without_secret_cause(api, tmp_path, monkeypatch, stage):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = api.TokenStore(directory)
    store.install(
        "fictional", "a" * 64, api.TokenMaterial("fictional-access", "fictional-refresh", None)
    )
    original = api.os.close

    def uncertain_close(fd):
        info = os.fstat(fd)
        original(fd)
        if (
            stage == "lock_close"
            and stat.S_ISREG(info.st_mode)
            and info.st_size == 0
            or stage == "store_close"
            and stat.S_ISDIR(info.st_mode)
        ):
            raise OSError("fictional-refresh must never leak")

    with monkeypatch.context() as patch:
        patch.setattr(api.os, "close", uncertain_close)
        with pytest.raises(CRMRefused, match="^secret_persistence_uncertain$") as caught:
            if stage == "store_close":
                store.close()
            else:
                store.load("fictional", "a" * 64)
        assert caught.value.__suppress_context__
    if stage == "lock_close":
        with pytest.raises(CRMRefused, match="^secret_store_paused$"):
            store.load("fictional", "a" * 64)
    store.close()
