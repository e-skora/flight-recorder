"""Canonical remote-record identity must precede every ledger and lock key."""

import hashlib
import importlib
from dataclasses import FrozenInstanceError

import pytest

ORG = "00D000000000001EAA"
ACCOUNT = "001000000000001AAA"


def api(name="contracts"):
    try:
        return importlib.import_module("flight_recorder.adapters.crm." + name)
    except ModuleNotFoundError:
        pytest.fail("CRM identity/store foundation is missing")


def target(**changes):
    values = dict(provider="salesforce", instance_id=ORG, object_type="Account", record_id=ACCOUNT)
    return api().CRMTarget(**(values | changes))


def test_canonical_remote_identity_is_frozen_and_defensive():
    item = target()
    key = (
        '{"instance_id":"00D000000000001EAA","object_type":"Account",'
        '"provider":"salesforce","record_id":"001000000000001AAA"}'
    )
    assert item.key == key
    assert item.account_ref == "fr-crm-account-v1-" + hashlib.sha256(key.encode()).hexdigest()
    document = item.document
    document["record_id"] = "001000000000002AAA"
    assert item.document["record_id"] == ACCOUNT
    with pytest.raises(FrozenInstanceError):
        item.record_id = "001000000000002AAA"


def test_same_record_in_another_instance_has_another_lock_and_history():
    first = target()
    other = target(instance_id="00D000000000002EAA")
    assert first.key != other.key
    assert first.account_ref != other.account_ref


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "Salesforce"),
        ("provider", "unknown"),
        ("object_type", "account"),
        ("object_type", "Contact"),
        ("record_id", ACCOUNT[:15]),
        ("instance_id", ORG[:15]),
        ("record_id", ACCOUNT[:-3] + "aaa"),
        ("record_id", ACCOUNT[:-1] + "B"),
        ("instance_id", "00d000000000001EAA"),
        ("record_id", " " + ACCOUNT),
        ("record_id", ACCOUNT + " "),
        ("record_id", "００1000000000001AAA"),
        ("record_id", ORG),
        ("instance_id", ACCOUNT),
        ("record_id", 100),
        ("instance_id", True),
        ("provider", None),
    ],
)
def test_aliases_and_invalid_types_are_rejected_before_identity(field, value):
    with pytest.raises(api().CRMRefused):
        target(**{field: value})


def test_salesforce_letter_case_is_not_lowercased_or_truncated():
    valid = target(record_id="001Ab0000000001IAA")
    assert valid.document["record_id"] == "001Ab0000000001IAA"
    with pytest.raises(api().CRMRefused):
        target(record_id="001ab0000000001IAA")


def test_hubspot_ids_have_their_own_closed_rules():
    item = api().CRMTarget("hubspot", "71", "company", "74")
    assert item.document == {
        "provider": "hubspot",
        "instance_id": "71",
        "object_type": "company",
        "record_id": "74",
    }
    assert item.key != api().CRMTarget("hubspot", "72", "company", "74").key
    for field, value in [
        ("record_id", "074"),
        ("instance_id", "0"),
        ("record_id", 74),
        ("instance_id", True),
        ("record_id", "+74"),
        ("object_type", "Account"),
        ("record_id", "７４"),
    ]:
        values = item.document | {field: value}
        with pytest.raises(api().CRMRefused):
            api().CRMTarget(**values)
