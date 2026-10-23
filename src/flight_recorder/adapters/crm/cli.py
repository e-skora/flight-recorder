"""Explicit empty-file initialization; never reset or configure external systems."""

import hashlib
import json
import os
import re
from dataclasses import dataclass

from sqlalchemy import event

from flight_recorder.adapters.crm.contracts import (
    CRMRefused,
    observation_time,
    require,
    validate_unit_id,
)
from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig, absolute_path
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.ledger.crm_schema import create_crm_schema
from flight_recorder.ledger.database import make_engine
from flight_recorder.ledger.schema import create_schema


def initialize(config, *, writer_capable=False):
    require(type(writer_capable) is bool, "invalid_writer_mode")
    require(type(config) is CRMRuntimeConfig, "invalid_runtime_config")
    config.__post_init__()
    lock_path = config.operational_path.with_name(config.operational_path.name + ".runtime-lock")
    paths = (config.domain_path, config.operational_path, lock_path)
    for path in paths:
        absolute_path(str(path))
        require(
            not any(
                path.with_name(path.name + suffix).exists()
                for suffix in ("", "-wal", "-shm", "-journal")
            ),
            "crm_init_requires_new_files",
        )
    engine = store = domain_fd = None
    try:
        domain_fd = os.open(
            config.domain_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )

        def check_domain():
            absolute_path(str(config.domain_path))
            original = os.fstat(domain_fd)
            current = os.stat(config.domain_path, follow_symlinks=False)
            require(
                (original.st_dev, original.st_ino) == (current.st_dev, current.st_ino)
                and original.st_nlink == current.st_nlink == 1,
                "crm_init_incomplete",
            )

        store = CRMStore(
            config.operational_path, format_version=5 if writer_capable else 4, exclusive=True
        )
        engine = make_engine(config.domain_path)

        @event.listens_for(engine, "do_connect")
        def existing_reserved_domain(dialect, connection_record, cargs, cparams):
            check_domain()
            cargs[0] = config.domain_path.as_uri() + "?mode=rw"
            cparams["uri"] = True

        @event.listens_for(engine, "connect", insert=True)
        def validate_open_domain(connection, connection_record):
            check_domain()

        create_schema(engine)
        create_crm_schema(engine)
        if writer_capable:
            from flight_recorder.ledger.crm_operations import create_crm_operation_schema

            create_crm_operation_schema(engine)
        from flight_recorder.adapters.crm.runtime import qualify_domain

        qualify_domain(engine)
        check_domain()
        store._check_created_file()
        with lock_path.open("xb") as lock:
            os.fchmod(lock.fileno(), 0o600)
            op_info, lock_info = config.operational_path.stat(), os.fstat(lock.fileno())
            document = dict(
                format_version=1,
                operational_path=str(config.operational_path),
                operational_device=op_info.st_dev,
                operational_inode=op_info.st_ino,
                lock_path=str(lock_path),
                lock_device=lock_info.st_dev,
                lock_inode=lock_info.st_ino,
            )
            body = canonical_bytes(document)
            lock.write(body)
            lock.flush()
            os.fsync(lock.fileno())
        with store.transaction() as conn:
            check_domain()
            conn.execute(
                "INSERT INTO crm_runtime_binding VALUES (1,?,?)",
                (body, hashlib.sha256(body).hexdigest()),
            )
        parent_fd = os.open(lock_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        check_domain()
        store._check_created_file()
    except Exception:
        raise CRMRefused("crm_init_incomplete") from None
    finally:
        if store is not None:
            store.close()
        if engine is not None:
            engine.dispose()
        if domain_fd is not None:
            os.close(domain_fd)
    return "initialized"


def n8n_poll_workflow(*, mode="local_loopback"):
    """The only reviewed export; no runtime import or activation is performed."""
    require(
        type(mode) is str and mode in {"local_loopback", "docker_host"},
        "invalid_n8n_mode",
    )
    workflow = {
        "name": "Flight Recorder Salesforce polling (inactive)",
        "active": False,
        "nodes": [
            {
                "id": "fcaf7d3a-ccde-4e65-89e1-adbc60c74968",
                "name": "Every minute",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.3,
                "position": [0, 0],
                "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
            },
            {
                "id": "f690bfae-5abc-4de7-a630-37be6a4d1766",
                "name": "Queue observed poll",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [240, 0],
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 1000,
                "credentials": {
                    "httpHeaderAuth": {
                        "id": "REVIEWED_LOCAL_INGRESS_CREDENTIAL",
                        "name": "Flight Recorder local ingress",
                    }
                },
                "parameters": {
                    "method": "POST",
                    "url": "http://localhost:8768/api/internal/crm/poll",
                    "authentication": "genericCredentialType",
                    "genericAuthType": "httpHeaderAuth",
                    "sendBody": True,
                    "contentType": "json",
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ { connection_id: 'REVIEWED_CONNECTION_ID', tick_id: $wor"
                        "kflow.id + ':' + $execution.id } }}"
                    ),
                    "options": {
                        "timeout": 30000,
                        "redirect": {"redirect": {"followRedirects": False}},
                    },
                },
            },
        ],
        "connections": {
            "Every minute": {
                "main": [[{"node": "Queue observed poll", "type": "main", "index": 0}]]
            }
        },
        "settings": {"executionOrder": "v1", "timezone": "UTC"},
    }

    if mode == "docker_host":
        workflow["name"] = (
            "Flight Recorder Salesforce polling through Docker host (proposed inactive)"
        )
        request = workflow["nodes"][1]["parameters"]
        request["url"] = "http://host.docker.internal:8768/api/internal/crm/poll"
        request["sendHeaders"] = True
        request["headerParameters"] = {"parameters": [{"name": "Host", "value": "localhost:8768"}]}
    return workflow


def n8n_qualification_workflow(*, mode="local_loopback"):
    """An inactive manual check, never a scheduled poll or provider request."""
    import json

    workflow = n8n_poll_workflow(mode=mode)
    workflow["name"] = "Flight Recorder local connection check (inactive)"
    workflow["nodes"][0] = {
        "id": "8fdc2130-2d79-4e38-a1ec-664c2da37f00",
        "name": "Start connection check",
        "type": "n8n-nodes-base.manualTrigger",
        "typeVersion": 1,
        "position": [0, 0],
        "parameters": {},
    }
    node = workflow["nodes"][1]
    node["id"] = "d5c0c0d7-4088-4b7e-a151-977e4e110fbf"
    node["name"] = "Check local Flight Recorder link"
    host = "localhost" if mode == "local_loopback" else "host.docker.internal"
    node["parameters"]["url"] = f"http://{host}:8768/api/internal/crm/network-check"
    node["parameters"]["jsonBody"] = json.dumps(
        dict(format_version=1, connection_id="REVIEWED_CONNECTION_ID", probe_id="0" * 64),
        separators=(",", ":"),
    )
    workflow["connections"] = {
        "Start connection check": {
            "main": [[{"node": "Check local Flight Recorder link", "type": "main", "index": 0}]]
        }
    }
    return workflow


@dataclass(frozen=True, repr=False)
class N8nBinding:
    """Closed nonsecret rendering inputs, never installation or execution authority."""

    canonical_bytes: bytes
    config: CRMRuntimeConfig

    def __post_init__(self):
        try:
            require(type(self.config) is CRMRuntimeConfig, "invalid_n8n_binding")
            self.config.__post_init__()
            d = self.document
            require(
                type(d) is dict
                and set(d)
                == {
                    "format_version",
                    "type",
                    "owner_request_sha256",
                    "config_sha256",
                    "connection_id",
                    "mode",
                    "credential_id",
                    "credential_name",
                    "credential_type",
                    "issued_at",
                    "expires_at",
                    "poll_template_sha256",
                    "qualification_template_sha256",
                    "probe_id",
                }
                and type(d["format_version"]) is int
                and d["format_version"] == 1
                and d["type"] == "n8n_binding"
                and canonical_bytes(d) == self.canonical_bytes,
                "invalid_n8n_binding",
            )
            for key in (
                "owner_request_sha256",
                "config_sha256",
                "poll_template_sha256",
                "qualification_template_sha256",
                "probe_id",
            ):
                validate_unit_id(d[key])
            require(
                d["config_sha256"] == self.config.binding
                and d["connection_id"] == self.config.connection.connection_id
                and d["connection_id"] != "REVIEWED_CONNECTION_ID"
                and type(d["credential_id"]) is str
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", d["credential_id"], re.ASCII))
                and d["credential_id"] != "REVIEWED_LOCAL_INGRESS_CREDENTIAL"
                and d["credential_name"] == "Flight Recorder local ingress"
                and d["credential_type"] == "httpHeaderAuth"
                and d["probe_id"] != "0" * 64
                and observation_time(d["issued_at"]) < observation_time(d["expires_at"]),
                "invalid_n8n_binding",
            )
            require(
                d["poll_template_sha256"] == canonical_hash(n8n_poll_workflow(mode=d["mode"]))
                and d["qualification_template_sha256"]
                == canonical_hash(n8n_qualification_workflow(mode=d["mode"])),
                "invalid_n8n_binding",
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CRMRefused("invalid_n8n_binding") from None

    @classmethod
    def from_document(cls, document, config):
        try:
            require(type(document) is dict, "invalid_n8n_binding")
            return cls(canonical_bytes(document), config)
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_n8n_binding") from None

    @property
    def document(self):
        from flight_recorder.adapters.crm.http import strict_json

        return strict_json(self.canonical_bytes)

    def __repr__(self):
        return "N8nBinding(<nonsecret references; no execution authority>)"


def bound_n8n_workflow(config, binding, *, kind="poll"):
    require(type(binding) is N8nBinding and type(config) is CRMRuntimeConfig, "invalid_n8n_binding")
    binding.__post_init__()
    require(binding.config.canonical_bytes == config.canonical_bytes, "invalid_n8n_binding")
    require(type(kind) is str and kind in {"poll", "qualification"}, "unreviewed_n8n_workflow")
    d = binding.document
    workflow = (
        n8n_poll_workflow(mode=d["mode"])
        if kind == "poll"
        else n8n_qualification_workflow(mode=d["mode"])
    )
    node = workflow["nodes"][1]
    node["credentials"]["httpHeaderAuth"]["id"] = d["credential_id"]
    if kind == "poll":
        # A JSON string literal is also a safe JavaScript literal, including U+2028/2029.
        literal = json.dumps(config.connection.connection_id, ensure_ascii=True)
        node["parameters"]["jsonBody"] = (
            "={{ { connection_id: " + literal + ", tick_id: $workflow.id + ':' + $execution.id } }}"
        )
    else:
        node["parameters"]["jsonBody"] = json.dumps(
            dict(
                format_version=1,
                connection_id=config.connection.connection_id,
                probe_id=d["probe_id"],
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return workflow


def validate_bound_n8n_export(document, config, binding, *, kind="poll"):
    try:
        expected = bound_n8n_workflow(config, binding, kind=kind)
        require(
            type(document) is dict and canonical_bytes(document) == canonical_bytes(expected),
            "unreviewed_n8n_workflow",
        )
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("unreviewed_n8n_workflow") from None
    return True


def validate_n8n_export(document):
    """Pin the reviewed inactive artifact, refusing added nodes or changed requests."""
    from flight_recorder.collector.canonical import canonical_bytes

    try:
        require(
            type(document) is dict
            and canonical_bytes(document) == canonical_bytes(n8n_poll_workflow()),
            "unreviewed_n8n_workflow",
        )
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("unreviewed_n8n_workflow") from None
    return True


def command(operation, config_path):
    import sys

    from flight_recorder.adapters.crm.runtime_config import load_config

    try:
        config = load_config(config_path)
        if operation in ("crm-init", "crm-writer-init"):
            initialize(config, writer_capable=operation == "crm-writer-init")
            print(f"{operation}: initialized new local files")
        elif operation == "crm-write-status":
            import json
            from dataclasses import asdict

            from flight_recorder.adapters.crm.runtime import CRMRuntime
            from flight_recorder.adapters.crm.status import connection_status

            runtime = CRMRuntime(config, writer_capable=True)
            try:
                from sqlalchemy.exc import SQLAlchemyError

                from flight_recorder.collector.errors import CollectorError

                try:
                    runtime.start(background=False)
                    print(json.dumps(asdict(connection_status(runtime)), sort_keys=True))
                except (CollectorError, SQLAlchemyError):
                    raise CRMRefused("write_status_history_integrity") from None
            finally:
                runtime.close()
        else:
            require(operation in ("crm-serve", "crm-writer-serve"), "unknown_crm_command")
            import uvicorn

            from flight_recorder.crm_app import create_crm_app

            uvicorn.run(
                create_crm_app(config, writer_capable=operation == "crm-writer-serve"),
                host="localhost",
                port=8768,
                workers=1,
                access_log=False,
            )
        return 0
    except CRMRefused as exc:
        print(f"{operation}: {exc.reason}", file=sys.stderr)
        return 1


def read_command(
    operation, config_path, readiness_path, authority_path, evidence_directory, *, clock=None
):
    import sys
    from datetime import UTC, datetime

    from flight_recorder.adapters.crm.read_control import ReadSessionGate

    try:
        require(operation in {"crm-read-check", "crm-read-serve"}, "invalid_crm_arguments")
        gate = ReadSessionGate(
            config_path,
            readiness_path,
            authority_path,
            evidence_directory,
            clock=clock if clock is not None else lambda: datetime.now(UTC),
        )
        if operation == "crm-read-check":
            gate.check()
            print(
                json.dumps(
                    dict(
                        state="read_session_checked",
                        action=gate.authority.document["action"],
                        ready_for_write=False,
                    ),
                    sort_keys=True,
                )
            )
        else:
            import uvicorn

            from flight_recorder.crm_app import create_crm_read_app

            uvicorn.run(
                create_crm_read_app(gate), host="127.0.0.1", port=8768, workers=1, access_log=False
            )
        return 0
    except CRMRefused:
        print("crm-read: refused", file=sys.stderr)
        return 1


def network_check_command(config_path, qualification_path, evidence_directory, *, clock=None):
    import sys

    try:
        import uvicorn

        from flight_recorder.adapters.crm.read_control import PinnedReadFile
        from flight_recorder.crm_app import create_crm_network_check_app

        config = CRMRuntimeConfig(PinnedReadFile(config_path).body)
        app = create_crm_network_check_app(
            config, qualification_path, evidence_directory, clock=clock
        )
        uvicorn.run(app, host="127.0.0.1", port=8768, workers=1, access_log=False)
        return 0
    except CRMRefused:
        print("crm-network-check: refused", file=sys.stderr)
        return 1
