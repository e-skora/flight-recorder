"""New-only operational format and immutable writer value boundaries."""

import hashlib
import json
import pickle
import sqlite3

import pytest

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.store import CRMStore
from flight_recorder.collector.canonical import canonical_bytes
from tests.acceptance.test_crm_operations import intent
from tests.adapters.crm.cases import make_case
from tests.adapters.crm.test_salesforce_writer import feature


def test_intent_is_canonical_immutable_and_decodes_fresh_values():
    api = feature("writer_contracts")
    document = intent(make_case())
    frozen = api.CRMWriteIntent.from_document(document)
    before = frozen.canonical_bytes
    document["intended"]["fr_score"] += 1
    decoded = frozen.document
    decoded["target"]["record_id"] = "foreign"
    assert frozen.canonical_bytes == before
    assert frozen.document["target"]["record_id"] != "foreign"
    with pytest.raises((AttributeError, TypeError)):
        frozen.canonical_bytes = b"{}"


def test_intent_rejects_noncanonical_bytes_or_forged_value():
    api = feature("writer_contracts")
    document = intent(make_case())
    with pytest.raises((CRMRefused, ValueError)):
        api.CRMWriteIntent(b" " + canonical_bytes(document))
    document["intended"]["fr_score"] += 1
    with pytest.raises((CRMRefused, ValueError)):
        api.CRMWriteIntent.from_document(document)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_store_schema_stays_exact_and_has_no_writer_tables(tmp_path, version):
    feature("writer_schema")
    path = tmp_path / f"old-{version}.sqlite"
    with_store = CRMStore(path, format_version=version)
    with_store.close()
    before = path.read_bytes()
    reopened = CRMStore(path, format_version=version, create=False)
    reopened.close()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert not any("crm_write_" in r[0] for r in conn.execute("SELECT name FROM sqlite_master"))
    with pytest.raises(CRMRefused, match="unrecognized"):
        CRMStore(path, format_version=5, create=False)
    assert path.read_bytes() == before


def test_format5_is_explicit_and_never_created_when_opening(tmp_path):
    feature("writer_schema")
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(CRMRefused):
        CRMStore(missing, format_version=5, create=False)
    assert not missing.exists()
    store = CRMStore(missing, format_version=5)
    try:
        with store._read() as conn:
            assert conn.execute("SELECT format_version FROM crm_store_meta").fetchall() == [(5,)]
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name='crm_write_attempts'"
            ).fetchone()
    finally:
        store.close()


def test_all_immutable_writer_tables_reject_update_delete_and_replace(tmp_path):
    from tests.adapters.crm.test_salesforce_writer import enable, prepare, send, setup

    runtime, _, decision = setup(tmp_path)
    try:
        enable(runtime)
        frozen = prepare(runtime, decision)
        assert send(runtime, frozen).state == "observed_applied_at_t"
        with sqlite3.connect(runtime.config.operational_path) as conn:
            conn.execute(
                "INSERT INTO crm_write_local_refusals VALUES (?,?,?)",
                (frozen.operation_id, b"{}", "0" * 64),
            )
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'crm_write_%' AND name != 'crm_write_progress'"
                )
            ]
            assert len(tables) == 7
            for table in tables:
                before = conn.execute(f"SELECT * FROM {table}").fetchall()
                assert before
                for query in [
                    f"UPDATE {table} SET body=body",
                    f"DELETE FROM {table}",
                    f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
                ]:
                    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                        conn.execute(query)
                    assert conn.execute(f"SELECT * FROM {table}").fetchall() == before
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", 1),
        ("generation", True),
        ("generation", 0),
        ("target_keys", []),
        ("output_fields", []),
    ],
)
def test_policy_rejects_ambiguous_types(field, value):
    api = feature("writer_contracts")
    cap = make_case()["capture"]
    from flight_recorder.adapters.crm.contracts import CRMTarget
    from flight_recorder.adapters.crm.mapping import OUTPUTS

    document = dict(
        enabled=True,
        connection_id=cap["connection_id"],
        expected_instance_id=cap["target"]["instance_id"],
        target_keys=(CRMTarget(**cap["target"]).key,),
        output_fields=tuple(sorted(OUTPUTS.values())),
        mapping_hash=cap["mapping_hash"],
        selection_identity=cap["selection_identity"],
        generation=1,
    )
    document[field] = value
    with pytest.raises((CRMRefused, ValueError, TypeError)):
        api.CRMWritePolicy(**document)


def test_intent_wrapper_does_not_allow_extra_mutable_attributes():
    api = feature("writer_contracts")
    frozen = api.CRMWriteIntent.from_document(intent(make_case()))
    with pytest.raises((AttributeError, TypeError)):
        frozen.mutable_alias = {"fr_score": 100}


@pytest.mark.parametrize("generation", [None, True, "1", 1.0, 0])
def test_send_admission_requires_exact_credential_generation(generation):
    api = feature("writer_contracts")
    frozen = api.CRMWriteIntent.from_document(intent(make_case()))
    with pytest.raises(CRMRefused):
        api._SendAdmission(frozen, object(), generation, lambda: None)


def test_send_admission_is_one_use_even_when_guard_refuses_and_never_serializes():
    api = feature("writer_contracts")
    frozen = api.CRMWriteIntent.from_document(intent(make_case()))
    client = object()
    calls = []

    def refuse():
        calls.append(1)
        raise CRMRefused("fictional_guard_refusal")

    grant = api._SendAdmission(frozen, client, 1, refuse)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(grant)
    with pytest.raises(CRMRefused, match="fictional_guard_refusal"):
        grant.consume(frozen, client, credential_generation=1)
    with pytest.raises(CRMRefused, match="consumed"):
        grant.consume(frozen, client, credential_generation=1)
    assert calls == [1]


def test_boolean_cannot_match_integer_credential_generation_at_consumption():
    api = feature("writer_contracts")
    frozen = api.CRMWriteIntent.from_document(intent(make_case()))
    client = object()
    calls = []
    grant = api._SendAdmission(frozen, client, 1, lambda: calls.append(1))
    with pytest.raises(CRMRefused, match="binding_changed"):
        grant.consume(frozen, client, credential_generation=True)
    with pytest.raises(CRMRefused, match="consumed"):
        grant.consume(frozen, client, credential_generation=1)
    assert calls == []


def _rehash_retained_policy(runtime, generation, change):
    with sqlite3.connect(runtime.config.operational_path) as conn:
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='crm_write_policies_no_update'"
        ).fetchone()[0]
        body = conn.execute(
            "SELECT body FROM crm_write_policies WHERE generation=?", (generation,)
        ).fetchone()[0]
        document = json.loads(body)
        change(document)
        modified = canonical_bytes(document)
        conn.execute("DROP TRIGGER crm_write_policies_no_update")
        conn.execute(
            "UPDATE crm_write_policies SET body=?,body_sha256=? WHERE generation=?",
            (modified, hashlib.sha256(modified).hexdigest(), generation),
        )
        conn.execute(trigger)


@pytest.mark.parametrize(
    "field",
    [
        "config_binding",
        "connection_binding",
        "artifact_hash",
        "evaluator_version",
        "selection_identity",
        "mapping_hash",
        "target_keys",
        "recorded_at",
    ],
)
def test_every_unreferenced_policy_is_bound_before_start_and_provider_work(tmp_path, field):
    from dataclasses import replace

    from tests.adapters.crm.test_salesforce_writer import dispatch, enable, policy, setup

    runtime, provider, decision = setup(tmp_path)
    cfg, clock = runtime.config, runtime.clock
    try:
        enable(runtime)
        feature().set_write_policy(
            runtime, policy(runtime, generation=2, enabled=False), clock=clock
        )

        def change(d):
            if field == "recorded_at":
                d[field] = "2026-04-17T10:11:00.000000Z"
            elif field == "target_keys":
                d["policy"][field] = [replace(cfg.targets[0], record_id="001000000000002AAA").key]
            elif field in ("selection_identity", "mapping_hash"):
                d["policy"][field] = "f" * 64 if field == "mapping_hash" else "changed-selection"
            else:
                d[field] = "changed-evaluator" if field == "evaluator_version" else "f" * 64

        _rehash_retained_policy(runtime, 1, change)
        before = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
        op_bytes, domain_bytes = cfg.operational_path.read_bytes(), cfg.domain_path.read_bytes()
        with pytest.raises(CRMRefused, match="write_policy"):
            runtime.validate_write_state()
        for action in [
            lambda: feature().prepare_write(runtime, decision, clock=clock),
            lambda: feature().send_write_once(runtime, "f" * 64, clock=clock),
            lambda: feature().observe_write(runtime, "f" * 64, clock=clock),
            lambda: feature().set_write_policy(runtime, policy(runtime, generation=3), clock=clock),
            lambda: feature("poll").capture_and_submit(
                runtime, cfg.targets[0], "original", clock=clock
            ),
        ]:
            with pytest.raises(CRMRefused, match="write_policy"):
                action()
        with pytest.raises(CRMRefused):
            dispatch(runtime, "must-hold-before-read")
            runtime.run_pending()
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == before
        assert cfg.operational_path.read_bytes() == op_bytes
        assert cfg.domain_path.read_bytes() == domain_bytes
    finally:
        runtime.close()
    resumed = feature("runtime").CRMRuntime(
        cfg,
        writer_capable=True,
        client_factory=lambda: pytest.fail("damaged historical policy reached credentials"),
        clock=clock,
    )
    try:
        with pytest.raises(CRMRefused, match="write_policy"):
            resumed.start(background=False)
    finally:
        resumed.close()


def test_policy_cannot_be_recorded_after_its_completed_original_intent(tmp_path):
    from tests.adapters.crm.test_salesforce_writer import enable, prepare, send, setup

    runtime, _, decision = setup(tmp_path)
    try:
        enable(runtime)
        frozen = prepare(runtime, decision)
        assert send(runtime, frozen).state == "observed_applied_at_t"
        _rehash_retained_policy(
            runtime, 1, lambda d: d.update(recorded_at="2026-04-17T10:11:00.000000Z")
        )
        with pytest.raises(CRMRefused, match="write_policy"):
            runtime.validate_write_state()
    finally:
        runtime.close()


def test_completed_policy_binding_is_checked_after_later_disable(tmp_path):
    from tests.adapters.crm.test_salesforce_writer import (
        dispatch,
        enable,
        policy,
        prepare,
        send,
        setup,
    )

    runtime, provider, decision = setup(tmp_path)
    try:
        enable(runtime)
        frozen = prepare(runtime, decision)
        assert send(runtime, frozen).state == "observed_applied_at_t"
        feature().set_write_policy(
            runtime, policy(runtime, generation=2, enabled=False), clock=runtime.clock
        )
        _rehash_retained_policy(runtime, 1, lambda d: d.update(connection_binding="f" * 64))
        before = (provider.calls, provider.verifications, len(provider.sent), provider.output_reads)
        with pytest.raises(CRMRefused, match="write_policy"):
            dispatch(runtime, "completed-corruption")
        assert (
            provider.calls,
            provider.verifications,
            len(provider.sent),
            provider.output_reads,
        ) == before
    finally:
        runtime.close()


def test_backwards_policy_clock_is_refused_before_append(tmp_path):
    from datetime import UTC, datetime

    from tests.adapters.crm.test_salesforce_writer import enable, policy, setup

    runtime, _, _ = setup(tmp_path)
    try:
        enable(runtime)
        before = runtime.config.operational_path.read_bytes()
        with pytest.raises(CRMRefused):
            feature().set_write_policy(
                runtime,
                policy(runtime, generation=2),
                clock=lambda: datetime(2020, 1, 1, tzinfo=UTC),
            )
        assert runtime.config.operational_path.read_bytes() == before
        runtime.validate_write_state()
    finally:
        runtime.close()
