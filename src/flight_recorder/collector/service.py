"""Collector ingestion: validate, normalize, canonicalize, and persist atomically.

Order of operations for one envelope (INV-11):

1. strict validation of the raw JSON body;
2. timestamp normalization to UTC (done by the schema's `Timestamp` type);
3. canonical serialization of the complete validated envelope;
4. idempotency hash from those canonical bytes;
5. account rules and cross-event reference validation, neither of which writes;
6. persistence of payload and metadata in that same canonical representation,
   plus the domain projections of `PRODUCT.md` §6, all inside one transaction.

Steps 5 and 6 are ordered so that every check runs before the first write: a
rejected or conflicting envelope leaves no event row and no projection row.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.errors import CollectorError, ConflictError, RejectedError
from flight_recorder.collector.projections import project, validate_references
from flight_recorder.collector.schema import (
    AccountDiscoveredEnvelope,
    AnyEnvelope,
    LogicArtifactRegisteredEnvelope,
    validate_envelope_json,
)
from flight_recorder.ledger.schema import (
    SYSTEM_ACCOUNT_DOMAIN,
    SYSTEM_ACCOUNT_NAME,
    SYSTEM_ACCOUNT_REF,
    accounts,
    events,
)

__all__ = [
    "Collector",
    "CollectorError",
    "ConflictError",
    "IngestResult",
    "RejectedError",
]


@dataclass(frozen=True)
class IngestResult:
    status: str  # "created" | "duplicate"
    event_id: str
    canonical_hash: str
    http_status: int

    def body(self) -> dict[str, str]:
        return {
            "status": self.status,
            "event_id": self.event_id,
            "canonical_hash": self.canonical_hash,
        }


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


class Collector:
    """Owns the ingest path. One instance per application/engine."""

    def __init__(self, engine: Engine):
        self.engine = engine
        # Test seam: called inside the transaction after all writes and before
        # commit. Tests inject a raising callable to prove atomic rollback.
        self.before_commit: Callable[[AnyEnvelope], None] | None = None

    def ingest_json(self, body: bytes | str) -> IngestResult:
        try:
            envelope = validate_envelope_json(body)
        except ValidationError as exc:
            raise RejectedError(
                {
                    "status": "rejected",
                    "reason": "invalid_envelope",
                    "errors": _validation_errors(exc),
                }
            ) from exc
        return self.ingest(envelope)

    def ingest(self, envelope: AnyEnvelope) -> IngestResult:
        normalized = envelope.model_dump(mode="json")
        digest = canonical_hash(normalized)

        with self.engine.begin() as conn:
            existing = conn.execute(
                select(events.c.canonical_hash).where(events.c.event_id == envelope.event_id)
            ).first()
            if existing is not None:
                if existing.canonical_hash == digest:
                    return IngestResult("duplicate", envelope.event_id, digest, 200)
                raise ConflictError(
                    {
                        "status": "conflict",
                        "reason": "event_id_reused_with_different_content",
                        "event_id": envelope.event_id,
                        "stored_hash": existing.canonical_hash,
                        "submitted_hash": digest,
                    }
                )

            self._check_account_rules(conn, envelope)
            validate_references(conn, envelope, normalized)

            self._materialize_account(conn, envelope)
            inserted = conn.execute(
                events.insert().values(
                    event_id=envelope.event_id,
                    schema_version=normalized["schema_version"],
                    event_type=normalized["event_type"],
                    source=normalized["source"],
                    account_ref=normalized["account_ref"],
                    occurred_at=normalized["occurred_at"],
                    recorded_at=normalized["recorded_at"],
                    canonical_hash=digest,
                    payload=canonical_text(normalized["payload"]),
                )
            )
            project(conn, normalized, inserted.inserted_primary_key[0])
            if self.before_commit is not None:
                self.before_commit(envelope)

        return IngestResult("created", envelope.event_id, digest, 201)

    @staticmethod
    def _account_row(conn, account_ref: str):
        return conn.execute(
            select(accounts.c.name, accounts.c.domain).where(accounts.c.account_ref == account_ref)
        ).first()

    @classmethod
    def _check_account_rules(cls, conn, envelope: AnyEnvelope) -> None:
        """Account existence and identity. Writes nothing."""
        row = cls._account_row(conn, envelope.account_ref)

        if isinstance(envelope, LogicArtifactRegisteredEnvelope):
            # The reserved `_system` principal needs no discovery event; the
            # envelope schema already guarantees this is the only type using it.
            return

        if isinstance(envelope, AccountDiscoveredEnvelope):
            if row is not None and (row.name, row.domain) != (
                envelope.payload.name,
                envelope.payload.domain,
            ):
                raise ConflictError(
                    {
                        "status": "conflict",
                        "reason": "account_identity_mismatch",
                        "account_ref": envelope.account_ref,
                        "stored": {"name": row.name, "domain": row.domain},
                        "submitted": {
                            "name": envelope.payload.name,
                            "domain": envelope.payload.domain,
                        },
                    }
                )
            return

        if row is None:
            raise RejectedError(
                {
                    "status": "rejected",
                    "reason": "unknown_account_ref",
                    "account_ref": envelope.account_ref,
                    "detail": "the trace must begin with an account.discovered event",
                }
            )

    @classmethod
    def _materialize_account(cls, conn, envelope: AnyEnvelope) -> None:
        """Create the account row a discovery or a first registration implies."""
        if isinstance(envelope, LogicArtifactRegisteredEnvelope):
            if cls._account_row(conn, SYSTEM_ACCOUNT_REF) is None:
                conn.execute(
                    accounts.insert().values(
                        account_ref=SYSTEM_ACCOUNT_REF,
                        name=SYSTEM_ACCOUNT_NAME,
                        domain=SYSTEM_ACCOUNT_DOMAIN,
                        first_seen_event_id=envelope.event_id,
                    )
                )
            return

        if isinstance(envelope, AccountDiscoveredEnvelope):
            if cls._account_row(conn, envelope.account_ref) is None:
                conn.execute(
                    accounts.insert().values(
                        account_ref=envelope.account_ref,
                        name=envelope.payload.name,
                        domain=envelope.payload.domain,
                        first_seen_event_id=envelope.event_id,
                    )
                )
