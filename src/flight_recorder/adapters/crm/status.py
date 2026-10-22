"""Credential-free status derived only from retained facts, never an active health check."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from flight_recorder.adapters.crm.contracts import ConfirmedConnection
from flight_recorder.collector.crm_provenance import _original_capture
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
        write_mode="disabled",
        atomic_source_guard=False,
        reason=reason,
        confirmation_at=confirmed.confirmed_at.isoformat() if confirmed else None,
        confirmation_origin=SOURCE_LABELS[confirmed.observation_origin] if confirmed else None,
        observation_origin=SOURCE_LABELS[latest["observation_origin"]] if latest else None,
    )
