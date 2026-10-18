"""FR-V2-01 operational proofs; no collector/domain/integration acceptance.

Mutations caught: attempt metadata in identity, typed-value collapse, retry reads,
ownership acquired after reads, stale-fence commits, mutable capture rows, future
availability, malformed field repair, and invented evidence for missing inputs.
INV-01/02/03/04/09/11; S02/03/04/08/16/17.
"""

import importlib
import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.fixtures import canonical_artifacts

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)  # Local synthetic test time, not demo data.


def api():
    try:
        return {
            name: importlib.import_module(f"flight_recorder.adapters.hubspot.{name}")
            for name in ("contracts", "identity", "store", "capture")
        }
    except ModuleNotFoundError as error:
        pytest.fail(f"FR-V2-01 operational capture API is not implemented: {error}")


def payload(**changes):
    return (
        dict(
            portalId=71,
            appId=72,
            subscriptionId=73,
            subscriptionType="company.propertyChange",
            objectId=74,
            propertyName="fr_integration_pressure",
            propertyValue="MEDIUM",
            occurredAt=1760000000000,
            eventId="synthetic-event",
            changeSource="SYNTHETIC",
            **changes,
        )
        if not changes
        else {**payload(), **changes}
    )


def notification(a, body=None, raw=None):
    body = payload() if body is None else body
    return a["contracts"].SourceNotification.from_payload(
        body, raw_body=json.dumps(body).encode() if raw is None else raw
    )


def unit(a, body=None, **changes):
    fields = dict(
        notification=notification(a, body),
        mapping_identity=a["capture"].MAPPING_IDENTITY,
        selection_identity=canonical_hash({"selection": "supplied-rule-only"}),
        adapter_identity="local-capture-test-v1",
        reprocesses=None,
    )
    return a["contracts"].ProcessingUnit(**(fields | changes))


def rule(a):
    artifact = next(iter(canonical_artifacts().values()))
    return a["contracts"].RuleIdentity(
        canonical_hash(artifact.model_dump(mode="json")), artifact.evaluator_version
    )


def observation(a, *, properties=None, **kw):
    return a["contracts"].ReadResult(
        {"fr_integration_pressure": {"value": "MEDIUM", "basis": "Supplied synthetic basis"}}
        if properties is None
        else properties,
        **kw,
    )


def capture(a, store, processing_unit=None, *, observe=None, select_logic=None, clock=None):
    return a["capture"].capture_once(
        store,
        unit(a) if processing_unit is None else processing_unit,
        observe=(lambda owner: observation(a)) if observe is None else observe,
        select_logic=(lambda: rule(a)) if select_logic is None else select_logic,
        clock=(lambda: NOW) if clock is None else clock,
    )


def test_semantic_identity_excludes_attempt_metadata():
    a = api()
    first = notification(a)
    changed_attempt = notification(
        a, payload(attemptNumber=9, messageId="retry", messageType="delivery")
    )
    assert a["identity"].logical_key(first) == a["identity"].logical_key(changed_attempt)
    alias = payload()
    alias["eventType"] = alias.pop("subscriptionType")
    assert a["identity"].logical_key(first) == a["identity"].logical_key(notification(a, alias))
    for body in [payload(propertyValue="HIGH"), payload(objectId=75), payload(eventId="other")]:
        assert a["identity"].logical_key(first) != a["identity"].logical_key(notification(a, body))
    for body in [payload(eventType="conflict"), payload(unknownSemantic="bad")]:
        with pytest.raises(ValueError):
            notification(a, body)
    missing = payload()
    missing.pop("propertyValue")
    with pytest.raises(ValueError):
        notification(a, missing)
    with pytest.raises(ValueError):
        notification(a, payload(), raw=b'{"unrelated":true}')


@pytest.mark.invariant
@given(st.one_of(st.none(), st.booleans(), st.integers(-1000, 1000), st.text(max_size=20)))
def test_identity_preserves_json_types(value):
    a = api()
    body = payload(propertyValue=value)
    raw = json.dumps(body).encode()
    assert notification(a, body, raw).raw_body == raw
    keys = [
        a["identity"].logical_key(notification(a, payload(propertyValue=v)))
        for v in [None, False, 0, "0", True, 1, "1"]
    ]
    assert len(set(keys)) == 7


def test_retry_returns_frozen_observation_without_read(tmp_path):
    a = api()
    path = tmp_path / "capture.sqlite"
    u = unit(a)
    calls = []

    def read(owner):
        calls.append(owner)
        return observation(a)

    first = capture(a, a["store"].CaptureStore(path), u, observe=read)

    def forbidden(*args):
        pytest.fail("A captured retry must not read or reselect logic")

    second = capture(a, a["store"].CaptureStore(path), u, observe=forbidden, select_logic=forbidden)
    assert first.canonical_bytes == second.canonical_bytes
    assert len(calls) == 1
    assert second.document["rule_hash"] == rule(a).artifact_hash
    assert second.document["evaluator_version"] == rule(a).evaluator_version
    assert a["store"].CaptureStore(path).receipt_count(u) == 2


@pytest.mark.invariant
def test_same_unit_has_one_capture_winner(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    entered, release = Event(), Event()
    reads = []

    def read(owner):
        reads.append(owner)
        entered.set()
        assert release.wait(5)
        return observation(a)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(capture, a, store, u, observe=read)
        assert entered.wait(5)
        try:
            with pytest.raises(a["contracts"].CaptureRefused, match="busy"):
                capture(
                    a,
                    a["store"].CaptureStore(store.path),
                    u,
                    observe=lambda owner: pytest.fail("Concurrent duplicate performed a read"),
                )
        finally:
            release.set()
        first = pending.result(timeout=5)
    again = capture(a, store, u, observe=lambda owner: pytest.fail("Duplicate performed a read"))
    assert first.canonical_bytes == again.canonical_bytes
    assert first.document["capture_sequence"] == 1 and len(reads) == 1


@pytest.mark.invariant
def test_distinct_units_share_company_read_ownership(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u1, u2 = unit(a), unit(a, payload(eventId="second-event"))
    entered, release = Event(), Event()
    order = []

    def read_a(owner):
        order.append("A")
        entered.set()
        assert release.wait(5)
        return observation(a)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(capture, a, store, u1, observe=read_a)
        assert entered.wait(5)
        try:
            with pytest.raises(a["contracts"].CaptureRefused, match="busy"):
                capture(
                    a,
                    a["store"].CaptureStore(store.path),
                    u2,
                    observe=lambda owner: pytest.fail("B read while A owned the company"),
                )
        finally:
            release.set()
        first = pending.result(timeout=5)

    def read_b(owner):
        order.append("B")
        return observation(a)

    second = capture(a, store, u2, observe=read_b)
    assert order == ["A", "B"]
    assert [first.document["capture_sequence"], second.document["capture_sequence"]] == [1, 2]


@pytest.mark.invariant
def test_abandoned_generation_cannot_freeze(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    entered, release = Event(), Event()
    owners = []

    def stale_read(owner):
        owners.append(owner)
        entered.set()
        assert release.wait(5)
        return observation(a)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(capture, a, store, u, observe=stale_read)
        assert entered.wait(5)
        try:
            with pytest.raises(a["contracts"].CaptureRefused):
                store.abandon_capture(owners[0], known_abandoned=False)
            store.abandon_capture(owners[0], known_abandoned=True)
            winner = capture(a, store, u)
        finally:
            release.set()
        with pytest.raises(a["contracts"].CaptureRefused, match="stale|fenc"):
            pending.result(timeout=5)
    assert store.load_capture(u).canonical_bytes == winner.canonical_bytes
    assert winner.document["capture_sequence"] == 1


def test_restart_before_and_after_freeze(tmp_path):
    a = api()
    path = tmp_path / "capture.sqlite"
    store = a["store"].CaptureStore(path)
    u = unit(a)
    owners = []

    def crash(owner):
        owners.append(owner)
        raise RuntimeError("synthetic crash before durable freeze")

    with pytest.raises(RuntimeError, match="synthetic crash"):
        capture(a, store, u, observe=crash)
    restarted = a["store"].CaptureStore(path)
    assert restarted.load_capture(u) is None
    with pytest.raises(a["contracts"].CaptureRefused, match="busy"):
        capture(a, restarted, u)
    restarted.abandon_capture(owners[0], known_abandoned=True)
    first = capture(a, restarted, u)
    after_lost_ack = capture(
        a,
        a["store"].CaptureStore(path),
        u,
        observe=lambda owner: pytest.fail("Read after durable capture"),
    )
    assert after_lost_ack.canonical_bytes == first.canonical_bytes


@pytest.mark.invariant
def test_capture_content_cannot_be_updated_or_deleted(tmp_path):
    a = api()
    path = tmp_path / "capture.sqlite"
    store = a["store"].CaptureStore(path)
    u = unit(a)
    first = capture(a, store, u)
    before = first.canonical_bytes
    document = first.document
    document["inputs"].clear()
    assert first.canonical_bytes == before
    for command in ["UPDATE captures SET body_json = body_json", "DELETE FROM captures"]:
        with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError):
            db.execute(command)
    assert store.load_capture(u).canonical_bytes == before
    assert first.document["raw_notification_ref"]
    assert store.load_raw(first.document["raw_notification_ref"]) == u.notification.raw_body
    assert first.document["mapping_identity"] == u.mapping_identity
    assert first.document["selection_identity"] == u.selection_identity


def test_mapping_states_and_strict_fields(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    data = {
        "fr_integration_pressure": {"value": "MEDIUM", "basis": "Synthetic basis"},
        "country": "ZZ",
    }
    result = observation(
        a, properties=data, ignored=("country",), unavailable=("fr_funding_event",)
    )
    cap = capture(a, store, observe=lambda owner: result)
    states = cap.document["inputs"]
    assert states["verified_integration_pressure"]["state"] == "consumed"
    assert states["headquarters_country"]["state"] == "ignored"
    assert states["funding_event"]["state"] == "unavailable"
    assert states["industry"]["state"] == "absent"
    data["fr_integration_pressure"]["value"] = "HIGH"
    assert cap.document["inputs"]["verified_integration_pressure"]["value"]["value"] == "MEDIUM"
    invalid = [
        {"fr_integration_pressure": {"value": "EXTREME", "basis": "x"}},
        {"fr_integration_pressure": {"value": "MEDIUM"}},
        {"fr_funding_event": {"description": "bad", "observed_at": "2026-10-01"}},
        {"fr_funding_event": {"value": "bad", "observed_at": "not-a-date"}},
        {"numberofemployees": True},
        {"fr_score": 1},
    ]
    for i, props in enumerate(invalid):
        u = unit(a, payload(eventId=f"invalid-{i}"))
        with pytest.raises(a["contracts"].CaptureRefused):
            capture(
                a, store, u, observe=lambda owner, props=props: observation(a, properties=props)
            )
        assert store.load_capture(u) is None
    good = unit(a, payload(eventId="valid-funding"))
    cap = capture(
        a,
        store,
        good,
        observe=lambda owner: observation(
            a,
            properties={
                "fr_funding_event": {"value": "Synthetic funding", "observed_at": "2026-10-01"}
            },
        ),
    )
    assert cap.document["inputs"]["funding_event"]["value"]["value"] == "Synthetic funding"


@pytest.mark.parametrize("ignored", [False, True])
def test_no_usable_owned_inputs_refuses_without_decision(tmp_path, ignored):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    obs = (
        observation(a, ignored=("fr_integration_pressure",))
        if ignored
        else observation(a, properties={}, unavailable=("fr_integration_pressure",))
    )
    with pytest.raises(a["contracts"].CaptureRefused, match="no_usable_inputs"):
        capture(a, store, u, observe=lambda owner: obs)
    assert store.load_capture(u) is None
    assert "no_usable_inputs" in store.progress(u)


def test_processing_version_appends_lineage(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    old = unit(a)
    first = capture(a, store, old)
    new = unit(a, adapter_identity="local-capture-test-v2", reprocesses=first.document["unit_id"])
    second = capture(a, store, new)
    assert first.document["unit_id"] != second.document["unit_id"]
    assert second.document["reprocesses"] == first.document["unit_id"]
    assert store.load_capture(old).canonical_bytes == first.canonical_bytes
    assert second.document["capture_sequence"] == 2


@pytest.mark.invariant
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(delta=st.integers(-2, 2), offset=st.sampled_from([-7, 0, 5]))
def test_capture_boundary_before_at_after(tmp_path, delta, offset):
    from datetime import timezone

    a = api()
    available = (NOW + timedelta(microseconds=delta)).astimezone(timezone(timedelta(hours=offset)))
    with tempfile.TemporaryDirectory(dir=tmp_path) as d:
        store = a["store"].CaptureStore(f"{d}/capture.sqlite")
        obs = observation(a, available_at={"fr_integration_pressure": available})
        if delta > 0:
            with pytest.raises(a["contracts"].CaptureRefused, match="future|boundary|available"):
                capture(a, store, observe=lambda owner: obs)
        else:
            cap = capture(a, store, observe=lambda owner: obs)
            assert (
                datetime.fromisoformat(cap.document["capture_boundary"].replace("Z", "+00:00"))
                == NOW
            )


def test_identity_collision_refuses_semantic_conflict(tmp_path, monkeypatch):
    a = api()
    monkeypatch.setattr(a["identity"], "canonical_hash", lambda value: "0" * 64)
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    first = unit(a)
    capture(a, store, first)
    different = unit(a, payload(propertyValue="HIGH"))
    with pytest.raises(a["contracts"].CaptureRefused, match="identity|collision|conflict"):
        capture(a, store, different)


@pytest.mark.invariant
@pytest.mark.parametrize(
    "times",
    [
        [NOW.replace(tzinfo=None)] * 3,
        [NOW, NOW - timedelta(microseconds=1), NOW],
        [NOW, NOW, NOW - timedelta(microseconds=1)],
    ],
)
def test_invalid_read_interval_refuses_capture(tmp_path, times):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    clock_values = iter(times)
    with pytest.raises(a["contracts"].CaptureRefused, match="time|interval|boundary|aware"):
        capture(a, store, u, clock=lambda: next(clock_values))
    assert store.load_capture(u) is None


@pytest.mark.invariant
@pytest.mark.parametrize(
    "case", ["duplicate", "nan", "infinity", "overflow", "false-zero", "zero-false"]
)
def test_strict_raw_json_rejects_ambiguous_values(case):
    a = api()
    body = payload(propertyValue=False if case == "false-zero" else 0)
    raw = json.dumps(body).encode()
    if case == "duplicate":
        raw = raw[:-1] + b', "eventId": "synthetic-event"}'
    elif case in ("nan", "infinity", "overflow"):
        token = {"nan": "NaN", "infinity": "Infinity", "overflow": "1e999"}[case]
        raw = raw.replace(b'"propertyValue": 0', ('"propertyValue": ' + token).encode())
    elif case == "false-zero":
        raw = raw.replace(b'"propertyValue": false', b'"propertyValue": 0')
    else:
        raw = raw.replace(b'"propertyValue": 0', b'"propertyValue": false')
    with pytest.raises(ValueError):
        notification(a, body, raw)


def test_closed_mapping_values_and_external_observation(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    props = {
        "numberofemployees": 42,
        "fr_open_platform_roles": 3,
        "fr_head_of_platform_start": "2026-10-01",
        "industry": "COMPUTER_SOFTWARE",
        "hubspotscore": 12,
    }
    cap = capture(a, store, observe=lambda owner: observation(a, properties=props))
    inputs = cap.document["inputs"]
    for key, value in {
        "employee_count": 42,
        "open_platform_engineering_roles": 3,
        "head_of_platform_start_date": "2026-10-01",
        "industry": "software",
    }.items():
        assert inputs[key] == {"state": "consumed", "value": value}
    assert inputs["external_vendor_score"] == {"state": "ignored", "value": 12, "replayable": False}
    external = unit(a, payload(eventId="external-only"))
    with pytest.raises(a["contracts"].CaptureRefused, match="no_usable_inputs"):
        capture(
            a,
            store,
            external,
            observe=lambda owner: observation(a, properties={"hubspotscore": 12}),
        )
    assert store.load_capture(external) is None
    unknown = unit(a, payload(eventId="unmapped-industry"))
    mixed = observation(a, properties={"industry": "UNMAPPED", "numberofemployees": 0})
    cap = capture(a, store, unknown, observe=lambda owner: mixed)
    assert cap.document["inputs"]["industry"]["state"] == "unavailable"
    assert cap.document["inputs"]["employee_count"]["value"] == 0
    empty = unit(a, payload(eventId="empty-values"))
    cap = capture(
        a,
        store,
        empty,
        observe=lambda owner: observation(
            a,
            properties={
                "numberofemployees": None,
                "fr_open_platform_roles": "",
                "industry": "",
                "country": "",
                "fr_head_of_platform_start": None,
                "fr_funding_event": None,
                "fr_integration_pressure": {"value": "LOW", "basis": "Synthetic supplied basis"},
            },
        ),
    )
    for key in (
        "employee_count",
        "open_platform_engineering_roles",
        "industry",
        "headquarters_country",
        "head_of_platform_start_date",
        "funding_event",
    ):
        assert cap.document["inputs"][key]["state"] == "unavailable"


@pytest.mark.parametrize(
    "props",
    [
        {"numberofemployees": -1},
        {"numberofemployees": "42"},
        {"fr_open_platform_roles": True},
        {"fr_open_platform_roles": -1},
        {"fr_head_of_platform_start": "2026-02-30"},
        {"fr_head_of_platform_start": "2026-10-01T12:00:00Z"},
        {"unsupported_property": "x"},
        {"fr_output": "x"},
    ],
)
def test_closed_mapping_invalid_values_refuse(tmp_path, props):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    with pytest.raises(a["contracts"].CaptureRefused):
        capture(a, store, u, observe=lambda owner: observation(a, properties=props))
    assert store.load_capture(u) is None


def test_unrecognized_mapping_refuses_before_read(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a, mapping_identity="unsupported-mapping")
    with pytest.raises(a["contracts"].CaptureRefused, match="mapping"):
        capture(
            a, store, u, observe=lambda owner: pytest.fail("Unrecognized mapping performed read")
        )
    assert store.load_capture(u) is None


@pytest.mark.invariant
def test_stale_abandon_cannot_release_successor(tmp_path):
    a = api()
    store = a["store"].CaptureStore(tmp_path / "capture.sqlite")
    u = unit(a)
    old = a["store"].acquire_capture(store, u)
    store.abandon_capture(old, known_abandoned=True)
    entered, release = Event(), Event()
    owners = []

    def successor_read(owner):
        owners.append(owner)
        entered.set()
        assert release.wait(5)
        return observation(a)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(capture, a, store, u, observe=successor_read)
        assert entered.wait(5)
        try:
            before = store.progress(u)
            with pytest.raises(a["contracts"].CaptureRefused, match="stale|fenc"):
                store.abandon_capture(old, known_abandoned=True)
            assert store.progress(u) == before
            assert owners[0].generation > old.generation
            with pytest.raises(a["contracts"].CaptureRefused, match="busy"):
                capture(
                    a,
                    store,
                    unit(a, payload(eventId="third")),
                    observe=lambda owner: pytest.fail("C read while successor owned company"),
                )
        finally:
            release.set()
        winner = pending.result(timeout=5)
    assert store.load_capture(u).canonical_bytes == winner.canonical_bytes
    assert winner.document["capture_sequence"] == 1


@pytest.mark.invariant
@pytest.mark.parametrize(
    "artifact_hash",
    ["0" * 64 + "\n", "0" * 63, "0" * 65, "g" * 64, "A" * 64, "0" * 64 + "\r"],
)
def test_rule_identity_requires_exact_lowercase_sha256(artifact_hash):
    a = api()
    with pytest.raises(ValueError):
        a["contracts"].RuleIdentity(artifact_hash, "supplied-synthetic-evaluator")
