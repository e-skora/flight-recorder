"""Credential-free status derived only from retained facts, never an active health check."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.crm.contracts import ConfirmedConnection, CRMRefused
from flight_recorder.collector.crm_provenance import _original_capture
from flight_recorder.collector.errors import CollectorError
from flight_recorder.ledger.crm_schema import crm_captures

SOURCE_LABELS = {
    "fixture": "Fictional fixture",
    "injected_transport": "Injected transport test",
    "provider_http": "Recorded provider HTTP observation",
}


@dataclass(frozen=True)
class ConnectionStatus:
    provider: str
    instance_id: str
    auth_state: str
    last_observed_at: str | None
    write_mode: str
    atomic_source_guard: bool
    reason: str
    confirmation_at: str | None = None
    confirmation_origin: str | None = None
    observation_origin: str | None = None
    policy_generation: int | None = 0
    policy_enabled: bool | None = False
    writer_reason: str = ""
    writer_operations: tuple | None = ()


def writer_status(runtime):
    disabled = dict(
        write_mode="disabled",
        policy_generation=0,
        policy_enabled=False,
        writer_reason="not_writer_capable",
        writer_operations=(),
    )
    if not getattr(runtime, "writer_capable", False):
        return disabled
    if not runtime._execution.acquire(blocking=False):
        return dict(
            disabled,
            write_mode="check_in_progress",
            writer_reason="writer_busy",
            policy_generation=None,
            policy_enabled=None,
            writer_operations=None,
        )
    try:
        snap = runtime.validate_write_state()
        documents = [
            d
            for (connection, _), d in snap["policies"].items()
            if connection == runtime.config.connection.connection_id
        ]
        policy = documents[-1]["policy"] if documents else None
        operations = []
        for op, intent in sorted(snap["intents"].items()):
            progress = runtime._writer_store.progress(op, snap)
            origin = min(snap["boxes"][op], key=lambda d: d["payload"]["observation_sequence"])[
                "payload"
            ]["observation_origin"]
            operations.append(
                dict(
                    operation_id=op,
                    decision_event_id=intent.document["decision_event_id"],
                    state=progress.state,
                    reason=progress.reason,
                    admitted=progress.admitted,
                    attempted=progress.attempted,
                    held=progress.held,
                    observation_origin=origin,
                )
            )
        mode = "enabled_unqualified" if policy and policy["enabled"] else "disabled"
        if any(o["held"] and o["state"] == "unknown" for o in operations):
            mode = "uncertainty_hold"
        elif any(o["held"] for o in operations):
            mode = "observation_pending"
        return dict(
            write_mode=mode,
            policy_generation=policy["generation"] if policy else 0,
            policy_enabled=policy["enabled"] if policy else False,
            writer_reason="live_readiness_unqualified",
            writer_operations=tuple(operations),
        )
    except (CRMRefused, CollectorError, SQLAlchemyError) as exc:
        return dict(
            disabled,
            write_mode="integrity_hold",
            writer_reason=getattr(exc, "reason", "write_history_integrity"),
            policy_generation=None,
            policy_enabled=None,
            writer_operations=None,
        )
    finally:
        runtime._execution.release()


def saved_confirmation(runtime):
    value = getattr(runtime, "_confirmed_connection", None)
    cfg = runtime.config.connection
    if (
        type(value) is not ConfirmedConnection
        or (
            value.connection_id,
            value.provider,
            value.instance_id,
            value.user_id,
            value.api_version,
        )
        != (
            cfg.connection_id,
            cfg.provider,
            cfg.expected_instance_id,
            cfg.expected_user_id,
            cfg.api_version,
        )
        or type(value.confirmed_at) is not datetime
        or value.confirmed_at.utcoffset() is None
        or value.observation_origin not in ("injected_transport", "provider_http")
    ):
        return None
    return value


def connection_status(runtime):
    confirmed = saved_confirmation(runtime)
    latest = None
    reason = runtime.background_reason or "identity_not_checked_in_this_process"
    try:
        with runtime.collector.engine.connect() as conn:
            rows = conn.execute(
                select(crm_captures.c.capture_hash).where(
                    crm_captures.c.account_ref.in_([t.account_ref for t in runtime.config.targets])
                )
            ).all()
            for row in rows:
                cap = _original_capture(conn, row.capture_hash)
                if cap["connection_id"] != runtime.config.connection.connection_id:
                    raise ValueError("connection mismatch")
                if latest is None or cap["read_finished_at"] > latest["read_finished_at"]:
                    latest = cap
    except (ValueError, KeyError, TypeError, SQLAlchemyError):
        latest = None
        reason = "stored_observation_integrity_failure"
    if (
        confirmed is not None
        and not runtime.background_reason
        and reason != "stored_observation_integrity_failure"
    ):
        reason = "last_identity_confirmation_is_not_current_health"
    return ConnectionStatus(
        provider=runtime.config.connection.provider,
        instance_id=runtime.config.connection.expected_instance_id,
        auth_state="identity_last_confirmed" if confirmed is not None else "not_checked",
        last_observed_at=latest["read_finished_at"] if latest else None,
        atomic_source_guard=False,
        reason=reason,
        confirmation_at=confirmed.confirmed_at.isoformat() if confirmed else None,
        confirmation_origin=SOURCE_LABELS[confirmed.observation_origin] if confirmed else None,
        observation_origin=SOURCE_LABELS[latest["observation_origin"]] if latest else None,
        **writer_status(runtime),
    )
