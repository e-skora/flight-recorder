"""Private local token boundary; ready denotes storage state, never CRM health.

This protects cooperating adapter instances and rejects unsafe filesystem setup.
It is not a sandbox against a hostile process running as the same user or root.
There is no network, automatic refresh retry, reauthorization or recovery here.
"""

import errno
import fcntl
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.collector.canonical import canonical_hash, canonical_text

_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC
_LIMIT = 1024 * 1024
_BINDING = re.compile(r"[a-f0-9]{64}", re.ASCII)
_BOUNDARY_ERRNOS = {
    errno.ELOOP,
    errno.EACCES,
    errno.EPERM,
    errno.ENOENT,
    errno.ENOTDIR,
    errno.EISDIR,
}


def _text(value, limit, reason):
    require(type(value) is str and 0 < len(value) <= limit, reason)
    require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), reason)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CRMRefused(reason) from None


def _binding(connection, binding):
    _text(connection, 256, "invalid_token_binding")
    require(type(binding) is str and bool(_BINDING.fullmatch(binding)), "invalid_token_binding")


def _generation(value):
    require(type(value) is int and value > 0, "stale_token_generation")


@dataclass(frozen=True, repr=False)
class TokenMaterial:
    access_token: str
    refresh_token: str
    expires_at: datetime | None
    client_secret: str | None = None

    def __post_init__(self):
        _text(self.access_token, 16384, "invalid_token_material")
        _text(self.refresh_token, 16384, "invalid_token_material")
        if self.client_secret is not None:
            _text(self.client_secret, 16384, "invalid_token_material")
        if self.expires_at is not None:
            try:
                require(type(self.expires_at) is datetime, "invalid_token_material")
                require(self.expires_at.utcoffset() is not None, "invalid_token_material")
                object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
            except Exception:
                # A supplied tzinfo can execute code and raise credential-bearing errors.
                raise CRMRefused("invalid_token_material") from None

    def __repr__(self):
        return "TokenMaterial(<redacted>)"


def _material(value):
    require(type(value) is TokenMaterial, "invalid_token_material")
    # Revalidate even if a caller bypassed the frozen value constructor.
    TokenMaterial(value.access_token, value.refresh_token, value.expires_at, value.client_secret)


@dataclass(frozen=True, repr=False)
class TokenRead:
    material: TokenMaterial
    generation: int

    def __post_init__(self):
        _material(self.material)
        _generation(self.generation)

    def __repr__(self):
        return "TokenRead(<redacted>)"


@dataclass(frozen=True, repr=False)
class RefreshClaim:
    connection_id: str
    config_binding: str
    generation: int
    material: TokenMaterial

    def __post_init__(self):
        _binding(self.connection_id, self.config_binding)
        _generation(self.generation)
        _material(self.material)

    def __repr__(self):
        return "RefreshClaim(<redacted>)"


@dataclass(frozen=True)
class TokenStatus:
    connection_id: str
    config_binding: str
    generation: int
    state: str


def _date(value):
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _material_record(value):
    return {
        "access_token": value.access_token,
        "refresh_token": value.refresh_token,
        "expires_at": _date(value.expires_at),
        "client_secret": value.client_secret,
    }


def _record(connection, binding, generation, state, material):
    return dict(
        format_version=1,
        connection_id=connection,
        config_binding=binding,
        generation=generation,
        state=state,
        material=_material_record(material),
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "secret_store_unrecognized")
        result[key] = value
    return result


def _nonfinite(_):
    raise CRMRefused("secret_store_unrecognized")


def _decode(body, connection):
    try:
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
        )
        require(type(document) is dict, "secret_store_unrecognized")
        require(
            set(document)
            == {
                "format_version",
                "connection_id",
                "config_binding",
                "generation",
                "state",
                "material",
            },
            "secret_store_unrecognized",
        )
        require(
            type(document["format_version"]) is int and document["format_version"] == 1,
            "secret_store_unrecognized",
        )
        _binding(document["connection_id"], document["config_binding"])
        require(document["connection_id"] == connection, "secret_store_unrecognized")
        _generation(document["generation"])
        require(
            document["state"] in ("ready", "refresh_started", "needs_attention"),
            "secret_store_unrecognized",
        )
        value = document["material"]
        require(
            type(value) is dict
            and set(value) == {"access_token", "refresh_token", "expires_at", "client_secret"},
            "secret_store_unrecognized",
        )
        expiry = value["expires_at"]
        if expiry is not None:
            require(type(expiry) is str, "secret_store_unrecognized")
            parsed = datetime.fromisoformat(expiry)
            require(
                parsed.utcoffset() is not None and _date(parsed) == expiry,
                "secret_store_unrecognized",
            )
            expiry = parsed
        material = TokenMaterial(
            value["access_token"], value["refresh_token"], expiry, value["client_secret"]
        )
        return document, material
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise CRMRefused("secret_store_unrecognized") from None


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _private_file(info):
    require(
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1,
        "secret_boundary_invalid",
    )


class TokenStore:
    def __init__(self, directory: Path | str):
        self._mutex = threading.Lock()
        self._closed = False
        self._paused = False
        self._dirfd = None
        try:
            self._directory = Path(directory)
            require(".." not in self._directory.parts, "secret_boundary_invalid")
            self._directory = self._directory.absolute()
            self._check_components()
            self._dirfd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | _FLAGS)
            self._identity = os.fstat(self._dirfd)
            self._check_directory()
        except (OSError, ValueError, TypeError) as error:
            reason = "secret_boundary_invalid"
            if isinstance(error, OSError) and error.errno not in _BOUNDARY_ERRNOS:
                reason = "secret_persistence_uncertain"
            self._closed = True
            if self._dirfd is not None:
                fd = self._dirfd
                self._dirfd = None
                try:
                    os.close(fd)
                except OSError:
                    # A failed close may already have released this descriptor; never retry it.
                    reason = "secret_persistence_uncertain"
            raise CRMRefused(reason) from None

    def _check_components(self):
        for path in [*reversed(self._directory.parents), self._directory]:
            require(not stat.S_ISLNK(os.lstat(path).st_mode), "secret_boundary_invalid")

    def _check_directory(self):
        self._check_components()
        current = os.lstat(self._directory)
        pinned = os.fstat(self._dirfd)
        require(
            (current.st_dev, current.st_ino)
            == (self._identity.st_dev, self._identity.st_ino)
            == (pinned.st_dev, pinned.st_ino),
            "secret_boundary_invalid",
        )
        for info in (current, pinned):
            require(
                stat.S_ISDIR(info.st_mode)
                and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == os.geteuid(),
                "secret_boundary_invalid",
            )

    def _stat(self, name, *, missing=False):
        try:
            info = os.stat(name, dir_fd=self._dirfd, follow_symlinks=False)
        except FileNotFoundError:
            if missing:
                return None
            raise CRMRefused("token_material_missing") from None
        _private_file(info)
        return info

    def _lock_fd(self):
        try:
            old = self._stat(".lock", missing=True)
            if old is None:
                try:
                    fd = os.open(
                        ".lock",
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS,
                        0o600,
                        dir_fd=self._dirfd,
                    )
                except FileExistsError:
                    fd = os.open(".lock", os.O_RDWR | _FLAGS, dir_fd=self._dirfd)
            else:
                fd = os.open(".lock", os.O_RDWR | _FLAGS, dir_fd=self._dirfd)
        except OSError as error:
            if error.errno in _BOUNDARY_ERRNOS:
                raise CRMRefused("secret_boundary_invalid") from None
            raise
        try:
            _private_file(os.fstat(fd))
            require(
                _signature(self._stat(".lock")) == _signature(os.fstat(fd)),
                "secret_boundary_invalid",
            )
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _locked(self):
        with self._mutex:
            require(not self._closed, "secret_store_closed")
            require(not self._paused, "secret_store_paused")
            fd = None
            try:
                self._check_directory()
                fd = self._lock_fd()
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise CRMRefused("secret_store_contended") from None
                        time.sleep(0.01)
                self._check_directory()
                require(
                    _signature(self._stat(".lock")) == _signature(os.fstat(fd)),
                    "secret_boundary_invalid",
                )
                yield
            except OSError:
                self._paused = True
                raise CRMRefused("secret_persistence_uncertain") from None
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        self._paused = True
                        raise CRMRefused("secret_persistence_uncertain") from None

    def close(self):
        with self._mutex:
            if not self._closed:
                self._closed = True
                fd = self._dirfd
                self._dirfd = None
                try:
                    os.close(fd)
                except OSError:
                    self._paused = True
                    raise CRMRefused("secret_persistence_uncertain") from None

    @staticmethod
    def _name(connection):
        return canonical_hash({"connection_id": connection}) + ".json"

    def _read(self, connection, binding, *, missing=False):
        name = self._name(connection)
        original = self._stat(name, missing=missing)
        if original is None:
            return None
        try:
            fd = os.open(name, os.O_RDONLY | _FLAGS, dir_fd=self._dirfd)
        except OSError as error:
            if error.errno in _BOUNDARY_ERRNOS:
                raise CRMRefused("secret_boundary_invalid") from None
            raise
        try:
            require(_signature(original) == _signature(os.fstat(fd)), "secret_boundary_invalid")
            require(original.st_size <= _LIMIT, "secret_store_unrecognized")
            body = bytearray()
            while part := os.read(fd, min(65536, _LIMIT + 1 - len(body))):
                body.extend(part)
                require(len(body) <= _LIMIT, "secret_store_unrecognized")
            require(
                _signature(original) == _signature(os.fstat(fd)) == _signature(self._stat(name)),
                "secret_boundary_invalid",
            )
        finally:
            os.close(fd)
        self._check_directory()
        document, material = _decode(bytes(body), connection)
        require(document["config_binding"] == binding, "invalid_token_binding")
        return document, material, _signature(original)

    def _write(self, connection, document, expected):
        name = self._name(connection)
        temporary = "." + uuid.uuid4().hex + ".tmp"
        body = canonical_text(document).encode("utf-8")
        fd = None
        owned = None
        replaced = False
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600, dir_fd=self._dirfd
            )
            owned = os.fstat(fd)
            _private_file(owned)
            remaining = memoryview(body)
            while remaining:
                size = os.write(fd, remaining)
                if size <= 0:
                    raise OSError("incomplete write")
                remaining = remaining[size:]
            os.fsync(fd)
            self._check_directory()
            current = self._stat(name, missing=True)
            require(
                (None if current is None else _signature(current)) == expected,
                "secret_boundary_invalid",
            )
            require(
                _signature(os.fstat(fd)) == _signature(self._stat(temporary)),
                "secret_boundary_invalid",
            )
            os.replace(temporary, name, src_dir_fd=self._dirfd, dst_dir_fd=self._dirfd)
            replaced = True
            os.fsync(self._dirfd)
            self._check_directory()
        except CRMRefused:
            if replaced:
                self._paused = True
                raise CRMRefused("secret_persistence_uncertain") from None
            raise
        finally:
            if fd is not None:
                os.close(fd)
            if owned is not None:
                try:
                    remaining = os.stat(temporary, dir_fd=self._dirfd, follow_symlinks=False)
                    if (remaining.st_dev, remaining.st_ino) == (owned.st_dev, owned.st_ino):
                        os.unlink(temporary, dir_fd=self._dirfd)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _status(document):
        return TokenStatus(
            *(document[key] for key in ("connection_id", "config_binding", "generation", "state"))
        )

    @staticmethod
    def _ready(document, generation=None):
        require(document["state"] == "ready", "token_state_blocked")
        if generation is not None:
            require(document["generation"] == generation, "stale_token_generation")

    def install(self, connection_id, binding, material):
        with self._locked():
            _binding(connection_id, binding)
            _material(material)
            require(self._read(connection_id, binding, missing=True) is None, "token_record_exists")
            record = _record(connection_id, binding, 1, "ready", material)
            self._write(connection_id, record, None)
            return self._status(record)

    def read_ready(self, connection_id, binding):
        with self._locked():
            _binding(connection_id, binding)
            record, material, _ = self._read(connection_id, binding)
            self._ready(record)
            return TokenRead(material, record["generation"])

    def load(self, connection_id, binding):
        return self.read_ready(connection_id, binding).material

    def status(self, connection_id, binding):
        with self._locked():
            _binding(connection_id, binding)
            return self._status(self._read(connection_id, binding)[0])

    def replace(self, connection_id, material, *, binding, expected_generation):
        with self._locked():
            _binding(connection_id, binding)
            _material(material)
            _generation(expected_generation)
            record, _, fingerprint = self._read(connection_id, binding)
            self._ready(record, expected_generation)
            updated = _record(connection_id, binding, expected_generation + 1, "ready", material)
            self._write(connection_id, updated, fingerprint)
            return self._status(updated)

    def begin_refresh(self, connection_id, binding, *, expected_generation):
        with self._locked():
            _binding(connection_id, binding)
            _generation(expected_generation)
            record, material, fingerprint = self._read(connection_id, binding)
            self._ready(record, expected_generation)
            updated = _record(
                connection_id, binding, expected_generation + 1, "refresh_started", material
            )
            self._write(connection_id, updated, fingerprint)
            return RefreshClaim(connection_id, binding, expected_generation + 1, material)

    def _complete(self, claim, material, state):
        with self._locked():
            require(type(claim) is RefreshClaim, "stale_token_generation")
            _binding(claim.connection_id, claim.config_binding)
            _generation(claim.generation)
            _material(claim.material)
            material = claim.material if state == "needs_attention" else material
            _material(material)
            record, old, fingerprint = self._read(claim.connection_id, claim.config_binding)
            require(
                record["state"] == "refresh_started"
                and record["generation"] == claim.generation
                and old == claim.material,
                "stale_token_generation",
            )
            updated = _record(
                claim.connection_id, claim.config_binding, claim.generation + 1, state, material
            )
            self._write(claim.connection_id, updated, fingerprint)
            return self._status(updated)

    def finish_refresh(self, claim, new_material):
        return self._complete(claim, new_material, "ready")

    def fail_refresh(self, claim):
        return self._complete(claim, None, "needs_attention")
