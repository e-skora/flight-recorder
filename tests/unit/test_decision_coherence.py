"""A decision envelope must not contradict itself (INV-02, INV-03, INV-04, INV-09).

Each case mutates a deep copy of the canonical decision envelope and asserts the
specific validation error. The fixture object itself is never modified.
"""

import copy
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from flight_recorder.collector.schema import validate_envelope_json
from tests.conftest import canonical_by_type

UNAVAILABLE_KEY = "website_intent"


def decision() -> dict:
    return copy.deepcopy(canonical_by_type("decision.recorded"))


def validate(envelope: dict):
    return validate_envelope_json(json.dumps(envelope))


def context_entry(envelope: dict, input_key: str) -> dict:
    return next(e for e in envelope["payload"]["historical_context"] if e["input_key"] == input_key)


def first_consumed(envelope: dict) -> dict:
    return envelope["payload"]["consumed_inputs"][0]


def test_canonical_decision_envelope_is_coherent():
    validate(decision())


# --- Rule 1: an unavailable input carries no value and no evidence ----------


def test_unavailable_input_with_a_value_is_rejected():
    env = decision()
    context_entry(env, UNAVAILABLE_KEY)["value"] = "high"
    with pytest.raises(ValidationError, match="must have a null value"):
        validate(env)


def test_unavailable_input_with_an_evidence_reference_is_rejected():
    env = decision()
    context_entry(env, UNAVAILABLE_KEY)["evidence_version_id"] = "ev-invented-v1"
    with pytest.raises(ValidationError, match="must not reference any evidence"):
        validate(env)


def test_available_input_without_an_evidence_reference_is_rejected():
    env = decision()
    entry = env["payload"]["historical_context"][0]
    entry.pop("evidence_version_id")
    with pytest.raises(ValidationError, match="must reference an evidence_version_id"):
        validate(env)


# --- Rules 2 and 3: input keys are unique on both sides ---------------------


def test_duplicate_historical_context_input_key_is_rejected():
    env = decision()
    entry = env["payload"]["historical_context"][0]
    env["payload"]["historical_context"].append(copy.deepcopy(entry))
    with pytest.raises(ValidationError, match="historical_context has more than one entry"):
        validate(env)
    with pytest.raises(ValidationError, match=repr(entry["input_key"])):
        validate(env)


def test_duplicate_consumed_input_key_is_rejected():
    env = decision()
    consumed = first_consumed(env)
    env["payload"]["consumed_inputs"].append(copy.deepcopy(consumed))
    with pytest.raises(ValidationError, match="consumed_inputs has more than one entry"):
        validate(env)
    with pytest.raises(ValidationError, match=repr(consumed["input_key"])):
        validate(env)


# --- Rule 4: every consumed input resolves to one available context entry ---


def test_consumed_input_without_a_context_entry_is_rejected():
    env = decision()
    first_consumed(env)["input_key"] = "no_such_input"
    with pytest.raises(ValidationError, match="no historical_context entry with that input_key"):
        validate(env)


def test_consuming_an_unavailable_input_is_rejected():
    env = decision()
    consumed = first_consumed(env)
    consumed["input_key"] = UNAVAILABLE_KEY
    with pytest.raises(ValidationError, match="is unavailable, so it cannot have been consumed"):
        validate(env)


# --- Rule 5: the consumed value and evidence match the context entry --------


def test_consumed_value_disagreeing_with_context_is_rejected():
    env = decision()
    consumed = first_consumed(env)
    entry = context_entry(env, consumed["input_key"])
    assert entry["value"] == consumed["value"]
    consumed["value"] = 999 if isinstance(consumed["value"], int) else "other"
    with pytest.raises(ValidationError, match="does not equal the historical_context value"):
        validate(env)


def test_consumed_value_of_a_different_type_is_rejected():
    """`True` and `1` are distinct preserved values, not the same one."""
    env = decision()
    entry = context_entry(env, "employee_count")
    consumed = next(
        c for c in env["payload"]["consumed_inputs"] if c["input_key"] == "employee_count"
    )
    entry["value"] = True
    consumed["value"] = 1
    with pytest.raises(ValidationError, match="does not equal the historical_context value"):
        validate(env)


def test_consumed_evidence_version_disagreeing_with_context_is_rejected():
    env = decision()
    first_consumed(env)["evidence_version_id"] = "ev-some-other-version-v1"
    with pytest.raises(ValidationError, match="does not equal the historical_context"):
        validate(env)


# --- Rule 6: the boundary is the occurrence instant -------------------------


def test_decision_boundary_differing_from_occurred_at_is_rejected():
    env = decision()
    env["payload"]["decision_boundary"] = "2026-04-17T10:05:03Z"
    with pytest.raises(ValidationError, match="must be the same instant as occurred_at"):
        validate(env)


def test_decision_boundary_in_another_offset_for_the_same_instant_is_accepted():
    env = decision()
    assert env["occurred_at"] == "2026-04-17T10:05:02Z"
    env["payload"]["decision_boundary"] = "2026-04-17T12:05:02+02:00"
    validated = validate(env)
    assert validated.payload.decision_boundary == validated.occurred_at


# --- Order independence -----------------------------------------------------


@given(data=st.data())
def test_a_coherent_envelope_stays_valid_under_list_permutation(data):
    env = decision()
    payload = env["payload"]
    payload["historical_context"] = data.draw(st.permutations(payload["historical_context"]))
    payload["consumed_inputs"] = data.draw(st.permutations(payload["consumed_inputs"]))
    validated = validate(env)
    assert validated.payload.result.score == decision()["payload"]["result"]["score"]
    assert len(validated.payload.consumed_inputs) == len(decision()["payload"]["consumed_inputs"])
