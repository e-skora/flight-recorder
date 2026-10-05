"""D-018 piece 1: the snapshot command, and admission of exactly the release
snapshot. Tests 5 (visible failure) and 6 (the snapshot command) of the task.

A missing, invalid, incomplete or differently built snapshot fails startup with
a named error, and refusal writes nothing (INV-09: failure is visible, never
silently repaired; INV-11: the snapshot is built only through the collector).
The canonical decision and the `v5.2` hash checks stay required alongside the
content identity, and each is shown to be the check that fires for its case.
The schedule digest is reported separately and never stands in for the
content identity.
"""

import functools
import os
import re
import subprocess
import sys

import pytest

import flight_recorder.cli as cli
import flight_recorder.dataset.schedule as schedule_module
import flight_recorder.fixtures as fixtures_module
from flight_recorder.cli import (
    cmd_build_demo_snapshot,
    cmd_register_current_logic,
    cmd_reset,
    cmd_seed,
    cmd_seed_dataset,
    main,
)
from flight_recorder.public_demo import (
    DEMO_DB_ENV_VAR,
    CanonicalDecisionMissing,
    ContentIdentityMismatch,
    CurrentLogicMismatch,
    SnapshotEmpty,
    SnapshotMissing,
    SnapshotNotSQLite,
    SnapshotPathUnset,
    SnapshotRefused,
    content_identity,
    create_public_demo,
    open_read_only,
    schedule_digest,
)
from tests.public_demo.conftest import (
    EXPECTED_CONTENT_IDENTITY,
    FRESH_SEED_DIGEST,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)


def refused_without_writing(path, error_type):
    """Startup over `path` raises exactly `error_type`, and changes nothing."""
    state, sha256 = full_state(path), file_sha256(path)
    with pytest.raises(error_type) as caught:
        create_public_demo(path)
    assert_unchanged(path, state, sha256)
    return caught.value


# --- Test 5: visible failure ---------------------------------------------------------


def test_the_untouched_release_snapshot_starts(built_snapshot, tmp_path):
    app = create_public_demo(owned_copy(built_snapshot, tmp_path))
    assert app.state.public_demo is True


def test_an_unset_variable_is_named_and_flight_recorder_db_never_substitutes(
    built_snapshot, tmp_path, monkeypatch
):
    valid = owned_copy(built_snapshot, tmp_path)
    monkeypatch.delenv(DEMO_DB_ENV_VAR, raising=False)
    monkeypatch.setenv("FLIGHT_RECORDER_DB", str(valid))
    with pytest.raises(SnapshotPathUnset, match=DEMO_DB_ENV_VAR):
        create_public_demo()
    monkeypatch.setenv(DEMO_DB_ENV_VAR, "")
    with pytest.raises(SnapshotPathUnset):
        create_public_demo()
    monkeypatch.chdir(tmp_path)  # a local default file beside the process is no substitute
    owned_copy(built_snapshot, tmp_path, "flight_recorder.db")
    with pytest.raises(SnapshotPathUnset):
        create_public_demo()


def test_the_variable_alone_names_the_snapshot(built_snapshot, tmp_path, monkeypatch):
    monkeypatch.setenv(DEMO_DB_ENV_VAR, str(owned_copy(built_snapshot, tmp_path)))
    assert create_public_demo().state.public_demo is True


def test_a_missing_file_is_named_and_not_created(tmp_path):
    missing = tmp_path / "absent.db"
    with pytest.raises(SnapshotMissing, match="absent.db"):
        create_public_demo(missing)
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_sqlite_is_named_and_unchanged(tmp_path):
    path = tmp_path / "not-a-database.db"
    path.write_bytes(b"this is not a SQLite database\n" * 200)
    before = file_sha256(path)
    with pytest.raises(SnapshotNotSQLite, match="not a SQLite database"):
        create_public_demo(path)
    assert file_sha256(path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["not-a-database.db"]


def test_an_empty_database_is_named(tmp_path):
    zero_bytes = tmp_path / "zero.db"
    zero_bytes.write_bytes(b"")
    with pytest.raises(SnapshotEmpty):
        create_public_demo(zero_bytes)
    assert zero_bytes.read_bytes() == b""

    import sqlite3

    header_only = tmp_path / "no-tables.db"
    connection = sqlite3.connect(header_only)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    before = file_sha256(header_only)
    with pytest.raises(SnapshotEmpty):
        create_public_demo(header_only)
    assert file_sha256(header_only) == before


@pytest.fixture(scope="module")
def canonical_only(tmp_path_factory):
    """`seed` then `register-current-logic`: the canonical decision and `v5.2`
    are present, but it is not the release snapshot."""
    path = tmp_path_factory.mktemp("canonical-only") / "canonical.db"
    assert cmd_reset(path) == 0
    assert cmd_seed(path) == 0
    assert cmd_register_current_logic(path) == 0
    return path


@pytest.fixture(scope="module")
def dataset_without_overlay(tmp_path_factory):
    path = tmp_path_factory.mktemp("no-overlay") / "dataset.db"
    assert cmd_reset(path) == 0
    assert cmd_seed_dataset(path) == 0
    return path


def test_a_canonical_only_database_fails_on_the_content_identity(canonical_only, tmp_path):
    error = refused_without_writing(owned_copy(canonical_only, tmp_path), ContentIdentityMismatch)
    assert error.computed not in (None, EXPECTED_CONTENT_IDENTITY)
    assert "is not the pinned release identity" in str(error)


def test_a_dataset_without_the_overlay_fails_on_the_v5_2_check(dataset_without_overlay, tmp_path):
    error = refused_without_writing(
        owned_copy(dataset_without_overlay, tmp_path), CurrentLogicMismatch
    )
    assert "v5.2" in str(error) and "found none" in str(error)


def test_the_overlay_alone_fails_on_the_canonical_decision_check(tmp_path):
    path = tmp_path / "overlay-only.db"
    assert cmd_reset(path) == 0
    assert cmd_register_current_logic(path) == 0
    error = refused_without_writing(path, CanonicalDecisionMissing)
    assert "evt-novasignal-04-decision-recorded" in str(error)


def test_every_refusal_is_a_named_subclass_of_snapshot_refused():
    for error_type in (
        SnapshotPathUnset,
        SnapshotMissing,
        SnapshotNotSQLite,
        SnapshotEmpty,
        CanonicalDecisionMissing,
        CurrentLogicMismatch,
        ContentIdentityMismatch,
    ):
        assert issubclass(error_type, SnapshotRefused)
        assert error_type is not SnapshotRefused


def uvicorn_start(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != DEMO_DB_ENV_VAR}
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "flight_recorder.public_demo:create_public_demo",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_uvicorn_exits_non_zero_with_the_named_error_when_the_variable_is_unset():
    result = uvicorn_start({})
    assert result.returncode != 0
    assert "SnapshotPathUnset" in result.stderr


def test_uvicorn_exits_non_zero_with_the_named_error_for_a_non_release_snapshot(
    canonical_only, tmp_path
):
    path = owned_copy(canonical_only, tmp_path)
    state, sha256 = full_state(path), file_sha256(path)
    result = uvicorn_start({DEMO_DB_ENV_VAR: str(path)})
    assert result.returncode != 0
    assert "ContentIdentityMismatch" in result.stderr
    assert_unchanged(path, state, sha256)


# --- Test 6: the snapshot command ----------------------------------------------------


def test_an_existing_out_path_is_refused_and_left_byte_identical(tmp_path, capsys):
    out = tmp_path / "demo.db"
    out.write_bytes(b"someone else's file\n")
    before = file_sha256(out)
    assert cmd_build_demo_snapshot(out) == 1
    assert "already exists" in capsys.readouterr().err
    assert file_sha256(out) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.db"]


def test_an_independent_build_reproduces_the_pinned_identity_and_reads_no_local_database(
    built_snapshot, tmp_path, monkeypatch, capsys
):
    """A second, independent build, run through `main` with `--db` and
    FLIGHT_RECORDER_DB both pointing elsewhere: neither is read or created,
    and the content identity equals the session build's and the pin."""

    def forbidden():
        raise AssertionError("build-demo-snapshot resolved the local database path")

    flag_db, env_db = tmp_path / "flag.db", tmp_path / "env.db"
    monkeypatch.setenv("FLIGHT_RECORDER_DB", str(env_db))
    monkeypatch.setattr(cli, "db_path_from_env", forbidden)
    out = tmp_path / "second" / "demo.db"
    out.parent.mkdir()
    assert main(["--db", str(flag_db), "build-demo-snapshot", "--out", str(out)]) == 0
    assert not flag_db.exists() and not env_db.exists()
    assert sorted(p.name for p in out.parent.iterdir()) == ["demo.db"]

    printed = capsys.readouterr().out
    identity = re.search(r"^content identity: ([0-9a-f]{64})$", printed, re.M).group(1)
    digest = re.search(
        r"^schedule digest \(reproducibility evidence, not the content identity\): ([0-9a-f]{64})$",
        printed,
        re.M,
    ).group(1)
    assert f"digest {FRESH_SEED_DIGEST}" in printed  # the fresh seed, before the overlay
    assert "register-current-logic 201 created" in printed
    assert identity == EXPECTED_CONTENT_IDENTITY
    assert digest != identity

    for path in (out, owned_copy(built_snapshot, tmp_path)):
        engine = open_read_only(path)
        try:
            with engine.connect() as conn:
                assert content_identity(conn) == EXPECTED_CONTENT_IDENTITY
                assert schedule_digest(conn) == digest
        finally:
            engine.dispose()


def forced_failures():
    """Each forces one build step to fail, from the test, never in production code."""

    def overlay_duplicate(monkeypatch):
        # The overlay step posts an envelope the dataset already holds: a 200
        # duplicate, not the one created event the release requires.
        canonical = fixtures_module.canonical_envelope_paths()[0]
        monkeypatch.setattr(fixtures_module, "current_logic_registration_path", lambda: canonical)

    def seed_incomplete(monkeypatch):
        original = schedule_module.run_schedule
        monkeypatch.setattr(
            schedule_module, "run_schedule", functools.partial(original, stop_after=25)
        )

    def seed_raises(monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("forced seed failure")

        monkeypatch.setattr(schedule_module, "run_schedule", boom)

    def link_fails(monkeypatch):
        def refuse(*args, **kwargs):
            raise OSError("forced link failure")

        monkeypatch.setattr(os, "link", refuse)

    return {
        "overlay_duplicate": overlay_duplicate,
        "seed_incomplete": seed_incomplete,
        "seed_raises": seed_raises,
        "link_fails": link_fails,
    }


@pytest.mark.parametrize("failure", sorted(forced_failures()))
def test_a_failed_build_leaves_no_snapshot_and_no_temporary_file(
    failure, tmp_path, monkeypatch, capsys
):
    forced_failures()[failure](monkeypatch)
    out = tmp_path / "demo.db"
    assert cmd_build_demo_snapshot(out) == 1
    assert "no snapshot written" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [], [p.name for p in tmp_path.iterdir()]
