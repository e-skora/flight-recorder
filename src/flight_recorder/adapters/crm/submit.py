"""Freeze original v3 events, then deliver only those bytes to the local Collector."""

import hashlib
from pathlib import Path

from sqlalchemy import select

from flight_recorder.adapters.crm.contracts import CRMRefused, CRMTarget, require
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.adapters.crm.submission_contracts import (
    CRMSubmission,
    SubmissionProgress,
    normalize,
    original_evaluation,
    prerequisite_binding,
)
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash, canonical_text
from flight_recorder.collector.crm_provenance import (
    _context,
    _evaluate,
    _evidence_row,
    _lineage,
    _original_capture,
    _verify_evidence,
    _verify_origin,
    ensure_crm_schema,
)
from flight_recorder.collector.crm_schema import observation_body
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.crm_schema import (
    crm_decision_consumed_inputs,
    crm_decision_context,
    crm_decisions,
)
from flight_recorder.ledger.schema import accounts, events, logic_artifacts
from flight_recorder.replay.reconstruct import load_artifact_row, verify_artifact_row


def _path(collector):
    require(type(collector) is Collector, "in_process_sql_collector_required")
    url = collector.engine.url
    require(
        url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"),
        "file_backed_local_collector_required",
    )
    path = Path(url.database).resolve()
    require(path.is_file(), "initialized_collector_required")
    return str(path)


def _event(conn, event_id):
    row = conn.execute(select(events).where(events.c.event_id == event_id)).first()
    if row is None:
        return None
    envelope = {
        key: getattr(row, key)
        for key in [
            "schema_version",
            "event_id",
            "event_type",
            "source",
            "account_ref",
            "occurred_at",
            "recorded_at",
        ]
    }
    envelope["payload"] = strict_json(row.payload.encode("utf-8"))
    n = normalize(canonical_bytes(envelope))
    require(
        canonical_bytes(n) == canonical_bytes(envelope) and canonical_hash(n) == row.canonical_hash,
        "ledger_event_integrity",
    )
    return n


def _prerequisites(conn, cap, pri):
    target = CRMTarget(**cap["target"])
    account, registration = pri["account"], pri["artifact"]
    require(
        account["account_ref"] == target.account_ref
        and account["source"] == "salesforce-test-account",
        "account_prerequisite_identity",
    )
    original_evaluation(registration["payload"]["artifact"], cap, pri["supersessions"])
    row = conn.execute(select(accounts).where(accounts.c.account_ref == target.account_ref)).first()
    if row is None:
        require(
            account["payload"]["name"] == cap["properties"]["Name"]
            and _event(conn, account["event_id"]) is None,
            "account_prerequisite_identity",
        )
    else:
        account = _event(conn, row.first_seen_event_id)
        require(
            account is not None
            and account["event_type"] == "account.discovered"
            and account["account_ref"] == target.account_ref
            and account["source"] == "salesforce-test-account"
            and (row.name, row.domain)
            == (account["payload"]["name"], account["payload"]["domain"]),
            "discovery_projection_integrity",
        )
    row = conn.execute(
        select(logic_artifacts).where(logic_artifacts.c.artifact_hash == cap["rule_hash"])
    ).first()
    if row is None:
        require(_event(conn, registration["event_id"]) is None, "registration_projection_integrity")
    else:
        verified = verify_artifact_row(
            load_artifact_row(conn, cap["rule_hash"]), cap["rule_hash"]
        ).artifact
        require(
            verified.evaluator_version == cap["evaluator_version"], "selected_artifact_mismatch"
        )
        registration = _event(conn, row.source_event_id)
        require(
            registration is not None
            and registration["event_type"] == "logic_artifact.registered"
            and canonical_hash(registration["payload"]["artifact"]) == cap["rule_hash"],
            "registration_projection_integrity",
        )
    return account, registration


def _reuse(conn, cap, item):
    target = CRMTarget(**cap["target"])
    row = _evidence_row(conn, item["evidence_version_id"])
    if row is None:
        return None
    _verify_evidence(row, target.account_ref, cap, item)
    origin = _verify_origin(conn, row)
    require(origin["target"] == cap["target"], "evidence_origin_integrity")
    return dict(
        input_key=item["evidence_type"],
        evidence_version_id=item["evidence_version_id"],
        origin_capture_hash=row.capture_hash,
        observation=observation_body(target.account_ref, cap, item),
    )


def freeze_submission(store, collector, owner, *, prerequisites):
    path = _path(collector)
    pri = prerequisite_binding(prerequisites)
    saved = store.load_submission(owner)
    if saved is not None:
        require(saved.document["domain_path"] == path, "submission_domain_mismatch")
        require(
            canonical_bytes(saved.document["prerequisite_binding"]) == canonical_bytes(pri),
            "submission_prerequisite_conflict",
        )
        try:
            history = store.read_submission_history(owner, saved)
            _verify_domain(collector, saved, history[0])
        except CRMRefused as exc:
            raise CRMRefused(exc.reason) from None
        except Exception:
            raise CRMRefused("submission_preparation_refused") from None
        return saved
    cap = store.capture_for_owner(owner)
    require(cap is not None, "missing_frozen_capture")
    c = cap.document
    require(pri["recorded_at"] >= c["capture_boundary"], "submission_recorded_before_capture")
    try:
        with collector.engine.connect() as conn:
            ensure_crm_schema(conn)
            account, registration = _prerequisites(conn, c, pri)
            _lineage(conn, CRMTarget(**c["target"]).account_ref, c)
            items, context, consumed, result = original_evaluation(
                registration["payload"]["artifact"], c, pri["supersessions"]
            )
            reused = []
            for item in items:
                predecessor = item["supersedes_evidence_version_id"]
                if predecessor is not None:
                    prior = _evidence_row(conn, predecessor)
                    require(prior is not None, "supersession_lineage_mismatch")
                    old = _verify_origin(conn, prior)
                    require(
                        prior.account_ref == CRMTarget(**c["target"]).account_ref
                        and prior.evidence_type == item["evidence_type"]
                        and old["target"] == c["target"]
                        and old["mapping_hash"] == c["mapping_hash"]
                        and old["capture_sequence"] < c["capture_sequence"]
                        and prior.available_at
                        <= observation_body(prior.account_ref, c, item)["eligible_at"],
                        "supersession_lineage_mismatch",
                    )
                proof = _reuse(conn, c, item)
                if proof is not None:
                    reused.append(proof)
            require(not reused or len(reused) == len(items), "mixed_evidence_reuse_unsupported")
        common = dict(
            schema_version="3",
            source="salesforce-test-account",
            account_ref=CRMTarget(**c["target"]).account_ref,
            recorded_at=pri["recorded_at"],
        )
        stages = [("account", account), ("artifact", registration)]
        if not reused:
            stages.append(
                (
                    "evidence",
                    dict(
                        **common,
                        event_id="fr-crm-evidence-v1-" + cap.unit_id,
                        event_type="evidence.recorded",
                        occurred_at=c["read_finished_at"],
                        payload=dict(capture=c, capture_hash=cap.capture_hash, items=items),
                    ),
                )
            )
        artifact = registration["payload"]["artifact"]
        ref = {key: artifact[key] for key in ["artifact_id", "logic_version", "evaluator_version"]}
        ref["artifact_hash"] = c["rule_hash"]
        stages.append(
            (
                "decision",
                dict(
                    **common,
                    event_id="fr-crm-decision-v1-" + cap.unit_id,
                    event_type="decision.recorded",
                    occurred_at=c["capture_boundary"],
                    payload=dict(
                        capture=c,
                        capture_hash=cap.capture_hash,
                        decision_class="account_prioritization",
                        decision_boundary=c["capture_boundary"],
                        workflow_version=c["processing_version"],
                        historical_context=context,
                        consumed_inputs=consumed,
                        logic_artifact=ref,
                        result=result,
                        explanation=None,
                    ),
                ),
            )
        )
        frozen_stages = []
        for name, envelope in stages:
            n = normalize(canonical_bytes(envelope))
            frozen_stages.append(
                dict(
                    stage=name, event_id=n["event_id"], canonical_hash=canonical_hash(n), envelope=n
                )
            )
        frozen = CRMSubmission.from_document(
            dict(
                format_version=1,
                unit_id=cap.unit_id,
                capture_hash=cap.capture_hash,
                capture_bytes_sha256=hashlib.sha256(cap.canonical_bytes).hexdigest(),
                target_key=owner.target_key,
                claim_id=owner.unit_id,
                ownership_generation=owner.generation,
                domain_path=path,
                prerequisite_binding=pri,
                prerequisite_hash=canonical_hash(pri),
                stages=frozen_stages,
                reused_evidence=reused,
            )
        )
        return store.freeze_submission(owner, frozen)
    except CRMRefused:
        raise
    except Exception:
        raise CRMRefused("submission_preparation_refused") from None


def _verify_stage(conn, stage):
    n = stage["envelope"]
    p = n["payload"]
    actual = _event(conn, stage["event_id"])
    require(
        actual is not None and canonical_bytes(actual) == canonical_bytes(n),
        "acknowledged_event_missing_or_changed",
    )
    if stage["stage"] == "account":
        row = conn.execute(
            select(accounts).where(accounts.c.account_ref == n["account_ref"])
        ).first()
        require(
            row is not None
            and row.first_seen_event_id == stage["event_id"]
            and (row.name, row.domain) == (p["name"], p["domain"]),
            "discovery_projection_integrity",
        )
    elif stage["stage"] == "artifact":
        hash_ = canonical_hash(p["artifact"])
        row = load_artifact_row(conn, hash_)
        verify_artifact_row(row, hash_)
        registered = conn.execute(
            select(logic_artifacts.c.source_event_id).where(
                logic_artifacts.c.artifact_hash == hash_
            )
        ).scalar_one()
        require(registered == stage["event_id"], "registration_projection_integrity")
    elif stage["stage"] == "evidence":
        require(
            _original_capture(conn, p["capture_hash"]) == p["capture"],
            "acknowledged_capture_integrity",
        )
        for item in p["items"]:
            proof = _reuse(conn, p["capture"], item)
            row = _evidence_row(conn, item["evidence_version_id"])
            require(
                proof is not None
                and proof["origin_capture_hash"] == p["capture_hash"]
                and row.source_event_id == stage["event_id"],
                "acknowledged_evidence_integrity",
            )
    else:
        cap = p["capture"]
        ref = p["logic_artifact"]
        row = conn.execute(
            select(crm_decisions).where(crm_decisions.c.decision_event_id == stage["event_id"])
        ).first()
        event_row = conn.execute(
            select(events.c.ingest_sequence).where(events.c.event_id == stage["event_id"])
        ).scalar_one()
        expected = dict(
            decision_event_id=stage["event_id"],
            account_ref=n["account_ref"],
            decision_class=p["decision_class"],
            decision_boundary=p["decision_boundary"],
            workflow_version=p["workflow_version"],
            artifact_hash=ref["artifact_hash"],
            evaluator_version=ref["evaluator_version"],
            logic_version=ref["logic_version"],
            **p["result"],
            explanation=p["explanation"],
            ingest_sequence=event_row,
            unit_id=cap["unit_id"],
            logical_key=cap["logical_key"],
            processing_version=cap["processing_version"],
            capture_hash=p["capture_hash"],
            reprocesses=cap["reprocesses"],
        )
        require(
            row is not None and dict(row._mapping) == expected, "acknowledged_decision_integrity"
        )
        require(_original_capture(conn, p["capture_hash"]) == cap, "acknowledged_capture_integrity")
        for table, key in [
            (crm_decision_context, "historical_context"),
            (crm_decision_consumed_inputs, "consumed_inputs"),
        ]:
            stored = [
                dict(r._mapping)
                for r in conn.execute(
                    select(table).where(table.c.decision_event_id == stage["event_id"])
                )
            ]
            expected_rows = []
            for entry in p[key]:
                one = dict(
                    decision_event_id=stage["event_id"],
                    input_key=entry["input_key"],
                    value_text=canonical_text(entry["value"])
                    if entry.get("availability", "available") == "available"
                    else None,
                    evidence_version_id=entry["evidence_version_id"],
                )
                one["availability" if key == "historical_context" else "contribution"] = entry[
                    "availability" if key == "historical_context" else "contribution"
                ]
                expected_rows.append(one)
            require(
                canonical_bytes(sorted(stored, key=lambda x: x["input_key"]))
                == canonical_bytes(sorted(expected_rows, key=lambda x: x["input_key"])),
                "acknowledged_context_integrity",
            )
        context = _context(conn, n["account_ref"], cap, p["historical_context"])
        _evaluate(conn, p, cap, context)


def _verify_domain(collector, frozen, accepted):
    with collector.engine.connect() as conn:
        ensure_crm_schema(conn)
        d = frozen.document
        cap = d["stages"][-1]["envelope"]["payload"]["capture"]
        items, _, _, _ = original_evaluation(
            d["stages"][1]["envelope"]["payload"]["artifact"],
            cap,
            d["prerequisite_binding"]["supersessions"],
        )
        for proof in d["reused_evidence"]:
            item = next(
                x for x in items if x["evidence_version_id"] == proof["evidence_version_id"]
            )
            require(
                canonical_bytes(_reuse(conn, cap, item)) == canonical_bytes(proof),
                "reused_evidence_integrity",
            )
        for stage in d["stages"]:
            if stage["stage"] in accepted or _event(conn, stage["event_id"]) is not None:
                _verify_stage(conn, stage)


def _progress(frozen, history):
    accepted, failure, latest = history
    d = frozen.document
    pending = next((s["stage"] for s in d["stages"] if s["stage"] not in accepted), None)
    state = "complete" if pending is None else "frozen"
    reason = None
    if failure:
        state = "failed"
        pending, reason = failure
    elif latest:
        state = "retryable" if latest[1] in ("retryable", "unknown") else "incomplete"
        reason = latest[2]["reason"]
    return SubmissionProgress(
        d["unit_id"], frozen.submission_hash, state, pending, d["stages"][-1]["event_id"], reason
    )


def _resume_submission(store, collector, owner):
    path = _path(collector)
    frozen = store.load_submission(owner)
    require(frozen is not None, "missing_frozen_submission")
    require(frozen.document["domain_path"] == path, "submission_domain_mismatch")
    for stage in frozen.document["stages"]:
        history = store.read_submission_history(owner, frozen)
        _verify_domain(collector, frozen, history[0])
        if history[1]:
            return _progress(frozen, history)
        if stage["stage"] in history[0]:
            continue
        attempt = store.start_submission_attempt(owner, frozen, stage)
        uncertain = False
        stop = False
        with store.submission_delivery(owner, frozen, attempt) as conn:
            history = store.submission_history(conn, frozen)
            _verify_domain(collector, frozen, history[0])
            if history[1]:
                store.observe_submission(
                    conn,
                    owner,
                    frozen,
                    attempt,
                    "not_sent_prior_failure",
                    dict(reason="prior_stage_failed"),
                )
                stop = True
            elif stage["stage"] in history[0]:
                store.observe_submission(
                    conn,
                    owner,
                    frozen,
                    attempt,
                    "not_sent_stage_already_accepted",
                    dict(reason="stage_already_accepted"),
                )
            else:
                try:
                    result = collector.ingest_json(canonical_bytes(stage["envelope"]))
                except CollectorError as exc:
                    retryable = (
                        exc.status_code == 409
                        and exc.body.get("reason") == "v2_admission_contended"
                    )
                    store.observe_submission(
                        conn,
                        owner,
                        frozen,
                        attempt,
                        "retryable" if retryable else "rejected",
                        dict(reason="collector_contended" if retryable else "collector_rejected"),
                    )
                    stop = True
                except Exception:
                    store.observe_submission(
                        conn,
                        owner,
                        frozen,
                        attempt,
                        "unknown",
                        dict(reason="collector_return_unknown"),
                    )
                    uncertain = True
                else:
                    good = (
                        getattr(result, "status", None) in ("created", "duplicate")
                        and type(getattr(result, "http_status", None)) is int
                        and result.http_status == (201 if result.status == "created" else 200)
                        and getattr(result, "event_id", None) == stage["event_id"]
                        and getattr(result, "canonical_hash", None) == stage["canonical_hash"]
                    )
                    if not good:
                        store.observe_submission(
                            conn,
                            owner,
                            frozen,
                            attempt,
                            "rejected",
                            dict(reason="invalid_collector_ack"),
                        )
                        stop = True
                    else:
                        with collector.engine.connect() as domain:
                            _verify_stage(domain, stage)
                        store.observe_submission(
                            conn,
                            owner,
                            frozen,
                            attempt,
                            "accepted_" + result.status,
                            dict(
                                status=result.status,
                                http_status=result.http_status,
                                event_id=result.event_id,
                                canonical_hash=result.canonical_hash,
                            ),
                        )
        if uncertain:
            raise CRMRefused("collector_return_unknown") from None
        if stop:
            return _progress(frozen, store.read_submission_history(owner, frozen))
    history = store.read_submission_history(owner, frozen)
    _verify_domain(collector, frozen, history[0])
    return _progress(frozen, history)


def resume_submission(store, collector, owner):
    try:
        return _resume_submission(store, collector, owner)
    except CRMRefused as exc:
        raise CRMRefused(exc.reason) from None
    except CollectorError as exc:
        reason = (
            "crm_schema_uninitialized"
            if exc.body.get("reason") == "crm_schema_uninitialized"
            else "submission_collector_refused"
        )
        raise CRMRefused(reason) from None
    except Exception:
        raise CRMRefused("submission_delivery_refused") from None
