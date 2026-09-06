"""Logic artifacts enter through the collector as `logic_artifact.registered` (INV-05).

The artifact is data with identity: the collector hashes the submitted artifact
canonically and projects one `logic_artifacts` row per hash. Registration runs
under the reserved `_system` principal, which is infrastructure metadata and
must never surface as an account.
"""

import copy
import json

import pytest
from sqlalchemy import func, select

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF, accounts_query, logic_artifacts
from tests.conftest import (
    Harness,
    canonical_by_type,
    canonical_raw,
    logic_artifact,
    register_artifacts,
    seed_all,
    system_envelope_paths,
    system_raw,
)

V32_HASH = "db3a8bdebf2befe286ab49a2381dfe6fb931ac6f848923d35e0e732adcc82db0"


def _registration(version: str = "v3.2") -> dict:
    index = 0 if version == "v3.2" else 1
    return json.loads(system_envelope_paths()[index].read_text())


def _artifact_rows(harness: Harness) -> list:
    with harness.engine.connect() as conn:
        return conn.execute(select(logic_artifacts).order_by(logic_artifacts.c.artifact_id)).all()


# --- Registration ----------------------------------------------------------


def test_both_canonical_artifacts_register_under_their_fixture_hashes(harness):
    register_artifacts(harness)
    rows = _artifact_rows(harness)
    assert [r.logic_version for r in rows] == ["v3.2", "v5.1"]
    by_version = {r.logic_version: r for r in rows}
    assert by_version["v3.2"].artifact_hash == V32_HASH
    for version in ("v3.2", "v5.1"):
        row = by_version[version]
        artifact = logic_artifact(version)
        assert row.artifact_hash == canonical_hash(artifact)
        assert row.artifact_id == artifact["artifact_id"]
        assert row.decision_class == artifact["decision_class"]
        assert row.artifact_schema_version == artifact["artifact_schema_version"]
        assert row.evaluator_version == artifact["evaluator_version"]
        assert json.loads(row.artifact_json) == artifact


def test_registered_hash_is_the_hash_the_canonical_decision_references(harness):
    register_artifacts(harness)
    decision = canonical_by_type("decision.recorded")
    referenced = decision["payload"]["logic_artifact"]["artifact_hash"]
    with harness.engine.connect() as conn:
        stored = conn.execute(
            select(logic_artifacts.c.artifact_hash).where(
                logic_artifacts.c.artifact_hash == referenced
            )
        ).scalar_one()
    assert stored == referenced


def test_the_row_retains_the_registration_event_id(harness):
    register_artifacts(harness)
    registration = _registration("v3.2")
    with harness.engine.connect() as conn:
        source = conn.execute(
            select(logic_artifacts.c.source_event_id).where(
                logic_artifacts.c.artifact_hash == V32_HASH
            )
        ).scalar_one()
    assert source == registration["event_id"]


def test_identical_content_under_a_new_event_id_adds_an_event_but_no_artifact_row(harness):
    assert harness.post_raw(system_raw(0)).status_code == 201
    again = _registration("v3.2")
    again["event_id"] = "evt-system-00a-logic-artifact-v3.2-again"
    response = harness.post(again)
    assert response.status_code == 201
    assert harness.event_count() == 2
    rows = _artifact_rows(harness)
    assert len(rows) == 1
    assert rows[0].source_event_id == _registration("v3.2")["event_id"]


def test_a_different_artifact_reusing_an_id_and_version_conflicts_without_writes(harness):
    assert harness.post_raw(system_raw(0)).status_code == 201
    before = harness.snapshot()
    env = _registration("v3.2")
    env["event_id"] = "evt-system-00a-logic-artifact-v3.2-reweighted"
    env["payload"]["artifact"]["factors"][0]["weight"] = 26
    response = harness.post(env)
    assert response.status_code == 409
    assert response.json()["reason"] == "logic_artifact_identity_reused_with_different_content"
    assert harness.snapshot() == before


# --- Strict artifact model -------------------------------------------------


def _drop_identity(env):
    del env["payload"]["artifact"]["artifact_id"]


def _malformed_factors(env):
    env["payload"]["artifact"]["factors"] = "employee_count +25"


def _factor_missing_rule(env):
    del env["payload"]["artifact"]["factors"][0]["rule"]


def _duplicate_factor_key(env):
    factors = env["payload"]["artifact"]["factors"]
    factors.append(copy.deepcopy(factors[0]))


def _float_weight(env):
    env["payload"]["artifact"]["factors"][0]["weight"] = 25.0


def _string_threshold(env):
    env["payload"]["artifact"]["threshold"] = "75"


INVALID_ARTIFACTS = [
    pytest.param(lambda e: e["payload"].update(artifact="v3.2"), id="artifact-is-not-an-object"),
    pytest.param(_drop_identity, id="missing-artifact-id"),
    pytest.param(lambda e: e["payload"]["artifact"].pop("logic_version"), id="missing-version"),
    pytest.param(_malformed_factors, id="factors-not-a-list"),
    pytest.param(_factor_missing_rule, id="factor-missing-rule"),
    pytest.param(_duplicate_factor_key, id="duplicate-factor-key"),
    pytest.param(lambda e: e["payload"]["artifact"].update(factors=[]), id="no-factors"),
    pytest.param(_float_weight, id="float-weight"),
    pytest.param(_string_threshold, id="string-threshold"),
    pytest.param(
        lambda e: e["payload"]["artifact"].update(extra="x"), id="unknown-top-level-field"
    ),
    pytest.param(
        lambda e: e["payload"]["artifact"]["factors"][0].update(extra="x"),
        id="unknown-nested-field-in-factor",
    ),
    pytest.param(
        lambda e: e["payload"]["artifact"]["activation"].update(extra="x"),
        id="unknown-nested-field-in-activation",
    ),
    pytest.param(
        lambda e: e["payload"]["artifact"]["output_mapping"].pop("below_threshold"),
        id="incomplete-output-mapping",
    ),
    pytest.param(
        lambda e: e["payload"]["artifact"]["activation"].update(activated_at="2026-01-12T09:00:00"),
        id="naive-activation-timestamp",
    ),
    pytest.param(
        lambda e: e["payload"]["artifact"].update(decision_class="lead_scoring"),
        id="unsupported-decision-class",
    ),
    pytest.param(lambda e: e["payload"].update(unexpected="x"), id="unknown-payload-field"),
]


@pytest.mark.parametrize("mutate", INVALID_ARTIFACTS)
def test_invalid_artifacts_are_rejected_without_writes(harness, mutate):
    env = _registration("v3.2")
    env["event_id"] = "evt-system-invalid"
    mutate(env)
    response = harness.post(env)
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert harness.is_empty()


def test_both_canonical_artifact_files_validate_unchanged(harness):
    register_artifacts(harness)
    for version in ("v3.2", "v5.1"):
        with harness.engine.connect() as conn:
            stored = conn.execute(
                select(logic_artifacts.c.artifact_json).where(
                    logic_artifacts.c.logic_version == version
                )
            ).scalar_one()
        assert json.loads(stored) == logic_artifact(version)


# --- The `_system` principal ----------------------------------------------


def test_registration_materializes_the_system_account_once(harness):
    register_artifacts(harness)
    rows = [r for r in harness.account_rows() if r[0] == SYSTEM_ACCOUNT_REF]
    assert len(rows) == 1
    assert rows[0][3] == _registration("v3.2")["event_id"]


def test_registration_satisfies_every_foreign_key(harness):
    """Foreign keys are on; a violated reference would abort the transaction."""
    register_artifacts(harness)
    with harness.engine.connect() as conn:
        violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    assert violations == []


def test_an_ordinary_event_type_under_the_system_principal_is_rejected(harness):
    env = copy.deepcopy(canonical_by_type("account.discovered"))
    env["event_id"] = "evt-system-discovery"
    env["account_ref"] = SYSTEM_ACCOUNT_REF
    response = harness.post(env)
    assert response.status_code == 422
    assert "reserved" in json.dumps(response.json())
    assert harness.event_count() == 0
    assert harness.account_rows() == []


def test_registration_under_an_ordinary_account_is_rejected(harness):
    assert harness.post_raw(canonical_raw(0)).status_code == 201
    before = harness.snapshot()
    env = _registration("v3.2")
    env["account_ref"] = "novasignal-ai"
    response = harness.post(env)
    assert response.status_code == 422
    assert "_system" in json.dumps(response.json())
    assert harness.snapshot() == before


def test_the_system_account_never_appears_on_an_account_facing_surface(harness):
    seed_all(harness)
    with harness.engine.connect() as conn:
        listed = [r.account_ref for r in conn.execute(accounts_query())]
        total = conn.execute(
            select(func.count()).select_from(accounts_query().subquery())
        ).scalar_one()
    assert listed == ["novasignal-ai"]
    assert total == 1

    home = harness.client.get("/")
    assert home.status_code == 200
    assert SYSTEM_ACCOUNT_REF not in home.text
    assert "NovaSignal AI" in home.text


def test_the_system_account_has_no_trace_page(harness):
    seed_all(harness)
    assert harness.client.get(f"/accounts/{SYSTEM_ACCOUNT_REF}").status_code == 404


def test_the_novasignal_trace_still_shows_seven_rows(harness):
    seed_all(harness)
    page = harness.client.get("/accounts/novasignal-ai")
    assert page.status_code == 200
    assert page.text.count("Occurred at:") == 7
