"""Seal an already acquired selected read; never fetch, submit or score here."""

from dataclasses import dataclass, field

from flight_recorder.adapters.crm.contracts import (
    ConnectionConfig,
    CRMCapture,
    CRMRefused,
    ObservedRead,
    Ownership,
    require,
)
from flight_recorder.adapters.crm.http import utc_now
from flight_recorder.adapters.crm.mapping import map_inputs
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash
from flight_recorder.collector.crm_schema import identities
from flight_recorder.collector.schema import format_utc


@dataclass(frozen=True)
class CaptureProgress:
    state: str
    observation_fact_id: str
    capture: CRMCapture | None = field(repr=False)


def capture_observation(store, owner, observed_read, config, *, selection_identity, clock):
    require(type(config) is ConnectionConfig, "invalid_connection_config")
    config.__post_init__()
    require(
        type(owner) is Ownership and type(observed_read) is ObservedRead, "invalid_capture_request"
    )
    observed_read.__post_init__()
    require(
        type(selection_identity) is str
        and 0 < len(selection_identity) <= 256
        and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in selection_identity),
        "invalid_selection_identity",
    )
    try:
        selection_identity.encode("utf-8")
    except UnicodeError:
        raise CRMRefused("invalid_selection_identity") from None
    processing = dict(
        mapping_hash=config.mapping_hash,
        rule_hash=config.artifact_hash,
        evaluator_version=config.evaluator_version,
        selection_identity=selection_identity,
        adapter_version="crm-adapter-v1",
    )
    version = canonical_hash(processing)
    old = store.observation_for_owner(owner)
    if old is not None:
        require(
            canonical_bytes(old["observation"]) == observed_read.canonical_bytes
            and (
                old["config_binding"],
                old["connection_id"],
                old["selection_identity"],
                old["processing_version"],
            )
            == (config.binding, config.connection_id, selection_identity, version),
            "capture_claim_conflict",
        )
        cap = store.capture_for_owner(owner) if old["state"] == "captured" else None
        return CaptureProgress(old["state"], store._fact_key(owner), cap)
    target = observed_read.target
    require(
        target.key == owner.target_key
        and target.provider == "salesforce"
        and target.instance_id == config.expected_instance_id
        and target.record_id in config.record_ids,
        "target_not_allowlisted",
    )
    inputs = map_inputs(observed_read.properties, observed_read.unavailable_fields)
    digest = canonical_hash(dict(mapping_hash=config.mapping_hash, inputs=inputs))
    state = store.polling_state(target)
    if (digest, version) == (state["input_digest"], state["processing_version"]):
        store.record_unchanged(
            owner,
            observed_read,
            version,
            config_binding=config.binding,
            selection_identity=selection_identity,
            connection_id=config.connection_id,
        )
        return CaptureProgress("scoring_inputs_unchanged", store._fact_key(owner), None)
    try:
        document = dict(
            format_version=1,
            data_origin="fictional_records_real_crm",
            **observed_read.document,
            connection_id=config.connection_id,
            capture_boundary=format_utc(utc_now(clock)),
            inputs=inputs,
            **processing,
            capture_sequence=store.next_capture_sequence(owner),
            ownership_generation=owner.generation,
            reprocesses=None,
        )
        document.update(identities(document))
        cap = CRMCapture.from_document(document)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CRMRefused("capture_preparation_refused") from None
    frozen = store.freeze_capture(owner, cap, config_binding=config.binding)
    return CaptureProgress("captured", store._fact_key(owner), frozen)
