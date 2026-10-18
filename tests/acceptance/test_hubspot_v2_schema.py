"""V2 schema contract, with strict negative cases and immutable legacy oracle."""

import copy
import json

import pytest
from pydantic import ValidationError

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import validate_envelope_json
from tests.adapters.hubspot.domain_cases import make_case


def test_strict_v2_pair_is_supported(tmp_path):
    case = make_case(tmp_path)
    for name in ("evidence", "decision"):
        validated = validate_envelope_json(json.dumps(case[name]))
        assert validated.schema_version == "2"
        assert validated.model_dump(mode="json")["payload"]["capture"] == case["capture"]


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "extra_capture",
        "sequence",
        "unit",
        "source",
        "account",
        "value",
        "item_id",
        "head_date",
        "boundary",
    ],
)
def test_invalid_v2_binding_rejects_without_writes(harness, tmp_path, change):
    case = make_case(tmp_path)
    env = copy.deepcopy(case["evidence"])
    cap = env["payload"]["capture"]
    if change == "hash":
        env["payload"]["capture_hash"] = "0" * 64
    elif change == "extra_capture":
        cap["unexpected"] = True
    elif change == "sequence":
        cap["capture_sequence"] = True
    elif change == "unit":
        cap["unit_id"] = "0" * 64
    elif change == "source":
        env["source"] = "real-hubspot"
    elif change == "account":
        env["account_ref"] = "other-company"
    elif change == "value":
        env["payload"]["items"][0]["value"] = "conflicting"
    elif change == "item_id":
        env["payload"]["items"][0]["evidence_version_id"] = "unbound"
    elif change == "head_date":
        next(
            x
            for x in env["payload"]["items"]
            if x["evidence_type"] == "head_of_platform_start_date"
        )["observed_at"] = "2026-01-01"
    elif change == "boundary":
        cap["capture_boundary"] = cap["read_started_at"]
    if change != "hash":
        env["payload"]["capture_hash"] = canonical_hash(cap)
    before = harness.snapshot()
    assert harness.post(env).status_code == 422
    assert harness.snapshot() == before


def test_v2_missing_source_dates_are_not_fabricated(tmp_path):
    case = make_case(tmp_path)
    env = validate_envelope_json(json.dumps(case["evidence"])).model_dump(mode="json")
    assert (
        next(
            i
            for i in env["payload"]["items"]
            if i["evidence_type"] == "head_of_platform_start_date"
        )["observed_at"]
        is None
    )
    assert all(
        p["available_at"] is None for p in env["payload"]["capture"]["input_provenance"].values()
    )


def test_other_event_types_do_not_gain_v2(tmp_path):
    env = make_case(tmp_path)["account"]
    env["schema_version"] = "2"
    with pytest.raises(ValidationError):
        validate_envelope_json(json.dumps(env))


def test_legacy_canonical_dumps_and_nine_hashes_unchanged():
    from tests.conftest import canonical_envelopes

    # Frozen base oracle; no new-version production helper computes expectations.
    expected = [
        "bfb2ad92007b511fe196c0ee400ba5793ce9b6946e458ecc9471d78930c7aa29",
        "569142d25e6ac9768b653147ae5faaf48c3d182d5c23e099f9eb22338e80d5a6",
        "60df3c9ccb161d7f63e6beb420873136b930a0399084e4528a405921918501e8",
        "a2daa528754cd34ab88caa4aac50e0ab678cf9ac7173b8ef4fe9507e82053417",
        "0eecf8535bcf00359dda091f7994158dbd7628b9d60413f672377ae394763d4b",
        "c6036673cf0888e09ab25a0001b8019767b9cf8b9b31b8cef08def9c51859f34",
        "bc0cc67280cffc7931c4378f38c6c302067de695a587456da02bbde998669779",
        "87b79ca4f5ae368992431b2a6a24888ff2afd04977d5bb26db384ad8be0fbe0a",
        "0d72dfdc0a37854930a9229d50fef19aba6c6e5d1e00c83e943505d861396429",
    ]
    assert [
        canonical_hash(validate_envelope_json(json.dumps(e)).model_dump(mode="json"))
        for e in canonical_envelopes()
    ] == expected
