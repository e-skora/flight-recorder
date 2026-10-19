"""Immutable operational submission format; domain envelopes retain their own schemas."""

import hashlib
from dataclasses import dataclass

from flight_recorder.adapters.hubspot.contracts import strict_json_loads, typed_equal
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.schema import validate_envelope_json


class SubmissionError(ValueError):
    """A named local integrity/refusal failure; never permission to repair or remint."""


def require(condition, reason):
    if not condition:
        raise SubmissionError(reason)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def normalized(body):
    """Reject ambiguous JSON before the existing strict, versioned validator."""
    try:
        require(isinstance(body, bytes), "envelope_bytes_required")
        strict_json_loads(body)
        return validate_envelope_json(body).model_dump(mode="json")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SubmissionError("invalid_submission_envelope") from exc


@dataclass(frozen=True)
class SubmissionPrerequisites:
    account_envelope: bytes
    artifact_envelope: bytes
    recorded_at: str
    supersessions: bytes


@dataclass(frozen=True)
class FrozenSubmission:
    canonical_bytes: bytes

    def __post_init__(self):
        require(type(self.canonical_bytes) is bytes, "immutable_submission_bytes_required")

    @classmethod
    def from_document(cls, document):
        frozen = cls(canonical_bytes(document))
        _ = frozen.document
        return frozen

    @property
    def submission_hash(self):
        return digest(self.canonical_bytes)

    @property
    def document(self):
        try:
            d = strict_json_loads(self.canonical_bytes)
            require(
                type(d) is dict
                and set(d)
                == {
                    "format_version",
                    "unit_id",
                    "capture_hash",
                    "capture_bytes_sha256",
                    "account_ref",
                    "stages",
                    "reused_evidence",
                },
                "invalid_frozen_shape",
            )
            require(
                type(d["format_version"]) is int and d["format_version"] == 1, "submission_version"
            )
            require(canonical_bytes(d) == self.canonical_bytes, "noncanonical_frozen_bytes")
            for key in ("unit_id", "capture_hash", "capture_bytes_sha256"):
                require(
                    isinstance(d[key], str)
                    and len(d[key]) == 64
                    and all(c in "0123456789abcdef" for c in d[key]),
                    "invalid_frozen_identity",
                )
            stages = d["stages"]
            require(type(stages) is list, "invalid_stages")
            names = [s["stage"] for s in stages]
            require(
                names
                in (
                    ["account", "artifact", "evidence", "decision"],
                    ["account", "artifact", "decision"],
                ),
                "invalid_stage_order",
            )
            for s in stages:
                require(
                    set(s) == {"stage", "event_id", "canonical_hash", "envelope"}, "invalid_stage"
                )
                envelope = normalized(canonical_bytes(s["envelope"]))
                require(
                    typed_equal(envelope, s["envelope"])
                    and s["canonical_hash"] == canonical_hash(envelope)
                    and s["event_id"] == envelope["event_id"],
                    "stage_identity_mismatch",
                )
                event_type = {
                    "account": "account.discovered",
                    "artifact": "logic_artifact.registered",
                    "evidence": "evidence.recorded",
                    "decision": "decision.recorded",
                }[s["stage"]]
                require(envelope["event_type"] == event_type, "stage_type_mismatch")
                require(
                    envelope["schema_version"]
                    == ("1" if s["stage"] in ("account", "artifact") else "2"),
                    "stage_version_mismatch",
                )
                if s["stage"] != "artifact":
                    require(envelope["account_ref"] == d["account_ref"], "stage_account_mismatch")
                if s["stage"] in ("evidence", "decision"):
                    require(
                        envelope["event_id"] == "fr-submit-" + s["stage"] + "-v1-" + d["unit_id"],
                        "stage_event_id_mismatch",
                    )
                    require(
                        envelope["payload"]["capture_hash"] == d["capture_hash"],
                        "stage_capture_mismatch",
                    )
            reused = d["reused_evidence"]
            require(type(reused) is list, "invalid_reuse")
            keys = []
            for item in reused:
                require(
                    set(item)
                    == {"input_key", "evidence_version_id", "observation", "origin_capture_hash"},
                    "invalid_reuse_shape",
                )
                keys.append(item["input_key"])
            require(keys == sorted(set(keys)), "invalid_reuse_keys")
            return d
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise SubmissionError("invalid_frozen_submission") from exc


@dataclass(frozen=True)
class SubmissionProgress:
    unit_id: str
    submission_hash: str
    state: str
    next_stage: str | None
    decision_event_id: str
    detail: dict | None
