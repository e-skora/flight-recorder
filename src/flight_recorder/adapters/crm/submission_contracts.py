"""Immutable operational plans around unchanged Collector envelope contracts."""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from flight_recorder.adapters.crm.contracts import (
    CRMCapture,
    CRMRefused,
    CRMTarget,
    require,
    validate_unit_id,
)
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash, canonical_text
from flight_recorder.collector.crm_schema import available_keys, bound_item, expected_context
from flight_recorder.collector.schema import format_utc, validate_envelope_json
from flight_recorder.logic.evaluator import ContextInput, EvaluationError, InputState, evaluate
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.reconstruct import ArtifactRow, ReconstructionError, verify_artifact_row


def normalize(body):
    try:
        strict_json(body)
        return validate_envelope_json(body).model_dump(mode="json")
    except (ValueError, TypeError, KeyError, RecursionError):
        raise CRMRefused("invalid_submission_envelope") from None


@dataclass(frozen=True, repr=False)
class CRMPrerequisites:
    account_envelope: bytes
    artifact_envelope: bytes
    recorded_at: str
    supersessions: bytes

    def __post_init__(self):
        require(
            type(self.account_envelope) is bytes
            and type(self.artifact_envelope) is bytes
            and type(self.supersessions) is bytes
            and type(self.recorded_at) is str,
            "invalid_submission_prerequisites",
        )

    def __repr__(self):
        return "CRMPrerequisites(<frozen envelopes>)"


def prerequisite_binding(prerequisites):
    require(type(prerequisites) is CRMPrerequisites, "invalid_submission_prerequisites")
    try:
        account = normalize(prerequisites.account_envelope)
        artifact = normalize(prerequisites.artifact_envelope)
        stamp = datetime.fromisoformat(prerequisites.recorded_at)
        require(stamp.utcoffset() is not None, "invalid_submission_prerequisites")
        supersessions = strict_json(prerequisites.supersessions)
        require(
            type(supersessions) is dict
            and all(
                type(key) is str
                and (
                    value is None
                    or type(value) is str
                    and re.fullmatch(r"fr-crm-evidence-version-v1-[0-9a-f]{64}", value)
                )
                for key, value in supersessions.items()
            ),
            "invalid_submission_prerequisites",
        )
        require(
            account["schema_version"] == "1"
            and account["event_type"] == "account.discovered"
            and artifact["schema_version"] == "1"
            and artifact["event_type"] == "logic_artifact.registered",
            "invalid_submission_prerequisites",
        )
        return dict(
            account=account,
            artifact=artifact,
            recorded_at=format_utc(stamp),
            supersessions=supersessions,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CRMRefused("invalid_submission_prerequisites") from None


def original_evaluation(artifact_document, cap, supersessions):
    """Build sealed inputs, then call the one shared evaluator, never a new scorer."""
    require(canonical_hash(artifact_document) == cap["rule_hash"], "selected_artifact_mismatch")
    try:
        row = ArtifactRow(
            artifact_hash=canonical_hash(artifact_document),
            artifact_json=canonical_text(artifact_document),
            **{
                key: artifact_document[key]
                for key in [
                    "artifact_id",
                    "artifact_schema_version",
                    "logic_version",
                    "decision_class",
                    "evaluator_version",
                ]
            },
        )
        artifact = verify_artifact_row(row, cap["rule_hash"]).artifact
    except ReconstructionError:
        raise CRMRefused("selected_artifact_unsupported") from None
    require(
        artifact.evaluator_version == cap["evaluator_version"]
        and artifact.decision_class == "account_prioritization",
        "selected_artifact_mismatch",
    )
    account = CRMTarget(**cap["target"]).account_ref
    require(set(supersessions) <= set(available_keys(cap)), "invalid_supersession_keys")
    items = [bound_item(account, cap, key, supersessions.get(key)) for key in available_keys(cap)]
    bykey = {item["evidence_type"]: item for item in items}
    context = expected_context(account, cap, supersessions)
    result = evaluate(
        artifact,
        [
            ContextInput(
                key=x["input_key"],
                availability=x["availability"],
                value=x["value"],
                evidence_version_id=x["evidence_version_id"],
                observed_at=date.fromisoformat(bykey[x["input_key"]]["observed_at"])
                if x["input_key"] in bykey and bykey[x["input_key"]].get("observed_at")
                else None,
            )
            for x in context
        ],
        datetime.fromisoformat(cap["capture_boundary"]),
    )
    entries = {x["input_key"]: x for x in context}
    consumed = [
        dict(
            input_key=factor.key,
            value=entries[factor.key]["value"],
            evidence_version_id=factor.evidence_version_id,
            contribution=factor.contribution,
        )
        for factor in result.factors
        if factor.input_state is InputState.CONSUMED
    ]
    require(bool(consumed), "no_usable_inputs")
    return (
        items,
        context,
        consumed,
        dict(score=result.score, threshold=result.threshold, output=result.output),
    )


@dataclass(frozen=True, repr=False)
class CRMSubmission:
    canonical_bytes: bytes

    def __post_init__(self):
        try:
            require(
                type(self.canonical_bytes) is bytes
                and len(self.canonical_bytes) <= 16 * 1024 * 1024,
                "invalid_frozen_submission",
            )
            # Each envelope is separately checked below with the owned strict JSON boundary.
            import json

            d = json.loads(self.canonical_bytes.decode("utf-8"))
            require(
                canonical_bytes(d) == self.canonical_bytes
                and set(d)
                == {
                    "format_version",
                    "unit_id",
                    "capture_hash",
                    "capture_bytes_sha256",
                    "target_key",
                    "claim_id",
                    "ownership_generation",
                    "domain_path",
                    "prerequisite_binding",
                    "prerequisite_hash",
                    "stages",
                    "reused_evidence",
                },
                "invalid_frozen_submission",
            )
            require(
                type(d["format_version"]) is int
                and d["format_version"] == 1
                and type(d["ownership_generation"]) is int
                and d["ownership_generation"] > 0,
                "invalid_frozen_submission",
            )
            for key in [
                "unit_id",
                "capture_hash",
                "capture_bytes_sha256",
                "claim_id",
                "prerequisite_hash",
            ]:
                validate_unit_id(d[key])
            require(
                type(d["domain_path"]) is str
                and Path(d["domain_path"]).is_absolute()
                and ".." not in Path(d["domain_path"]).parts,
                "invalid_frozen_submission",
            )
            pri = d["prerequisite_binding"]
            require(
                type(pri) is dict
                and set(pri) == {"account", "artifact", "recorded_at", "supersessions"},
                "invalid_frozen_submission",
            )
            require(
                prerequisite_binding(
                    CRMPrerequisites(
                        canonical_bytes(pri["account"]),
                        canonical_bytes(pri["artifact"]),
                        pri["recorded_at"],
                        canonical_bytes(pri["supersessions"]),
                    )
                )
                == pri
                and canonical_hash(pri) == d["prerequisite_hash"],
                "invalid_frozen_submission",
            )
            stages = d["stages"]
            require(
                type(stages) is list
                and [s.get("stage") for s in stages]
                in [
                    ["account", "artifact", "evidence", "decision"],
                    ["account", "artifact", "decision"],
                ],
                "invalid_frozen_submission",
            )
            for stage in stages:
                require(
                    type(stage) is dict
                    and set(stage) == {"stage", "event_id", "canonical_hash", "envelope"}
                    and normalize(canonical_bytes(stage["envelope"])) == stage["envelope"]
                    and stage["event_id"] == stage["envelope"]["event_id"]
                    and stage["canonical_hash"] == canonical_hash(stage["envelope"]),
                    "invalid_frozen_submission",
                )
            decision = stages[-1]["envelope"]
            p = decision["payload"]
            cap = CRMCapture.from_document(p["capture"])
            c = cap.document
            target = CRMTarget(**c["target"])
            require(
                (
                    d["unit_id"],
                    d["capture_hash"],
                    d["capture_bytes_sha256"],
                    d["target_key"],
                    d["ownership_generation"],
                )
                == (
                    cap.unit_id,
                    cap.capture_hash,
                    hashlib.sha256(cap.canonical_bytes).hexdigest(),
                    target.key,
                    c["ownership_generation"],
                ),
                "invalid_frozen_submission",
            )
            require(
                decision["schema_version"] == "3"
                and decision["source"] == "salesforce-test-account"
                and decision["account_ref"] == target.account_ref
                and decision["event_id"] == "fr-crm-decision-v1-" + cap.unit_id
                and p["capture_hash"] == cap.capture_hash
                and decision["recorded_at"] == pri["recorded_at"]
                and decision["occurred_at"] == c["capture_boundary"]
                and decision["recorded_at"] >= c["capture_boundary"],
                "invalid_frozen_submission",
            )
            registration = stages[1]["envelope"]
            require(
                registration["schema_version"] == "1"
                and registration["event_type"] == "logic_artifact.registered"
                and stages[0]["envelope"]["schema_version"] == "1"
                and stages[0]["envelope"]["event_type"] == "account.discovered"
                and stages[0]["envelope"]["account_ref"] == target.account_ref
                and stages[0]["envelope"]["source"] == "salesforce-test-account"
                and pri["account"]["account_ref"] == target.account_ref
                and pri["account"]["source"] == "salesforce-test-account"
                and canonical_bytes(pri["artifact"]["payload"]["artifact"])
                == canonical_bytes(registration["payload"]["artifact"]),
                "invalid_frozen_submission",
            )
            items, context, consumed, result = original_evaluation(
                registration["payload"]["artifact"], c, pri["supersessions"]
            )
            require(
                canonical_bytes((context, consumed, result))
                == canonical_bytes((p["historical_context"], p["consumed_inputs"], p["result"]))
                and p["logic_artifact"]
                == dict(
                    artifact_hash=c["rule_hash"],
                    **{
                        key: registration["payload"]["artifact"][key]
                        for key in ["artifact_id", "logic_version", "evaluator_version"]
                    },
                )
                and p["decision_class"] == "account_prioritization"
                and p["workflow_version"] == c["processing_version"]
                and p["explanation"] is None,
                "invalid_frozen_submission",
            )
            require(type(d["reused_evidence"]) is list, "invalid_frozen_submission")
            if len(stages) == 4:
                evidence = stages[2]["envelope"]
                require(
                    not d["reused_evidence"]
                    and evidence["schema_version"] == "3"
                    and evidence["event_type"] == "evidence.recorded"
                    and evidence["event_id"] == "fr-crm-evidence-v1-" + cap.unit_id
                    and evidence["account_ref"] == target.account_ref
                    and evidence["occurred_at"] == c["read_finished_at"]
                    and evidence["recorded_at"] == pri["recorded_at"]
                    and canonical_bytes(evidence["payload"])
                    == canonical_bytes(dict(capture=c, capture_hash=cap.capture_hash, items=items)),
                    "invalid_frozen_submission",
                )
            else:
                require(
                    {x["evidence_version_id"] for x in d["reused_evidence"]}
                    == {x["evidence_version_id"] for x in items}
                    and len(d["reused_evidence"]) == len(items),
                    "invalid_frozen_submission",
                )
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            OverflowError,
            RuleError,
            EvaluationError,
        ):
            raise CRMRefused("invalid_frozen_submission") from None

    @classmethod
    def from_document(cls, document):
        try:
            return cls(canonical_bytes(document))
        except (ValueError, TypeError, RecursionError):
            raise CRMRefused("invalid_frozen_submission") from None

    @property
    def document(self):
        import json

        return json.loads(self.canonical_bytes)

    @property
    def submission_hash(self):
        return canonical_hash(self.document)

    def __repr__(self):
        return "CRMSubmission(<frozen envelopes>)"


@dataclass(frozen=True)
class SubmissionProgress:
    unit_id: str
    submission_hash: str
    state: str
    stage: str | None
    decision_event_id: str
    reason: str | None
