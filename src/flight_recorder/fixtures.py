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


def canonical_account() -> tuple[str, str]:
    """`(account_ref, name)` of the canonical account, from its `account.discovered` envelope."""
    envelope = next(
        envelope
        for envelope in map(load_json, canonical_envelope_paths())
        if envelope["event_type"] == "account.discovered"
    )
    return envelope["account_ref"], envelope["payload"]["name"]


# --- The seeded dataset (D-014 Q4) --------------------------------------------------
#
# `fixtures/dataset/` holds the generator config and the planted-effects
# manifest. The analytics engine never calls these helpers: callers load the
# manifest's descriptive inputs here and pass them in explicitly.

DATASET_DIR = REPO_ROOT / "fixtures" / "dataset"


def dataset_config_path() -> Path:
    return DATASET_DIR / "config.json"


def planted_effects_path() -> Path:
    return DATASET_DIR / "planted-effects.json"


def dataset_config_mapping() -> dict:
    """The shipped generator config as plain data."""
    return load_json(dataset_config_path())


def dataset_config():
    """The shipped generator config, validated."""
    from flight_recorder.dataset.generator import DatasetConfig

    return DatasetConfig.from_mapping(dataset_config_mapping())


def canonical_artifacts() -> dict:
    """Every canonical logic artifact, by logic version, through the strict model."""
    from flight_recorder.collector.schema import LogicArtifact

    artifacts = [
        LogicArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in sorted(CANONICAL_DIR.glob("logic-*.json"))
    ]
    return {artifact.logic_version: artifact for artifact in artifacts}


def planted_effects() -> dict:
    """The planted-effects manifest as plain data."""
    return load_json(planted_effects_path())


def dataset_signals() -> tuple:
    """The manifest's signal definitions, in manifest order, for `insights`."""
    from flight_recorder.analytics.insights import SignalDefinition

    return tuple(
        SignalDefinition(
            id=effect["id"],
            kind=effect["cohort"]["kind"],
            input_key=effect["cohort"]["input_key"],
            rule=effect["cohort"].get("rule"),
            equals=effect["cohort"].get("equals"),
        )
        for effect in planted_effects()["effects"]
        if effect["cohort"]["kind"] in ("rule", "context_value")
    )


def dataset_comparison_workflow_version() -> str:
    """The manifest's comparison workflow cohort, for `insights`."""
    return planted_effects()["comparison_workflow_version"]
