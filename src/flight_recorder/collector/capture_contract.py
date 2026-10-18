"""Pure, closed FR-V2-02A capture and property-observation contracts.

Raw notification references are shape checked, never authenticated or fetched.
Eligibility is adapter read/source availability, not an invented vendor date.
"""

import copy
import re
from datetime import datetime

from flight_recorder.adapters.hubspot.capture import MAPPING_DESCRIPTION, validate_pending_capture
from flight_recorder.adapters.hubspot.contracts import (
    CaptureOwnership,
    FrozenCapture,
    ProcessingUnit,
    SourceNotification,
    typed_equal,
)
from flight_recorder.collector.canonical import canonical_hash, canonical_text

SOURCE = "hubspot-synthetic-capture"
OWNED = {v["evidence_type"]: k for k, v in MAPPING_DESCRIPTION["owned_inputs"].items()}


def validate_capture(capture, digest):
    """Validate published bytes using the accepted Task1 closed mapping."""
    try:
        if (
            not isinstance(capture, dict)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("capture/hash must be a JSON object and exact lowercase SHA256")
        if canonical_hash(capture) != digest:
            raise ValueError("capture_hash mismatch")
        for key in ("capture_sequence", "ownership_generation"):
            if type(capture[key]) is not int or capture[key] <= 0:
                raise ValueError(f"{key} must be a positive exact integer")
        raw_ref = capture["raw_notification_ref"]
        if not isinstance(raw_ref, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw_ref):
            raise ValueError("invalid raw_notification_ref")
        body = capture["notification"]
        ns = body["namespace"]
        payload = dict(
            portalId=ns["portal_id"],
            appId=ns["app_id"],
            subscriptionId=ns["subscription_id"],
            subscriptionType=ns["subscription_type"],
            objectId=body["object_id"],
            propertyName=body["property_name"],
            propertyValue=body["property_value"],
            occurredAt=body["occurred_at"],
            eventId=body["event_id"],
        )
        if "change_source" in body:
            payload["changeSource"] = body["change_source"]
        notification = SourceNotification.from_payload(
            payload, raw_body=canonical_text(payload).encode()
        )
        if not typed_equal(notification.semantic_body(), body):
            raise ValueError("notification is not its closed normalized body")
        unit = ProcessingUnit(
            notification,
            capture["mapping_identity"],
            capture["selection_identity"],
            capture["adapter_identity"],
            capture["reprocesses"],
        )
        owner = CaptureOwnership(unit.company_key, unit.unit_id, capture["ownership_generation"])
        pending = copy.deepcopy(capture)
        pending["capture_sequence"] = None
        validate_pending_capture(unit, owner, raw_ref, FrozenCapture.from_document(pending))
        return unit
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"invalid capture binding: {exc}") from exc


def account_identity(capture):
    n = capture["notification"]
    return "fr-account-v2-" + canonical_hash([n["namespace"]["portal_id"], n["object_id"]])


def semantic_item(capture, key, supersedes=None):
    value = capture["inputs"][key]["value"]
    item = dict(evidence_type=key, value=value, supersedes_evidence_version_id=supersedes)
    if key == "funding_event":
        item.update(value)
    elif key == "verified_integration_pressure":
        item.update(value=value["value"], basis=[value["basis"]])
    elif key == "head_of_platform_start_date":
        item["observed_at"] = None
    return item


def observation_body(account_ref, capture, item):
    key = item["evidence_type"]
    source_at = capture["input_provenance"][key]["available_at"]
    # Capture validation already guarantees canonical UTC text for these instants.
    eligible = (
        max(capture["read_finished_at"], source_at) if source_at else capture["read_finished_at"]
    )
    if datetime.fromisoformat(eligible) > datetime.fromisoformat(capture["capture_boundary"]):
        raise ValueError("observation available after captured boundary")
    return dict(
        account_ref=account_ref,
        source=SOURCE,
        evidence_type=key,
        semantic={
            k: v
            for k, v in item.items()
            if k not in {"evidence_type", "evidence_version_id", "supersedes_evidence_version_id"}
        },
        supersedes_evidence_version_id=item.get("supersedes_evidence_version_id"),
        mapping_identity=capture["mapping_identity"],
        source_property=OWNED[key],
        read_started_at=capture["read_started_at"],
        read_finished_at=capture["read_finished_at"],
        source_available_at=source_at,
        eligible_at=eligible,
    )


def bound_item(account_ref, capture, key, supersedes=None):
    item = semantic_item(capture, key, supersedes)
    item["evidence_version_id"] = "fr-evidence-v2-" + canonical_hash(
        observation_body(account_ref, capture, item)
    )
    return item


def available_keys(capture):
    return sorted(
        key for key in OWNED if capture["inputs"][key]["state"] in ("consumed", "ignored")
    )


def expected_context(account_ref, capture, supersessions):
    entries = []
    for key in sorted(OWNED):
        entry = capture["inputs"][key]
        if entry["state"] == "absent":
            continue
        available = entry["state"] in ("consumed", "ignored")
        item = bound_item(account_ref, capture, key, supersessions.get(key)) if available else None
        entries.append(
            dict(
                input_key=key,
                value=item["value"] if available else None,
                availability="available" if available else "unavailable",
                evidence_version_id=item["evidence_version_id"] if available else None,
            )
        )
    return entries


def validate_wire(normalized):
    p = normalized["payload"]
    cap = p["capture"]
    validate_capture(cap, p["capture_hash"])
    if normalized["account_ref"] != account_identity(cap):
        raise ValueError("account_ref does not bind the captured source company")
    if normalized["event_type"] == "evidence.recorded":
        if normalized["occurred_at"] != cap["read_finished_at"]:
            raise ValueError("evidence occurred_at must equal captured read finish")
        keys = [i["evidence_type"] for i in p["items"]]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate evidence types")
        for item in p["items"]:
            key = item["evidence_type"]
            if key not in available_keys(cap) or not typed_equal(
                item,
                bound_item(
                    normalized["account_ref"], cap, key, item["supersedes_evidence_version_id"]
                ),
            ):
                raise ValueError("evidence does not match complete captured observation identity")
    else:
        if (
            p["decision_boundary"] != cap["capture_boundary"]
            or normalized["occurred_at"] != cap["capture_boundary"]
        ):
            raise ValueError("decision boundary must equal captured boundary")
        if p["workflow_version"] != cap["processing_version"]:
            raise ValueError("workflow version must bind captured processing version")
        if (
            p["logic_artifact"]["artifact_hash"] != cap["rule_hash"]
            or p["logic_artifact"]["evaluator_version"] != cap["evaluator_version"]
        ):
            raise ValueError("selected artifact must bind capture rule identity")
