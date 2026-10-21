"""A frozen observation is retried exactly; current state never repairs it."""

import importlib
from datetime import datetime

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused, ObservedRead
from flight_recorder.collector.canonical import canonical_hash
from tests.adapters.crm.cases import seal
from tests.adapters.crm.test_capture_store import capture, store2, target
from tests.adapters.crm.test_oauth import configuration


def helper():
    try:
        return importlib.import_module("flight_recorder.adapters.crm.capture").capture_observation
    except (ModuleNotFoundError, AttributeError):
        pytest.fail("missing observation-to-durable-capture boundary", pytrace=False)


def read(seconds=0, count=184, **changes):
    d = capture(seconds=seconds, count=count).document
    observed = {
        k: d[k]
        for k in [
            "target",
            "properties",
            "unavailable_fields",
            "read_started_at",
            "read_finished_at",
            "source_updated_at",
            "precondition",
            "request_id",
            "response_body_sha256",
            "observation_origin",
        ]
    }
    observed.update(changes)
    return ObservedRead.from_document(observed)


def config(**updates):
    return configuration(artifact_hash=capture().document["rule_hash"], **updates)


def freeze(store, owner, observation=None, cfg=None, selection="test-selection-v1", clock=None):
    observation = observation or read()
    return helper()(
        store,
        owner,
        observation,
        cfg or config(),
        selection_identity=selection,
        clock=clock
        or (lambda: datetime.fromisoformat(capture(seconds=100).document["capture_boundary"])),
    )


def test_helper_seals_original_read_and_independently_derived_identity(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    observed = read()
    result = freeze(store, owner, observed)
    assert result.state == "captured"
    cap = result.capture
    d = cap.document
    assert {k: d[k] for k in observed.document} == observed.document
    assert d["connection_id"] == config().connection_id
    assert d["ownership_generation"] == owner.generation and d["capture_sequence"] == 1
    assert d["unit_id"] == seal(d.copy())["unit_id"]
    assert store.load_capture(cap.unit_id) == cap
    assert store.observation_count(target()) == 1
    store.close()


def test_exact_retry_returns_frozen_bytes_without_consulting_clock_even_after_restart(tmp_path):
    path = tmp_path / "store.sqlite"
    store = store2(path)
    owner = store.acquire(target(), "a" * 64)
    first = freeze(store, owner)
    state = store.polling_state(target())
    store.close()
    resumed = store2(path)

    def broken_clock():
        pytest.fail("a frozen retry consulted the current clock")

    again = freeze(resumed, owner, clock=broken_clock)
    assert again.capture.canonical_bytes == first.capture.canonical_bytes
    assert resumed.polling_state(target()) == state
    assert resumed.observation_count(target()) == 1
    resumed.close()


@pytest.mark.parametrize("change", ["read", "config", "selection"])
def test_changed_retry_binding_refuses_without_clock_or_any_persisted_change(tmp_path, change):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    first = freeze(store, owner)
    state = store.polling_state(target())

    def forbidden():
        pytest.fail("changed frozen retry consulted clock")

    with pytest.raises(CRMRefused, match="capture_claim_conflict"):
        freeze(
            store,
            owner,
            read(seconds=10) if change == "read" else read(),
            cfg=config(refresh_rotation="nonrotating") if change == "config" else config(),
            selection="different-selection" if change == "selection" else "test-selection-v1",
            clock=forbidden,
        )
    assert store.polling_state(target()) == state
    assert store.load_capture(first.capture.unit_id) == first.capture
    assert store.observation_count(target()) == 1
    store.close()


def test_source_and_display_changes_record_observations_without_new_capture(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    original = None
    for index in range(3):
        observed = read(seconds=index * 10)
        if index == 2:
            d = observed.document
            d["properties"]["Name"] = "Fictional renamed company"
            d["response_body_sha256"] = canonical_hash(d["properties"])
            observed = ObservedRead.from_document(d)
        owner = store.acquire(target(), str(index + 1) * 64)
        result = freeze(store, owner, observed)
        if index == 0:
            original = result.capture
            assert result.state == "captured"
        else:
            assert result.state == "scoring_inputs_unchanged" and result.capture is None
        store.release(owner)
    assert store.observation_count(target()) == 3
    assert store.polling_state(target())["capture_sequence"] == 1
    assert store.load_capture(original.unit_id) == original
    store.close()


def test_a_b_a_observed_inputs_create_three_captures(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    units = []
    for index, count in enumerate([184, 185, 184]):
        owner = store.acquire(target(), str(index + 1) * 64)
        result = freeze(store, owner, read(seconds=index * 10, count=count))
        assert result.state == "captured"
        assert result.capture.document["capture_sequence"] == index + 1
        units.append(result.capture.unit_id)
        store.release(owner)
    assert len(set(units)) == 3
    assert store.polling_state(target())["capture_sequence"] == 3
    store.close()


def test_changed_processing_identity_is_not_skipped_for_equal_inputs(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    first = freeze(store, owner)
    store.release(owner)
    owner = store.acquire(target(), "b" * 64)
    second = freeze(store, owner, read(seconds=10), selection="new-selection")
    assert second.state == "captured"
    assert (
        first.capture.document["processing_version"]
        != second.capture.document["processing_version"]
    )
    assert store.polling_state(target())["capture_sequence"] == 2
    store.close()


def test_no_available_input_is_preserved_for_actual_artifact_usability_check(tmp_path):
    observed = read().document
    observed["properties"] = {
        k: v
        for k, v in observed["properties"].items()
        if k in ["Id", "Name", "Website", "LastModifiedDate"]
    }
    observed["response_body_sha256"] = canonical_hash(observed["properties"])
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    result = freeze(store, owner, ObservedRead.from_document(observed))
    assert result.state == "captured"
    assert {entry["state"] for entry in result.capture.document["inputs"].values()} == {"absent"}
    store.close()


@pytest.mark.parametrize("precondition", [None, 'W/"weak"'])
def test_missing_or_weak_source_version_does_not_invent_write_qualification(tmp_path, precondition):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    result = freeze(store, owner, read(precondition=precondition))
    assert result.capture.document["precondition"] == precondition
    assert "write_enabled" not in result.capture.document
    store.close()


def test_backward_capture_clock_refuses_without_promotion_or_facts(tmp_path):
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    with pytest.raises(CRMRefused):
        freeze(
            store, owner, clock=lambda: datetime.fromisoformat(read().document["read_started_at"])
        )
    assert store.polling_state(target())["capture_sequence"] == 0
    assert store.observation_count(target()) == 0
    store.close()


def test_lossless_numbers_null_and_absent_inputs_remain_distinct_after_freeze(tmp_path):
    d = read().document
    d["properties"]["NumberOfEmployees"] = {"json_number": "1.84e2"}
    d["properties"]["FR_Funding_Stage__c"] = None
    d["properties"]["FR_Funding_Observed_On__c"] = None
    del d["properties"]["BillingCountry"]
    d["response_body_sha256"] = canonical_hash(d["properties"])
    observed = ObservedRead.from_document(d)
    store = store2(tmp_path / "store.sqlite")
    owner = store.acquire(target(), "a" * 64)
    cap = freeze(store, owner, observed).capture.document
    assert cap["properties"]["NumberOfEmployees"] == {"json_number": "1.84e2"}
    assert cap["inputs"]["employee_count"]["value"] == 184
    assert cap["inputs"]["funding_event"]["state"] == "unavailable"
    assert cap["inputs"]["headquarters_country"]["state"] == "absent"
    assert cap["response_body_sha256"] == observed.response_body_sha256
    store.close()


def test_unchanged_observation_restarts_and_retries_without_clock(tmp_path):
    path = tmp_path / "store.sqlite"
    store = store2(path)
    first = store.acquire(target(), "a" * 64)
    freeze(store, first)
    store.release(first)
    second = store.acquire(target(), "b" * 64)
    observed = read(seconds=10)
    result = freeze(store, second, observed)
    assert result.state == "scoring_inputs_unchanged"
    state = store.polling_state(target())
    store.close()
    resumed = store2(path)

    def forbidden():
        pytest.fail("unchanged retry consulted clock")

    assert freeze(resumed, second, observed, clock=forbidden) == result
    assert resumed.polling_state(target()) == state and resumed.observation_count(target()) == 2
    with pytest.raises(CRMRefused, match="capture_claim_conflict"):
        freeze(resumed, second, read(seconds=20), clock=forbidden)
    resumed.close()


@pytest.mark.parametrize("count", [184, 185])
@pytest.mark.parametrize("clock_kind", ["future_read", "naive", "invalid"])
def test_fresh_read_requires_valid_current_clock_before_any_observation_write(
    tmp_path, count, clock_kind
):
    import sqlite3

    store = store2(tmp_path / "store.sqlite")
    first = store.acquire(target(), "a" * 64)
    freeze(store, first)
    store.release(first)
    second = store.acquire(target(), "b" * 64)
    before = store.polling_state(target())
    with sqlite3.connect(store.path) as conn:
        counts = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in [
                "crm_frozen_captures",
                "crm_read_observations",
                "crm_source_versions",
                "crm_polling_state",
            ]
        }
    now = datetime.fromisoformat(capture().document["capture_boundary"])
    if clock_kind == "naive":
        now = now.replace(tzinfo=None)
    if clock_kind == "invalid":
        now = "not-a-clock"
    calls = []

    def clock():
        calls.append("called")
        return now

    with pytest.raises(CRMRefused):
        freeze(store, second, read(seconds=20, count=count), clock=clock)
    assert calls == ["called"]
    assert store.polling_state(target()) == before
    with sqlite3.connect(store.path) as conn:
        assert {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in counts
        } == counts
    store.close()


@pytest.mark.parametrize("count", [184, 185])
def test_each_fresh_capture_or_unchanged_observation_uses_one_clock_value(tmp_path, count):
    store = store2(tmp_path / "store.sqlite")
    first = store.acquire(target(), "a" * 64)
    freeze(store, first)
    store.release(first)
    second = store.acquire(target(), "b" * 64)
    calls = []

    def clock():
        calls.append("called")
        return datetime.fromisoformat(capture(seconds=100).document["capture_boundary"])

    result = freeze(store, second, read(seconds=20, count=count), clock=clock)
    assert calls == ["called"]
    assert result.state == ("captured" if count == 185 else "scoring_inputs_unchanged")
    store.close()
