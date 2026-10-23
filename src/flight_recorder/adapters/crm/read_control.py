"""Private explicit read-session authority; never a writer grant."""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import timedelta

from flight_recorder.adapters.crm.contracts import (
    CRMRefused,
    _salesforce_id,
    observation_time,
    require,
    validate_unit_id,
)
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.adapters.crm.runtime_config import absolute_path
from flight_recorder.collector.canonical import canonical_bytes

READ_ACTIONS = frozenset({"oauth_connect", "metadata_probe", "capture_once"})
_AUTHORITY_KEYS = frozenset(
    {
        "format_version",
        "action",
        "config_sha256",
        "expected_instance_id",
        "expected_user_id",
        "record_ids",
        "issued_at",
        "expires_at",
        "owner_request_sha256",
        "tick_id",
        "metadata_receipt_sha256",
    }
)


@dataclass(frozen=True, repr=False)
class ReadAuthority:
    canonical_bytes: bytes

    def __post_init__(self):
        try:
            require(type(self.canonical_bytes) is bytes, "invalid_read_authority")
            d = strict_json(self.canonical_bytes)
            require(
                type(d) is dict
                and set(d) == _AUTHORITY_KEYS
                and type(d["format_version"]) is int
                and d["format_version"] == 1
                and type(d["action"]) is str
                and d["action"] in READ_ACTIONS
                and canonical_bytes(d) == self.canonical_bytes,
                "invalid_read_authority",
            )
            for key in ("config_sha256", "owner_request_sha256"):
                validate_unit_id(d[key])
            _salesforce_id(d["expected_instance_id"], "00D")
            _salesforce_id(d["expected_user_id"], "005")
            require(
                type(d["record_ids"]) is list and len(d["record_ids"]) == 1,
                "invalid_read_authority",
            )
            _salesforce_id(d["record_ids"][0], "001")
            require(
                observation_time(d["issued_at"]) < observation_time(d["expires_at"]),
                "invalid_read_authority",
            )
            if d["action"] == "capture_once":
                tick = d["tick_id"]
                require(
                    type(tick) is str
                    and 0 < len(tick) <= 512
                    and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in tick),
                    "invalid_read_authority",
                )
                tick.encode("utf-8")
                validate_unit_id(d["metadata_receipt_sha256"])
            else:
                require(
                    d["tick_id"] is None and d["metadata_receipt_sha256"] is None,
                    "invalid_read_authority",
                )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CRMRefused("invalid_read_authority") from None

    @classmethod
    def from_document(cls, document):
        try:
            require(type(document) is dict, "invalid_read_authority")
            return cls(canonical_bytes(document))
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_read_authority") from None

    @property
    def document(self):
        return strict_json(self.canonical_bytes)

    def __repr__(self):
        return "ReadAuthority(<private read grant>)"


def _file_signature(path):
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        raise CRMRefused("read_control_file_changed") from None
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class PinnedReadFile:
    """A held operator file; even an equal-byte rewrite requires a new invocation."""

    def __init__(self, path):
        self.path = absolute_path(str(path))
        self.body, self.signature = self._read()
        require(canonical_bytes(strict_json(self.body)) == self.body, "read_control_file_invalid")
        self.sha256 = hashlib.sha256(self.body).hexdigest()

    def _read(self):
        from flight_recorder.adapters.crm.routes import read_private_file

        try:
            absolute_path(str(self.path))
            before = _file_signature(self.path)
            body = read_private_file(self.path, limit=1024 * 1024)
            require(before == _file_signature(self.path), "read_control_file_changed")
            return body, before
        except CRMRefused:
            raise
        except (OSError, ValueError, TypeError):
            raise CRMRefused("read_control_file_invalid") from None

    def check(self):
        try:
            body, signature = self._read()
            require(signature == self.signature and body == self.body, "read_control_file_changed")
            return body
        except (OSError, ValueError, TypeError):
            raise CRMRefused("read_control_file_changed") from None

    def __repr__(self):
        return "PinnedReadFile(<private pinned operator file>)"


class ReadEvidenceStore:
    """Separate private content-addressed evidence; no retained CRM format changes."""

    def __init__(self, directory):
        self.directory = absolute_path(str(directory))
        self._directory_identity = self._directory_check()
        self._pinned = {}

    def _directory_check(self):
        try:
            absolute_path(str(self.directory))
            value = self.directory.stat(follow_symlinks=False)
            require(
                stat.S_ISDIR(value.st_mode)
                and value.st_uid == os.getuid()
                and stat.S_IMODE(value.st_mode) == 0o700,
                "read_evidence_directory_invalid",
            )
            return value.st_dev, value.st_ino, value.st_mode, value.st_uid
        except (OSError, ValueError, TypeError):
            raise CRMRefused("read_evidence_directory_invalid") from None

    def check(self):
        try:
            require(
                self._directory_check() == self._directory_identity, "read_control_file_changed"
            )
        except (OSError, ValueError, TypeError):
            raise CRMRefused("read_control_file_changed") from None

    def load(self, digest):
        self.check()
        validate_unit_id(digest)
        pinned = self._pinned.get(digest)
        if pinned is None:
            pinned = PinnedReadFile(self.directory / (digest + ".json"))
            require(pinned.sha256 == digest, "read_evidence_mismatch")
            self._pinned[digest] = pinned
        body = pinned.check()
        self.check()
        return strict_json(body)

    def save(self, document):
        self.check()
        require(type(document) is dict, "read_evidence_invalid")
        body = canonical_bytes(document)
        require(len(body) <= 1024 * 1024, "read_evidence_invalid")
        digest = hashlib.sha256(body).hexdigest()
        path = self.directory / (digest + ".json")
        fd = None
        try:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                require(self.load(digest) == document, "read_evidence_mismatch")
                return digest
            os.fchmod(fd, 0o600)
            remaining = memoryview(body)
            while remaining:
                count = os.write(fd, remaining)
                require(count > 0, "read_evidence_write_failed")
                remaining = remaining[count:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                require(
                    os.fstat(directory_fd).st_ino == self._directory_identity[1],
                    "read_control_file_changed",
                )
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            require(self.load(digest) == document, "read_evidence_mismatch")
            return digest
        except CRMRefused:
            raise
        except OSError:
            raise CRMRefused("read_evidence_write_failed") from None
        finally:
            if fd is not None:
                os.close(fd)

    def __repr__(self):
        return "ReadEvidenceStore(<private immutable receipts>)"


class NetworkCheckGate:
    """A no-provider grant for exactly one fixed local probe route/body."""

    def __init__(self, config, qualification_path, evidence_directory, *, clock):
        from flight_recorder.adapters.crm.cli import N8nBinding
        from flight_recorder.adapters.crm.routes import ingress_secret
        from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig

        require(type(config) is CRMRuntimeConfig, "invalid_network_qualification")
        config.__post_init__()
        self.config, self.clock = config, clock
        self.file = PinnedReadFile(qualification_path)
        self.evidence = ReadEvidenceStore(evidence_directory)
        grant = strict_json(self.file.body)
        require(
            type(grant) is dict
            and set(grant)
            == {
                "format_version",
                "config_sha256",
                "owner_request_sha256",
                "mode",
                "probe_id",
                "issued_at",
                "expires_at",
                "binding_sha256",
            }
            and type(grant["format_version"]) is int
            and grant["format_version"] == 1,
            "invalid_network_qualification",
        )
        for key in ("config_sha256", "owner_request_sha256", "probe_id", "binding_sha256"):
            validate_unit_id(grant[key])
        self.binding = N8nBinding.from_document(self.evidence.load(grant["binding_sha256"]), config)
        binding = self.binding.document
        require(
            grant["config_sha256"] == config.binding
            and all(grant[k] == binding[k] for k in ("owner_request_sha256", "mode", "probe_id")),
            "invalid_network_qualification",
        )
        self.grant = grant
        self.secret = ingress_secret(config)
        self.secret_path = absolute_path(config.document["ingress_secret_file"])
        self.secret_signature = _file_signature(self.secret_path)
        self.check()

    def check(self):
        from flight_recorder.adapters.crm.cli import N8nBinding
        from flight_recorder.adapters.crm.http import utc_now
        from flight_recorder.adapters.crm.routes import ingress_secret

        self.file.check()
        self.config.__post_init__()
        self.evidence.check()
        binding = N8nBinding.from_document(
            self.evidence.load(self.grant["binding_sha256"]), self.config
        )
        require(
            binding.canonical_bytes == self.binding.canonical_bytes, "read_control_file_changed"
        )
        require(
            _file_signature(self.secret_path) == self.secret_signature
            and ingress_secret(self.config) == self.secret,
            "read_control_file_changed",
        )
        now = utc_now(self.clock)
        for d in (self.grant, binding.document):
            require(
                observation_time(d["issued_at"]) <= now < observation_time(d["expires_at"]),
                "network_qualification_expired",
            )
        return now

    def observe(self, body):
        from flight_recorder.collector.canonical import canonical_hash

        now = self.check()
        require(
            type(body) is dict
            and set(body) == {"format_version", "connection_id", "probe_id"}
            and type(body["format_version"]) is int
            and body["format_version"] == 1
            and body["connection_id"] == self.config.connection.connection_id
            and body["probe_id"] == self.grant["probe_id"],
            "invalid_network_probe",
        )
        result = dict(
            state="network_check_received",
            config_sha256=self.config.binding,
            probe_sha256=canonical_hash(self.grant["probe_id"]),
            body_sha256=canonical_hash(body),
        )
        self.evidence.save(
            dict(
                format_version=1,
                type="n8n_network_observation",
                qualification_sha256=self.file.sha256,
                binding_sha256=self.grant["binding_sha256"],
                owner_request_sha256=self.grant["owner_request_sha256"],
                mode=self.grant["mode"],
                config_sha256=self.config.binding,
                method="POST",
                path="/api/internal/crm/network-check",
                received_host="localhost:8768",
                logical_ingress="http://localhost:8768",
                observed_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                probe_sha256=result["probe_sha256"],
                body_sha256=result["body_sha256"],
                result=result,
            )
        )
        return result

    def __repr__(self):
        return "NetworkCheckGate(<no provider capability>)"


@dataclass(frozen=True, repr=False)
class ReadActionContext:
    gate: object
    action: str
    fingerprint: tuple

    def check(self):
        require(
            type(self.gate) is ReadSessionGate
            and self.fingerprint == self.gate.fingerprint
            and self.action == self.gate.authority.document["action"],
            "read_context_refused",
        )
        return self.gate.check()

    def before_attempt(self, method, path, category):
        self.check()
        c = self.gate.config.connection
        identities = {
            f"/id/{org}/{user}"
            for org in (c.expected_instance_id, c.expected_instance_id[:15])
            for user in (c.expected_user_id, c.expected_user_id[:15])
        }
        allowed = method == "GET" and (
            category == "identity"
            and path in identities
            or category == "api_versions"
            and path == "/services/data"
            or self.action == "metadata_probe"
            and category == "account_metadata"
            and path == f"/services/data/v{c.api_version}/sobjects/Account/describe"
            or self.action == "capture_once"
            and category == "account_read"
            and path == f"/services/data/v{c.api_version}/sobjects/Account/{c.record_ids[0]}"
        )
        allowed = allowed or (
            method == "POST"
            and path == "/services/oauth2/token"
            and (
                self.action == "oauth_connect"
                and category == "authorization_code"
                or self.action in {"metadata_probe", "capture_once"}
                and category == "refresh_token"
            )
        )
        require(allowed, "read_request_refused")
        if method == "POST" and category == "authorization_code":
            require(not self.gate._oauth_attempted, "read_request_refused")
            self.gate._oauth_attempted = True

    def __repr__(self):
        return "ReadActionContext(<read only, no writer capability>)"


class ReadSessionGate:
    def __init__(self, config_path, readiness_path, authority_path, evidence_directory, *, clock):
        from flight_recorder.adapters.crm.readiness import ReadinessManifest
        from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig

        self.config_file = PinnedReadFile(config_path)
        self.readiness_file = PinnedReadFile(readiness_path)
        self.authority_file = PinnedReadFile(authority_path)
        self.config = CRMRuntimeConfig(self.config_file.body)
        self.manifest = ReadinessManifest(self.readiness_file.body)
        self.authority = ReadAuthority(self.authority_file.body)
        self.evidence = ReadEvidenceStore(evidence_directory)
        self.clock = clock
        self._oauth_attempted = False
        from flight_recorder.adapters.crm.routes import ingress_secret

        self._ingress_secret = ingress_secret(self.config)
        self._ingress_path = absolute_path(self.config.document["ingress_secret_file"])
        self._ingress_signature = _file_signature(self._ingress_path)
        self.fingerprint = tuple(
            f.sha256 for f in (self.config_file, self.readiness_file, self.authority_file)
        )

    def _transport(self, now):
        from flight_recorder.adapters.crm.cli import N8nBinding, bound_n8n_workflow
        from flight_recorder.collector.canonical import canonical_hash

        try:
            m = self.manifest.document
            proof = m["evidence"]["n8n"]
            require(type(proof) is dict, "n8n_transport_unqualified")
            t = self.evidence.load(proof["sha256"])
            keys = {
                "format_version",
                "type",
                "config_sha256",
                "owner_request_sha256",
                "binding_sha256",
                "runtime",
                "poll_template_sha256",
                "qualification_template_sha256",
                "bound_poll_sha256",
                "bound_qualification_sha256",
                "credential_id",
                "connection_id",
                "mode",
                "transport_url",
                "logical_host",
                "ingress_origin",
                "poll_workflow_id",
                "dummy",
                "actual",
                "observed_at",
                "reviewed_at",
            }
            require(
                type(t) is dict
                and set(t) == keys
                and type(t["format_version"]) is int
                and t["format_version"] == 1
                and t["type"] == "n8n_transport_qualification"
                and t["config_sha256"] == self.config.binding,
                "n8n_transport_unqualified",
            )
            b = N8nBinding.from_document(self.evidence.load(t["binding_sha256"]), self.config)
            d = b.document
            require(
                observation_time(d["issued_at"]) <= now < observation_time(d["expires_at"]),
                "n8n_transport_unqualified",
            )
            for key in (
                "owner_request_sha256",
                "credential_id",
                "connection_id",
                "mode",
                "poll_template_sha256",
                "qualification_template_sha256",
            ):
                require(t[key] == d[key], "n8n_transport_unqualified")
            qualifier = bound_n8n_workflow(self.config, b, kind="qualification")
            url = qualifier["nodes"][1]["parameters"]["url"]
            require(
                t["bound_poll_sha256"] == canonical_hash(bound_n8n_workflow(self.config, b))
                and t["bound_qualification_sha256"] == canonical_hash(qualifier)
                and m["n8n"]["workflow_sha256"] == t["bound_poll_sha256"]
                and t["transport_url"] == url
                and t["logical_host"] == "localhost:8768"
                and t["ingress_origin"] == "http://localhost:8768"
                and t["poll_workflow_id"] is None,
                "n8n_transport_unqualified",
            )
            runtime = t["runtime"]
            require(
                type(runtime) is dict
                and set(runtime) == {"id", "version", "container", "image"}
                and runtime["id"] == m["n8n"]["runtime_id"]
                and runtime["version"] == m["n8n"]["version"]
                and runtime["version"] == m["n8n"]["expected_version"],
                "n8n_transport_unqualified",
            )
            if d["mode"] == "docker_host":
                require(
                    type(runtime["container"]) is str
                    and re.fullmatch(r"[0-9a-f]{64}", runtime["container"])
                    and type(runtime["image"]) is str
                    and re.fullmatch(
                        r"docker\.n8n\.io/n8nio/n8n:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}",
                        runtime["image"],
                    )
                    and runtime["image"].split(":")[-2].split("@")[0] == runtime["version"],
                    "n8n_transport_unqualified",
                )
            else:
                require(
                    runtime["container"] is None and runtime["image"] is None,
                    "n8n_transport_unqualified",
                )
            body = dict(format_version=1, connection_id=d["connection_id"], probe_id=d["probe_id"])
            result = dict(
                state="network_check_received",
                config_sha256=self.config.binding,
                probe_sha256=canonical_hash(d["probe_id"]),
                body_sha256=canonical_hash(body),
            )
            dummy = t["dummy"]
            require(
                type(dummy) is dict
                and set(dummy)
                == {
                    "credential_id",
                    "workflow_id",
                    "execution_id",
                    "workflow_sha256",
                    "request_url",
                    "method",
                    "received_host",
                    "body_sha256",
                    "probe_sha256",
                    "result",
                    "executed_at",
                    "observed_at",
                    "reviewed_at",
                }
                and type(dummy["credential_id"]) is str
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dummy["credential_id"])
                and dummy["credential_id"] != d["credential_id"],
                "n8n_transport_unqualified",
            )
            dummy_export = bound_n8n_workflow(self.config, b, kind="qualification")
            dummy_url = url.replace(":8768/", ":18768/")
            dummy_export["nodes"][1]["parameters"]["url"] = dummy_url
            dummy_export["nodes"][1]["credentials"]["httpHeaderAuth"]["id"] = dummy["credential_id"]
            require(
                dummy["workflow_sha256"] == canonical_hash(dummy_export)
                and dummy["request_url"] == dummy_url
                and dummy["method"] == "POST"
                and dummy["received_host"]
                == ("localhost:8768" if d["mode"] == "docker_host" else "localhost:18768")
                and dummy["body_sha256"] == result["body_sha256"]
                and dummy["probe_sha256"] == result["probe_sha256"]
                and dummy["result"] == result,
                "n8n_transport_unqualified",
            )
            actual = t["actual"]
            require(
                type(actual) is dict
                and set(actual)
                == {
                    "observation_sha256",
                    "workflow_id",
                    "execution_id",
                    "workflow_sha256",
                    "executed_at",
                    "reviewed_at",
                }
                and actual["workflow_sha256"] == t["bound_qualification_sha256"],
                "n8n_transport_unqualified",
            )
            for step in (dummy, actual):
                for key in ("workflow_id", "execution_id"):
                    require(
                        type(step[key]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", step[key]),
                        "n8n_transport_unqualified",
                    )
                require(
                    observation_time(d["issued_at"])
                    <= observation_time(step["executed_at"])
                    <= observation_time(step["reviewed_at"])
                    <= now,
                    "n8n_transport_unqualified",
                )
            obs = self.evidence.load(actual["observation_sha256"])
            require(
                type(obs) is dict
                and set(obs)
                == {
                    "format_version",
                    "type",
                    "qualification_sha256",
                    "binding_sha256",
                    "owner_request_sha256",
                    "mode",
                    "config_sha256",
                    "method",
                    "path",
                    "received_host",
                    "logical_ingress",
                    "observed_at",
                    "probe_sha256",
                    "body_sha256",
                    "result",
                }
                and type(obs["format_version"]) is int
                and obs["format_version"] == 1
                and obs["type"] == "n8n_network_observation",
                "n8n_transport_unqualified",
            )
            validate_unit_id(obs["qualification_sha256"])
            require(
                all(
                    obs[k] == t[k]
                    for k in ("binding_sha256", "owner_request_sha256", "mode", "config_sha256")
                )
                and obs["method"] == "POST"
                and obs["path"] == "/api/internal/crm/network-check"
                and obs["received_host"] == "localhost:8768"
                and obs["logical_ingress"] == t["ingress_origin"]
                and obs["body_sha256"] == result["body_sha256"]
                and obs["probe_sha256"] == result["probe_sha256"]
                and obs["result"] == result
                and obs["observed_at"] == t["observed_at"]
                and observation_time(actual["executed_at"])
                <= observation_time(obs["observed_at"])
                <= observation_time(actual["reviewed_at"])
                <= observation_time(t["reviewed_at"])
                <= now
                and observation_time(dummy["executed_at"])
                <= observation_time(dummy["observed_at"])
                <= observation_time(dummy["reviewed_at"])
                <= observation_time(t["reviewed_at"])
                and proof["observed_at"] == t["observed_at"]
                and proof["reviewed_at"] == t["reviewed_at"],
                "n8n_transport_unqualified",
            )
            return b
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CRMRefused("n8n_transport_unqualified") from None

    def check(self):
        from flight_recorder.adapters.crm.http import utc_now

        self._check_pinned()
        return self._validate_at(utc_now(self.clock))

    def _validate_at(self, now):
        from flight_recorder.adapters.crm.readiness import validate_readiness

        a, c, m = self.authority.document, self.config.connection, self.manifest.document
        require(
            len(c.record_ids) == 1
            and a["record_ids"] == list(c.record_ids)
            and a["config_sha256"] == self.config.binding
            and a["expected_instance_id"] == c.expected_instance_id
            and a["expected_user_id"] == c.expected_user_id
            and not c.write_enabled,
            "read_authority_binding_mismatch",
        )
        require(
            observation_time(a["issued_at"]) <= now < observation_time(a["expires_at"]),
            "read_authority_expired",
        )
        require(m["authority"]["stage"] == "read_only", "read_stage_refused")
        binding = self._transport(now)
        report = validate_readiness(self.manifest, self.config, now=now, n8n_binding=binding)
        if a["action"] == "capture_once":
            require(report.ready_for_read and not report.ready_for_write, "readiness_refused")
            self._capture_metadata(now)
        else:
            missing = set()
            for field, proof, reason in (
                ("advertised_api_versions", "api_versions", "api_version_unqualified"),
                ("field_schema", "field_schema", "field_schema_unqualified"),
            ):
                if m[field] == [] and m["evidence"][proof] is None:
                    missing.update({reason, "evidence_" + proof + "_unqualified"})
            p = m["permissions"]
            if (
                p
                == dict(
                    user_id=None,
                    account_read=None,
                    account_update=None,
                    read_fields=[],
                    write_fields=[],
                )
                and m["evidence"]["permissions"] is None
            ):
                missing.update({"permissions_unqualified", "evidence_permissions_unqualified"})
            require(
                set(report.reasons) <= missing and not report.ready_for_write, "readiness_refused"
            )
        return now

    def _capture_metadata(self, now):
        from flight_recorder.adapters.crm.salesforce import ObservedAccountMetadata

        a, m, c = self.authority.document, self.manifest.document, self.config.connection
        receipt = self.evidence.load(a["metadata_receipt_sha256"])
        require(
            type(receipt) is dict
            and set(receipt)
            == {
                "format_version",
                "type",
                "config_sha256",
                "owner_request_sha256",
                "authority_sha256",
                "observation",
            }
            and type(receipt["format_version"]) is int
            and receipt["format_version"] == 1
            and receipt["type"] == "metadata"
            and receipt["config_sha256"] == self.config.binding,
            "metadata_unqualified",
        )
        validate_unit_id(receipt["owner_request_sha256"])
        validate_unit_id(receipt["authority_sha256"])
        obs = ObservedAccountMetadata(canonical_bytes(receipt["observation"])).document
        require(
            obs["observation_origin"] == "provider_http"
            and obs["connection_binding"] == c.binding
            and obs["expected_instance_id"] == c.expected_instance_id
            and obs["expected_user_id"] == c.expected_user_id
            and obs["api_origin"] == c.api_origin
            and obs["api_version"] == c.api_version,
            "metadata_unqualified",
        )
        finished = observation_time(obs["read_finished_at"])
        require(timedelta(0) <= now - finished < timedelta(minutes=15), "metadata_expired")
        for key in ("api_versions", "field_schema", "permissions"):
            proof = m["evidence"][key]
            require(
                type(proof) is dict
                and proof["sha256"] == a["metadata_receipt_sha256"]
                and proof["observed_at"] == obs["read_finished_at"]
                and finished <= observation_time(proof["reviewed_at"]) <= now,
                "metadata_unqualified",
            )
        fields = [
            {k: v for k, v in f.items() if k not in {"readable", "updateable"}}
            for f in obs["fields"]
        ]
        require(
            not obs["missing_fields"]
            and fields == sorted(m["field_schema"], key=lambda f: f["name"])
            and obs["advertised_versions"] == sorted(set(m["advertised_api_versions"]))
            and m["permissions"]["account_read"]
            == (obs["account"]["queryable"] and obs["account"]["retrieveable"])
            and m["permissions"]["account_update"] == obs["account"]["updateable"]
            and m["permissions"]["read_fields"] == sorted(f["name"] for f in obs["fields"])
            and m["permissions"]["write_fields"]
            == (
                sorted(f["name"] for f in obs["fields"] if f["updateable"])
                if obs["account"]["updateable"]
                else []
            ),
            "metadata_unqualified",
        )
        return receipt

    def _authorize(self, action):
        require(self.authority.document["action"] == action, "read_action_refused")
        self.check()
        return ReadActionContext(self, action, self.fingerprint)

    def authorize_oauth(self):
        return self._authorize("oauth_connect")

    def authorize_metadata(self):
        return self._authorize("metadata_probe")

    def authorize_capture(self, tick_id, target):
        require(
            tick_id == self.authority.document["tick_id"] and target == self.config.targets[0],
            "read_action_refused",
        )
        return self._authorize("capture_once")

    def _capture_binding(self, tick_id):
        a = self.authority.document
        require(
            a["action"] == "capture_once"
            and tick_id == a["tick_id"]
            and len(self.config.targets) == 1,
            "read_action_refused",
        )
        return dict(
            format_version=1,
            type="capture_admission",
            action="capture_once",
            config_sha256=self.config_file.sha256,
            readiness_sha256=self.readiness_file.sha256,
            authority_sha256=self.authority_file.sha256,
            metadata_receipt_sha256=a["metadata_receipt_sha256"],
            owner_request_sha256=a["owner_request_sha256"],
            target_key=self.config.targets[0].key,
            connection_id=self.config.connection.connection_id,
            tick_id=tick_id,
        )

    def _documents(self):
        self.evidence.check()
        try:
            paths = list(self.evidence.directory.iterdir())
        except OSError:
            raise CRMRefused("read_control_file_changed") from None
        require(len(paths) <= 10000, "read_evidence_invalid")
        for path in paths:
            require(
                re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is not None, "read_evidence_invalid"
            )
            yield path.stem, self.evidence.load(path.stem)

    def _capture_admission(self, tick_id):
        expected = self._capture_binding(tick_id)
        matching = []
        for digest, document in self._documents():
            if document.get("type") != "capture_admission":
                continue
            if (
                document.get("connection_id") == expected["connection_id"]
                and document.get("tick_id") == tick_id
            ):
                require(
                    set(document) == {*expected, "admitted_at"}
                    and {k: v for k, v in document.items() if k != "admitted_at"} == expected,
                    "capture_admission_mismatch",
                )
                admitted = observation_time(document["admitted_at"])
                a = self.authority.document
                require(
                    observation_time(a["issued_at"])
                    <= admitted
                    < observation_time(a["expires_at"]),
                    "capture_admission_mismatch",
                )
                self._validate_at(admitted)
                matching.append(digest)
        require(len(matching) <= 1, "capture_admission_mismatch")
        return matching[0] if matching else None

    def _check_pinned(self):
        from flight_recorder.adapters.crm.routes import ingress_secret

        for file in (self.config_file, self.readiness_file, self.authority_file):
            file.check()
        self.config.__post_init__()
        self.evidence.check()
        require(
            _file_signature(self._ingress_path) == self._ingress_signature
            and ingress_secret(self.config) == self._ingress_secret,
            "read_control_file_changed",
        )

    def admit_capture(self, runtime, tick_id):
        from flight_recorder.adapters.crm.poll import dispatch_poll

        self._check_pinned()
        require(
            runtime.config.canonical_bytes == self.config.canonical_bytes
            and not runtime.writer_capable,
            "read_context_refused",
        )
        admission = self._capture_admission(tick_id)
        if runtime.queue.has_tick(tick_id):
            require(admission is not None, "capture_admission_required")
            self.capture_result(runtime, tick_id)
            # Returning an existing receipt does not grant another network attempt.
            return runtime.queue.report(tick_id)
        now = self.authorize_capture(tick_id, self.config.targets[0]).check()
        if admission is None:
            admission = self.evidence.save(
                dict(
                    **self._capture_binding(tick_id),
                    admitted_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
            )
        report = dispatch_poll(
            runtime, self.config.connection.connection_id, tick_id, clock=self.clock
        )
        self.capture_result(runtime, tick_id)
        return report

    def prepare_capture(self, runtime, tick_id):
        from flight_recorder.adapters.crm.contracts import Ownership

        self._check_pinned()
        require(self._capture_admission(tick_id) is not None, "capture_admission_required")
        self.capture_result(runtime, tick_id)
        report = runtime.queue.report(tick_id)
        if report.disposition == "complete":
            return False
        with runtime.store._read() as conn:
            member = runtime.queue._member(conn, tick_id, self.config.targets[0].key)
        if member[1] is not None:
            owner = Ownership(self.config.targets[0].key, member[0], member[1])
            fact = runtime.store.observation_for_owner(owner)
            if fact is not None:
                if fact["state"] != "scoring_inputs_unchanged":
                    require(
                        runtime.store.capture_for_owner(owner) is not None, "missing_frozen_capture"
                    )
                return False
        self.authorize_capture(tick_id, self.config.targets[0])
        return True

    def capture_result(self, runtime, tick_id):
        from dataclasses import asdict

        from flight_recorder.collector.canonical import canonical_hash

        self._check_pinned()
        admission = self._capture_admission(tick_id)
        require(admission is not None, "capture_admission_required")
        report = runtime.queue.report(tick_id)
        with runtime.store._read() as conn:
            durable, _ = runtime.queue._tick(conn, tick_id)
        receipt_sha = canonical_hash(durable)
        expected = dict(
            format_version=1,
            type="capture_result",
            admission_sha256=admission,
            config_sha256=self.config.binding,
            connection_id=self.config.connection.connection_id,
            tick_id=tick_id,
            receipt_body_sha256=receipt_sha,
        )
        for _, previous in self._documents():
            if (
                previous.get("type") == "capture_result"
                and previous.get("admission_sha256") == admission
            ):
                require(
                    set(previous) == {*expected, "report"}
                    and {k: v for k, v in previous.items() if k != "report"} == expected,
                    "capture_result_mismatch",
                )
                self._audit_result_report(previous["report"], report)
        return self.evidence.save(
            dict(**expected, report=strict_json(canonical_bytes(asdict(report))))
        )

    def _audit_result_report(self, saved, current):
        from dataclasses import asdict

        require(
            type(saved) is dict
            and set(saved) == {"connection_id", "tick_id", "disposition", "members"}
            and saved["connection_id"] == current.connection_id
            and saved["tick_id"] == current.tick_id
            and type(saved["disposition"]) is str
            and saved["disposition"] in {"waiting", "active", "complete"}
            and type(saved["members"]) is list
            and len(saved["members"]) == 1,
            "capture_result_mismatch",
        )
        member = saved["members"][0]
        require(
            type(member) is dict
            and set(member) == {"target_key", "state", "reason", "unit_id", "decision_event_id"}
            and member["target_key"] == self.config.targets[0].key
            and type(member["state"]) is str
            and member["state"]
            in {
                "queued",
                "claimed",
                "read_started",
                "blocked",
                "complete",
                "refused",
                "scoring_inputs_unchanged",
            }
            and (
                member["reason"] is None
                or type(member["reason"]) is str
                and re.fullmatch(r"[a-z0-9_]{1,128}", member["reason"]) is not None
            ),
            "capture_result_mismatch",
        )
        terminal = member["state"] in {"complete", "refused", "scoring_inputs_unchanged"}
        require((saved["disposition"] == "complete") == terminal, "capture_result_mismatch")
        if terminal:
            # A terminal history cannot precede or contradict the durable terminal it names.
            require(
                saved == strict_json(canonical_bytes(asdict(current))), "capture_result_mismatch"
            )
        else:
            require(
                member["unit_id"] is None
                and member["decision_event_id"] is None
                and (member["state"] == "blocked" or member["reason"] is None)
                and not (saved["disposition"] == "active" and current.disposition == "waiting")
                and not (member["state"] != "queued" and current.members[0].state == "queued"),
                "capture_result_mismatch",
            )

    def observe_metadata(self, runtime, context):
        from flight_recorder.adapters.crm.http import OwnedHTTP
        from flight_recorder.adapters.crm.runtime import CRMRuntime
        from flight_recorder.adapters.crm.salesforce import SalesforceClient

        require(
            type(context) is ReadActionContext
            and context.gate is self
            and context.action == "metadata_probe"
            and type(runtime) is CRMRuntime
            and runtime.read_gate is self
            and runtime.config.canonical_bytes == self.config.canonical_bytes,
            "metadata_unqualified",
        )
        runtime.authority.assert_held()
        client = runtime.client()
        require(
            type(client) is SalesforceClient
            and type(client._http) is OwnedHTTP
            and client._http.observation_origin == "provider_http"
            and client._config.binding == self.config.connection.binding
            and client._http._before_attempt == runtime._before_read_attempt,
            "metadata_unqualified",
        )
        context.check()
        observation = client.read_metadata()
        context.check()
        return self.evidence.save(
            dict(
                format_version=1,
                type="metadata",
                config_sha256=self.config.binding,
                owner_request_sha256=self.authority.document["owner_request_sha256"],
                authority_sha256=self.authority_file.sha256,
                observation=observation.document,
            )
        )

    def __repr__(self):
        return "ReadSessionGate(<pinned private read session>)"
