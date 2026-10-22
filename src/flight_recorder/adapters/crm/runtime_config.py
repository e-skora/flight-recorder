"""Closed, credential-free runtime configuration pinned into durable poll receipts."""

import re
from dataclasses import dataclass
from pathlib import Path

from flight_recorder.adapters.crm.contracts import ConnectionConfig, CRMRefused, CRMTarget, require
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.adapters.crm.submission_contracts import normalize
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash, canonical_text
from flight_recorder.replay.reconstruct import ArtifactRow, verify_artifact_row


def absolute_path(value):
    require(type(value) is str and value and "\x00" not in value, "invalid_runtime_config")
    p = Path(value)
    require(p.is_absolute() and str(p) == value and ".." not in p.parts, "invalid_runtime_config")
    require(not any(q.is_symlink() for q in (p, *p.parents)), "runtime_path_unsafe")
    return p


@dataclass(frozen=True, repr=False)
class CRMRuntimeConfig:
    canonical_bytes: bytes

    def __post_init__(self):
        try:
            require(
                type(self.canonical_bytes) is bytes and len(self.canonical_bytes) <= 1024 * 1024,
                "invalid_runtime_config",
            )
            d = strict_json(self.canonical_bytes)
            require(
                type(d) is dict
                and set(d)
                == {
                    "format_version",
                    "connection",
                    "domain_path",
                    "operational_path",
                    "credential_directory",
                    "ingress_secret_file",
                    "ingress_origin",
                    "selection_identity",
                    "discoveries",
                    "artifact_registration",
                },
                "invalid_runtime_config",
            )
            require(
                type(d["format_version"]) is int
                and d["format_version"] == 1
                and canonical_bytes(d) == self.canonical_bytes,
                "invalid_runtime_config",
            )
            cfg = self.connection
            require(d["ingress_origin"] == "http://localhost:8768", "invalid_runtime_config")
            selection = d["selection_identity"]
            require(
                type(selection) is str
                and 0 < len(selection) <= 256
                and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in selection),
                "invalid_runtime_config",
            )
            selection.encode("utf-8")
            paths = {
                key: absolute_path(d[key])
                for key in [
                    "domain_path",
                    "operational_path",
                    "credential_directory",
                    "ingress_secret_file",
                ]
            }
            require(
                paths["domain_path"] != paths["operational_path"]
                and paths["domain_path"].parent.is_dir()
                and paths["operational_path"].parent.is_dir(),
                "invalid_runtime_config",
            )
            require(
                paths["credential_directory"].is_dir() and paths["ingress_secret_file"].is_file(),
                "runtime_private_files_missing",
            )
            require(
                len(set(paths.values())) == 4
                and paths["ingress_secret_file"].parent == paths["credential_directory"],
                "invalid_runtime_config",
            )
            lock_path = Path(str(paths["operational_path"]) + ".runtime-lock")
            require(lock_path not in paths.values(), "invalid_runtime_config")
            families = [
                {Path(str(path) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
                for path in (paths["domain_path"], paths["operational_path"], lock_path)
            ]
            private = paths["credential_directory"]
            families += [
                {paths["ingress_secret_file"]},
                {private},
                {private / ".lock"},
                {private / (canonical_hash({"connection_id": cfg.connection_id}) + ".json")},
                {private / (cfg.credential_reference + ".client-secret")},
            ]
            seen = set()
            for family in families:
                require(not seen.intersection(family), "invalid_runtime_config")
                seen.update(family)
            require(
                not any(
                    path.parent == private and re.fullmatch(r"\.[0-9a-f]{32}\.tmp", path.name)
                    for path in seen
                ),
                "invalid_runtime_config",
            )
            discoveries = d["discoveries"]
            require(
                type(discoveries) is dict and set(discoveries) == set(cfg.record_ids),
                "invalid_runtime_config",
            )
            for target in self.targets:
                envelope = normalize(canonical_bytes(discoveries[target.record_id]))
                require(
                    envelope == discoveries[target.record_id]
                    and envelope["schema_version"] == "1"
                    and envelope["event_type"] == "account.discovered"
                    and envelope["source"] == "salesforce-test-account"
                    and envelope["account_ref"] == target.account_ref,
                    "invalid_runtime_discovery",
                )
            registration = normalize(canonical_bytes(d["artifact_registration"]))
            require(
                registration == d["artifact_registration"]
                and registration["schema_version"] == "1"
                and registration["event_type"] == "logic_artifact.registered",
                "invalid_runtime_artifact",
            )
            artifact = registration["payload"]["artifact"]
            require(canonical_hash(artifact) == cfg.artifact_hash, "invalid_runtime_artifact")
            row = ArtifactRow(
                artifact_hash=canonical_hash(artifact),
                artifact_json=canonical_text(artifact),
                **{
                    k: artifact[k]
                    for k in [
                        "artifact_id",
                        "artifact_schema_version",
                        "logic_version",
                        "decision_class",
                        "evaluator_version",
                    ]
                },
            )
            verified = verify_artifact_row(row, cfg.artifact_hash).artifact
            require(
                verified.evaluator_version == cfg.evaluator_version
                and verified.decision_class == "account_prioritization",
                "invalid_runtime_artifact",
            )
        except CRMRefused:
            raise
        except Exception:
            raise CRMRefused("invalid_runtime_config") from None

    @classmethod
    def from_document(cls, document):
        try:
            d = strict_json(canonical_bytes(document))
            d["artifact_registration"] = normalize(canonical_bytes(d["artifact_registration"]))
            d["discoveries"] = {
                key: normalize(canonical_bytes(value)) for key, value in d["discoveries"].items()
            }
            return cls(canonical_bytes(d))
        except CRMRefused:
            raise
        except Exception:
            raise CRMRefused("invalid_runtime_config") from None

    @property
    def document(self):
        return strict_json(self.canonical_bytes)

    @property
    def connection(self):
        d = self.document["connection"]
        require(type(d) is dict and type(d.get("record_ids")) is list, "invalid_runtime_config")
        d["record_ids"] = tuple(d["record_ids"])
        return ConnectionConfig(**d)

    @property
    def targets(self):
        cfg = self.connection
        return tuple(
            CRMTarget(cfg.provider, cfg.expected_instance_id, "Account", record)
            for record in cfg.record_ids
        )

    @property
    def binding(self):
        return canonical_hash(self.document)

    @property
    def domain_path(self):
        return Path(self.document["domain_path"])

    @property
    def operational_path(self):
        return Path(self.document["operational_path"])

    def __repr__(self):
        return "CRMRuntimeConfig(<pinned local references>)"


def load_config(path):
    try:
        p = absolute_path(str(path))
        require(p.is_file() and p.stat().st_size <= 1024 * 1024, "invalid_runtime_config")
        return CRMRuntimeConfig.from_document(strict_json(p.read_bytes()))
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("invalid_runtime_config") from None
