"""Closed mapping and one-shot operational capture (FR-V2-01).

`capture_once` turns one supplied synthetic notification plus one supplied
property-read result into the unit's single immutable `FrozenCapture`:

1. append a receipt and refuse identity collisions;
2. return an existing capture without calling any callback (retries never read
   or reselect logic);
3. refuse an unsupported mapping identity before any read;
4. reserve the company before `observe`, hold it through mapping,
   `select_logic` and durable freeze;
5. map strictly into the four input states, refuse values available after the
   capture boundary, and refuse `no_usable_inputs` rather than invent evidence.

Nothing here builds a collector envelope, evaluates logic or records a decision.
"""

import re
from datetime import UTC, date, datetime

from flight_recorder.adapters.hubspot.contracts import (
    CaptureRefused,
    FrozenCapture,
    ProcessingUnit,
    ReadResult,
    RuleIdentity,
)
from flight_recorder.collector.canonical import canonical_hash

#: The one supported declarative mapping. Its hash is `MAPPING_IDENTITY`, and the
#: converters below dispatch on its `kind` entries, so code and identity agree.
MAPPING_DESCRIPTION = {
    "mapping_id": "hubspot-company-map",
    "mapping_version": "1",
    "source": "hubspot",
    "subscription_type": "company.propertyChange",
    "empty_values": "null and empty text are unavailable",
    "unsupported_properties": "refused",
    "owned_inputs": {
        "numberofemployees": {"evidence_type": "employee_count", "kind": "nonnegative_integer"},
        "industry": {
            "evidence_type": "industry",
            "kind": "table",
            "table": {"COMPUTER_SOFTWARE": "software"},
            "unmapped": "unavailable",
        },
        "country": {"evidence_type": "headquarters_country", "kind": "nonempty_text"},
        "fr_funding_event": {"evidence_type": "funding_event", "kind": "funding_event"},
        "fr_open_platform_roles": {
            "evidence_type": "open_platform_engineering_roles",
            "kind": "nonnegative_integer",
        },
        "fr_head_of_platform_start": {
            "evidence_type": "head_of_platform_start_date",
            "kind": "iso_date",
        },
        "fr_integration_pressure": {
            "evidence_type": "verified_integration_pressure",
            "kind": "integration_pressure",
            "values": ["HIGH", "LOW", "MEDIUM"],
        },
    },
    "external_observations": {
        "hubspotscore": {"input_key": "external_vendor_score", "replayable": False},
    },
    "refused_properties": [
        "fr_decision_ref",
        "fr_logic_version",
        "fr_op_id",
        "fr_op_rev",
        "fr_output",
        "fr_score",
    ],
}

MAPPING_IDENTITY = canonical_hash(MAPPING_DESCRIPTION)

_OWNED = MAPPING_DESCRIPTION["owned_inputs"]
_EXTERNAL = MAPPING_DESCRIPTION["external_observations"]
_REFUSED = frozenset(MAPPING_DESCRIPTION["refused_properties"])
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_UNMAPPED = object()


def _refuse_mapping(message: str):
    raise CaptureRefused(f"invalid mapping: {message}")


def _is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _iso_date(name: str, value) -> str:
    if not isinstance(value, str) or not _DATE.match(value):
        _refuse_mapping(f"{name} must be an ISO date YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        _refuse_mapping(f"{name} is not a valid calendar date")
    return value


def _nonempty_text(name: str, value) -> str:
    if not isinstance(value, str) or not value:
        _refuse_mapping(f"{name} must be nonempty text")
    return value


def _exact_keys(name: str, value, keys: set) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        _refuse_mapping(f"{name} must be an object with exactly {sorted(keys)}")
    return value


def _convert(name: str, spec: dict, value):
    kind = spec["kind"]
    if kind == "nonnegative_integer":
        if type(value) is not int or value < 0:
            _refuse_mapping(f"{name} must be a nonnegative integer")
        return value
    if kind == "nonempty_text":
        return _nonempty_text(name, value)
    if kind == "table":
        if not isinstance(value, str):
            _refuse_mapping(f"{name} must be text")
        return spec["table"].get(value, _UNMAPPED)
    if kind == "iso_date":
        return _iso_date(name, value)
    if kind == "funding_event":
        body = _exact_keys(name, value, {"value", "observed_at"})
        return {
            "value": _nonempty_text(f"{name}.value", body["value"]),
            "observed_at": _iso_date(f"{name}.observed_at", body["observed_at"]),
        }
    if kind == "integration_pressure":
        body = _exact_keys(name, value, {"value", "basis"})
        if not isinstance(body["value"], str) or body["value"] not in spec["values"]:
            _refuse_mapping(f"{name}.value must be one of {spec['values']}")
        return {"value": body["value"], "basis": _nonempty_text(f"{name}.basis", body["basis"])}
    _refuse_mapping(f"unsupported mapping kind {kind!r}")


def map_read_result(result: ReadResult) -> tuple[dict, dict]:
    """Map a supplied read into input states and per-input provenance."""
    properties = result.properties
    ignored = set(result.ignored)
    unavailable = set(result.unavailable)
    for name in sorted(set(properties) | ignored | unavailable):
        if name in _REFUSED:
            _refuse_mapping(f"write-back property {name!r} is never an input")
        if name not in _OWNED and name not in _EXTERNAL:
            _refuse_mapping(f"unsupported property {name!r}")
    both = sorted(ignored & unavailable)
    if both:
        _refuse_mapping(f"properties both ignored and unavailable: {both}")
    for name in sorted(unavailable):
        if name in properties and not _is_empty(properties[name]):
            _refuse_mapping(f"{name!r} is declared unavailable but carries a value")
    for name in sorted(ignored):
        if name not in properties:
            _refuse_mapping(f"{name!r} is declared ignored but was not supplied")

    inputs: dict = {}
    provenance: dict = {}
    for name, spec in _OWNED.items():
        key = spec["evidence_type"]
        provenance[key] = {"property": name, "available_at": None}
        if name in unavailable:
            inputs[key] = {"state": "unavailable"}
        elif name not in properties:
            inputs[key] = {"state": "absent"}
        elif _is_empty(properties[name]):
            inputs[key] = {"state": "unavailable"}
        else:
            value = _convert(name, spec, properties[name])
            if value is _UNMAPPED:
                inputs[key] = {"state": "unavailable"}
            else:
                state = "ignored" if name in ignored else "consumed"
                inputs[key] = {"state": state, "value": value}
    for name, spec in _EXTERNAL.items():
        key = spec["input_key"]
        provenance[key] = {"property": name, "available_at": None}
        if name in unavailable or (name in properties and _is_empty(properties[name])):
            inputs[key] = {"state": "unavailable", "replayable": spec["replayable"]}
        elif name not in properties:
            inputs[key] = {"state": "absent", "replayable": spec["replayable"]}
        else:
            # A vendor score is a recorded external observation, never an owned input.
            inputs[key] = {
                "state": "ignored",
                "value": properties[name],
                "replayable": spec["replayable"],
            }
    return inputs, provenance


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _aware(value, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CaptureRefused(f"{what} time must be a timezone-aware datetime")
    # Compare instants, including repeated local wall times during a DST fold.
    return value.astimezone(UTC)


def _available_times(result: ReadResult, boundary: datetime) -> dict:
    properties = result.properties
    times = {}
    for name, when in sorted(result.available_at.items(), key=lambda item: str(item[0])):
        if name not in properties or _is_empty(properties[name]) or name in result.unavailable:
            raise CaptureRefused(
                f"available_at names {name!r}, which carries no available supplied value"
            )
        when = _aware(when, f"available_at for {name!r}")
        if when > boundary:
            raise CaptureRefused(
                f"available_at for {name!r} ({_utc_text(when)}) is after the capture boundary "
                f"({_utc_text(boundary)}); later observations never enter this capture"
            )
        times[name] = _utc_text(when)
    return times


def _build_candidate(unit, owner, raw_ref, observe, select_logic, clock) -> FrozenCapture:
    started = _aware(clock(), "read start")
    result = observe(owner)
    finished = _aware(clock(), "read finish")
    if finished < started:
        raise CaptureRefused("invalid read interval: read finished before it started")
    if not isinstance(result, ReadResult):
        raise CaptureRefused("invalid read result: observe must return a ReadResult")
    inputs, provenance = map_read_result(result)
    owned_keys = [spec["evidence_type"] for spec in _OWNED.values()]
    if not any(inputs[key]["state"] == "consumed" for key in owned_keys):
        raise CaptureRefused(
            "no_usable_inputs: every owned-rule input is unavailable, ignored or absent; "
            "no capture, evidence or decision is recorded"
        )
    rule = select_logic()
    if not isinstance(rule, RuleIdentity):
        raise CaptureRefused("identity conflict: select_logic must return a RuleIdentity")
    boundary = _aware(clock(), "capture boundary")
    if boundary < finished:
        raise CaptureRefused("invalid read interval: capture boundary precedes the read finish")
    times = _available_times(result, boundary)
    for entry in provenance.values():
        if entry["property"] in times:
            entry["available_at"] = times[entry["property"]]
    body = unit.notification.semantic_body()
    document = {
        "record_kind": "operational_capture",
        "scope": "FR-V2-01 synthetic operational capture; not a domain record",
        "unit_id": unit.unit_id,
        "logical_key": unit.logical_key,
        "processing_version": unit.processing_version,
        "mapping_identity": unit.mapping_identity,
        "selection_identity": unit.selection_identity,
        "adapter_identity": unit.adapter_identity,
        "reprocesses": unit.reprocesses,
        "company": {"portal_id": body["namespace"]["portal_id"], "company_id": body["object_id"]},
        "ownership_generation": owner.generation,
        "capture_sequence": None,
        "read_started_at": _utc_text(started),
        "read_finished_at": _utc_text(finished),
        "capture_boundary": _utc_text(boundary),
        "raw_notification_ref": raw_ref,
        "notification": body,
        "observation": {
            "properties": result.properties,
            "ignored": list(result.ignored),
            "unavailable": list(result.unavailable),
            "available_at": times,
        },
        "rule_hash": rule.artifact_hash,
        "evaluator_version": rule.evaluator_version,
        "inputs": inputs,
        "input_provenance": provenance,
    }
    return FrozenCapture.from_document(document)


def validate_pending_capture(unit, owner, raw_ref, candidate: FrozenCapture) -> None:
    """Check a complete pending document against registered identity and mapping.

    Reconstruct only from preserved candidate data; no external read or logic
    selection occurs. Exact canonical comparison also refuses missing/extra fields,
    altered input states/provenance and a caller-assigned publication sequence.
    """
    try:
        document = candidate.document
        if unit.mapping_identity != MAPPING_IDENTITY:
            raise ValueError("unsupported mapping identity")
        supplied = document["observation"]
        result = ReadResult(
            supplied["properties"],
            ignored=supplied["ignored"],
            unavailable=supplied["unavailable"],
            available_at={
                name: datetime.fromisoformat(value)
                for name, value in supplied["available_at"].items()
            },
        )
        rule = RuleIdentity(document["rule_hash"], document["evaluator_version"])
        times = iter(
            datetime.fromisoformat(document[name])
            for name in ("read_started_at", "read_finished_at", "capture_boundary")
        )
        expected = _build_candidate(
            unit, owner, raw_ref, lambda _: result, lambda: rule, lambda: next(times)
        )
        if expected.canonical_bytes != candidate.canonical_bytes:
            raise ValueError("document differs from complete registered capture")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CaptureRefused(f"invalid capture: {error}") from error


def capture_once(store, unit, *, observe, select_logic, clock) -> FrozenCapture:
    """Capture one processing unit exactly once; retries return the stored winner."""
    if not isinstance(unit, ProcessingUnit):
        raise CaptureRefused("identity conflict: a ProcessingUnit is required")
    raw_ref = store.record_receipt(unit)
    existing = store.load_capture(unit)
    if existing is not None:
        return existing
    if unit.mapping_identity != MAPPING_IDENTITY:
        detail = (
            f"invalid mapping: unsupported mapping identity {unit.mapping_identity!r}; "
            "refused before any read"
        )
        store.record_refusal(unit, detail)
        raise CaptureRefused(detail)
    owner, existing = store.acquire_or_load(unit)
    if existing is not None:
        return existing
    try:
        candidate = _build_candidate(unit, owner, raw_ref, observe, select_logic, clock)
        return store.freeze(owner, candidate)
    except CaptureRefused as refusal:
        # A known refusal releases only still-current ownership; a stale worker
        # changes neither the winner's progress nor its successor's ownership.
        # Any other exception leaves the reservation held for explicit abandonment.
        store.release_if_current(owner, str(refusal))
        raise
