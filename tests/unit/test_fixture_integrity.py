"""The canonical fixture is internally consistent (D-004, INV-04 identities)."""

import json
from datetime import datetime, timedelta

from flight_recorder.collector.schema import validate_envelope_json
from flight_recorder.fixtures import canonical_envelope_paths, load_json
from tests.conftest import canonical_envelopes, logic_artifact


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _events():
    return canonical_envelopes()


def _by_type(event_type):
    return [e for e in _events() if e["event_type"] == event_type]


def _evidence_index() -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for env in _by_type("evidence.recorded"):
        for item in env["payload"]["items"]:
            index.setdefault(item["evidence_version_id"], []).append(env)
    return index


def test_fixture_files_parse_and_validate_strictly():
    for path in canonical_envelope_paths():
        validate_envelope_json(path.read_bytes())


def test_seven_envelopes_in_chronological_file_order():
    envs = _events()
    assert len(envs) == 7
    occurred = [_ts(e["occurred_at"]) for e in envs]
    assert occurred == sorted(occurred)
    assert [e["event_type"] for e in envs] == [
        "account.discovered",
        "evidence.recorded",
        "evidence.recorded",
        "decision.recorded",
        "persona.selected",
        "action.recorded",
        "outcome.evaluated",
    ]


def test_recorded_at_is_explicit_and_equals_occurred_at():
    for env in _events():
        assert "recorded_at" in env
        assert env["recorded_at"] == env["occurred_at"]


def test_every_evidence_reference_resolves_to_one_earlier_item():
    decision = _by_type("decision.recorded")[0]
    boundary = _ts(decision["occurred_at"])
    index = _evidence_index()
    refs = [
        entry["evidence_version_id"]
        for entry in decision["payload"]["historical_context"]
        if entry["availability"] == "available"
    ] + [ci["evidence_version_id"] for ci in decision["payload"]["consumed_inputs"]]
    assert refs
    for ref in refs:
        owners = index[ref]
        assert len(owners) == 1, ref
        assert _ts(owners[0]["occurred_at"]) < boundary


def test_context_entries_match_their_availability():
    decision = _by_type("decision.recorded")[0]
    context = decision["payload"]["historical_context"]
    unavailable = [e for e in context if e["availability"] == "unavailable"]
    assert unavailable, "fixture must include an explicitly unavailable input"
    for entry in context:
        if entry["availability"] == "available":
            assert entry["evidence_version_id"] in _evidence_index()
        else:
            assert "evidence_version_id" not in entry
            assert not any("evidence" in k for k in entry if k != "input_key")


def test_consumed_inputs_reference_the_same_evidence_as_context():
    decision = _by_type("decision.recorded")[0]
    context = {e["input_key"]: e for e in decision["payload"]["historical_context"]}
    for ci in decision["payload"]["consumed_inputs"]:
        assert context[ci["input_key"]]["evidence_version_id"] == ci["evidence_version_id"]
        assert context[ci["input_key"]]["value"] == ci["value"]


def test_decision_and_action_references_resolve_to_earlier_events():
    envs = _events()
    position = {e["event_id"]: i for i, e in enumerate(envs)}
    for i, env in enumerate(envs):
        for key in ("decision_event_id", "action_event_id"):
            if key in env["payload"]:
                assert position[env["payload"][key]] < i
    assert envs[3]["event_type"] == "decision.recorded"
    assert envs[6]["payload"]["action_event_id"] == envs[5]["event_id"]


def test_contributions_sum_to_score_and_match_logic_weights():
    decision = _by_type("decision.recorded")[0]["payload"]
    total = sum(ci["contribution"] for ci in decision["consumed_inputs"])
    assert total == decision["result"]["score"]
    logic = logic_artifact(decision["logic_artifact"]["logic_version"])
    weights = {f["key"]: f["weight"] for f in logic["factors"]}
    assert {ci["input_key"]: ci["contribution"] for ci in decision["consumed_inputs"]} == weights
    assert decision["result"]["threshold"] == logic["threshold"]
    assert decision["result"]["output"] == logic["output_mapping"]["at_or_above_threshold"]
    assert "confidence" not in decision and "confidence" not in decision["result"]


def test_integration_pressure_available_but_not_consumed():
    decision = _by_type("decision.recorded")[0]["payload"]
    context_keys = {e["input_key"] for e in decision["historical_context"]}
    consumed_keys = {ci["input_key"] for ci in decision["consumed_inputs"]}
    assert "verified_integration_pressure" in context_keys
    assert "verified_integration_pressure" not in consumed_keys
    # v5.1 does consume it; the two artifacts differ in identity.
    v51 = logic_artifact("v5.1")
    assert "verified_integration_pressure" in {f["key"] for f in v51["factors"]}
    assert v51 != logic_artifact("v3.2")


def test_outcome_is_exactly_window_days_after_action():
    action = _by_type("action.recorded")[0]
    outcome = _by_type("outcome.evaluated")[0]
    window = timedelta(days=outcome["payload"]["window_days"])
    assert _ts(outcome["occurred_at"]) - _ts(action["occurred_at"]) == window


def test_example_payload_is_labeled_and_uses_a_different_account():
    from flight_recorder.fixtures import EXAMPLES_DIR

    example = load_json(EXAMPLES_DIR / "clay-http-step.json")
    assert "EXAMPLE" in example["_example"]
    body = example["request"]["body"]
    assert body["event_type"] == "decision.recorded"
    assert body["account_ref"] != _events()[0]["account_ref"]


def test_canonical_decision_satisfies_the_coherence_rules():
    """Rules 1-6 hold for the fixture, checked by the schema rather than restated here."""
    decision = _by_type("decision.recorded")[0]
    validated = validate_envelope_json(json.dumps(decision))
    assert validated.event_type == "decision.recorded"
    # The schema decided coherence; these assertions only show what it covered.
    context = validated.payload.historical_context
    consumed = validated.payload.consumed_inputs
    assert len({e.input_key for e in context}) == len(context)
    assert len({c.input_key for c in consumed}) == len(consumed)
    assert any(e.availability == "unavailable" for e in context)


def test_canonical_decision_boundary_is_the_occurrence_instant():
    decision = _by_type("decision.recorded")[0]
    assert decision["payload"]["decision_boundary"] == decision["occurred_at"]
    validated = validate_envelope_json(json.dumps(decision))
    assert validated.payload.decision_boundary == validated.occurred_at
