"""Provider-shaped offline mapping cases; no account or transport is used."""

import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused


def mapping():
    try:
        return importlib.import_module("flight_recorder.adapters.crm.mapping")
    except ModuleNotFoundError:
        pytest.fail("missing Salesforce raw-number and input mapping boundary")


@pytest.mark.parametrize("token", ["7", "7.0", "7e0", "0.7e1", "7.000e+0"])
def test_integral_provider_number_keeps_raw_representation_and_exact_semantic(token):
    m = mapping()
    raw = m.decode_salesforce_record(('{"FR_Open_Platform_Roles__c":' + token + "}").encode())
    expected_raw = 7 if token == "7" else {"json_number": token}
    assert raw["FR_Open_Platform_Roles__c"] == expected_raw
    entry = m.map_inputs(raw, [])["open_platform_engineering_roles"]
    assert entry == {"state": "available", "value": 7, "observed_at": None, "basis": None}
    assert type(entry["value"]) is int


@pytest.mark.parametrize(
    "token",
    ["7.1", "true", '"7"', "NaN", "Infinity", "-0.0", "-1", "1e129", "9223372036854775808"],
)
def test_invalid_provider_count_cannot_be_mapped(token):
    m = mapping()
    with pytest.raises(CRMRefused, match="invalid_crm_mapping"):
        raw = m.decode_salesforce_record(('{"FR_Open_Platform_Roles__c":' + token + "}").encode())
        m.map_inputs(raw, [])


@pytest.mark.parametrize(
    "body",
    [
        b'{"FR_Open_Platform_Roles__c":{"json_number":"7.0"}}',
        b'{"NumberOfEmployees":1,"NumberOfEmployees":2}',
        b'{"FR_Industry_Segment__c":7.0}',
        b'{"unknown_field":1}',
        b"[]",
    ],
)
def test_source_objects_duplicate_keys_and_unknown_selected_fields_refuse(body):
    m = mapping()
    with pytest.raises(CRMRefused, match="invalid_crm_mapping"):
        m.decode_salesforce_record(body)


def test_absent_unavailable_and_available_remain_distinct():
    m = mapping()
    entries = m.map_inputs({"NumberOfEmployees": None, "BillingCountry": "United States"}, [])
    assert entries["employee_count"]["state"] == "unavailable"
    assert entries["industry"]["state"] == "absent"
    assert entries["headquarters_country"]["state"] == "available"
    assert m.map_inputs({}, ["NumberOfEmployees"])["employee_count"]["state"] == "unavailable"
    with pytest.raises(CRMRefused):
        m.map_inputs({"NumberOfEmployees": 1}, ["NumberOfEmployees"])


@pytest.mark.parametrize(
    "properties",
    [
        {"FR_Funding_Stage__c": "Series B"},
        {"FR_Integration_Pressure__c": "HIGH", "FR_Integration_Pressure_Basis__c": None},
    ],
)
def test_partial_compound_input_refuses(properties):
    with pytest.raises(CRMRefused, match="incomplete_compound_input"):
        mapping().map_inputs(properties, [])


def test_optional_salesforce_metadata_is_not_a_selected_input():
    body = {
        "attributes": {"type": "Account", "url": "/services/data/vXX/sobjects/Account/example"},
        "NumberOfEmployees": 184,
    }
    assert mapping().decode_salesforce_record(json.dumps(body).encode()) == {
        "NumberOfEmployees": 184
    }


def test_seven_inputs_preserve_the_canonical_semantics_without_aliases():
    from tests.adapters.crm.cases import make_case
    from tests.conftest import canonical_by_type

    case = make_case()
    expected = {
        x["input_key"]: x["value"]
        for x in canonical_by_type("decision.recorded")["payload"]["historical_context"]
        if x["availability"] == "available"
    }
    assert {key: entry["value"] for key, entry in case["capture"]["inputs"].items()} == expected
    assert all(x["state"] == "available" for x in case["capture"]["inputs"].values())
    changed = copy.deepcopy(case["capture"]["properties"])
    changed["FR_Industry_Segment__c"] = "COMPUTER_SOFTWARE"
    assert mapping().map_inputs(changed, [])["industry"]["value"] == "COMPUTER_SOFTWARE"


@pytest.mark.parametrize("token", ["1.0000000000000000000000000001", "-0", "0e129", "1e-129"])
def test_rounding_and_exponent_edges_are_not_silently_repaired(token):
    m = mapping()
    with pytest.raises(CRMRefused):
        properties = m.decode_salesforce_record(('{"NumberOfEmployees":' + token + "}").encode())
        m.map_inputs(properties, [])


def test_mapping_artifact_identity_does_not_equate_bool_and_integer(tmp_path):
    m = mapping()
    root = Path(m.__file__).resolve().parents[4]
    isolated = tmp_path / "isolated"
    shutil.copytree(root / "src", isolated / "src")
    artifact = isolated / "fixtures/integration/crm/salesforce-map-v1.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(m.MAPPING_DESCRIPTION))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(isolated / "src") + ":" + env.get("PYTHONPATH", "")
    command = [sys.executable, "-B", "-s", "-c", "from flight_recorder.adapters.crm import mapping"]
    baseline = subprocess.run(command, cwd=isolated, env=env, capture_output=True, timeout=30)
    assert baseline.returncode == 0, baseline.stderr.decode()
    changed = copy.deepcopy(m.MAPPING_DESCRIPTION)
    changed["format_version"] = True
    artifact.write_text(json.dumps(changed))
    result = subprocess.run(command, cwd=isolated, env=env, capture_output=True, timeout=30)
    assert result.returncode != 0, "boolean artifact version was incorrectly treated as integer1"
    assert b"invalid_crm_mapping" in result.stderr


@pytest.mark.parametrize(
    "token,expected",
    [("0.0", 0), ("9223372036854775807.0", 9223372036854775807), ("1e18", 1000000000000000000)],
)
def test_exact_range_edges_do_not_round_through_binary_float(token, expected):
    m = mapping()
    properties = m.decode_salesforce_record(('{"NumberOfEmployees":' + token + "}").encode())
    assert m.map_inputs(properties, [])["employee_count"]["value"] == expected


@pytest.mark.parametrize("value", ["2026-2-3", "2026-02-30", True])
def test_malformed_provider_calendar_date_is_not_repaired(value):
    with pytest.raises(CRMRefused, match="invalid_crm_mapping"):
        mapping().map_inputs(
            {"FR_Funding_Stage__c": "Series B", "FR_Funding_Observed_On__c": value}, []
        )
