"""INV-11 exercised generatively: identity, idempotency, rejection, atomicity.

Each example builds a complete, valid chain — logic artifact, discovery,
evidence, decision, persona, action, outcome — because Phase 2A validates every
cross-event reference at ingest. One event of the chain is then the subject of
the identity and idempotency assertions.
"""

import copy
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF
from tests.conftest import Harness, reformatted

pytestmark = pytest.mark.invariant

OFFSETS = [timezone(timedelta(hours=h)) for h in (-8, -5, 0, 1, 2, 5, 9)]
IDENT = st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=12)
TEXT = st.text(min_size=1, max_size=20).filter(lambda s: s.strip() == s and s)
BASE = datetime(2026, 4, 17, 10, 0, tzinfo=UTC)

#: Chain positions whose envelope may be the subject of an identity assertion.
#: The decision is excluded: its boundary must equal `occurred_at`, so the
#: "changed field" mutation below would make it invalid rather than conflicting.
FOLLOW_KINDS = ("evidence", "persona", "action", "outcome")


@st.composite
def chains(draw):
    """A valid canonical-shaped chain, plus the kind of event under test."""
    account_ref = "acct-" + draw(IDENT)
    suffix = draw(IDENT)
    follow_kind = draw(st.sampled_from(FOLLOW_KINDS))

    # Six strictly increasing instants, each far enough apart that any
    # envelope's recorded_at still precedes the next envelope's occurrence.
    start = BASE + timedelta(seconds=draw(st.integers(0, 10_000_000)))
    steps = [draw(st.integers(3601, 7200)) for _ in range(5)]
    occurred = [start]
    for step in steps:
        occurred.append(occurred[-1] + timedelta(seconds=step))
    recorded = [t + timedelta(seconds=draw(st.integers(0, 3600))) for t in occurred]

    def stamps(index):
        return {
            "occurred_at": occurred[index].astimezone(draw(st.sampled_from(OFFSETS))).isoformat(),
            "recorded_at": recorded[index].astimezone(draw(st.sampled_from(OFFSETS))).isoformat(),
        }

    artifact = {
        "artifact_schema_version": "1",
        "artifact_id": "logic-" + suffix,
        "logic_version": "v" + draw(IDENT),
        "decision_class": "account_prioritization",
        "evaluator_version": "evaluator-v1",
        "factors": [
            {
                "key": "employee_count",
                "rule": draw(TEXT),
                "weight": draw(st.integers(-50, 50)),
            }
        ],
        "missing_value_behavior": "no_match_contributes_zero",
        "threshold": draw(st.integers(0, 100)),
        "output_mapping": {
            "at_or_above_threshold": "PRIORITIZE",
            "below_threshold": "DO_NOT_PRIORITIZE",
        },
        # Already in the persisted timestamp form, so the artifact the collector
        # normalizes hashes identically to the one submitted here.
        "activation": {
            "activated_at": format_utc(BASE - timedelta(days=draw(st.integers(1, 400)))),
            "deactivated_at": None,
            "status": "current",
        },
    }

    registration = {
        "schema_version": "1",
        "event_id": "evt-logic-" + suffix,
        "event_type": "logic_artifact.registered",
        "source": draw(TEXT),
        "account_ref": SYSTEM_ACCOUNT_REF,
        "payload": {"artifact": artifact},
        **stamps(0),
    }
    discovery = {
        "schema_version": "1",
        "event_id": "evt-disc-" + suffix,
        "event_type": "account.discovered",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {"name": draw(TEXT), "domain": draw(TEXT)},
        **stamps(1),
    }
    employee_count = draw(st.integers(0, 100_000))
    evidence_id = "ev-employee-count-" + suffix
    evidence = {
        "schema_version": "1",
        "event_id": "evt-ev-" + suffix,
        "event_type": "evidence.recorded",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {
            "items": [
                {
                    "evidence_version_id": evidence_id,
                    "evidence_type": "employee_count",
                    "value": employee_count,
                }
            ]
        },
        **stamps(2),
    }
    decision_stamps = stamps(3)
    decision = {
        "schema_version": "1",
        "event_id": "evt-dec-" + suffix,
        "event_type": "decision.recorded",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {
            "decision_class": "account_prioritization",
            "decision_boundary": decision_stamps["occurred_at"],
            "workflow_version": "v" + draw(IDENT),
            "historical_context": [
                {
                    "input_key": "employee_count",
                    "value": employee_count,
                    "availability": "available",
                    "evidence_version_id": evidence_id,
                }
            ],
            "consumed_inputs": [
                {
                    "input_key": "employee_count",
                    "value": employee_count,
                    "evidence_version_id": evidence_id,
                    "contribution": artifact["factors"][0]["weight"],
                }
            ],
            "logic_artifact": {
                "logic_version": artifact["logic_version"],
                "artifact_id": artifact["artifact_id"],
                "artifact_hash": canonical_hash(artifact),
                "evaluator_version": artifact["evaluator_version"],
            },
            "result": {
                "score": draw(st.integers(0, 100)),
                "threshold": artifact["threshold"],
                "output": draw(st.sampled_from(["PRIORITIZE", "DO_NOT_PRIORITIZE"])),
            },
        },
        **decision_stamps,
    }
    persona = {
        "schema_version": "1",
        "event_id": "evt-persona-" + suffix,
        "event_type": "persona.selected",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {"persona": draw(TEXT), "decision_event_id": decision["event_id"]},
        **stamps(4),
    }
    action = {
        "schema_version": "1",
        "event_id": "evt-action-" + suffix,
        "event_type": "action.recorded",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {
            "action_type": "outbound_play",
            "play_id": draw(st.integers(1, 999)),
            "target_persona": draw(TEXT),
            "status": draw(st.sampled_from(["sent", "completed", "failed"])),
            "cost": f"{draw(st.integers(0, 99999)) / 100:.2f}",
            "currency": "USD",
            "decision_event_id": decision["event_id"],
        },
        **stamps(4),
    }
    outcome = {
        "schema_version": "1",
        "event_id": "evt-outcome-" + suffix,
        "event_type": "outcome.evaluated",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "payload": {
            "window_days": draw(st.integers(1, 365)),
            "reply": draw(st.booleans()),
            "meeting": draw(st.booleans()),
            "opportunity": draw(st.booleans()),
            "action_event_id": action["event_id"],
        },
        **stamps(5),
    }

    chain = [registration, discovery, evidence, decision, persona, action, outcome]
    follow = {"evidence": evidence, "persona": persona, "action": action, "outcome": outcome}[
        follow_kind
    ]
    return chain, follow


def _integer_field(follow: dict) -> tuple[dict | None, str]:
    """The (container, key) of an integer field in the follow-on payload."""
    payload = follow["payload"]
    match follow["event_type"]:
        case "evidence.recorded":
            return payload["items"][0], "value"
        case "action.recorded":
            return payload, "play_id"
        case "outcome.evaluated":
            return payload, "window_days"
    return None, ""


def _shift_offset(text: str, hours: int) -> str:
    """Same instant, rendered in another UTC offset."""
    return datetime.fromisoformat(text).astimezone(timezone(timedelta(hours=hours))).isoformat()


def _store_chain(tmp_path_factory, chain) -> Harness:
    harness = Harness(tmp_path_factory.mktemp("inv11"))
    for envelope in chain:
        response = harness.post(envelope)
        assert response.status_code == 201, (envelope["event_type"], response.json())
    return harness


@given(chains())
def test_idempotent_under_key_permutation_and_whitespace(tmp_path_factory, case):
    chain, _ = case
    harness = _store_chain(tmp_path_factory, chain)
    before = harness.snapshot()
    for envelope in chain:
        raw = reformatted(envelope)
        assert raw != json.dumps(envelope).encode()
        response = harness.post_raw(raw)
        assert response.status_code == 200 and response.json()["status"] == "duplicate"
    assert harness.event_count() == len(chain)
    assert harness.snapshot() == before


@given(chains(), st.sampled_from(["source", "occurred_at", "payload", "account"]))
def test_content_changing_mutation_conflicts(tmp_path_factory, case, which):
    chain, follow = case
    harness = _store_chain(tmp_path_factory, chain)
    mutated = copy.deepcopy(follow)
    if which == "source":
        mutated["source"] = follow["source"] + "x"
    elif which == "occurred_at":
        earlier = datetime.fromisoformat(follow["occurred_at"]) - timedelta(seconds=1)
        mutated["occurred_at"] = earlier.isoformat()
    elif which == "payload":
        container, key = _integer_field(mutated)
        if container is None:
            mutated["payload"]["persona"] = follow["payload"]["persona"] + "x"
        else:
            container[key] = container[key] + 1
    else:
        mutated = copy.deepcopy(chain[1])
        mutated["payload"]["name"] = chain[1]["payload"]["name"] + "x"
    before = harness.snapshot()
    response = harness.post(mutated)
    assert response.status_code == 409
    assert response.json()["reason"] == "event_id_reused_with_different_content"
    assert harness.snapshot() == before


@given(chains(), st.sampled_from([-11, -3, 0, 4, 13]))
def test_same_instant_in_other_offset_is_duplicate(tmp_path_factory, case, hours):
    chain, follow = case
    harness = _store_chain(tmp_path_factory, chain)
    rewritten = copy.deepcopy(follow)
    rewritten["occurred_at"] = _shift_offset(follow["occurred_at"], hours)
    rewritten["recorded_at"] = _shift_offset(follow["recorded_at"], -hours)
    response = harness.post_raw(reformatted(rewritten))
    assert response.status_code == 200 and response.json()["status"] == "duplicate"
    assert harness.event_count() == len(chain)


@given(chains(), st.sampled_from(["occurred_at", "recorded_at"]))
def test_naive_timestamps_are_rejected(tmp_path_factory, case, field):
    harness = Harness(tmp_path_factory.mktemp("inv11"))
    chain, _ = case
    naive = copy.deepcopy(chain[1])
    naive[field] = datetime.fromisoformat(chain[1][field]).replace(tzinfo=None).isoformat()
    response = harness.post(naive)
    assert response.status_code == 422
    assert harness.is_empty()


@given(chains().filter(lambda case: _integer_field(case[1])[0] is not None))
def test_floats_in_integer_fields_are_rejected(tmp_path_factory, case):
    chain, follow = case
    harness = _store_chain(tmp_path_factory, chain[: chain.index(follow)])
    before = harness.snapshot()
    mutated = copy.deepcopy(follow)
    container, key = _integer_field(mutated)
    container[key] = float(container[key])
    raw = json.dumps(mutated)
    assert f'{key}": {container[key]}' in raw or ".0" in raw
    response = harness.post_raw(raw)
    assert response.status_code == 422
    assert harness.snapshot() == before


class _Boom(RuntimeError):
    pass


def _explode(_envelope):
    raise _Boom("injected failure after all writes")


@given(chains())
def test_failure_before_commit_leaves_zero_rows(tmp_path_factory, case):
    """The first event of a chain fails mid-transaction: nothing anywhere."""
    harness = Harness(tmp_path_factory.mktemp("inv11"), raise_server_exceptions=False)
    chain, _ = case

    harness.collector.before_commit = _explode
    assert harness.post(chain[0]).status_code == 500
    assert harness.is_empty()

    harness.collector.before_commit = None
    assert harness.post(chain[0]).status_code == 201
    assert harness.event_count() == 1


@given(chains())
def test_failure_before_commit_leaves_every_projection_table_unchanged(tmp_path_factory, case):
    """A chain is stored, then its last event fails mid-transaction: no partial
    projection survives, in `events` or in any table of PRODUCT.md §6."""
    harness = Harness(tmp_path_factory.mktemp("inv11"), raise_server_exceptions=False)
    chain, _ = case
    for envelope in chain[:-1]:
        assert harness.post(envelope).status_code == 201
    before = harness.snapshot()

    harness.collector.before_commit = _explode
    assert harness.post(chain[-1]).status_code == 500
    assert harness.snapshot() == before

    harness.collector.before_commit = None
    assert harness.post(chain[-1]).status_code == 201
    assert harness.event_count() == len(chain)
