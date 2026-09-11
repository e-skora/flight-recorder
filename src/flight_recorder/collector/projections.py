"""Cross-event validation and the domain projections written with each event.

Two steps run inside the collector's single transaction:

1. `validate_references` — every reference an envelope makes to an earlier event
   must resolve, belong to the same account, and respect the time boundary
   (INV-02, INV-04, INV-05). It performs no writes; a failure aborts the
   transaction before anything is written. Two exceptions are deliberate
   (D-013): an outcome schema v2 source claim is retained with a reason rather
   than rejected, and an `outcome.attributed` result is verified by
   recomputing `flight_recorder.attribution.policy` at its cutoff, the same
   code that computes it.
2. `project` — the accepted envelope is normalized into the tables of
   `PRODUCT.md` §6. Rows are appended and never updated; the database refuses
   the alternative (INV-01).

Both work from the *normalized* envelope (`model_dump(mode="json")`), which is
the same representation the canonical hash and the stored payload use, so a
projected value can never disagree with the stored event.
"""

import json
from typing import Any

from sqlalchemy import select

from flight_recorder.attribution import policy
from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.collector.errors import ConflictError, RejectedError
from flight_recorder.collector.schema import same_scalar
from flight_recorder.ledger.schema import (
    actions,
    decision_consumed_inputs,
    decision_context,
    decisions,
    evidence_versions,
    logic_artifacts,
    outcome_attributions,
    outcomes,
    persona_selections,
)

#: Item keys that identify an evidence version rather than describe its value.
#: Everything else is a semantic field and belongs in `value_json`.
_EVIDENCE_IDENTITY_KEYS = frozenset(
    {"evidence_version_id", "evidence_type", "supersedes_evidence_version_id"}
)


def _rejected(reason: str, detail: str, **extra: Any) -> RejectedError:
    return RejectedError({"status": "rejected", "reason": reason, "detail": detail, **extra})


def _conflict(reason: str, detail: str, **extra: Any) -> ConflictError:
    return ConflictError({"status": "conflict", "reason": reason, "detail": detail, **extra})


def evidence_value_json(item: dict) -> str:
    """Canonical JSON of an evidence item's semantic fields.

    Always `value`; plus `observed_at` or `basis` for the types that carry them.
    Identity and the supersession link are stored in their own columns.
    """
    return canonical_text({k: v for k, v in item.items() if k not in _EVIDENCE_IDENTITY_KEYS})


def stored_evidence_value(value_json: str) -> Any:
    """The `value` member of a stored evidence item, for type-exact comparison."""
    return json.loads(value_json)["value"]


# --- Validation -------------------------------------------------------------


def validate_references(conn, envelope, normalized: dict) -> None:
    """Check every cross-event reference the envelope makes. Writes nothing."""
    match normalized["event_type"]:
        case "logic_artifact.registered":
            _validate_artifact_identity(conn, normalized)
        case "evidence.recorded":
            _validate_evidence(conn, normalized)
        case "decision.recorded":
            _validate_decision(conn, normalized)
        case "persona.selected" | "action.recorded":
            _validate_decision_reference(conn, normalized)
        case "outcome.evaluated" if normalized["schema_version"] == "1":
            _validate_action_reference(conn, normalized)
        case "outcome.evaluated":
            _validate_outcome_supersession(conn, normalized)
        case "outcome.attributed":
            _validate_attribution(conn, normalized)


def _validate_artifact_identity(conn, normalized: dict) -> None:
    """A registered (`artifact_id`, `logic_version`) pair means one content hash."""
    artifact = normalized["payload"]["artifact"]
    digest = canonical_hash(artifact)
    already = conn.execute(
        select(logic_artifacts.c.artifact_hash).where(logic_artifacts.c.artifact_hash == digest)
    ).first()
    if already is not None:
        return  # same content, already registered; the event is still stored
    clash = conn.execute(
        select(logic_artifacts.c.artifact_hash).where(
            logic_artifacts.c.artifact_id == artifact["artifact_id"],
            logic_artifacts.c.logic_version == artifact["logic_version"],
        )
    ).first()
    if clash is not None:
        raise _conflict(
            "logic_artifact_identity_reused_with_different_content",
            f"artifact_id {artifact['artifact_id']!r} with logic_version "
            f"{artifact['logic_version']!r} is already registered under a different hash",
            artifact_id=artifact["artifact_id"],
            logic_version=artifact["logic_version"],
            stored_hash=clash.artifact_hash,
            submitted_hash=digest,
        )


def _validate_evidence(conn, normalized: dict) -> None:
    """An evidence version is minted exactly once; a supersession target pre-exists."""
    account_ref = normalized["account_ref"]
    available_at = normalized["recorded_at"]
    for item in normalized["payload"]["items"]:
        version_id = item["evidence_version_id"]
        existing = conn.execute(
            select(evidence_versions).where(evidence_versions.c.evidence_version_id == version_id)
        ).first()
        if existing is not None:
            # Content identity of an evidence version. `available_at` belongs
            # here because it decides which historical decisions the version was
            # eligible for (INV-02, INV-04); re-stamping it is a different
            # version, not a re-mint of this one. `source_event_id` does not: a
            # re-mint always arrives under a different event, and an identical
            # retry of the original event never reaches this function.
            submitted = (
                account_ref,
                item["evidence_type"],
                evidence_value_json(item),
                normalized["source"],
                item.get("observed_at"),
                available_at,
                item.get("supersedes_evidence_version_id"),
            )
            stored = (
                existing.account_ref,
                existing.evidence_type,
                existing.value_json,
                existing.source,
                existing.observed_at,
                existing.available_at,
                existing.supersedes_evidence_version_id,
            )
            if submitted == stored:
                raise _rejected(
                    "evidence_version_already_minted",
                    f"evidence_version_id {version_id!r} was already minted by event "
                    f"{existing.source_event_id!r}; an evidence version is minted once",
                    evidence_version_id=version_id,
                )
            raise _conflict(
                "evidence_version_id_reused_with_different_content",
                f"evidence_version_id {version_id!r} already exists with different content",
                evidence_version_id=version_id,
                stored_event_id=existing.source_event_id,
            )

        superseded = item.get("supersedes_evidence_version_id")
        if superseded is None:
            continue
        target = conn.execute(
            select(evidence_versions).where(evidence_versions.c.evidence_version_id == superseded)
        ).first()
        if target is None:
            raise _rejected(
                "unknown_superseded_evidence_version",
                f"evidence_version_id {version_id!r} supersedes {superseded!r}, which is not "
                "a stored evidence version; a correction may not supersede a version minted "
                "in the same envelope",
                evidence_version_id=version_id,
                supersedes_evidence_version_id=superseded,
            )
        if target.account_ref != account_ref:
            raise _rejected(
                "superseded_evidence_belongs_to_another_account",
                f"{superseded!r} belongs to account {target.account_ref!r}, not {account_ref!r}",
                supersedes_evidence_version_id=superseded,
            )
        if target.evidence_type != item["evidence_type"]:
            raise _rejected(
                "superseded_evidence_has_a_different_type",
                f"{superseded!r} is {target.evidence_type!r}, not {item['evidence_type']!r}",
                supersedes_evidence_version_id=superseded,
            )
        if target.available_at > available_at:
            raise _rejected(
                "superseded_evidence_is_later",
                f"{superseded!r} became available at {target.available_at}, after the "
                f"correction's availability time {available_at}",
                supersedes_evidence_version_id=superseded,
            )


def _decision_references(payload: dict) -> list[tuple[str, str, str, Any]]:
    """(section, input_key, evidence_version_id, preserved value) for every reference."""
    references = [
        ("historical_context", entry["input_key"], entry["evidence_version_id"], entry["value"])
        for entry in payload["historical_context"]
        if entry["availability"] == "available"
    ]
    references += [
        ("consumed_inputs", used["input_key"], used["evidence_version_id"], used["value"])
        for used in payload["consumed_inputs"]
    ]
    return references


def _validate_decision(conn, normalized: dict) -> None:
    """INV-02/INV-04/INV-05: every referenced evidence version and the artifact resolve."""
    payload = normalized["payload"]
    account_ref = normalized["account_ref"]
    boundary = payload["decision_boundary"]

    for section, input_key, version_id, value in _decision_references(payload):
        where = f"{section}[{input_key!r}]"
        row = conn.execute(
            select(evidence_versions).where(evidence_versions.c.evidence_version_id == version_id)
        ).first()
        if row is None:
            raise _rejected(
                "unknown_evidence_version",
                f"{where} references evidence version {version_id!r}, which is not stored",
                evidence_version_id=version_id,
            )
        if row.account_ref != account_ref:
            raise _rejected(
                "evidence_version_belongs_to_another_account",
                f"{where} references {version_id!r}, which belongs to account "
                f"{row.account_ref!r}, not {account_ref!r}",
                evidence_version_id=version_id,
            )
        if row.available_at > boundary:
            raise _rejected(
                "evidence_version_available_after_the_boundary",
                f"{where} references {version_id!r}, available at {row.available_at}, after "
                f"the decision boundary {boundary}",
                evidence_version_id=version_id,
            )
        if row.evidence_type != input_key:
            raise _rejected(
                "evidence_type_does_not_match_input_key",
                f"{where} references {version_id!r}, whose evidence_type is {row.evidence_type!r}",
                evidence_version_id=version_id,
            )
        stored = stored_evidence_value(row.value_json)
        if not same_scalar(value, stored):
            raise _rejected(
                "preserved_value_does_not_match_the_evidence_version",
                f"{where} preserves {value!r}, but evidence version {version_id!r} "
                f"recorded {stored!r}",
                evidence_version_id=version_id,
            )

    reference = payload["logic_artifact"]
    artifact = conn.execute(
        select(logic_artifacts).where(logic_artifacts.c.artifact_hash == reference["artifact_hash"])
    ).first()
    if artifact is None:
        raise _rejected(
            "unregistered_logic_artifact",
            f"logic_artifact.artifact_hash {reference['artifact_hash']!r} is not a registered "
            "artifact; register it with a logic_artifact.registered event first",
            artifact_hash=reference["artifact_hash"],
        )
    mismatched = {
        field: (getattr(artifact, field), expected)
        for field, expected in (
            ("artifact_id", reference["artifact_id"]),
            ("logic_version", reference["logic_version"]),
            ("evaluator_version", reference["evaluator_version"]),
            ("decision_class", payload["decision_class"]),
        )
        if getattr(artifact, field) != expected
    }
    if mismatched:
        raise _rejected(
            "logic_artifact_identity_mismatch",
            "the decision's logic identity disagrees with the registered artifact: "
            + "; ".join(
                f"{field} is {registered!r}, decision says {claimed!r}"
                for field, (registered, claimed) in sorted(mismatched.items())
            ),
            artifact_hash=reference["artifact_hash"],
        )


def _validate_decision_reference(conn, normalized: dict) -> None:
    """A persona selection or action follows a decision for the same account."""
    decision_event_id = normalized["payload"]["decision_event_id"]
    row = conn.execute(
        select(decisions).where(decisions.c.decision_event_id == decision_event_id)
    ).first()
    if row is None:
        raise _rejected(
            "unknown_decision_event_id",
            f"decision_event_id {decision_event_id!r} is not a recorded decision",
            decision_event_id=decision_event_id,
        )
    if row.account_ref != normalized["account_ref"]:
        raise _rejected(
            "decision_belongs_to_another_account",
            f"decision {decision_event_id!r} belongs to account {row.account_ref!r}, not "
            f"{normalized['account_ref']!r}",
            decision_event_id=decision_event_id,
        )
    if row.decision_boundary > normalized["occurred_at"]:
        raise _rejected(
            "decision_is_later_than_the_event",
            f"decision {decision_event_id!r} has boundary {row.decision_boundary}, after this "
            f"event's occurred_at {normalized['occurred_at']}",
            decision_event_id=decision_event_id,
        )


def _validate_action_reference(conn, normalized: dict) -> None:
    """INV-08 / D-007: an outcome is a *strictly* later observation of an action
    for the same account. Persona and action events may share their decision's
    boundary instant; an outcome may not share its action's."""
    action_event_id = normalized["payload"]["action_event_id"]
    row = conn.execute(select(actions).where(actions.c.action_event_id == action_event_id)).first()
    if row is None:
        raise _rejected(
            "unknown_action_event_id",
            f"action_event_id {action_event_id!r} is not a recorded action",
            action_event_id=action_event_id,
        )
    if row.account_ref != normalized["account_ref"]:
        raise _rejected(
            "action_belongs_to_another_account",
            f"action {action_event_id!r} belongs to account {row.account_ref!r}, not "
            f"{normalized['account_ref']!r}",
            action_event_id=action_event_id,
        )
    if row.occurred_at >= normalized["occurred_at"]:
        raise _rejected(
            "action_is_not_before_the_outcome",
            f"action {action_event_id!r} occurred at {row.occurred_at}, which is not before "
            f"the outcome's occurred_at {normalized['occurred_at']}; an outcome is a later "
            "observation of an action that already happened",
            action_event_id=action_event_id,
        )


def _validate_outcome_supersession(conn, normalized: dict) -> None:
    """INV-08 / D-013 for outcome schema v2.

    Source claims are *not* checked here: a well-formed claim that does not
    resolve is retained with a reason and accepted (see `_project_outcome`).
    Only the supersession link is a real reference: it must name a recorded
    outcome of the same account that is the current effective version of its
    chain, so two replacements of one version can never both be accepted.
    """
    superseded = normalized["payload"]["supersedes_outcome_event_id"]
    if superseded is None:
        return
    target = conn.execute(
        select(outcomes.c.account_ref).where(outcomes.c.outcome_event_id == superseded)
    ).first()
    if target is None:
        raise _rejected(
            "unknown_superseded_outcome",
            f"supersedes_outcome_event_id {superseded!r} is not a recorded outcome",
            supersedes_outcome_event_id=superseded,
        )
    if target.account_ref != normalized["account_ref"]:
        raise _rejected(
            "superseded_outcome_belongs_to_another_account",
            f"outcome {superseded!r} belongs to account {target.account_ref!r}, not "
            f"{normalized['account_ref']!r}",
            supersedes_outcome_event_id=superseded,
        )
    maximum = policy.ledger_maximum(conn)
    effective = policy.effective_outcome_version(conn, superseded, cutoff=maximum)
    if effective != superseded:
        raise _rejected(
            "superseded_outcome_is_not_effective",
            f"outcome {superseded!r} is already superseded; the current effective version of "
            f"its chain is {effective!r}, and only that version may be replaced",
            supersedes_outcome_event_id=superseded,
            effective_outcome_event_id=effective,
        )


def _attribution_failure(error: policy.AttributionError, **extra: Any) -> RejectedError:
    return _rejected(error.reason, str(error), **extra)


def _validate_attribution(conn, normalized: dict) -> None:
    """D-013: an attribution result is accepted only when the policy agrees.

    In order: the outcome exists and belongs to this account; the policy is
    implemented; the cutoff is a recorded sequence no later than the ledger's
    maximum and includes the outcome; the event id is this operation's derived
    identity and the operation is not already recorded; the supersession link
    is a first result or names the current effective result; the resolved
    references are real records of this account; and the submitted result
    equals `policy.attribute` recomputed at the cutoff, field by field. Every
    check reads; none writes.
    """
    payload = normalized["payload"]
    account_ref = normalized["account_ref"]
    outcome_event_id = payload["outcome_event_id"]
    policy_version = payload["policy_version"]
    cutoff = payload["ingest_cutoff"]

    outcome = conn.execute(
        select(outcomes.c.account_ref).where(outcomes.c.outcome_event_id == outcome_event_id)
    ).first()
    if outcome is None:
        raise _rejected(
            "unknown_outcome_event_id",
            f"outcome_event_id {outcome_event_id!r} is not a recorded outcome",
            outcome_event_id=outcome_event_id,
        )
    if outcome.account_ref != account_ref:
        raise _rejected(
            "attributed_outcome_belongs_to_another_account",
            f"outcome {outcome_event_id!r} belongs to account {outcome.account_ref!r}; an "
            f"attribution is recorded under the outcome's own account, not {account_ref!r}",
            outcome_event_id=outcome_event_id,
        )
    if policy_version not in policy.IMPLEMENTED_POLICY_VERSIONS:
        raise _attribution_failure(policy.UnsupportedPolicyVersion(policy_version))
    try:
        policy.validate_cutoff(conn, cutoff)
        policy.load_outcome(conn, outcome_event_id, cutoff=cutoff)
    except policy.AttributionError as error:
        raise _attribution_failure(error, ingest_cutoff=cutoff) from error

    expected_id = policy.attribution_event_id(outcome_event_id, policy_version, cutoff)
    operation = conn.execute(
        select(outcome_attributions.c.attribution_event_id).where(
            outcome_attributions.c.outcome_event_id == outcome_event_id,
            outcome_attributions.c.policy_version == policy_version,
            outcome_attributions.c.ingest_cutoff == cutoff,
        )
    ).first()
    if operation is not None:
        raise _rejected(
            "attribution_operation_already_recorded",
            f"outcome {outcome_event_id!r} under {policy_version!r} at ingest_cutoff {cutoff} "
            f"is already recorded as {operation.attribution_event_id!r}; a retry must resubmit "
            "that envelope, and a new event id is not a new operation",
            stored_event_id=operation.attribution_event_id,
        )
    if normalized["event_id"] != expected_id:
        raise _rejected(
            "attribution_event_id_is_not_the_operation_identity",
            f"event_id {normalized['event_id']!r} is not the identity derived from "
            f"(outcome_event_id, policy_version, ingest_cutoff), which is {expected_id!r}",
            expected_event_id=expected_id,
        )

    _validate_attribution_supersession(conn, payload)
    _validate_resolved_references(conn, payload, account_ref)

    try:
        recomputed = policy.attribute(
            conn, outcome_event_id, cutoff=cutoff, policy_version=policy_version
        )
    except policy.AttributionError as error:
        raise _attribution_failure(error, ingest_cutoff=cutoff) from error
    disagreements = {
        name: {"submitted": payload[name], "recomputed": getattr(recomputed, name)}
        for name in policy.POLICY_RESULT_FIELDS
        if payload[name] != getattr(recomputed, name)
    }
    if disagreements:
        raise _rejected(
            "attribution_disagrees_with_policy",
            f"{policy_version} recomputed for outcome {outcome_event_id!r} at ingest_cutoff "
            f"{cutoff} disagrees with the submitted result on: " + ", ".join(sorted(disagreements)),
            disagreements=disagreements,
        )


def _validate_attribution_supersession(conn, payload: dict) -> None:
    """Exactly one effective result per outcome version and policy (D-013)."""
    outcome_event_id = payload["outcome_event_id"]
    policy_version = payload["policy_version"]
    superseded = payload["supersedes_attribution_event_id"]
    maximum = policy.ledger_maximum(conn)
    current = policy.effective_attribution(conn, outcome_event_id, policy_version, cutoff=maximum)

    if superseded is None:
        if current is not None:
            raise _rejected(
                "attribution_already_recorded",
                f"outcome {outcome_event_id!r} already has a {policy_version} result, "
                f"{current.attribution_event_id!r}; a new result must supersede it explicitly",
                effective_attribution_event_id=current.attribution_event_id,
            )
        return

    target = conn.execute(
        select(outcome_attributions).where(
            outcome_attributions.c.attribution_event_id == superseded
        )
    ).first()
    if target is None:
        raise _rejected(
            "unknown_superseded_attribution",
            f"supersedes_attribution_event_id {superseded!r} is not a recorded attribution",
            supersedes_attribution_event_id=superseded,
        )
    if target.outcome_event_id != outcome_event_id:
        raise _rejected(
            "superseded_attribution_is_for_another_outcome",
            f"attribution {superseded!r} evaluates outcome {target.outcome_event_id!r}, not "
            f"{outcome_event_id!r}",
            supersedes_attribution_event_id=superseded,
        )
    if target.policy_version != policy_version:
        raise _rejected(
            "superseded_attribution_is_for_another_policy",
            f"attribution {superseded!r} is under {target.policy_version!r}, not "
            f"{policy_version!r}",
            supersedes_attribution_event_id=superseded,
        )
    if current is None or current.attribution_event_id != superseded:
        raise _rejected(
            "superseded_attribution_is_not_effective",
            f"attribution {superseded!r} is already superseded; the current effective result "
            f"is {current.attribution_event_id if current else None!r}, and only that result "
            "may be replaced",
            supersedes_attribution_event_id=superseded,
        )


def _validate_resolved_references(conn, payload: dict, account_ref: str) -> None:
    """Resolved references, unlike source claims, are real references."""
    action_id = payload["resolved_action_event_id"]
    if action_id is not None:
        row = conn.execute(
            select(actions.c.account_ref).where(actions.c.action_event_id == action_id)
        ).first()
        if row is None:
            raise _rejected(
                "unknown_resolved_action",
                f"resolved_action_event_id {action_id!r} is not a recorded action",
                resolved_action_event_id=action_id,
            )
        if row.account_ref != account_ref:
            raise _rejected(
                "resolved_action_belongs_to_another_account",
                f"action {action_id!r} belongs to account {row.account_ref!r}, not {account_ref!r}",
                resolved_action_event_id=action_id,
            )
    decision_id = payload["resolved_decision_event_id"]
    if decision_id is not None:
        row = conn.execute(
            select(decisions.c.account_ref).where(decisions.c.decision_event_id == decision_id)
        ).first()
        if row is None:
            raise _rejected(
                "unknown_resolved_decision",
                f"resolved_decision_event_id {decision_id!r} is not a recorded decision",
                resolved_decision_event_id=decision_id,
            )
        if row.account_ref != account_ref:
            raise _rejected(
                "resolved_decision_belongs_to_another_account",
                f"decision {decision_id!r} belongs to account {row.account_ref!r}, not "
                f"{account_ref!r}",
                resolved_decision_event_id=decision_id,
            )


# --- Projection -------------------------------------------------------------


def project(conn, normalized: dict, ingest_sequence: int) -> None:
    """Write the domain records this accepted event produces (PRODUCT.md §6)."""
    match normalized["event_type"]:
        case "logic_artifact.registered":
            _project_artifact(conn, normalized)
        case "evidence.recorded":
            _project_evidence(conn, normalized)
        case "decision.recorded":
            _project_decision(conn, normalized, ingest_sequence)
        case "persona.selected":
            _project_persona(conn, normalized)
        case "action.recorded":
            _project_action(conn, normalized)
        case "outcome.evaluated":
            _project_outcome(conn, normalized, ingest_sequence)
        case "outcome.attributed":
            _project_attribution(conn, normalized)


def _project_artifact(conn, normalized: dict) -> None:
    artifact = normalized["payload"]["artifact"]
    digest = canonical_hash(artifact)
    already = conn.execute(
        select(logic_artifacts.c.artifact_hash).where(logic_artifacts.c.artifact_hash == digest)
    ).first()
    if already is not None:
        # Re-registering identical content is a new event but the same artifact;
        # the row keeps the event_id of the registration that created it.
        return
    conn.execute(
        logic_artifacts.insert().values(
            artifact_hash=digest,
            artifact_id=artifact["artifact_id"],
            logic_version=artifact["logic_version"],
            decision_class=artifact["decision_class"],
            artifact_schema_version=artifact["artifact_schema_version"],
            evaluator_version=artifact["evaluator_version"],
            artifact_json=canonical_text(artifact),
            source_event_id=normalized["event_id"],
        )
    )


def _project_evidence(conn, normalized: dict) -> None:
    conn.execute(
        evidence_versions.insert(),
        [
            {
                "evidence_version_id": item["evidence_version_id"],
                "account_ref": normalized["account_ref"],
                "evidence_type": item["evidence_type"],
                "value_json": evidence_value_json(item),
                "source": normalized["source"],
                "observed_at": item.get("observed_at"),
                "available_at": normalized["recorded_at"],
                "source_event_id": normalized["event_id"],
                "supersedes_evidence_version_id": item.get("supersedes_evidence_version_id"),
            }
            for item in normalized["payload"]["items"]
        ],
    )


def _project_decision(conn, normalized: dict, ingest_sequence: int) -> None:
    payload = normalized["payload"]
    event_id = normalized["event_id"]
    reference = payload["logic_artifact"]
    conn.execute(
        decisions.insert().values(
            decision_event_id=event_id,
            account_ref=normalized["account_ref"],
            decision_class=payload["decision_class"],
            decision_boundary=payload["decision_boundary"],
            workflow_version=payload["workflow_version"],
            artifact_hash=reference["artifact_hash"],
            evaluator_version=reference["evaluator_version"],
            logic_version=reference["logic_version"],
            score=payload["result"]["score"],
            threshold=payload["result"]["threshold"],
            output=payload["result"]["output"],
            explanation=payload["explanation"],
            ingest_sequence=ingest_sequence,
        )
    )
    conn.execute(
        decision_context.insert(),
        [
            {
                "decision_event_id": event_id,
                "input_key": entry["input_key"],
                "availability": entry["availability"],
                "value_text": (
                    canonical_text(entry["value"]) if entry["availability"] == "available" else None
                ),
                "evidence_version_id": entry["evidence_version_id"],
            }
            for entry in payload["historical_context"]
        ],
    )
    conn.execute(
        decision_consumed_inputs.insert(),
        [
            {
                "decision_event_id": event_id,
                "input_key": used["input_key"],
                "value_text": canonical_text(used["value"]),
                "evidence_version_id": used["evidence_version_id"],
                "contribution": used["contribution"],
            }
            for used in payload["consumed_inputs"]
        ],
    )


def _project_persona(conn, normalized: dict) -> None:
    payload = normalized["payload"]
    conn.execute(
        persona_selections.insert().values(
            event_id=normalized["event_id"],
            account_ref=normalized["account_ref"],
            decision_event_id=payload["decision_event_id"],
            persona=payload["persona"],
            explanation=payload["explanation"],
        )
    )


def _project_action(conn, normalized: dict) -> None:
    payload = normalized["payload"]
    conn.execute(
        actions.insert().values(
            action_event_id=normalized["event_id"],
            account_ref=normalized["account_ref"],
            decision_event_id=payload["decision_event_id"],
            action_type=payload["action_type"],
            play_id=payload["play_id"],
            target_persona=payload["target_persona"],
            status=payload["status"],
            cost=payload["cost"],
            currency=payload["currency"],
            occurred_at=normalized["occurred_at"],
        )
    )


def _project_outcome(conn, normalized: dict, ingest_sequence: int) -> None:
    """One `outcomes` row. A v1 row writes exactly the columns it always wrote,
    plus its schema version; every v2-only column stays NULL."""
    payload = normalized["payload"]
    if normalized["schema_version"] == "1":
        conn.execute(
            outcomes.insert().values(
                outcome_event_id=normalized["event_id"],
                account_ref=normalized["account_ref"],
                action_event_id=payload["action_event_id"],
                window_days=payload["window_days"],
                reply=payload["reply"],
                meeting=payload["meeting"],
                opportunity=payload["opportunity"],
                occurred_at=normalized["occurred_at"],
                recorded_at=normalized["recorded_at"],
                schema_version=normalized["schema_version"],
            )
        )
        return

    # Each claim is retained unmodified with the reason it was unusable as of
    # this outcome's own ingestion. The snapshot is this event's sequence: the
    # ledger exactly as it stood when the observation was recorded.
    action_reason, decision_reason = policy.source_claim_problems(
        conn,
        account_ref=normalized["account_ref"],
        observation_instant=payload["observed_at"],
        source_action_event_id=payload["source_action_event_id"],
        source_decision_event_id=payload["source_decision_event_id"],
        cutoff=ingest_sequence,
    )
    conn.execute(
        outcomes.insert().values(
            outcome_event_id=normalized["event_id"],
            account_ref=normalized["account_ref"],
            action_event_id=None,
            window_days=None,
            reply=payload["reply"],
            meeting=payload["meeting"],
            opportunity=payload["opportunity"],
            occurred_at=normalized["occurred_at"],
            recorded_at=normalized["recorded_at"],
            schema_version=normalized["schema_version"],
            window_opened_at=payload["window_opened_at"],
            window_closes_at=payload["window_closes_at"],
            evaluation_state=payload["evaluation_state"],
            observed_at=payload["observed_at"],
            source_action_event_id=payload["source_action_event_id"],
            source_action_unusable_reason=action_reason,
            source_decision_event_id=payload["source_decision_event_id"],
            source_decision_unusable_reason=decision_reason,
            supersedes_outcome_event_id=payload["supersedes_outcome_event_id"],
        )
    )


def _project_attribution(conn, normalized: dict) -> None:
    payload = normalized["payload"]
    conn.execute(
        outcome_attributions.insert().values(
            attribution_event_id=normalized["event_id"],
            account_ref=normalized["account_ref"],
            source_event_id=normalized["event_id"],
            outcome_event_id=payload["outcome_event_id"],
            policy_version=payload["policy_version"],
            method=payload["method"],
            window_days=payload["window_days"],
            resolved_action_event_id=payload["resolved_action_event_id"],
            resolved_decision_event_id=payload["resolved_decision_event_id"],
            status=payload["status"],
            reason=payload["reason"],
            attributed_at=payload["attributed_at"],
            ingest_cutoff=payload["ingest_cutoff"],
            supersedes_attribution_event_id=payload["supersedes_attribution_event_id"],
        )
    )
