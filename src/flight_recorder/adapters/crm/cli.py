"""Explicit empty-file initialization; never reset or configure external systems."""

import hashlib
import os

from sqlalchemy import event

from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.adapters.crm.runtime_config import CRMRuntimeConfig, absolute_path
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_bytes
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


def n8n_poll_workflow():
    """The only reviewed export; no runtime import or activation is performed."""
    return {
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
