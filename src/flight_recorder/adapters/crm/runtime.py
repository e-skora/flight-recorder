"""Explicit process lifetime and bounded durable work; construction performs no reads."""

import fcntl
import hashlib
import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import UTC, datetime

from sqlalchemy import event
from sqlalchemy.schema import CreateTable

from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.adapters.crm.http import OwnedHTTP as _OwnedHTTP
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.adapters.crm.poll_store import PollStore
from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig, absolute_path
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_bytes
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.crm_schema import schema_ready
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import PROJECTION_TABLES, _append_only_triggers, accounts, events


def _sql(value):
    return " ".join(value.split()).replace(" IF NOT EXISTS ", " ")


def qualify_domain(engine):
    try:
        with engine.connect() as conn:
            require(schema_ready(conn), "crm_domain_uninitialized")
            actual = {
                row[0]: _sql(row[1])
                for row in conn.exec_driver_sql(
                    "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger')"
                )
            }
            for table in (accounts, events, *PROJECTION_TABLES):
                require(
                    actual.get(table.name)
                    == _sql(str(CreateTable(table).compile(dialect=engine.dialect))),
                    "crm_domain_uninitialized",
                )
            for table in (events, *PROJECTION_TABLES):
                for ddl in _append_only_triggers(table.name):
                    require(_sql(ddl) in actual.values(), "crm_domain_uninitialized")
            require(
                not conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall(),
                "crm_domain_integrity",
            )
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("crm_domain_uninitialized") from None


class ProcessAuthority:
    def __init__(self, path):
        self.path = path
        self.lock_path = path.with_name(path.name + ".runtime-lock")
        self._fd = self._opfd = None

    def _validate(self, opfd, lockfd):
        try:
            absolute_path(str(self.path))
            absolute_path(str(self.lock_path))
            op, lock = os.fstat(opfd), os.fstat(lockfd)
            for path, info in [(self.path, op), (self.lock_path, lock)]:
                current = os.stat(path, follow_symlinks=False)
                require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_nlink == 1
                    and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino),
                    "runtime_path_changed",
                )
            require(
                stat.S_IMODE(lock.st_mode) == 0o600 and lock.st_size <= 65536,
                "runtime_lock_invalid",
            )
            body = os.pread(lockfd, 65537, 0)
            d = strict_json(body)
            expected = dict(
                format_version=1,
                operational_path=str(self.path),
                operational_device=op.st_dev,
                operational_inode=op.st_ino,
                lock_path=str(self.lock_path),
                lock_device=lock.st_dev,
                lock_inode=lock.st_ino,
            )
            require(d == expected and canonical_bytes(expected) == body, "runtime_lock_invalid")
            with closing(
                sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
            ) as conn:
                rows = conn.execute(
                    "SELECT singleton,body,body_sha256 FROM crm_runtime_binding"
                ).fetchall()
            require(
                rows == [(1, body, hashlib.sha256(body).hexdigest())],
                "runtime_lock_identity_mismatch",
            )
        except CRMRefused:
            raise
        except Exception:
            raise CRMRefused("runtime_lock_invalid") from None

    def validate_files(self):
        opfd = lockfd = None
        try:
            absolute_path(str(self.path))
            absolute_path(str(self.lock_path))
            opfd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            lockfd = os.open(self.lock_path, os.O_RDONLY | os.O_NOFOLLOW)
            self._validate(opfd, lockfd)
        except CRMRefused:
            raise
        except Exception:
            raise CRMRefused("runtime_lock_invalid") from None
        finally:
            for fd in (lockfd, opfd):
                if fd is not None:
                    os.close(fd)

    def acquire(self):
        require(self._fd is None and self._opfd is None, "runtime_already_owned")
        opfd = lockfd = None
        try:
            absolute_path(str(self.path))
            absolute_path(str(self.lock_path))
            opfd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            lockfd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
            self._validate(opfd, lockfd)
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._validate(opfd, lockfd)
            self._opfd, self._fd = opfd, lockfd
            opfd = lockfd = None
        except BlockingIOError:
            raise CRMRefused("runtime_already_owned") from None
        except CRMRefused:
            raise
        except Exception:
            raise CRMRefused("runtime_lock_refused") from None
        finally:
            for fd in (lockfd, opfd):
                if fd is not None:
                    os.close(fd)

    def assert_held(self):
        require(self._fd is not None and self._opfd is not None, "runtime_not_owned")
        self._validate(self._opfd, self._fd)

    def close(self):
        opfd, lockfd = self._opfd, self._fd
        self._opfd = self._fd = None
        for fd in (lockfd, opfd):
            if fd is not None:
                os.close(fd)


class CRMRuntime:
    def __init__(self, config, *, client_factory=None, clock=None, writer_capable=False):
        require(type(writer_capable) is bool, "invalid_writer_mode")
        require(type(config) is CRMRuntimeConfig, "invalid_runtime_config")
        config.__post_init__()
        require(
            config.domain_path.is_file() and config.operational_path.is_file(),
            "crm_runtime_uninitialized",
        )
        self.config, self.clock = config, clock or (lambda: datetime.now(UTC))
        self.writer_capable = writer_capable
        self._writer_store = None
        self.authority = ProcessAuthority(config.operational_path)
        self.store = CRMStore(
            config.operational_path, format_version=5 if writer_capable else 4, create=False
        )
        engine = make_engine(config.domain_path)

        @event.listens_for(engine, "do_connect")
        def _existing_database_only(dialect, connection_record, cargs, cparams):
            cargs[0] = config.domain_path.as_uri() + "?mode=rw"
            cparams["uri"] = True

        self.collector = Collector(engine)
        try:
            qualify_domain(self.collector.engine)
            if writer_capable:
                from flight_recorder.ledger.crm_operations import (
                    schema_ready as writer_schema_ready,
                )

                with self.collector.engine.connect() as conn:
                    require(writer_schema_ready(conn), "crm_writer_domain_uninitialized")
            self.authority.validate_files()
        except BaseException:
            self.collector.engine.dispose()
            self.store.close()
            raise
        self.queue = PollStore(self.store, config, self.authority)
        self._client_factory = client_factory
        self.read_gate = None
        self._read_context = None
        self._read_capture_tick = None
        self._client = self._http = self._tokens = None
        self._wake, self._stop = threading.Event(), threading.Event()
        self._execution = threading.Lock()
        self._thread = None
        self._closed = False
        self.background_reason = None
        self._confirmed_connection = None

    def __repr__(self):
        return "CRMRuntime(<local owned work>)"

    def start(self, *, background=True):
        require(not self._closed, "runtime_closed")
        self.authority.acquire()
        try:
            self.validate_write_state()
            self.queue.active_tick()
            if self.read_gate is not None:
                from flight_recorder.adapters.crm.read_control import ReadSessionGate

                require(type(self.read_gate) is ReadSessionGate, "live_read_authority_required")
                try:
                    self.read_gate.check()
                except CRMRefused:
                    tick = self.read_gate.authority.document["tick_id"]
                    require(
                        tick is not None and not self.read_gate.prepare_capture(self, tick),
                        "read_recovery_refused",
                    )
                    # Only an audited already-frozen/terminal capture can reach this branch.
                    self.run_pending()
            if background:
                self._thread = threading.Thread(
                    target=self._runner, name="crm-local-poll-runner", daemon=False
                )
                self._thread.start()
                self.notify()
        except BaseException:
            self.authority.close()
            raise

    def notify(self):
        self.authority.assert_held()
        self._wake.set()

    def validate_write_state(self):
        if not self.writer_capable:
            return None
        from flight_recorder.adapters.crm.writer_store import WriterStore

        if self._writer_store is None:
            self._writer_store = WriterStore(self)
        return self._writer_store.audit()

    def _runner(self):
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if not self._stop.is_set():
                try:
                    self.run_pending()
                    self.background_reason = None
                except CRMRefused as exc:
                    self.background_reason = exc.reason
                except Exception:
                    self.background_reason = "poll_runner_refused"

    def admit_read_tick(self, tick_id):
        from flight_recorder.adapters.crm.read_control import ReadSessionGate

        self.authority.assert_held()
        require(type(self.read_gate) is ReadSessionGate, "live_read_authority_required")
        require(self._execution.acquire(blocking=False), "poll_runner_busy")
        try:
            return self.read_gate.admit_capture(self, tick_id)
        finally:
            self._execution.release()

    def require_read_context(self):
        self.authority.assert_held()
        from flight_recorder.adapters.crm.read_control import ReadActionContext, ReadSessionGate

        injected = (
            type(self._http) is _OwnedHTTP
            and self._http.observation_origin == "injected_transport"
            and self._http.config_binding == self.config.connection.binding
        )
        if self.read_gate is None:
            require(self._client_factory is not None or injected, "live_read_authority_required")
            return None
        require(
            type(self.read_gate) is ReadSessionGate
            and not self.writer_capable
            and self.read_gate.config.canonical_bytes == self.config.canonical_bytes,
            "live_read_authority_required",
        )
        action = self.read_gate.authority.document["action"]
        if self._read_context is None:
            if action == "oauth_connect":
                self._read_context = self.read_gate.authorize_oauth()
            elif action == "metadata_probe":
                self._read_context = self.read_gate.authorize_metadata()
            else:
                require(self._read_capture_tick is not None, "capture_admission_required")
                self._read_context = self.read_gate.authorize_capture(
                    self._read_capture_tick, self.config.targets[0]
                )
        require(
            type(self._read_context) is ReadActionContext
            and self._read_context.gate is self.read_gate,
            "live_read_authority_required",
        )
        self._read_context.check()
        return self._read_context

    def _before_read_attempt(self, method, path, category):
        self.authority.assert_held()
        context = self.require_read_context()
        require(context is not None, "live_read_authority_required")
        context.before_attempt(method, path, category)
        self.authority.assert_held()

    def owned_http(self):
        context = self.require_read_context()
        if self._http is None:
            require(context is not None, "live_read_authority_required")
            self._http = _OwnedHTTP(
                self.config.connection, clock=self.clock, before_attempt=self._before_read_attempt
            )
        require(
            type(self._http) is _OwnedHTTP
            and self._http.config_binding == self.config.connection.binding,
            "invalid_connection_binding",
        )
        if context is not None:
            require(self._http._before_attempt == self._before_read_attempt, "read_context_refused")
        else:
            require(
                self._http.observation_origin == "injected_transport",
                "live_read_authority_required",
            )
        return self._http

    def client(self):
        self.authority.assert_held()
        self.require_read_context()
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from flight_recorder.adapters.crm.salesforce import SalesforceClient
                from flight_recorder.adapters.crm.secrets import TokenStore

                http = self.owned_http()
                if self._tokens is None:
                    self._tokens = TokenStore(self.config.document["credential_directory"])
                self._client = SalesforceClient(
                    self.config.connection, self._tokens, http, self.clock
                )
        elif self._client_factory is None:
            from flight_recorder.adapters.crm.salesforce import SalesforceClient

            self.owned_http()
            require(
                type(self._client) is SalesforceClient
                and self._client._http is self._http
                and self._client._tokens is self._tokens
                and self._client._config.binding == self.config.connection.binding,
                "read_context_refused",
            )
        return self._client

    def verify_for_read(self, client):
        self.require_read_context()
        try:
            self._confirmed_connection = client.verify_connection()
        except CRMRefused as exc:
            self._confirmed_connection = None
            if self._client_factory is not None or exc.reason not in (
                "token_expired",
                "connection_confirmation_stale",
            ):
                raise
            from flight_recorder.adapters.crm.oauth import refresh_connection

            self.require_read_context()
            refresh_connection(self.config.connection, self._tokens, self._http, self.clock)
            self._confirmed_connection = client.verify_connection()

    def run_pending(self):
        from flight_recorder.adapters.crm.poll import _capture_and_submit_owned

        self.authority.assert_held()
        require(not self._closed and not self._stop.is_set(), "runtime_closed")
        require(self._execution.acquire(blocking=False), "poll_runner_busy")
        try:
            self.validate_write_state()
            while not self._stop.is_set():
                tick = self.queue.active_tick()
                if tick is None:
                    break
                if self.read_gate is not None:
                    from flight_recorder.adapters.crm.read_control import ReadSessionGate

                    require(type(self.read_gate) is ReadSessionGate, "live_read_authority_required")
                    if self.read_gate.prepare_capture(self, tick):
                        self._read_capture_tick = tick
                        self.require_read_context()
                for target in self.config.targets:
                    if self._stop.is_set():
                        break
                    report = self.queue.report(tick)
                    member = next(m for m in report.members if m.target_key == target.key)
                    if member.state not in ("complete", "refused", "scoring_inputs_unchanged"):
                        try:
                            _capture_and_submit_owned(self, target, tick, clock=self.clock)
                        finally:
                            if self.read_gate is not None:
                                self.read_gate.capture_result(self, tick)
                if self.queue.report(tick).disposition != "complete":
                    break
        finally:
            self._execution.release()

    def poll_report(self, tick):
        return self.queue.report(tick)

    def close(self):
        if self._closed:
            return
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            require(not self._thread.is_alive(), "runtime_shutdown_pending")
        require(self._execution.acquire(blocking=False), "runtime_shutdown_pending")
        try:
            if self._http is not None:
                self._http.close()
            if self._tokens is not None:
                self._tokens.close()
            self.store.close()
            self.collector.engine.dispose()
            self.authority.close()
            self._closed = True
        finally:
            self._execution.release()
