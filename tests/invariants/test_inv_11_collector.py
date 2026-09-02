"""INV-11 exercised generatively: identity, idempotency, rejection, atomicity."""

import copy
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.conftest import Harness, reformatted

pytestmark = pytest.mark.invariant

OFFSETS = [timezone(timedelta(hours=h)) for h in (-8, -5, 0, 1, 2, 5, 9)]
IDENT = st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=12)
TEXT = st.text(min_size=1, max_size=20).filter(lambda s: s.strip() == s and s)
BASE = datetime(2026, 4, 17, 10, 0, tzinfo=UTC)


@st.composite
def instants(draw):
    occurred = BASE + timedelta(seconds=draw(st.integers(0, 10_000_000)))
    recorded = occurred + timedelta(seconds=draw(st.integers(0, 3600)))
    tz_o, tz_r = draw(st.sampled_from(OFFSETS)), draw(st.sampled_from(OFFSETS))
    return occurred.astimezone(tz_o).isoformat(), recorded.astimezone(tz_r).isoformat()


@st.composite
def envelope_pairs(draw):
    """A discovery envelope plus one follow-on envelope for the same account."""
    account_ref = "acct-" + draw(IDENT)
    occ, rec = draw(instants())
    discovery = {
        "schema_version": "1",
        "event_id": "evt-disc-" + draw(IDENT),
        "event_type": "account.discovered",
        "source": draw(TEXT),
        "account_ref": account_ref,
        "occurred_at": occ,
        "recorded_at": rec,
        "payload": {"name": draw(TEXT), "domain": draw(TEXT)},
    }
    kind = draw(st.sampled_from(["evidence", "persona", "action", "outcome"]))
    occ2, rec2 = draw(instants())
    follow = {
        "schema_version": "1",
        "event_id": "evt-follow-" + draw(IDENT),
        "source": draw(TEXT),
        "account_ref": account_ref,
        "occurred_at": occ2,
        "recorded_at": rec2,
    }
    if kind == "evidence":
        follow["event_type"] = "evidence.recorded"
        follow["payload"] = {
            "items": [
                {
                    "evidence_version_id": "ev-" + draw(IDENT),
                    "evidence_type": "employee_count",
                    "value": draw(st.integers(0, 100_000)),
                }
            ]
        }
    elif kind == "persona":
        follow["event_type"] = "persona.selected"
        follow["payload"] = {"persona": draw(TEXT), "decision_event_id": "evt-" + draw(IDENT)}
    elif kind == "action":
        follow["event_type"] = "action.recorded"
        follow["payload"] = {
            "action_type": "outbound_play",
            "play_id": draw(st.integers(1, 999)),
            "target_persona": draw(TEXT),
            "status": draw(st.sampled_from(["sent", "completed", "failed"])),
            "cost": f"{draw(st.integers(0, 99999)) / 100:.2f}",
            "currency": "USD",
            "decision_event_id": "evt-" + draw(IDENT),
        }
    else:
        follow["event_type"] = "outcome.evaluated"
        follow["payload"] = {
            "window_days": draw(st.integers(1, 365)),
            "reply": draw(st.booleans()),
            "meeting": draw(st.booleans()),
            "opportunity": draw(st.booleans()),
            "action_event_id": "evt-" + draw(IDENT),
        }
    return discovery, follow


def _integer_field(follow: dict) -> tuple[dict, str]:
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


def _store_pair(tmp_path_factory, pair):
    harness = Harness(tmp_path_factory.mktemp("inv11"))
    discovery, follow = pair
    assert harness.post(discovery).status_code == 201
    assert harness.post(follow).status_code == 201
    return harness


@given(envelope_pairs())
def test_idempotent_under_key_permutation_and_whitespace(tmp_path_factory, pair):
    harness = _store_pair(tmp_path_factory, pair)
    for env in pair:
        raw = reformatted(env)
        assert raw != json.dumps(env).encode()
        response = harness.post_raw(raw)
        assert response.status_code == 200 and response.json()["status"] == "duplicate"
    assert harness.event_count() == 2


@given(envelope_pairs(), st.sampled_from(["source", "occurred_at", "payload", "account"]))
def test_content_changing_mutation_conflicts(tmp_path_factory, pair, which):
    harness = _store_pair(tmp_path_factory, pair)
    discovery, follow = pair
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
        mutated = copy.deepcopy(discovery)
        mutated["payload"]["name"] = discovery["payload"]["name"] + "x"
    before = harness.snapshot()
    response = harness.post(mutated)
    assert response.status_code == 409
    assert response.json()["reason"] == "event_id_reused_with_different_content"
    assert harness.snapshot() == before


@given(envelope_pairs(), st.sampled_from([-11, -3, 0, 4, 13]))
def test_same_instant_in_other_offset_is_duplicate(tmp_path_factory, pair, hours):
    harness = _store_pair(tmp_path_factory, pair)
    _, follow = pair
    rewritten = copy.deepcopy(follow)
    rewritten["occurred_at"] = _shift_offset(follow["occurred_at"], hours)
    rewritten["recorded_at"] = _shift_offset(follow["recorded_at"], -hours)
    response = harness.post_raw(reformatted(rewritten))
    assert response.status_code == 200 and response.json()["status"] == "duplicate"
    assert harness.event_count() == 2


@given(envelope_pairs(), st.sampled_from(["occurred_at", "recorded_at"]))
def test_naive_timestamps_are_rejected(tmp_path_factory, pair, field):
    harness = Harness(tmp_path_factory.mktemp("inv11"))
    discovery, _ = pair
    naive = copy.deepcopy(discovery)
    naive[field] = datetime.fromisoformat(discovery[field]).replace(tzinfo=None).isoformat()
    response = harness.post(naive)
    assert response.status_code == 422
    assert harness.snapshot() == (0, [])


@given(envelope_pairs().filter(lambda p: _integer_field(p[1])[0] is not None))
def test_floats_in_integer_fields_are_rejected(tmp_path_factory, pair):
    harness = Harness(tmp_path_factory.mktemp("inv11"))
    discovery, follow = pair
    assert harness.post(discovery).status_code == 201
    before = harness.snapshot()
    container, key = _integer_field(follow)
    container[key] = float(container[key])
    raw = json.dumps(follow)
    assert f'{key}": {container[key]}' in raw or ".0" in raw
    response = harness.post_raw(raw)
    assert response.status_code == 422
    assert harness.snapshot() == before


class _Boom(RuntimeError):
    pass


@given(envelope_pairs())
def test_failure_before_commit_leaves_zero_rows(tmp_path_factory, pair):
    harness = Harness(tmp_path_factory.mktemp("inv11"), raise_server_exceptions=False)
    discovery, _ = pair

    def explode(_envelope):
        raise _Boom("injected failure after all writes")

    harness.collector.before_commit = explode
    response = harness.post(discovery)
    assert response.status_code == 500
    assert harness.snapshot() == (0, [])

    harness.collector.before_commit = None
    assert harness.post(discovery).status_code == 201
    assert harness.event_count() == 1
