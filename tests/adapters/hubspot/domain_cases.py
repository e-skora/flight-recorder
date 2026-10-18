"""Independent synthetic v2 wire oracle, generated only from the accepted capture/fixtures."""

import copy
import json
from datetime import date, datetime, timedelta

from flight_recorder.adapters.hubspot.capture import (
    MAPPING_DESCRIPTION,
    MAPPING_IDENTITY,
    capture_once,
)
from flight_recorder.adapters.hubspot.contracts import (
    ProcessingUnit,
    ReadResult,
    RuleIdentity,
    SourceNotification,
)
from flight_recorder.adapters.hubspot.store import CaptureStore
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import format_utc
from flight_recorder.fixtures import canonical_artifacts
from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
from tests.conftest import canonical_by_type, canonical_envelopes

SOURCE = "hubspot-synthetic-capture"
OWNED = {v["evidence_type"]: k for k, v in MAPPING_DESCRIPTION["owned_inputs"].items()}


def observation_body(account_ref, capture, item):
    key = item["evidence_type"]
    source_at = capture["input_provenance"][key]["available_at"]
    eligible = (
        max(capture["read_finished_at"], source_at) if source_at else capture["read_finished_at"]
    )
    return {
        "account_ref": account_ref,
        "source": SOURCE,
        "evidence_type": key,
        "semantic": {
            k: v
            for k, v in item.items()
            if k not in {"evidence_version_id", "evidence_type", "supersedes_evidence_version_id"}
        },
        "supersedes_evidence_version_id": item.get("supersedes_evidence_version_id"),
        "mapping_identity": capture["mapping_identity"],
        "source_property": OWNED[key],
        "read_started_at": capture["read_started_at"],
        "read_finished_at": capture["read_finished_at"],
        "source_available_at": source_at,
        "eligible_at": eligible,
    }


def make_case(
    tmp_path,
    *,
    selection="original",
    reprocesses=None,
    ignored=(),
    properties=None,
    clock_times=None,
    available_at=None,
):
    original = canonical_by_type("decision.recorded")
    reference = original["payload"]["logic_artifact"]
    artifact = canonical_artifacts()[reference["logic_version"]]
    boundary = datetime.fromisoformat(
        original["payload"]["decision_boundary"].replace("Z", "+00:00")
    )
    evidence = {
        item["evidence_type"]: item
        for env in canonical_envelopes()
        if env["event_type"] == "evidence.recorded"
        for item in env["payload"]["items"]
    }
    props = (
        {
            "numberofemployees": evidence["employee_count"]["value"],
            "industry": "COMPUTER_SOFTWARE",
            "country": evidence["headquarters_country"]["value"],
            "fr_funding_event": {k: evidence["funding_event"][k] for k in ("value", "observed_at")},
            "fr_open_platform_roles": evidence["open_platform_engineering_roles"]["value"],
            "fr_head_of_platform_start": evidence["head_of_platform_start_date"]["value"],
            "fr_integration_pressure": {
                "value": evidence["verified_integration_pressure"]["value"],
                "basis": evidence["verified_integration_pressure"]["basis"][0],
            },
        }
        if properties is None
        else properties
    )
    notification_body = {
        "portalId": 71,
        "appId": 72,
        "subscriptionId": 73,
        "subscriptionType": "company.propertyChange",
        "objectId": 74,
        "propertyName": "industry",
        "propertyValue": "COMPUTER_SOFTWARE",
        "occurredAt": int((boundary - timedelta(seconds=3)).timestamp() * 1000),
        "eventId": "synthetic-v2-notification",
        "changeSource": "SYNTHETIC",
    }
    notification = SourceNotification.from_payload(
        notification_body, raw_body=json.dumps(notification_body).encode()
    )
    unit = ProcessingUnit(
        notification,
        MAPPING_IDENTITY,
        canonical_hash({"selection": selection}),
        "synthetic-v2-local",
        reprocesses,
    )
    store = CaptureStore(tmp_path / "capture.sqlite")
    times = iter(
        clock_times or [boundary - timedelta(seconds=2), boundary - timedelta(seconds=1), boundary]
    )
    frozen = capture_once(
        store,
        unit,
        observe=lambda owner: ReadResult(props, ignored=ignored, available_at=available_at),
        select_logic=lambda: RuleIdentity(
            reference["artifact_hash"], reference["evaluator_version"]
        ),
        clock=lambda: next(times),
    )
    cap = frozen.document
    store.close()
    account_ref = "fr-account-v2-" + canonical_hash(
        [notification.portal_id, notification.company_id]
    )
    items = []
    for key, entry in cap["inputs"].items():
        if key not in OWNED or entry["state"] not in {"consumed", "ignored"}:
            continue
        value = entry["value"]
        item = {"evidence_type": key, "value": value, "supersedes_evidence_version_id": None}
        if key == "funding_event":
            item.update(value)
        elif key == "verified_integration_pressure":
            item.update(value=value["value"], basis=[value["basis"]])
        elif key == "head_of_platform_start_date":
            item["observed_at"] = None
        item["evidence_version_id"] = "fr-evidence-v2-" + canonical_hash(
            observation_body(account_ref, cap, item)
        )
        items.append(item)
    items.sort(key=lambda item: item["evidence_type"])
    by_key = {item["evidence_type"]: item for item in items}
    context = []
    for key, entry in sorted(cap["inputs"].items()):
        if key not in OWNED or entry["state"] == "absent":
            continue
        available = entry["state"] in {"consumed", "ignored"}
        context.append(
            {
                "input_key": key,
                "value": by_key[key]["value"] if available else None,
                "availability": "available" if available else "unavailable",
                "evidence_version_id": by_key[key]["evidence_version_id"] if available else None,
            }
        )
    evaluated = evaluate(
        artifact,
        [
            ContextInput(
                key=c["input_key"],
                availability=c["availability"],
                value=c["value"],
                evidence_version_id=c["evidence_version_id"],
                observed_at=date.fromisoformat(by_key[c["input_key"]]["observed_at"])
                if c["availability"] == "available" and by_key[c["input_key"]].get("observed_at")
                else None,
            )
            for c in context
        ],
        datetime.fromisoformat(cap["capture_boundary"].replace("Z", "+00:00")),
    )
    common = {
        "schema_version": "2",
        "source": SOURCE,
        "account_ref": account_ref,
        "recorded_at": format_utc(boundary + timedelta(days=1)),
    }
    binding = {"capture": cap, "capture_hash": canonical_hash(cap)}
    ev = {
        **common,
        "event_id": f"synthetic-v2-evidence-{unit.unit_id}",
        "event_type": "evidence.recorded",
        "occurred_at": cap["read_finished_at"],
        "payload": {**binding, "items": items},
    }
    dec = {
        **common,
        "event_id": f"synthetic-v2-decision-{unit.unit_id}",
        "event_type": "decision.recorded",
        "occurred_at": cap["capture_boundary"],
        "payload": {
            **binding,
            "decision_class": "account_prioritization",
            "decision_boundary": cap["capture_boundary"],
            "workflow_version": cap["processing_version"],
            "historical_context": context,
            "consumed_inputs": [
                {
                    "input_key": f.key,
                    "value": by_key[f.key]["value"],
                    "evidence_version_id": f.evidence_version_id,
                    "contribution": f.contribution,
                }
                for f in evaluated.factors
                if f.input_state is InputState.CONSUMED
            ],
            "logic_artifact": reference,
            "result": {
                "score": evaluated.score,
                "threshold": evaluated.threshold,
                "output": evaluated.output,
            },
            "explanation": None,
        },
    }
    account = copy.deepcopy(canonical_by_type("account.discovered"))
    account.update(account_ref=account_ref, event_id=f"synthetic-v2-account-{account_ref}")
    registration = next(
        copy.deepcopy(env)
        for env in canonical_envelopes()
        if env["event_type"] == "logic_artifact.registered"
        and canonical_hash(env["payload"]["artifact"]) == reference["artifact_hash"]
    )
    return {
        "capture": cap,
        "unit": unit,
        "account": account,
        "artifact": registration,
        "evidence": ev,
        "decision": dec,
        "result": evaluated,
    }


def admit(harness, case, *, decision=True):
    assert harness.post(case["account"]).status_code in (200, 201)
    assert harness.post(case["artifact"]).status_code in (200, 201)
    response = harness.post(case["evidence"])
    assert response.status_code == 201, response.json()
    if decision:
        response = harness.post(case["decision"])
        assert response.status_code == 201, response.json()
    return response
