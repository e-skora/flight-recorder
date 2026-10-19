"""Local synthetic submission: freeze first, then bounded real-Collector calls."""

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from flight_recorder.adapters.hubspot.contracts import strict_json_loads, typed_equal
from flight_recorder.adapters.hubspot.submission_contracts import (
    FrozenSubmission,
    SubmissionError,
    SubmissionPrerequisites,
    SubmissionProgress,
    digest,
    normalized,
    require,
)
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash, canonical_text
from flight_recorder.collector.capture_contract import (
    SOURCE,
    account_identity,
    available_keys,
    bound_item,
    expected_context,
    observation_body,
    validate_capture,
)
from flight_recorder.collector.errors import CollectorError
from flight_recorder.collector.provenance import _lineage, _verify_evidence
from flight_recorder.collector.schema import format_utc
from flight_recorder.ledger.schema import (
    accounts,
    capture_contexts,
    events,
    evidence_versions_v2,
    logic_artifacts,
)
from flight_recorder.logic.evaluator import ContextInput, InputState, evaluate
from flight_recorder.replay.reconstruct import (
    ArtifactRow,
    load_artifact_row,
    reconstruct,
    verify_artifact_row,
)


def _target(collector):
    url = collector.engine.url
    require(
        url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"),
        "file_backed_local_collector_required",
    )
    return str(Path(url.database).resolve())


def _event(conn, event_id):
    row = conn.execute(select(events).where(events.c.event_id == event_id)).first()
    if row is None:
        return None
    envelope = {
        key: getattr(row, key)
        for key in (
            "event_id",
            "schema_version",
            "event_type",
            "source",
            "account_ref",
            "occurred_at",
            "recorded_at",
        )
    }
    envelope["payload"] = strict_json_loads(row.payload.encode())
    checked = normalized(canonical_bytes(envelope))
    require(
        typed_equal(envelope, checked) and canonical_hash(checked) == row.canonical_hash,
        "ledger_event_integrity",
    )
    return checked


def _artifact(registration, cap):
    require(
        registration["schema_version"] == "1"
        and registration["event_type"] == "logic_artifact.registered",
        "artifact_prerequisite_type",
    )
    a = registration["payload"]["artifact"]
    fields = (
        "artifact_id",
        "artifact_schema_version",
        "logic_version",
        "decision_class",
        "evaluator_version",
    )
    row = ArtifactRow(
        artifact_hash=canonical_hash(a),
        artifact_json=canonical_text(a),
        **{k: a[k] for k in fields},
    )
    verified = verify_artifact_row(row, cap["rule_hash"]).artifact
    require(
        row.artifact_hash == cap["rule_hash"]
        and a["evaluator_version"] == cap["evaluator_version"],
        "selected_artifact_mismatch",
    )
    return verified


def _prerequisites(conn, cap, supplied_account, supplied_artifact):
    account = account_identity(cap)
    require(
        supplied_account["schema_version"] == "1"
        and supplied_account["event_type"] == "account.discovered"
        and supplied_account["account_ref"] == account,
        "account_prerequisite_type_or_identity",
    )
    _artifact(supplied_artifact, cap)
    row = conn.execute(select(accounts).where(accounts.c.account_ref == account)).first()
    if row:
        require(
            (row.name, row.domain)
            == (supplied_account["payload"]["name"], supplied_account["payload"]["domain"]),
            "account_identity_mismatch",
        )
        chosen_account = _event(conn, row.first_seen_event_id)
        require(
            chosen_account is not None
            and chosen_account["event_type"] == "account.discovered"
            and chosen_account["account_ref"] == account
            and typed_equal(chosen_account["payload"], supplied_account["payload"]),
            "discovery_projection_integrity",
        )
    else:
        require(_event(conn, supplied_account["event_id"]) is None, "missing_account_projection")
        chosen_account = supplied_account
    row = conn.execute(
        select(logic_artifacts).where(logic_artifacts.c.artifact_hash == cap["rule_hash"])
    ).first()
    if row:
        verify_artifact_row(load_artifact_row(conn, cap["rule_hash"]), cap["rule_hash"])
        chosen_artifact = _event(conn, row.source_event_id)
        require(
            chosen_artifact is not None
            and typed_equal(chosen_artifact["payload"], supplied_artifact["payload"]),
            "registration_projection_integrity",
        )
        _artifact(chosen_artifact, cap)
    else:
        require(_event(conn, supplied_artifact["event_id"]) is None, "missing_artifact_projection")
        chosen_artifact = supplied_artifact
    return chosen_account, chosen_artifact


def _reuse(conn, account, cap, item):
    row = conn.execute(
        select(evidence_versions_v2).where(
            evidence_versions_v2.c.evidence_version_id == item["evidence_version_id"]
        )
    ).first()
    if row is None:
        return None
    _verify_evidence(row, account, cap, item)
    origin = conn.execute(
        select(capture_contexts).where(capture_contexts.c.capture_hash == row.capture_hash)
    ).first()
    require(origin is not None, "missing_evidence_origin")
    old = strict_json_loads(origin.body_json.encode())
    validate_capture(old, origin.capture_hash)
    require(
        origin.body_json == canonical_text(old)
        and origin.account_ref == account
        and origin.unit_id == old["unit_id"]
        and origin.capture_sequence == old["capture_sequence"],
        "evidence_origin_integrity",
    )
    _verify_evidence(
        row,
        account,
        old,
        bound_item(account, old, item["evidence_type"], item["supersedes_evidence_version_id"]),
    )
    return {
        "input_key": item["evidence_type"],
        "evidence_version_id": row.evidence_version_id,
        "observation": observation_body(account, cap, item),
        "origin_capture_hash": row.capture_hash,
    }


def _evaluation(artifact, cap, context, items):
    by_key = {item["evidence_type"]: item for item in items}
    inputs = [
        ContextInput(
            key=x["input_key"],
            availability=x["availability"],
            value=x["value"],
            evidence_version_id=x["evidence_version_id"],
            observed_at=(
                date.fromisoformat(by_key[x["input_key"]]["observed_at"])
                if x["availability"] == "available" and by_key[x["input_key"]].get("observed_at")
                else None
            ),
        )
        for x in context
    ]
    result = evaluate(artifact, inputs, datetime.fromisoformat(cap["capture_boundary"]))
    context_by_key = {x["input_key"]: x for x in context}
    used = []
    for factor in result.factors:
        if factor.input_state is InputState.CONSUMED:
            require(
                cap["inputs"][factor.key]["state"] != "ignored",
                "explicit_ignore_conflicts_with_selected_logic",
            )
            x = context_by_key[factor.key]
            used.append(
                {
                    "input_key": factor.key,
                    "value": x["value"],
                    "evidence_version_id": factor.evidence_version_id,
                    "contribution": factor.contribution,
                }
            )
    require(bool(used), "no_usable_inputs")
    return used, {"score": result.score, "threshold": result.threshold, "output": result.output}


def freeze_submission(store, collector, unit, *, prerequisites):
    target = _target(collector)
    store.target(target)
    existing = store.load(unit)
    if existing is not None:
        return existing
    require(isinstance(prerequisites, SubmissionPrerequisites), "submission_prerequisites_required")
    frozen_cap = store.capture(unit)
    cap = frozen_cap.document
    account = account_identity(cap)
    try:
        timestamp = datetime.fromisoformat(prerequisites.recorded_at)
        require(timestamp.utcoffset() is not None, "aware_recorded_at_required")
        require(
            timestamp >= datetime.fromisoformat(cap["capture_boundary"]),
            "submission_recorded_before_capture",
        )
        recorded = format_utc(timestamp)
        supersessions = strict_json_loads(prerequisites.supersessions)
        require(
            type(supersessions) is dict and set(supersessions) <= set(available_keys(cap)),
            "invalid_supersession_keys",
        )
        require(
            all(
                x is None
                or (
                    isinstance(x, str)
                    and x.startswith("fr-evidence-v2-")
                    and len(x) == 79
                    and all(c in "0123456789abcdef" for c in x[15:])
                )
                for x in supersessions.values()
            ),
            "invalid_supersession_reference",
        )
        supplied_account = normalized(prerequisites.account_envelope)
        supplied_artifact = normalized(prerequisites.artifact_envelope)
        with collector.engine.connect() as conn:
            chosen_account, registration = _prerequisites(
                conn, cap, supplied_account, supplied_artifact
            )
            _lineage(conn, account, cap)
            artifact = _artifact(registration, cap)
            items = [
                bound_item(account, cap, key, supersessions.get(key)) for key in available_keys(cap)
            ]
            new, reused = [], []
            for item in items:
                predecessor = item["supersedes_evidence_version_id"]
                if predecessor:
                    prior = conn.execute(
                        select(evidence_versions_v2).where(
                            evidence_versions_v2.c.evidence_version_id == predecessor
                        )
                    ).first()
                    require(
                        prior is not None
                        and prior.account_ref == account
                        and prior.evidence_type == item["evidence_type"]
                        and prior.available_at
                        <= observation_body(account, cap, item)["eligible_at"],
                        "invalid_supersession_lineage",
                    )
                proof = _reuse(conn, account, cap, item)
                if proof is None:
                    new.append(item)
                else:
                    reused.append(proof)
        context = expected_context(account, cap, supersessions)
        consumed, result = _evaluation(artifact, cap, context, items)
        reference = {
            key: getattr(artifact, key)
            for key in ("artifact_id", "logic_version", "evaluator_version")
        }
        reference["artifact_hash"] = cap["rule_hash"]
        binding = {"capture": cap, "capture_hash": canonical_hash(cap)}
        common = {
            "schema_version": "2",
            "source": SOURCE,
            "account_ref": account,
            "recorded_at": recorded,
        }
        stages = [("account", chosen_account), ("artifact", registration)]
        if new:
            stages.append(
                (
                    "evidence",
                    {
                        **common,
                        "event_type": "evidence.recorded",
                        "event_id": "fr-submit-evidence-v1-" + unit.unit_id,
                        "occurred_at": cap["read_finished_at"],
                        "payload": {**binding, "items": new},
                    },
                )
            )
        stages.append(
            (
                "decision",
                {
                    **common,
                    "event_type": "decision.recorded",
                    "event_id": "fr-submit-decision-v1-" + unit.unit_id,
                    "occurred_at": cap["capture_boundary"],
                    "payload": {
                        **binding,
                        "decision_class": "account_prioritization",
                        "decision_boundary": cap["capture_boundary"],
                        "workflow_version": cap["processing_version"],
                        "historical_context": context,
                        "consumed_inputs": consumed,
                        "logic_artifact": reference,
                        "result": result,
                        "explanation": None,
                    },
                },
            )
        )
        normalized_stages = []
        for name, envelope in stages:
            n = normalized(canonical_bytes(envelope))
            normalized_stages.append(
                {
                    "stage": name,
                    "event_id": n["event_id"],
                    "canonical_hash": canonical_hash(n),
                    "envelope": n,
                }
            )
        candidate = FrozenSubmission.from_document(
            {
                "format_version": 1,
                "unit_id": unit.unit_id,
                "capture_hash": canonical_hash(cap),
                "capture_bytes_sha256": digest(frozen_cap.canonical_bytes),
                "account_ref": account,
                "stages": normalized_stages,
                "reused_evidence": reused,
            }
        )
        return store.freeze(unit, candidate, target)
    except SubmissionError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise SubmissionError("submission_preparation_refused") from exc


def _verify_stage(conn, stage):
    envelope = stage["envelope"]
    actual = _event(conn, stage["event_id"])
    require(
        actual is not None and typed_equal(actual, envelope),
        "acknowledged_event_missing_or_changed",
    )
    p = envelope["payload"]
    if stage["stage"] == "account":
        row = conn.execute(
            select(accounts).where(accounts.c.account_ref == envelope["account_ref"])
        ).first()
        require(
            row is not None and (row.name, row.domain) == (p["name"], p["domain"]),
            "acknowledged_account_missing_or_changed",
        )
        origin = _event(conn, row.first_seen_event_id)
        require(
            origin is not None
            and origin["event_type"] == "account.discovered"
            and origin["account_ref"] == envelope["account_ref"]
            and typed_equal(origin["payload"], p),
            "discovery_projection_integrity",
        )
    elif stage["stage"] == "artifact":
        cap = {
            "rule_hash": canonical_hash(p["artifact"]),
            "evaluator_version": p["artifact"]["evaluator_version"],
        }
        _artifact(envelope, cap)
        verify_artifact_row(load_artifact_row(conn, cap["rule_hash"]), cap["rule_hash"])
    elif stage["stage"] == "evidence":
        for item in p["items"]:
            proof = _reuse(conn, envelope["account_ref"], p["capture"], item)
            require(
                proof is not None and proof["origin_capture_hash"] == p["capture_hash"],
                "acknowledged_evidence_missing_or_changed",
            )
            row = conn.execute(
                select(evidence_versions_v2.c.source_event_id).where(
                    evidence_versions_v2.c.evidence_version_id == item["evidence_version_id"]
                )
            ).first()
            require(row.source_event_id == envelope["event_id"], "evidence_event_link_changed")
    else:
        result = reconstruct(conn, envelope["event_id"]).result
        require(
            typed_equal(
                {"score": result.score, "threshold": result.threshold, "output": result.output},
                p["result"],
            ),
            "acknowledged_decision_changed",
        )


def _verify_domain(collector, frozen, accepted):
    d = frozen.document
    cap = d["stages"][-1]["envelope"]["payload"]["capture"]
    with collector.engine.connect() as conn:
        for proof in d["reused_evidence"]:
            item = bound_item(
                d["account_ref"],
                cap,
                proof["input_key"],
                proof["observation"]["supersedes_evidence_version_id"],
            )
            require(
                typed_equal(_reuse(conn, d["account_ref"], cap, item), proof),
                "reused_evidence_integrity",
            )
        for stage in d["stages"]:
            if stage["stage"] in accepted or _event(conn, stage["event_id"]) is not None:
                _verify_stage(conn, stage)
        # Concurrent discovery/registration may precede this plan's event IDs.
        _prerequisites(conn, cap, d["stages"][0]["envelope"], d["stages"][1]["envelope"])


def _progress(frozen, accepted, failure, latest):
    d = frozen.document
    stage = next((s["stage"] for s in d["stages"] if s["stage"] not in accepted), None)
    state = "complete" if stage is None else ("frozen" if latest is None else "incomplete")
    detail = None
    if failure:
        stage, detail = failure
        state = "failed"
    elif latest and latest[0] == stage and latest[1] == "retryable":
        state, detail = "retryable", latest[2]
    return SubmissionProgress(
        d["unit_id"], frozen.submission_hash, state, stage, d["stages"][-1]["event_id"], detail
    )


def resume_submission(store, collector, unit):
    store.target(_target(collector))
    frozen = store.load(unit)
    require(frozen is not None, "missing_frozen_submission")
    for stage in frozen.document["stages"]:
        accepted, failure, latest = store.history(frozen)
        _verify_domain(collector, frozen, accepted)
        if failure:
            return _progress(frozen, accepted, failure, latest)
        if stage["stage"] in accepted:
            continue
        attempt = store.attempt(frozen, stage)
        try:
            result = collector.ingest_json(canonical_bytes(stage["envelope"]))
        except CollectorError as exc:
            retryable = (
                exc.status_code == 409 and exc.body.get("reason") == "v2_admission_contended"
            )
            store.observe(attempt, "retryable" if retryable else "rejected", exc.body)
            return _progress(frozen, *store.history(frozen))
        except Exception:
            try:
                store.observe(attempt, "unknown", {"reason": "collector_return_unknown"})
            except Exception:
                pass  # Original infrastructure error remains visible; no success is inferred.
            raise
        if not (
            getattr(result, "status", None) in ("created", "duplicate")
            and type(getattr(result, "http_status", None)) is int
            and getattr(result, "http_status", None) == (201 if result.status == "created" else 200)
            and getattr(result, "event_id", None) == stage["event_id"]
            and getattr(result, "canonical_hash", None) == stage["canonical_hash"]
        ):
            store.observe(attempt, "rejected", {"reason": "invalid_collector_ack"})
            return _progress(frozen, *store.history(frozen))
        with collector.engine.connect() as conn:
            _verify_stage(conn, stage)
        store.observe(
            attempt,
            "accepted_" + result.status,
            {
                "status": result.status,
                "http_status": result.http_status,
                "event_id": result.event_id,
                "canonical_hash": result.canonical_hash,
            },
        )
    accepted, failure, latest = store.history(frozen)
    _verify_domain(collector, frozen, accepted)
    return _progress(frozen, accepted, failure, latest)
