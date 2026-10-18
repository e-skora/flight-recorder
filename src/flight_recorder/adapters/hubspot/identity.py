"""Semantic identity of a source notification (S02).

The vendor `eventId` is one ingredient, never the identity. The logical key is a
SHA-256 over the canonical JSON of the normalized namespace and semantic body;
delivery-attempt metadata is excluded, and JSON types and missing/null
differences are preserved by canonical serialization. The key is an adapter
equivalence policy, not a vendor uniqueness guarantee: the store compares the
semantic bytes behind an existing key before treating a receipt as a retry, so a
digest collision is refused rather than merged.
"""

from flight_recorder.collector.canonical import canonical_hash


def logical_key(notification) -> str:
    """Logical source-event key of a `SourceNotification`."""
    return canonical_hash(notification.semantic_body())
