"""Original-only, durable single-attempt Salesforce writer; never autonomous delivery."""

import re
from contextlib import contextmanager

from flight_recorder.adapters.crm.contracts import (
    ConfirmedConnection,
    CRMRefused,
    CRMTarget,
    Ownership,
    require,
)
from flight_recorder.adapters.crm.mapping import FIELDS, OUTPUTS
from flight_recorder.adapters.crm.status import saved_confirmation
from flight_recorder.adapters.crm.submit import _verify_domain
from flight_recorder.adapters.crm.writer_contracts import (
    CRMWriteIntent,
    CRMWritePolicy,
    OutputObservation,
    PolicyReceipt,
    ProviderResponse,
    _SendAdmission,
)
from flight_recorder.adapters.crm.writer_store import put
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.crm_operations import (
    TERMINAL,
    intent_digest,
    operation_history,
    request_digest,
)
from flight_recorder.collector.errors import CollectorError, RejectedError
from flight_recorder.collector.schema import format_utc
from flight_recorder.replay.crm_context import verified_crm_history
from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct


@contextmanager
def _entry(runtime):
    runtime.authority.assert_held()
    require(
        runtime.writer_capable and not runtime._closed and not runtime._stop.is_set(),
        "writer_not_initialized",
    )
    require(runtime._execution.acquire(blocking=False), "writer_busy")
    try:
        runtime.validate_write_state()
        yield runtime._writer_store
    except (CollectorError, ReconstructionError):
        raise CRMRefused("write_history_refused") from None
    finally:
        runtime._execution.release()


def _policy(runtime, snap):
    cfg = runtime.config
    docs = [
        d
        for (connection, _), d in snap["policies"].items()
        if connection == cfg.connection.connection_id
    ]
    require(bool(docs), "policy_disabled")
    d = docs[-1]
    require(
        d["config_binding"] == cfg.binding
        and d["connection_binding"] == cfg.connection.binding
        and d["artifact_hash"] == cfg.connection.artifact_hash
        and d["evaluator_version"] == cfg.connection.evaluator_version,
        "write_policy_binding",
    )
    return CRMWritePolicy.from_document(d["policy"])


def set_write_policy(runtime, policy, *, clock):
    with _entry(runtime) as store:
        require(type(policy) is CRMWritePolicy, "invalid_write_policy")
        cfg, c = runtime.config, runtime.config.connection
        require(
            policy.connection_id == c.connection_id
            and policy.expected_instance_id == c.expected_instance_id
            and set(policy.target_keys) <= {t.key for t in cfg.targets}
            and policy.output_fields == tuple(sorted(OUTPUTS.values()))
            and policy.mapping_hash == c.mapping_hash
            and policy.selection_identity == cfg.document["selection_identity"],
            "write_policy_binding",
        )
        snap = store.audit()
        docs = {
            g: d for (connection, g), d in snap["policies"].items() if connection == c.connection_id
        }
        if policy.generation in docs:
            d = docs[policy.generation]
            require(
                canonical_bytes(d["policy"]) == policy.canonical_bytes
                and d["config_binding"] == cfg.binding,
                "write_policy_generation",
            )
        else:
            require(policy.generation == max(docs, default=0) + 1, "write_policy_generation")
            d = dict(
                format_version=1,
                policy=policy.document,
                config_binding=cfg.binding,
                connection_binding=c.binding,
                artifact_hash=c.artifact_hash,
                evaluator_version=c.evaluator_version,
                recorded_at=format_utc(clock()),
            )
            with store.store.transaction() as conn:
                put(
                    conn,
                    "crm_write_policies",
                    d,
                    connection_id=c.connection_id,
                    generation=policy.generation,
                )
        store.audit()
        return PolicyReceipt(policy.generation, canonical_hash(d), policy.enabled)


def _original(runtime, decision):
    with runtime.collector.engine.connect() as conn:
        row, cap, _, _, _ = verified_crm_history(conn, decision)
        original = reconstruct(conn, decision)
    c, cfg = runtime.config.connection, runtime.config
    target = CRMTarget(**cap["target"])
    require(
        target in cfg.targets
        and cap["connection_id"] == c.connection_id
        and cap["mapping_hash"] == c.mapping_hash
        and cap["selection_identity"] == cfg.document["selection_identity"]
        and cap["rule_hash"] == c.artifact_hash
        and cap["evaluator_version"] == c.evaluator_version
        and all(cap["inputs"][k]["state"] == "available" for k in FIELDS),
        "write_original_binding",
    )
    require(
        type(cap["precondition"]) is str
        and bool(re.fullmatch(r'"[\x21\x23-\x7e]{1,1022}"', cap["precondition"])),
        "write_precondition_unavailable",
    )
    with runtime.store._read() as conn:
        frozen_cap = runtime.store._load_capture(conn, row.unit_id)
        binding = conn.execute(
            "SELECT claim_id,generation,config_binding FROM crm_frozen_captures WHERE unit_id=?",
            (row.unit_id,),
        ).fetchone()
        require(
            frozen_cap is not None
            and frozen_cap.capture_hash == row.capture_hash
            and binding is not None
            and binding[2] == c.binding,
            "write_original_submission",
        )
        frozen = runtime.store._load_submission(conn, Ownership(target.key, binding[0], binding[1]))
        require(frozen is not None, "write_original_submission")
        accepted, failure, _ = runtime.store.submission_history(conn, frozen)
        require(
            failure is None
            and accepted == {s["stage"] for s in frozen.document["stages"]}
            and frozen.document["stages"][-1]["event_id"] == decision,
            "write_original_submission",
        )
        _verify_domain(runtime.collector, frozen, accepted)
    return cap, original


def _fresh(runtime, intent, *, owner=None):
    d = intent.document
    target = CRMTarget(**d["target"])
    with runtime.store._read() as conn:
        state = runtime.store._state(conn, target)
        if owner is not None:
            runtime.store.assert_owner(conn, owner)
        else:
            held = conn.execute(
                "SELECT 1 FROM crm_target_ownership WHERE target_key=? AND state='held'",
                (target.key,),
            ).fetchone()
            require(held is None, "target_busy")
        require(state["capture_sequence"] == d["capture_sequence"], "newer_capture")
    from sqlalchemy import func, select

    from flight_recorder.ledger.crm_schema import crm_captures

    with runtime.collector.engine.connect() as conn:
        latest = conn.execute(
            select(func.max(crm_captures.c.capture_sequence)).where(
                crm_captures.c.account_ref == target.account_ref
            )
        ).scalar_one()
        require(latest == d["capture_sequence"], "newer_capture")


def _event(
    intent, sequence, phase, state, origin, clock, *, response=None, output_read=None, reason=None
):
    now = format_utc(clock())
    return dict(
        schema_version="2",
        event_id=f"fr-crm-write-observation-v1-{intent.operation_id}-{sequence}",
        event_type="external_operation.observed",
        source="salesforce-test-account",
        account_ref=CRMTarget(**intent.document["target"]).account_ref,
        occurred_at=now,
        recorded_at=now,
        payload=dict(
            data_origin="fictional_records_real_crm",
            observation_origin=origin,
            intent=intent.document,
            observation_sequence=sequence,
            phase=phase,
            state=state,
            observed_at=now,
            response=response,
            output_read=output_read,
            reason=reason,
        ),
    )


def _flush(runtime, store):
    fresh = {}
    snap = store.audit()
    order = sorted(snap["intents"], key=lambda op: (snap["intents"][op].document["revision"], op))
    for op in order:
        if op in snap["refusals"]:
            continue
        for doc in sorted(snap["boxes"][op], key=lambda d: d["payload"]["observation_sequence"]):
            if doc["event_id"] in snap["receipts"]:
                continue
            try:
                result = runtime.collector.ingest_json(canonical_bytes(doc))
            except RejectedError as exc:
                with runtime.collector.engine.connect() as conn:
                    actual = operation_history(conn, doc["account_ref"])
                if (
                    doc["payload"]["phase"] == "prepared"
                    and op not in snap["attempts"]
                    and not any(d["event_id"] == doc["event_id"] for d in actual)
                ):
                    reason = exc.body.get("reason")
                    require(
                        type(reason) is str and bool(re.fullmatch("[a-z0-9_]+", reason)),
                        "write_delivery_pending",
                    )
                    with store.store.transaction() as conn:
                        put(
                            conn,
                            "crm_write_local_refusals",
                            dict(
                                format_version=1,
                                operation_id=op,
                                event_id=doc["event_id"],
                                event_hash=canonical_hash(doc),
                                reason="collector_preparation_refused",
                                collector_reason=reason,
                            ),
                            operation_id=op,
                        )
                        store.sync(conn, op)
                    break
                raise CRMRefused("write_delivery_pending") from None
            with runtime.collector.engine.connect() as conn:
                actual = operation_history(conn, doc["account_ref"])
                require(
                    any(canonical_bytes(d) == canonical_bytes(doc) for d in actual),
                    "invalid_write_collector_ack",
                )
            with store.store.transaction() as conn:
                store.acknowledge(conn, doc, result)
            fresh[doc["event_id"]] = result.status
        snap = store.audit()
    return fresh


def _finish(runtime, store, op):
    snap = store.audit()
    require(op in snap["intents"], "write_intent_missing")
    docs = snap["boxes"][op]
    last = max(docs, key=lambda d: d["payload"]["observation_sequence"])
    if (
        op not in snap["terminals"]
        and last["payload"]["state"] in TERMINAL
        and last["event_id"] in snap["receipts"]
    ):
        generation = snap["expected"][op]["generation"]
        with store.store.transaction() as conn:
            if generation is not None:
                owner = Ownership(
                    CRMTarget(**snap["intents"][op].document["target"]).key, op, generation
                )
                store.store.assert_owner(conn, owner)
            put(
                conn,
                "crm_write_terminals",
                dict(
                    format_version=1,
                    operation_id=op,
                    terminal_event_id=last["event_id"],
                    terminal_hash=canonical_hash(last),
                    state=last["payload"]["state"],
                    generation=generation,
                ),
                operation_id=op,
                terminal_event_id=last["event_id"],
            )
            if generation is not None:
                conn.execute(
                    "UPDATE crm_target_ownership SET state='released' WHERE target_key=?",
                    (owner.target_key,),
                )
            store.sync(conn, op)
    return store.progress(op)


def _refuse(runtime, store, op, reason, clock):
    snap = store.audit()
    require(op not in snap["attempts"], "write_already_attempted")
    docs = snap["boxes"][op]
    if not any(d["payload"]["state"] in TERMINAL for d in docs):
        doc = _event(
            snap["intents"][op],
            len(docs) + 1,
            "refused",
            "refused",
            docs[0]["payload"]["observation_origin"],
            clock,
            reason=reason,
        )
        with store.store.transaction() as conn:
            store.append(conn, doc)
            store.sync(conn, op)
    _flush(runtime, store)
    return _finish(runtime, store, op)


def prepare_write(runtime, original_decision_id, *, clock):
    with _entry(runtime) as store:
        snap = store.audit()
        policy = _policy(runtime, snap)
        require(policy.enabled, "policy_disabled")
        _flush(runtime, store)
        snap = store.audit()
        for frozen in snap["intents"].values():
            if frozen.document["decision_event_id"] == original_decision_id:
                return frozen
        cap, original = _original(runtime, original_decision_id)
        target = CRMTarget(**cap["target"])
        require(target.key in policy.target_keys, "target_unallowlisted")
        for op, frozen in snap["intents"].items():
            if (
                frozen.document["target"] == cap["target"]
                and op not in snap["terminals"]
                and op not in snap["refusals"]
            ):
                require(op not in snap["attempts"], "target_busy")
                require(
                    frozen.document["capture_sequence"] < cap["capture_sequence"], "target_busy"
                )
                _refuse(runtime, store, op, "newer_capture", clock)
        with runtime.collector.engine.connect() as conn:
            history = operation_history(conn, target.account_ref)
        revision = max([d["payload"]["intent"]["revision"] for d in history], default=0) + 1
        d = dict(
            format_version=1,
            target=cap["target"],
            connection_id=cap["connection_id"],
            decision_event_id=original_decision_id,
            capture_hash=canonical_hash(cap),
            capture_sequence=cap["capture_sequence"],
            artifact_hash=cap["rule_hash"],
            evaluator_version=cap["evaluator_version"],
            mapping_hash=cap["mapping_hash"],
            selection_identity=cap["selection_identity"],
            policy_generation=policy.generation,
            revision=revision,
            source_precondition=cap["precondition"],
            created_at=format_utc(clock()),
            intended=dict(
                fr_score=original.result.score,
                fr_output=original.result.output,
                fr_logic_version=original.logic_version,
                fr_decision_ref=original_decision_id,
                fr_op_id="0" * 64,
                fr_op_rev=revision,
            ),
        )
        d["operation_id"] = intent_digest(d)
        d["intended"]["fr_op_id"] = d["operation_id"]
        intent = CRMWriteIntent.from_document(d)
        _fresh(runtime, intent)
        doc = _event(intent, 1, "prepared", "prepared", cap["observation_origin"], clock)
        with store.store.transaction() as conn:
            put(
                conn,
                "crm_write_intents",
                d,
                operation_id=intent.operation_id,
                target_key=target.key,
                decision_event_id=original_decision_id,
                revision=revision,
            )
            store.append(conn, doc)
            store.sync(conn, intent.operation_id, new=True)
        _flush(runtime, store)
        return intent


def _verified_client(runtime, intent, origin):
    client = runtime.client()
    runtime.verify_for_read(client)
    c, confirmation = runtime.config.connection, saved_confirmation(runtime)
    require(
        type(confirmation) is ConfirmedConnection
        and (
            confirmation.connection_id,
            confirmation.provider,
            confirmation.instance_id,
            confirmation.user_id,
            confirmation.api_version,
            confirmation.observation_origin,
        )
        == (
            c.connection_id,
            c.provider,
            c.expected_instance_id,
            c.expected_user_id,
            c.api_version,
            origin,
        )
        and all(
            callable(getattr(client, name, None))
            for name in ("write_generation", "send_once", "read_outputs")
        ),
        "authorization_unavailable",
    )
    generation = client.write_generation()
    require(type(generation) is int and generation > 0, "authorization_unavailable")
    return client, generation


def _observe(runtime, store, op, clock):
    result = _finish(runtime, store, op)
    if not result.held:
        return result
    snap = store.audit()
    docs, intent = snap["boxes"][op], snap["intents"][op]
    marker = next(d for d in docs if d["payload"]["phase"] == "send_started")
    require(marker["event_id"] in snap["receipts"], "write_delivery_pending")
    origin = marker["payload"]["observation_origin"]
    client, _ = _verified_client(runtime, intent, origin)
    observation = client.read_outputs(intent)
    require(
        type(observation) is OutputObservation and observation.observation_origin == origin,
        "invalid_write_observation",
    )
    complete = any(
        d["payload"]["phase"] == "response" and d["payload"]["state"] == "awaiting_observation"
        for d in docs
    )
    matches = canonical_bytes(observation.document["values"]) == canonical_bytes(
        intent.document["intended"]
    )
    state = "observed_applied_at_t" if complete and matches else result.state
    doc = _event(
        intent,
        len(docs) + 1,
        "output_observed",
        state,
        origin,
        clock,
        output_read=observation.document,
    )
    with store.store.transaction() as conn:
        store.append(conn, doc)
        store.sync(conn, op)
    _flush(runtime, store)
    return _finish(runtime, store, op)


def observe_write(runtime, operation_id, *, clock):
    with _entry(runtime) as store:
        _flush(runtime, store)
        return _observe(runtime, store, operation_id, clock)


def send_write_once(runtime, operation_id, *, clock):
    with _entry(runtime) as store:
        _flush(runtime, store)
        result = _finish(runtime, store, operation_id)
        snap = store.audit()
        if (
            operation_id in snap["attempts"]
            or operation_id in snap["terminals"]
            or operation_id in snap["refusals"]
        ):
            return result
        intent = snap["intents"][operation_id]
        d = intent.document
        policy = _policy(runtime, snap)
        if not policy.enabled:
            return _refuse(runtime, store, operation_id, "policy_disabled", clock)
        if policy.generation != d["policy_generation"]:
            return _refuse(runtime, store, operation_id, "policy_changed", clock)
        target = CRMTarget(**d["target"])
        if target.key not in policy.target_keys:
            return _refuse(runtime, store, operation_id, "target_unallowlisted", clock)
        _original(runtime, d["decision_event_id"])
        try:
            _fresh(runtime, intent)
        except CRMRefused as exc:
            if exc.reason == "newer_capture":
                return _refuse(runtime, store, operation_id, "newer_capture", clock)
            raise
        origin = snap["boxes"][operation_id][0]["payload"]["observation_origin"]
        try:
            client, credential_generation = _verified_client(runtime, intent, origin)
        except Exception:
            return _refuse(runtime, store, operation_id, "authorization_unavailable", clock)
        marker = _event(intent, 2, "send_started", "unknown", origin, clock)
        with store.store.transaction() as conn:
            row = conn.execute(
                "SELECT generation,state FROM crm_target_ownership WHERE target_key=?",
                (target.key,),
            ).fetchone()
            require(row is None or row[1] != "held", "target_busy")
            generation = row[0] + 1 if row else 1
            if row is None:
                conn.execute(
                    "INSERT INTO crm_target_ownership VALUES (?,?,?,'held')",
                    (target.key, operation_id, generation),
                )
            else:
                conn.execute(
                    "UPDATE crm_target_ownership SET unit_id=?,generation=?,state='held' "
                    "WHERE target_key=?",
                    (operation_id, generation, target.key),
                )
            store.append(conn, marker)
            put(
                conn,
                "crm_write_attempts",
                dict(
                    format_version=1,
                    operation_id=operation_id,
                    marker_event_id=marker["event_id"],
                    marker_hash=canonical_hash(marker),
                    target_key=target.key,
                    generation=generation,
                    request_hash=request_digest(d),
                    credential_generation=credential_generation,
                    config_binding=runtime.config.binding,
                ),
                operation_id=operation_id,
                marker_event_id=marker["event_id"],
                target_key=target.key,
                generation=generation,
            )
            store.sync(conn, operation_id)
        fresh = _flush(runtime, store)
        if fresh.get(marker["event_id"]) != "created":
            return store.progress(operation_id)
        owner = Ownership(target.key, operation_id, generation)

        def guard():
            current = store.audit()
            require(_policy(runtime, current) == policy and policy.enabled, "write_policy_changed")
            _fresh(runtime, intent, owner=owner)

        admission = _SendAdmission(intent, client, credential_generation, guard)
        try:
            response = client.send_once(intent, admission)
        except Exception:
            return store.progress(operation_id)
        finally:
            admission.discard()
        require(
            type(response) is ProviderResponse and response.observation_origin == origin,
            "invalid_write_response",
        )
        r = response.document
        state = "unknown"
        if r["complete"]:
            if 200 <= r["status"] < 300:
                state = "awaiting_observation"
            elif r["status"] == 412:
                state = "refused_no_effect"
        doc = _event(intent, 3, "response", state, origin, clock, response=r)
        with store.store.transaction() as conn:
            store.append(conn, doc)
            store.sync(conn, operation_id)
        _flush(runtime, store)
        if state == "awaiting_observation":
            return _observe(runtime, store, operation_id, clock)
        return _finish(runtime, store, operation_id)
