"""Real incoming capture to local presentation; catches hidden or substituted history."""

import copy
import html
import importlib.util
import json

import pytest

from flight_recorder.collector.canonical import canonical_hash
from tests.acceptance.test_decision_detail_page import element
from tests.adapters.hubspot.test_submission_recovery import setup_case
from tests.conftest import (
    canonical_by_type,
    canonical_envelopes,
    register_current_logic,
)


def page_api():
    name = "flight_recorder.web.hubspot_decision_view"
    assert importlib.util.find_spec(name) is not None, "Task3 presentation interface missing"
    return importlib.import_module(name)


def captured(tmp_path, harness, **kwargs):
    submit, _, store, case, frozen, _ = setup_case(tmp_path, harness, **kwargs)
    assert submit.resume_submission(store, harness.collector, case["unit"]).state == "complete"
    case["decision"]["event_id"] = frozen.document["stages"][-1]["event_id"]
    case["url"] = (
        f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
    )
    store.close()
    return case


def get(harness, case, query=""):
    response = harness.client.get(case["url"] + query)
    assert response.status_code == 200, response.text
    return response.text


def historical_rules(body, artifact):
    rules = html.unescape(element(body, "v2-historical-ruleset"))
    assert rules
    for factor in artifact["factors"]:
        assert factor["key"] in rules
        assert factor["rule"] in rules
        assert str(factor["weight"]) in rules
    for value in [
        artifact["threshold"],
        artifact["missing_value_behavior"],
        *artifact["output_mapping"].values(),
        artifact["artifact_schema_version"],
        artifact["evaluator_version"],
        canonical_hash(artifact),
    ]:
        assert str(value) in rules
    return rules


def corrupt(harness, table, *, values=None, delete=False, drop=False):
    # Deliberately broken disposable ledger, never a repair path.
    raw = harness.engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        for suffix in ("no_update", "no_delete"):
            raw.execute(f"DROP TRIGGER IF EXISTS {table}_{suffix}")
        if drop:
            raw.execute(f"DROP TABLE {table}")
        elif delete:
            raw.execute(f"DELETE FROM {table}")
        else:
            for key, value in values.items():
                raw.execute(f"UPDATE {table} SET {key}=?", (value,))
        raw.commit()
    finally:
        raw.close()


def test_trace_to_v2_inspection_and_replay(tmp_path, harness):
    case = captured(tmp_path, harness)
    ref = case["decision"]["account_ref"]
    assert ref in harness.client.get("/").text
    assert case["url"] in harness.client.get(f"/accounts/{ref}").text
    current = register_current_logic(harness)
    before = harness.snapshot()
    body = get(harness, case, f"?current={current}")
    assert "Synthetic" in body or "synthetic" in body
    assert str(case["result"].score) in element(body, "v2-original")
    assert "counterfactual" in element(body, "v2-replay").lower()
    assert case["capture"]["unit_id"] in element(body, "v2-capture-provenance")
    historical_rules(body, case["artifact"]["payload"]["artifact"])
    assert harness.snapshot() == before


@pytest.mark.parametrize("selection", ["absent", "unregistered", "ambiguous"])
def test_verified_historical_ruleset_without_resolvable_current_artifact(
    tmp_path, harness, selection
):
    case = captured(tmp_path, harness)
    if selection == "ambiguous":
        source = canonical_by_type("logic_artifact.registered")["payload"]["artifact"]
        for i in range(2):
            env = copy.deepcopy(case["artifact"])
            env["event_id"] = f"ambiguous-{i}"
            env["payload"]["artifact"] = copy.deepcopy(source)
            env["payload"]["artifact"].update(
                artifact_id=f"test-ambiguous-{i}", logic_version="v5.2"
            )
            assert harness.post(env).status_code == 201
    query = "?current=" + "0" * 64 if selection == "unregistered" else ""
    body = get(harness, case, query)
    historical_rules(body, case["artifact"]["payload"]["artifact"])
    assert "Verified original" in element(body, "v2-original")
    assert "unavailable" in element(body, "v2-replay").lower()


def test_actual_consumption_and_four_input_states_are_textually_distinct(tmp_path, harness):
    head_date = next(
        item["value"]
        for env in canonical_envelopes()
        if env["event_type"] == "evidence.recorded"
        for item in env["payload"]["items"]
        if item["evidence_type"] == "head_of_platform_start_date"
    )
    case = captured(
        tmp_path,
        harness,
        properties={
            "numberofemployees": 1,
            "fr_head_of_platform_start": head_date,
            "fr_integration_pressure": None,
        },
    )
    body = get(harness, case)
    table = element(body, "v2-context-table")
    for state in ("consumed", "available but ignored", "unavailable", "absent"):
        assert state in table
    assert "not consumed" in table
    assert "employee_count" in table


def test_notification_read_capture_and_per_value_times_have_separate_meanings(tmp_path, harness):
    case = captured(tmp_path, harness)
    body = get(harness, case)
    region = element(body, "v2-capture-provenance")
    for key in ("read_started_at", "read_finished_at", "capture_boundary"):
        assert case["capture"][key] in region
    assert "notification" in region.lower()
    assert "not an atomic" in body
    assert "Source availability time not provided" in element(body, "v2-context-table")
    assert case["decision"]["recorded_at"][:10] in element(body, "v2-recorded-decision")


@pytest.mark.parametrize("vendor", [None, 37, ""])
def test_vendor_observation_and_recorded_explanation_are_not_owned_inputs(
    tmp_path, harness, vendor
):
    props = {"numberofemployees": 1}
    if vendor is not None:
        props["hubspotscore"] = vendor
    case = captured(tmp_path, harness, properties=props)
    region = element(get(harness, case), "v2-vendor-observation")
    assert "upstream" in region.lower() and "not replayable" in region.lower()
    assert ("ignored" if vendor == 37 else "absent" if vendor is None else "unavailable") in region
    body = get(harness, case)
    assert "No explanation recorded" in element(body, "v2-explanation")
    assert "confidence" in body.lower()


def test_reprocessed_decisions_keep_exact_predecessor_and_evidence_origin(tmp_path, harness):
    first = captured(tmp_path, harness)
    # Same capture store has the prior sequence; new processing identity is explicit.
    second = captured(tmp_path, harness, selection="next", reprocesses=first["unit"].unit_id)
    body = get(harness, second)
    assert first["unit"].unit_id in element(body, "v2-reprocessing")
    assert first["url"] in element(body, "v2-reprocessing")
    assert first["capture"]["unit_id"] in element(body, "v2-context-table")
    assert get(harness, first)


def test_v2_url_identity_and_current_selection_refusals(tmp_path, harness):
    case = captured(tmp_path, harness)
    for url in (
        case["url"].replace(case["decision"]["account_ref"], "missing-account"),
        case["url"] + "missing",
        case["url"].rsplit("/", 1)[0] + "/" + case["account"]["event_id"],
    ):
        assert harness.client.get(url).status_code == 404
    assert harness.client.get(case["url"] + "?current=Bad").status_code == 400
    assert "unavailable" in element(get(harness, case, "?current=" + "0" * 64), "v2-replay").lower()


@pytest.mark.parametrize(
    "fault,table,values",
    [
        ("missing", "decisions_v2", None),
        ("drop", "decisions_v2", None),
        ("missing", "capture_contexts", None),
        ("change", "capture_contexts", {"body_json": "{}"}),
        ("change", "capture_contexts", {"unit_id": "tampered"}),
        ("missing", "evidence_versions_v2", None),
        ("change", "evidence_versions_v2", {"observation_json": "{}"}),
        ("change", "evidence_versions_v2", {"available_at": "2099-01-01T00:00:00.000000Z"}),
        ("missing", "decision_context_v2", None),
        ("change", "decision_context_v2", {"value_text": "999"}),
        ("missing", "decision_consumed_inputs_v2", None),
        ("change", "decision_consumed_inputs_v2", {"contribution": 999}),
        ("change", "decisions_v2", {"reprocesses": "missing-unit"}),
        ("change", "decisions_v2", {"decision_boundary": "2099-01-01T00:00:00.000000Z"}),
        ("missing", "logic_artifacts", None),
        ("change", "logic_artifacts", {"artifact_json": "{}"}),
        ("change", "logic_artifacts", {"evaluator_version": "not-this-evaluator"}),
    ],
)
def test_known_v2_history_corruption_is_named_without_repair(
    tmp_path, harness, fault, table, values
):
    case = captured(tmp_path, harness)
    corrupt(harness, table, values=values, delete=fault == "missing", drop=fault == "drop")
    body = get(harness, case)
    assert "unavailable" in element(body, "v2-original").lower()
    assert "unverified" in element(body, "v2-recorded-decision").lower()
    assert "unavailable" in element(body, "v2-historical-ruleset").lower()
    assert "Verified original" not in element(body, "v2-original")
    assert (
        "counterfactual" not in element(body, "v2-replay").lower()
        or "unavailable" in element(body, "v2-replay").lower()
    )


@pytest.mark.parametrize("failure", ["missing", "hash", "evaluator", "class"])
def test_current_failure_retains_independently_verified_original(tmp_path, harness, failure):
    case = captured(tmp_path, harness)
    current = "0" * 64
    if failure != "missing":
        env = copy.deepcopy(case["artifact"])
        env["event_id"] = "current-fault"
        artifact = env["payload"]["artifact"]
        artifact.update(artifact_id="test-current-fault", logic_version="test-current-fault")
        if failure == "evaluator":
            artifact["evaluator_version"] = "other-evaluator"
        assert harness.post(env).status_code == 201
        current = canonical_hash(artifact)
        if failure == "class":
            raw = harness.engine.raw_connection()
            raw.execute("DROP TRIGGER logic_artifacts_no_update")
            raw.execute(
                "UPDATE logic_artifacts SET decision_class='other_class' WHERE artifact_hash=?",
                (current,),
            )
            raw.commit()
            raw.close()
        if failure == "hash":
            raw = harness.engine.raw_connection()
            raw.execute("DROP TRIGGER logic_artifacts_no_update")
            raw.execute(
                "UPDATE logic_artifacts SET artifact_json='{}' WHERE artifact_hash=?", (current,)
            )
            raw.commit()
            raw.close()
    body = get(harness, case, "?current=" + current)
    historical_rules(body, case["artifact"]["payload"]["artifact"])
    assert "Verified original" in element(body, "v2-original")
    assert "unavailable" in element(body, "v2-replay").lower()


def test_comparison_exposes_engine_scores_and_contribution_changes(tmp_path, harness):
    from flight_recorder.replay.counterfactual import compare, replay

    case = captured(tmp_path, harness)
    current = register_current_logic(harness)
    with harness.engine.connect() as conn:
        expected = compare(replay(conn, case["decision"]["event_id"], current))
    region = element(get(harness, case, "?current=" + current), "v2-comparison")
    assert region
    for value in (
        expected.original_score,
        expected.counterfactual_score,
        expected.score_delta,
        expected.original_output,
        expected.counterfactual_output,
        expected.historical_logic.artifact_hash,
        expected.current_logic.artifact_hash,
    ):
        assert str(value) in region
    for row in expected.contributions:
        assert row.key in region and row.change in region


def custom_capture(tmp_path, harness, *, registration=None, notification_changes=None):
    from datetime import datetime

    from flight_recorder.adapters.hubspot.capture import capture_once
    from flight_recorder.adapters.hubspot.contracts import (
        ProcessingUnit,
        ReadResult,
        RuleIdentity,
        SourceNotification,
    )
    from flight_recorder.adapters.hubspot.store import CaptureStore
    from flight_recorder.adapters.hubspot.submission_contracts import SubmissionPrerequisites
    from flight_recorder.adapters.hubspot.submission_store import SubmissionStore
    from flight_recorder.adapters.hubspot.submit import freeze_submission, resume_submission
    from flight_recorder.collector.canonical import canonical_bytes
    from tests.adapters.hubspot.domain_cases import make_case

    case = make_case(tmp_path)
    env = registration or case["artifact"]
    artifact = env["payload"]["artifact"]
    payload = json.loads(case["unit"].notification.raw_body)
    payload.update(notification_changes or {})
    note = SourceNotification.from_payload(payload, raw_body=canonical_bytes(payload))
    unit = ProcessingUnit(
        note,
        case["unit"].mapping_identity,
        canonical_hash({"custom": payload, "artifact": artifact}),
        "synthetic-v2-local",
    )
    capture_store = CaptureStore(tmp_path / "capture.sqlite")
    times = iter(
        datetime.fromisoformat(case["capture"][key])
        for key in ("read_started_at", "read_finished_at", "capture_boundary")
    )
    cap = capture_once(
        capture_store,
        unit,
        observe=lambda owner: ReadResult(case["capture"]["observation"]["properties"]),
        select_logic=lambda: RuleIdentity(canonical_hash(artifact), artifact["evaluator_version"]),
        clock=lambda: next(times),
    )
    store = SubmissionStore(capture_store)
    frozen = freeze_submission(
        store,
        harness.collector,
        unit,
        prerequisites=SubmissionPrerequisites(
            canonical_bytes(case["account"]),
            canonical_bytes(env),
            case["decision"]["recorded_at"],
            b"{}",
        ),
    )
    assert resume_submission(store, harness.collector, unit).state == "complete"
    case["artifact"] = env
    case["capture"] = cap.document
    case["decision"]["event_id"] = frozen.document["stages"][-1]["event_id"]
    case["url"] = (
        f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
    )
    store.close()
    return case


@pytest.mark.parametrize("epoch", [-1000, 10**30])
def test_notification_epoch_and_escaped_source_text_survive_display_limits(
    tmp_path, harness, epoch
):
    case = custom_capture(
        tmp_path,
        harness,
        notification_changes={"occurredAt": epoch, "propertyValue": "<script>unsafe()</script>"},
    )
    body = get(harness, case)
    region = element(body, "v2-capture-provenance")
    assert str(epoch) in region
    assert "<script>unsafe()" not in body
    assert "&lt;script&gt;" in body
    assert ("UTC conversion unavailable" in region) == (epoch == 10**30)
    assert "Verified original" in element(body, "v2-original")


def test_historical_bound_note_uses_preserved_artifact_without_current(tmp_path, harness):
    registration = next(
        env
        for env in canonical_envelopes()
        if env["event_type"] == "logic_artifact.registered"
        and env["payload"]["artifact"]["logic_version"] == "v5.1"
    )
    case = custom_capture(tmp_path, harness, registration=registration)
    rules = historical_rules(get(harness, case), registration["payload"]["artifact"])
    bound = sum(max(0, f["weight"]) for f in registration["payload"]["artifact"]["factors"])
    assert "upper bound" in rules
    assert str(bound) in rules
    assert str(registration["payload"]["artifact"]["threshold"]) in rules


def test_recorded_explanation_is_escaped_and_does_not_change_original(tmp_path, harness):
    from tests.adapters.hubspot.domain_cases import admit, make_case

    case = make_case(tmp_path)
    text = "<script>notEvidence()</script>"
    case["decision"]["payload"]["explanation"] = text
    admit(harness, case)
    case["url"] = (
        f"/accounts/{case['decision']['account_ref']}/decisions/{case['decision']['event_id']}"
    )
    before = harness.snapshot()
    body = get(harness, case)
    region = element(body, "v2-explanation")
    assert text not in body and "&lt;script&gt;" in body
    assert "not authoritative" in region
    assert str(case["result"].score) in element(body, "v2-original")
    assert harness.snapshot() == before
