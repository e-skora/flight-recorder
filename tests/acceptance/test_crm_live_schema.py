"""Closed v3 forms cannot admit shallow or contradictory capture claims."""

import copy
import json

import pytest
from pydantic import ValidationError

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import validate_envelope_json
from tests.adapters.crm.cases import feature, make_case


def test_closed_v3_pair_and_immutable_capture():
    case = make_case()
    for key in ("evidence", "decision"):
        model = validate_envelope_json(json.dumps(case[key]))
        assert model.schema_version == "3"
        assert model.model_dump(mode="json")["payload"]["capture"] == case["capture"]
    contracts = feature("adapters.crm.contracts")
    capture = contracts.CRMCapture.from_document(case["capture"])
    mutable = capture.document
    mutable["properties"]["NumberOfEmployees"] = 999
    assert capture.document == case["capture"]
    assert capture.capture_hash == canonical_hash(case["capture"])


@pytest.mark.parametrize(
    "change",
    [
        "capture_hash",
        "unit",
        "observation",
        "logical",
        "processing",
        "extra_capture",
        "extra_input",
        "raw_value",
        "source",
        "account",
        "event_id",
        "bool_sequence",
        "future_read",
        "unknown_mapping",
        "item_id",
        "head_date",
    ],
)
def test_strict_v3_capture_binding_refuses(change):
    case = make_case()
    env = copy.deepcopy(case["evidence"])
    cap = env["payload"]["capture"]
    if change == "capture_hash":
        env["payload"]["capture_hash"] = "0" * 64
    elif change in {"unit", "observation", "logical", "processing"}:
        name = {
            "unit": "unit_id",
            "observation": "observation_id",
            "logical": "logical_key",
            "processing": "processing_version",
        }[change]
        cap[name] = "0" * 64
    elif change == "extra_capture":
        cap["extra"] = True
    elif change == "extra_input":
        cap["inputs"]["employee_count"]["extra"] = True
    elif change == "raw_value":
        cap["properties"]["NumberOfEmployees"] += 1
    elif change == "source":
        env["source"] = "hubspot-test-account"
    elif change == "account":
        env["account_ref"] = "foreign"
    elif change == "event_id":
        env["event_id"] = "unbound"
    elif change == "bool_sequence":
        cap["capture_sequence"] = True
    elif change == "future_read":
        cap["read_finished_at"] = "2999-01-01T00:00:00.000000Z"
    elif change == "unknown_mapping":
        cap["mapping_hash"] = "0" * 64
    elif change == "item_id":
        env["payload"]["items"][0]["evidence_version_id"] = "unbound"
    else:
        next(
            i
            for i in env["payload"]["items"]
            if i["evidence_type"] == "head_of_platform_start_date"
        )["observed_at"] = "2026-01-01"
    if change != "capture_hash":
        env["payload"]["capture_hash"] = canonical_hash(cap)
    with pytest.raises(ValidationError):
        validate_envelope_json(json.dumps(env))


def test_canonical_capture_targets_only_salesforce_until_hubspot_mapping_review():
    case = make_case()
    case["evidence"]["source"] = "hubspot-test-account"
    with pytest.raises(ValidationError):
        validate_envelope_json(json.dumps(case["evidence"]))


def test_live_operation_schema_is_not_admitted_early():
    case = make_case()
    env = case["decision"]
    env.update(schema_version="2", event_type="external_operation.observed")
    with pytest.raises(ValidationError):
        validate_envelope_json(json.dumps(env))


@pytest.mark.parametrize("change", ["source_time", "read_finish", "funding_date", "head_date"])
def test_future_facts_refuse_even_with_recomputed_identity_hashes(change):
    from tests.adapters.crm.cases import seal

    case = make_case()
    cap = case["capture"]
    if change == "source_time":
        cap["source_updated_at"] = cap["properties"]["LastModifiedDate"] = (
            "2999-01-01T00:00:00.000000Z"
        )
    elif change == "read_finish":
        cap["read_finished_at"] = "2999-01-01T00:00:00.000000Z"
    elif change == "funding_date":
        cap["properties"]["FR_Funding_Observed_On__c"] = "2999-01-01"
        cap["inputs"]["funding_event"]["observed_at"] = "2999-01-01"
    else:
        cap["properties"]["FR_Head_Of_Platform_Start__c"] = "2999-01-01"
        cap["inputs"]["head_of_platform_start_date"]["value"] = "2999-01-01"
    with pytest.raises((ValidationError, ValueError)):
        feature("adapters.crm.contracts").CRMCapture.from_document(seal(cap))


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_capture_format_version_is_an_exact_integer(version):
    cap = make_case()["capture"]
    cap["format_version"] = version
    with pytest.raises((ValidationError, ValueError)):
        feature("adapters.crm.contracts").CRMCapture.from_document(cap)
