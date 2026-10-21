"""Pinned Salesforce mapping with a lossless provider JSON-number boundary."""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.fixtures import REPO_ROOT

FIELDS = {
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
OUTPUTS = {
    "fr_score": "FR_Score__c",
    "fr_output": "FR_Output__c",
    "fr_logic_version": "FR_Logic_Version__c",
    "fr_decision_ref": "FR_Decision_Ref__c",
    "fr_op_id": "FR_Operation_Id__c",
    "fr_op_rev": "FR_Operation_Revision__c",
}
NUMERIC = {
    "NumberOfEmployees",
    "FR_Open_Platform_Roles__c",
    "FR_Score__c",
    "FR_Operation_Revision__c",
}
INPUT_FIELDS = {field for fields in FIELDS.values() for field in fields}
ALLOWED = INPUT_FIELDS | set(OUTPUTS.values()) | {"Id", "Name", "Website", "LastModifiedDate"}
MAX_INTEGER = 9223372036854775807
_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?", re.ASCII)
MAPPING_DESCRIPTION = {
    "format_version": 1,
    "mapping_id": "salesforce-map-v1",
    "provider": "salesforce",
    "object_type": "Account",
    "inputs": FIELDS,
    "outputs": OUTPUTS,
    "identity_field": "Id",
    "display_fields": ["Name", "Website"],
    "source_time_field": "LastModifiedDate",
    "number_mapping": {
        "format": "lossless-json-number-v1",
        "max_token_length": 128,
        "max_absolute_exponent": 128,
        "minimum": 0,
        "maximum": MAX_INTEGER,
        "revision_minimum": 1,
        "negative_zero": "refused",
    },
}
MAPPING_HASH = canonical_hash(MAPPING_DESCRIPTION)
require(
    canonical_text(
        json.loads((REPO_ROOT / "fixtures/integration/crm/salesforce-map-v1.json").read_bytes())
    )
    == canonical_text(MAPPING_DESCRIPTION),
    "invalid_crm_mapping",
)


@dataclass(frozen=True)
class _NumberToken:
    text: str


def integer_value(raw, *, revision=False):
    """Explicit exact conversion; the raw tagged token remains in the capture."""
    minimum = 1 if revision else 0
    if type(raw) is int:
        value = raw
    else:
        require(type(raw) is dict and set(raw) == {"json_number"}, "invalid_crm_mapping")
        token = raw["json_number"]
        require(
            type(token) is str and 1 <= len(token) <= 128 and bool(_NUMBER.fullmatch(token)),
            "invalid_crm_mapping",
        )
        exponent = re.split("[eE]", token)
        require(len(exponent) == 1 or abs(int(exponent[1])) <= 128, "invalid_crm_mapping")
        try:
            decimal = Decimal(token)
            require(
                decimal.is_finite() and not (decimal.is_zero() and decimal.is_signed()),
                "invalid_crm_mapping",
            )
            require(
                minimum <= decimal <= MAX_INTEGER and decimal == decimal.to_integral_value(),
                "invalid_crm_mapping",
            )
            value = int(decimal)
        except (InvalidOperation, OverflowError, ValueError) as exc:
            raise CRMRefused("invalid_crm_mapping") from exc
    require(type(value) is int and minimum <= value <= MAX_INTEGER, "invalid_crm_mapping")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_crm_mapping")
        result[key] = value
    return result


def _json_int(token):
    require(len(token) <= 128 and token != "-0", "invalid_crm_mapping")
    return int(token)


def _constant(value):
    raise CRMRefused("invalid_crm_mapping")


def decode_salesforce_record(body: bytes) -> dict:
    """Decode only selected scalars; source objects never become numeric tags."""
    require(type(body) is bytes and len(body) <= 1024 * 1024, "invalid_crm_mapping")
    try:
        raw = json.loads(
            body,
            parse_float=_NumberToken,
            parse_int=_json_int,
            parse_constant=_constant,
            object_pairs_hook=_pairs,
        )
        require(type(raw) is dict, "invalid_crm_mapping")
        if "attributes" in raw:
            attributes = raw.pop("attributes")
            require(
                type(attributes) is dict
                and set(attributes) == {"type", "url"}
                and attributes["type"] == "Account"
                and type(attributes["url"]) is str
                and attributes["url"].startswith("/")
                and not attributes["url"].startswith("//"),
                "invalid_crm_mapping",
            )
        result = {}
        for key, value in raw.items():
            require(key in ALLOWED, "invalid_crm_mapping")
            if type(value) is _NumberToken:
                require(key in NUMERIC, "invalid_crm_mapping")
                value = {"json_number": value.text}
            else:
                require(value is None or type(value) in (str, int, bool), "invalid_crm_mapping")
            result[key] = value
        validate_properties(result)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CRMRefused("invalid_crm_mapping") from exc


def validate_properties(properties):
    require(type(properties) is dict and set(properties) <= ALLOWED, "invalid_crm_mapping")
    for key, value in properties.items():
        if value is None or value == "":
            continue
        if key in NUMERIC:
            integer_value(value, revision=key == "FR_Operation_Revision__c")
        else:
            require(type(value) is str, "invalid_crm_mapping")
            if key == "FR_Output__c":
                require(value in ("PRIORITIZE", "DO_NOT_PRIORITIZE"), "invalid_crm_mapping")
            if key == "FR_Operation_Id__c":
                require(bool(re.fullmatch("[0-9a-f]{64}", value)), "invalid_crm_mapping")


def _date(value):
    require(type(value) is str, "invalid_crm_mapping")
    try:
        require(date.fromisoformat(value).isoformat() == value, "invalid_crm_mapping")
    except ValueError as exc:
        raise CRMRefused("invalid_crm_mapping") from exc
    return value


def map_inputs(properties, unavailable_fields):
    validate_properties(properties)
    require(
        type(unavailable_fields) is list and all(type(k) is str for k in unavailable_fields),
        "invalid_crm_mapping",
    )
    require(
        unavailable_fields == sorted(set(unavailable_fields))
        and set(unavailable_fields) <= INPUT_FIELDS
        and not set(unavailable_fields) & set(properties),
        "invalid_crm_mapping",
    )
    result = {}
    for key, fields in sorted(FIELDS.items()):
        states = {
            "unavailable"
            if field in unavailable_fields
            or (field in properties and properties[field] in (None, ""))
            else "available"
            if field in properties
            else "absent"
            for field in fields
        }
        require(len(states) == 1, "incomplete_compound_input")
        state = next(iter(states))
        entry = dict(state=state, value=None, observed_at=None, basis=None)
        if state == "available":
            value = properties[fields[0]]
            if fields[0] in NUMERIC:
                value = integer_value(value)
            elif key == "head_of_platform_start_date":
                value = _date(value)
            elif key == "funding_event":
                entry["observed_at"] = _date(properties[fields[1]])
            elif key == "verified_integration_pressure":
                require(value in ("LOW", "MEDIUM", "HIGH"), "invalid_crm_mapping")
                entry["basis"] = [properties[fields[1]]]
            entry["value"] = value
        result[key] = entry
    return result
