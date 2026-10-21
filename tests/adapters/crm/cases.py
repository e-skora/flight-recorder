"""Fictional, offline CRM cases with independently constructed identity preimages."""

import copy
import importlib
import json
from datetime import datetime, timedelta

import pytest

from flight_recorder.adapters.crm.contracts import CRMTarget
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact, format_utc
from flight_recorder.fixtures import CANONICAL_DIR, load_json
from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate

ORG = "00D000000000001EAA"
ACCOUNT = "001000000000001AAA"


def feature(module):
    try:
        return importlib.import_module("flight_recorder." + module)
    except ModuleNotFoundError:
        pytest.fail("missing closed Salesforce capture and Collector admission")


def seal(cap):
    observed = [
        "target",
        "connection_id",
        "observation_origin",
        "read_started_at",
        "read_finished_at",
        "source_updated_at",
        "precondition",
        "request_id",
        "response_body_sha256",
        "properties",
        "unavailable_fields",
    ]
    cap["observation_id"] = canonical_hash({k: cap[k] for k in observed})
    cap["logical_key"] = canonical_hash(
        {"target": cap["target"], "observation_id": cap["observation_id"]}
    )
    cap["processing_version"] = canonical_hash(
        {
            k: cap[k]
            for k in [
                "mapping_hash",
                "rule_hash",
                "evaluator_version",
                "selection_identity",
                "adapter_version",
            ]
        }
    )
    cap["unit_id"] = canonical_hash(
        {k: cap[k] for k in ["logical_key", "processing_version", "reprocesses"]}
    )
    return cap


def source_fields():
    return {
        "employee_count": ["NumberOfEmployees"],
        "industry": ["FR_Industry_Segment__c"],
        "headquarters_country": ["BillingCountry"],
        "funding_event": ["FR_Funding_Stage__c", "FR_Funding_Observed_On__c"],
        "open_platform_engineering_roles": ["FR_Open_Platform_Roles__c"],
        "head_of_platform_start_date": ["FR_Head_Of_Platform_Start__c"],
        "verified_integration_pressure": [
            "FR_Integration_Pressure__c",
            "FR_Integration_Pressure_Basis__c",
        ],
    }


def observation(cap, item, account_ref):
    return dict(
        account_ref=account_ref,
        source="salesforce-test-account",
        target=cap["target"],
        observation_id=cap["observation_id"],
        mapping_hash=cap["mapping_hash"],
        evidence_type=item["evidence_type"],
        semantic={
            k: v
            for k, v in item.items()
            if k not in {"evidence_type", "evidence_version_id", "supersedes_evidence_version_id"}
        },
        source_fields=source_fields()[item["evidence_type"]],
        read_started_at=cap["read_started_at"],
        read_finished_at=cap["read_finished_at"],
        source_updated_at=cap["source_updated_at"],
        eligible_at=max(cap["read_finished_at"], cap["source_updated_at"]),
        observation_origin=cap["observation_origin"],
        response_body_sha256=cap["response_body_sha256"],
        precondition=cap["precondition"],
        supersedes_evidence_version_id=item["supersedes_evidence_version_id"],
    )


def envelopes(cap, artifact_registration=None, supersessions=None):
    supersessions = supersessions or {}
    ref = CRMTarget(**cap["target"]).account_ref
    if artifact_registration is None:
        artifact_registration = load_json(CANONICAL_DIR / "00a-logic-artifact-v3.2.json")
    artdoc = artifact_registration["payload"]["artifact"]
    artifact = LogicArtifact.model_validate_json(json.dumps(artdoc), strict=True)
    items = []
    context = []
    for key, entry in sorted(cap["inputs"].items()):
        if entry["state"] == "absent":
            continue
        if entry["state"] == "unavailable":
            context.append(
                dict(
                    input_key=key, value=None, availability="unavailable", evidence_version_id=None
                )
            )
            continue
        item = dict(
            evidence_type=key,
            value=entry["value"],
            supersedes_evidence_version_id=supersessions.get(key),
        )
        if key in ("funding_event", "head_of_platform_start_date"):
            item["observed_at"] = entry["observed_at"]
        if key == "verified_integration_pressure":
            item["basis"] = entry["basis"]
        item["evidence_version_id"] = "fr-crm-evidence-version-v1-" + canonical_hash(
            observation(cap, item, ref)
        )
        items.append(item)
        context.append(
            dict(
                input_key=key,
                value=entry["value"],
                availability="available",
                evidence_version_id=item["evidence_version_id"],
            )
        )
    dates = {i["evidence_type"]: i.get("observed_at") for i in items}
    from datetime import date

    result = evaluate(
        artifact,
        [
            ContextInput(
                key=x["input_key"],
                availability=x["availability"],
                value=x["value"],
                evidence_version_id=x["evidence_version_id"],
                observed_at=date.fromisoformat(dates[x["input_key"]])
                if dates.get(x["input_key"])
                else None,
            )
            for x in context
        ],
        datetime.fromisoformat(cap["capture_boundary"]),
    )
    bykey = {x["input_key"]: x for x in context}
    consumed = [
        dict(
            input_key=f.key,
            value=bykey[f.key]["value"],
            evidence_version_id=f.evidence_version_id,
            contribution=f.contribution,
        )
        for f in result.factors
        if f.input_state is InputState.CONSUMED
    ]
    common = dict(
        schema_version="3",
        source="salesforce-test-account",
        account_ref=ref,
        recorded_at=cap["capture_boundary"],
    )
    evidence = dict(
        **common,
        event_id="fr-crm-evidence-v1-" + cap["unit_id"],
        event_type="evidence.recorded",
        occurred_at=cap["read_finished_at"],
        payload=dict(items=items, capture=copy.deepcopy(cap), capture_hash=canonical_hash(cap)),
    )
    decision = dict(
        **common,
        event_id="fr-crm-decision-v1-" + cap["unit_id"],
        event_type="decision.recorded",
        occurred_at=cap["capture_boundary"],
        payload=dict(
            decision_class="account_prioritization",
            decision_boundary=cap["capture_boundary"],
            workflow_version=cap["processing_version"],
            historical_context=context,
            consumed_inputs=consumed,
            logic_artifact={
                k: artdoc[k] for k in ["logic_version", "artifact_id", "evaluator_version"]
            }
            | {"artifact_hash": canonical_hash(artdoc)},
            result=dict(score=result.score, threshold=result.threshold, output=result.output),
            explanation=None,
            capture=copy.deepcopy(cap),
            capture_hash=canonical_hash(cap),
        ),
    )
    account = load_json(CANONICAL_DIR / "01-account-discovered.json")
    account.update(
        account_ref=ref,
        event_id="fr-crm-discovery-v1-" + canonical_hash(cap["target"]),
        source="salesforce-test-account",
        occurred_at=cap["read_started_at"],
        recorded_at=cap["capture_boundary"],
    )
    return dict(
        capture=cap,
        evidence=evidence,
        decision=decision,
        account=account,
        artifact=artifact_registration,
    )


def make_case(**changes):
    m = feature("adapters.crm.mapping")
    base = load_json(CANONICAL_DIR / "04-decision-recorded.json")
    values = {x["input_key"]: x["value"] for x in base["payload"]["historical_context"]}
    funding = next(
        x
        for x in load_json(CANONICAL_DIR / "02-evidence-recorded-enrichment.json")["payload"][
            "items"
        ]
        if x["evidence_type"] == "funding_event"
    )
    pressure = next(
        x
        for x in load_json(CANONICAL_DIR / "03-evidence-recorded-integration-pressure.json")[
            "payload"
        ]["items"]
        if x["evidence_type"] == "verified_integration_pressure"
    )
    boundary = datetime.fromisoformat(base["occurred_at"])
    props = {
        "Id": ACCOUNT,
        "LastModifiedDate": format_utc(boundary - timedelta(seconds=30)),
        "Name": "Fictional Salesforce Test Account",
        "Website": "https://fictional.example",
    }
    for key, fields in source_fields().items():
        props[fields[0]] = values[key]
    props["FR_Funding_Observed_On__c"] = funding["observed_at"]
    props["FR_Integration_Pressure_Basis__c"] = "; ".join(pressure["basis"])
    cap = dict(
        format_version=1,
        data_origin="fictional_records_real_crm",
        observation_origin="fixture",
        target=CRMTarget("salesforce", ORG, "Account", ACCOUNT).document,
        connection_id="sf-fictional-test",
        read_started_at=format_utc(boundary - timedelta(seconds=2)),
        read_finished_at=format_utc(boundary - timedelta(seconds=1)),
        capture_boundary=format_utc(boundary),
        source_updated_at=props["LastModifiedDate"],
        precondition='"fictional-read-etag"',
        request_id=None,
        response_body_sha256=canonical_hash(props),
        properties=props,
        unavailable_fields=[],
        inputs=m.map_inputs(props, []),
        mapping_hash=m.MAPPING_HASH,
        rule_hash=base["payload"]["logic_artifact"]["artifact_hash"],
        evaluator_version=base["payload"]["logic_artifact"]["evaluator_version"],
        selection_identity="test-original-selection-v1",
        adapter_version="crm-adapter-v1",
        capture_sequence=1,
        ownership_generation=1,
        reprocesses=None,
    )
    cap.update(changes)
    return envelopes(seal(cap))
