"""Canonical JSON serialization and hashing.

The canonical form is what defines event identity (INV-11) and logic-artifact
identity (INV-05): sorted keys, no insignificant whitespace, UTF-8, SHA-256.

This module is deliberately schema-independent. Which values are admissible
(for example, that schema v1 has no float fields) is decided by the schema
layer, not here.
"""

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Serialize a JSON-compatible object to its canonical UTF-8 byte form."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(obj: Any) -> str:
    """Canonical form as text, for storage in text columns."""
    return canonical_bytes(obj).decode("utf-8")


def canonical_hash(obj: Any) -> str:
    """SHA-256 hex digest of the canonical byte form."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
