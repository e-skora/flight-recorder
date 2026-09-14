"""PRODUCT.md §8 in the planted-effects manifest, and the generator's in-memory contract.

Evidence: executed against the shipped `fixtures/dataset/` files and the
generator's output in memory; no database is touched (D-008, D-014 Q4).
"""

import ast
import copy
import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import flight_recorder.dataset
from flight_recorder.analytics.insights import WORKFLOW_UNDER_COMPARISON
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.dataset.generator import DatasetConfig, DatasetConfigError, generate
from flight_recorder.fixtures import (
    canonical_artifacts,
    dataset_config,
    dataset_config_mapping,
    planted_effects,
)
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF
from tests.conftest import ACCOUNT_REF, OUTCOME_EVENT_ID, canonical_by_type, logic_artifact

DATASET_SOURCE = Path(flight_recorder.dataset.__file__).parent


def test_the_manifest_honors_product_section_8():
    manifest = planted_effects()
    config = dataset_config()

    # §8: "A deterministic, seeded synthetic dataset with at least **200** accounts".
    assert manifest["account_minimum"] == 200
    assert config.account_count >= 200  # the shipped config only

    effects = {effect["id"]: effect for effect in manifest["effects"]}
    assert list(effects) == [
        "recently_funded",
        "verified_integration_pressure_high",
        "workflow_v4_2_underperforms",
    ]

    # §8.1: "`recently_funded` appears in at least 50 prioritization decisions with an
    # absolute observed 90-day opportunity-rate difference of no more than 2 percentage points".
    funded = effects["recently_funded"]
    assert funded == {
        "id": "recently_funded",
        "cohort": {
            "kind": "rule",
            "input_key": "funding_event",
            "rule": "funding_event observed within 90 days before the decision boundary",
        },
        "minimum_decisions": 50,
        "direction": "within",
        "max_difference_points": 2,
    }
    # §8.2: "`verified_integration_pressure = HIGH` appears in at least 40 decisions with an
    # observed 90-day opportunity rate at least 10 percentage points higher".
    assert effects["verified_integration_pressure_high"] == {
        "id": "verified_integration_pressure_high",
        "cohort": {
            "kind": "context_value",
            "input_key": "verified_integration_pressure",
            "equals": "HIGH",
        },
        "minimum_decisions": 40,
        "direction": "higher",
        "min_difference_points": 10,
    }
    # §8.3: "workflow `v4.2` appears in at least 40 decisions with an observed 90-day
    # opportunity rate at least 8 percentage points below the documented comparison cohort".
    workflow = effects["workflow_v4_2_underperforms"]
    assert workflow == {
        "id": "workflow_v4_2_underperforms",
        "cohort": {"kind": "workflow", "workflow_version": "v4.2"},
        "minimum_decisions": 40,
        "direction": "lower",
        "min_difference_points": 8,
    }

    assert manifest["comparison_workflow_version"] == config.comparison_workflow_version
    # The workflow the engine compares is the manifest's and the canonical decision's.
    assert (
        workflow["cohort"]["workflow_version"]
        == WORKFLOW_UNDER_COMPARISON
        == canonical_by_type("decision.recorded")["payload"]["workflow_version"]
    )

    demo = manifest["demo_states"]
    assert set(demo["attribution_standings"]) == {
        "direct",
        "inferred",
        "unresolved",
        "awaiting attribution",
    }
    assert set(demo["observation_states"]) == {"open", "closed known", "closed unknown"}
    minima = [
        *demo["attribution_standings"].values(),
        *demo["observation_states"].values(),
        demo["correction_histories"],
        demo["other_period_observations"],
        demo["outcomes_without_action"],
    ]
    assert all(type(minimum) is int and minimum >= 1 for minimum in minima)


def test_generation_is_deterministic_in_memory():
    config = dataset_config()
    artifacts = canonical_artifacts()
    first = generate(config, artifacts=artifacts)
    second = generate(config, artifacts=artifacts)
    assert first.items == second.items
    assert first == second

    other = generate(dataclasses.replace(config, seed=config.seed + 1), artifacts=artifacts)
    assert other.stage_1 != first.stage_1

    assert first.operations[0].outcome_event_id == OUTCOME_EVENT_ID
    stage_1_outcomes = [
        envelope["event_id"]
        for envelope in first.stage_1
        if envelope["event_type"] == "outcome.evaluated"
    ]
    assert [operation.outcome_event_id for operation in first.operations[1:]] == stage_1_outcomes
    assert first.cutoff_1 == len(first.canonical) + len(first.stage_1)
    assert first.scheduled_total == (
        len(first.canonical) + len(first.stage_1) + len(first.operations) + len(first.stage_2)
    )
    assert [item.sequence for item in first.items] == list(range(1, first.scheduled_total + 1))


def test_the_generator_never_emits_reserved_identities():
    schedule = generate(dataset_config(), artifacts=canonical_artifacts())
    generated = [*schedule.stage_1, *schedule.stage_2]

    assert {envelope["account_ref"] for envelope in generated}.isdisjoint(
        {ACCOUNT_REF, SYSTEM_ACCOUNT_REF}
    )
    assert all(envelope["event_type"] != "logic_artifact.registered" for envelope in generated)
    hashes = {canonical_hash(logic_artifact(version)) for version in ("v3.2", "v5.1")}
    decisions = [e for e in generated if e["event_type"] == "decision.recorded"]
    assert decisions
    assert {d["payload"]["logic_artifact"]["artifact_hash"] for d in decisions} <= hashes

    ids = [envelope["event_id"] for envelope in generated]
    assert len(ids) == len(set(ids))
    assert {json.loads(body)["event_id"] for body in schedule.canonical}.isdisjoint(ids)

    # Every envelope precedes anything that references it.
    action_at = {
        (e["account_ref"], e["occurred_at"]): e["event_id"]
        for e in generated
        if e["event_type"] == "action.recorded"
    }
    seen: set[str] = set()
    discovered: set[str] = set()
    minted: set[str] = set()
    for envelope in generated:
        kind, payload, account = (
            envelope["event_type"],
            envelope["payload"],
            envelope["account_ref"],
        )
        if kind == "account.discovered":
            discovered.add(account)
        else:
            assert account in discovered, envelope["event_id"]
        if kind == "evidence.recorded":
            minted.update(item["evidence_version_id"] for item in payload["items"])
        if kind == "decision.recorded":
            referenced = {
                entry["evidence_version_id"]
                for entry in payload["historical_context"]
                if entry["availability"] == "available"
            }
            assert referenced <= minted, envelope["event_id"]
        if kind in ("persona.selected", "action.recorded"):
            assert payload["decision_event_id"] in seen, envelope["event_id"]
        if kind == "outcome.evaluated":
            for claim in (
                "source_action_event_id",
                "source_decision_event_id",
                "supersedes_outcome_event_id",
            ):
                if payload.get(claim) is not None:
                    assert payload[claim] in seen, (envelope["event_id"], claim)
            window_action = action_at.get((account, payload["window_opened_at"]))
            if window_action is not None:
                assert window_action in seen, envelope["event_id"]
        seen.add(envelope["event_id"])


def test_artifact_selection_reads_activation_from_the_artifacts():
    artifacts = canonical_artifacts()
    earlier, later = artifacts["v3.2"].activation, artifacts["v5.1"].activation
    hashes = {version: canonical_hash(logic_artifact(version)) for version in ("v3.2", "v5.1")}

    schedule = generate(dataset_config(), artifacts=artifacts)
    boundaries = []
    for envelope in schedule.stage_1:
        if envelope["event_type"] != "decision.recorded":
            continue
        boundary = datetime.fromisoformat(envelope["payload"]["decision_boundary"])
        artifact_hash = envelope["payload"]["logic_artifact"]["artifact_hash"]
        assert boundary >= earlier.activated_at
        if boundary < earlier.deactivated_at:
            assert artifact_hash == hashes["v3.2"], envelope["event_id"]
        if boundary >= later.activated_at:
            assert artifact_hash == hashes["v5.1"], envelope["event_id"]
        boundaries.append(boundary)
    assert later.activated_at in boundaries
    assert later.activated_at - timedelta(microseconds=1) in boundaries

    # No activation instant is restated in the dataset code.
    for name in ("generator.py", "schedule.py"):
        text = (DATASET_SOURCE / name).read_text()
        assert "2026-06-01" not in text, name
        assert "2026-01-12" not in text, name


def _missing_horizon(mapping: dict) -> None:
    del mapping["horizon"]


def _attribution_before_horizon(mapping: dict) -> None:
    horizon = datetime.fromisoformat(mapping["horizon"])
    mapping["attribution_instant"] = (horizon - timedelta(seconds=1)).isoformat()


def _comparison_is_the_compared_workflow(mapping: dict) -> None:
    compared = canonical_by_type("decision.recorded")["payload"]["workflow_version"]
    mapping["comparison_workflow_version"] = compared


@pytest.mark.parametrize(
    "mutate,field",
    [
        pytest.param(_missing_horizon, "horizon", id="missing-horizon"),
        pytest.param(_attribution_before_horizon, "attribution_instant", id="a-before-h"),
        pytest.param(
            _comparison_is_the_compared_workflow,
            "comparison_workflow_version",
            id="comparison-is-v4.2",
        ),
    ],
)
def test_a_malformed_config_is_refused(mutate, field):
    mapping = copy.deepcopy(dataset_config_mapping())
    mutate(mapping)
    with pytest.raises(DatasetConfigError) as refused:
        DatasetConfig.from_mapping(mapping)
    assert refused.value.field == field
    assert repr(field) in str(refused.value)


def test_the_generator_does_not_import_the_analytics_engine():
    imported = ast.parse((DATASET_SOURCE / "generator.py").read_text())
    modules = {
        node.module
        for node in ast.walk(imported)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(imported)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [module for module in modules if module.startswith("flight_recorder.analytics")]

    # Nor transitively: importing the generator leaves the engine unloaded.
    check = (
        "import sys, flight_recorder.dataset.generator; "
        "assert not [m for m in sys.modules if m.startswith('flight_recorder.analytics')]"
    )
    subprocess.run([sys.executable, "-c", check], check=True)
