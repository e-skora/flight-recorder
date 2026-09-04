"""Collector failure types.

They live in their own module so the ingest path's validation and projection
steps can raise them without importing the service that calls them.
"""

from typing import Any


class CollectorError(Exception):
    status_code: int = 400

    def __init__(self, body: dict[str, Any]):
        super().__init__(body)
        self.body = body


class RejectedError(CollectorError):
    """Envelope failed validation or an account rule; HTTP 422, nothing written."""

    status_code = 422


class ConflictError(CollectorError):
    """Identity conflict (event_id reuse or account identity mismatch); HTTP 409."""

    status_code = 409
