"""Locate and load the canonical fixture files.

`fixtures/canonical/` is the single source of shared demo constants (D-004,
D-010). Code and tests load these files; nothing re-declares their values.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DIR = REPO_ROOT / "fixtures" / "canonical"
EXAMPLES_DIR = REPO_ROOT / "fixtures" / "examples"


def canonical_envelope_paths() -> list[Path]:
    """Envelope files in file (chronological) order."""
    return sorted(p for p in CANONICAL_DIR.glob("*.json") if p.name[0].isdigit())


def logic_artifact_path(logic_version: str) -> Path:
    return CANONICAL_DIR / f"logic-{logic_version}.json"


def load_json(path: Path) -> dict:
    with path.open("rb") as handle:
        return json.load(handle)
