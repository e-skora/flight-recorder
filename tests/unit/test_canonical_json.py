"""Canonical JSON: known answers and the stored-representation contract."""

import json

from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.schema import validate_envelope_json
from flight_recorder.ledger.schema import events
from tests.conftest import (
    canonical_by_type,
    canonical_raw,
    logic_artifact,
    reformatted,
    register_artifacts,
    seed_all,
)


def test_canonical_bytes_known_answer():
    obj = {"b": 1, "a": [1, {"z": "ü", "y": None}], "c": True}
    assert canonical_bytes(obj) == b'{"a":[1,{"y":null,"z":"\xc3\xbc"}],"b":1,"c":true}'


def test_canonical_hash_known_answer():
    obj = {"b": 1, "a": [1, {"z": "ü", "y": None}], "c": True}
    assert canonical_hash(obj) == "3b238eca8c27217643d828f9e26b38472b2455bfa18a5aa923bbbc2bf23958e3"


def test_canonical_hash_ignores_key_order_and_whitespace():
    a = json.loads('{"x": 1, "y": {"p": [1, 2], "q": "s"}}')
    b = json.loads('{ "y" : { "q":"s" , "p":[1,2]} , "x":1 }')
    assert canonical_hash(a) == canonical_hash(b)


def test_decision_artifact_hash_matches_logic_artifact_file(harness):
    seed_all(harness)
    with harness.engine.connect() as conn:
        payload = conn.execute(
            select(events.c.payload).where(events.c.event_type == "decision.recorded")
        ).scalar_one()
    stored = json.loads(payload)
    assert stored["logic_artifact"]["artifact_hash"] == canonical_hash(logic_artifact("v3.2"))
    assert stored["logic_artifact"]["logic_version"] == logic_artifact("v3.2")["logic_version"]


def test_stored_payload_is_canonical_regardless_of_raw_formatting(harness):
    discovery = canonical_by_type("account.discovered")
    decision = canonical_by_type("decision.recorded")
    register_artifacts(harness)
    harness.post_raw(canonical_raw(0))
    harness.post_raw(canonical_raw(1))
    harness.post_raw(canonical_raw(2))
    assert harness.post_raw(reformatted(decision)).status_code == 201

    expected = validate_envelope_json(json.dumps(decision)).model_dump(mode="json")["payload"]
    with harness.engine.connect() as conn:
        row = conn.execute(
            select(events.c.payload, events.c.occurred_at).where(
                events.c.event_id == decision["event_id"]
            )
        ).one()
    assert row.payload.encode("utf-8") == canonical_bytes(expected)
    assert row.occurred_at.endswith("Z") and "." in row.occurred_at
    assert discovery["account_ref"] == decision["account_ref"]
