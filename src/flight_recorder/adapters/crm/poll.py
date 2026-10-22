"""Poll only approved targets; frozen work resumes without source acquisition."""

from flight_recorder.adapters.crm.capture import capture_observation
from flight_recorder.adapters.crm.contracts import CRMRefused, require
from flight_recorder.adapters.crm.http import utc_now
from flight_recorder.adapters.crm.submission_contracts import CRMPrerequisites
from flight_recorder.adapters.crm.submit import freeze_submission, resume_submission
from flight_recorder.collector.canonical import canonical_bytes
from flight_recorder.collector.schema import format_utc


def _dispatch_poll(runtime, connection_id, tick_id, *, clock):
    runtime.authority.assert_held()
    if getattr(runtime, "writer_capable", False):
        runtime.validate_write_state()
    require(
        type(connection_id) is str and connection_id == runtime.config.connection.connection_id,
        "unknown_crm_connection",
    )
    require(
        type(tick_id) is str
        and 0 < len(tick_id) <= 512
        and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in tick_id),
        "invalid_poll_tick",
    )
    try:
        tick_id.encode("utf-8")
    except UnicodeError:
        raise CRMRefused("invalid_poll_tick") from None
    if runtime.queue.has_tick(tick_id):
        result = runtime.queue.report(tick_id)
    else:
        result = runtime.queue.admit(tick_id, format_utc(utc_now(clock)))
    runtime.notify()
    return result


def dispatch_poll(runtime, connection_id, tick_id, *, clock):
    try:
        return _dispatch_poll(runtime, connection_id, tick_id, clock=clock)
    except CRMRefused as exc:
        raise CRMRefused(exc.reason) from None
    except Exception:
        raise CRMRefused("poll_admission_refused") from None


def capture_and_submit(runtime, target, receipt_id, *, clock):
    runtime.authority.assert_held()
    require(not runtime._closed and not runtime._stop.is_set(), "runtime_closed")
    require(runtime._execution.acquire(blocking=False), "poll_runner_busy")
    try:
        return _capture_and_submit_owned(runtime, target, receipt_id, clock=clock)
    finally:
        runtime._execution.release()


def _capture_and_submit_owned(runtime, target, receipt_id, *, clock):
    runtime.authority.assert_held()
    if getattr(runtime, "writer_capable", False):
        runtime.validate_write_state()
    require(target in runtime.config.targets, "target_not_allowlisted")
    owner = runtime.queue.claim(receipt_id, target)
    try:
        fact = runtime.store.observation_for_owner(owner)
        if fact is None:
            runtime.queue.mark_read_started(receipt_id, owner)
            client = runtime.client()
            runtime.verify_for_read(client)
            observation = client.read_selected(target)
            runtime.authority.assert_held()
            progress = capture_observation(
                runtime.store,
                owner,
                observation,
                runtime.config.connection,
                selection_identity=runtime.config.document["selection_identity"],
                clock=clock,
            )
            fact = runtime.store.observation_for_owner(owner)
            require(fact is not None, "poll_integrity")
        if fact["state"] == "scoring_inputs_unchanged":
            runtime.queue.settle(
                receipt_id,
                owner,
                runtime.collector,
                state="scoring_inputs_unchanged",
                reason="scoring_inputs_unchanged",
            )
            return runtime.queue.report(receipt_id)
        cap = runtime.store.capture_for_owner(owner)
        require(cap is not None, "missing_frozen_capture")
        frozen = runtime.store.load_submission(owner)
        if frozen is None:
            d = runtime.config.document
            pri = CRMPrerequisites(
                canonical_bytes(d["discoveries"][target.record_id]),
                canonical_bytes(d["artifact_registration"]),
                cap.document["capture_boundary"],
                b"{}",
            )
            try:
                frozen = freeze_submission(
                    runtime.store, runtime.collector, owner, prerequisites=pri
                )
            except CRMRefused as exc:
                if exc.reason not in ("no_usable_inputs", "mixed_evidence_reuse_unsupported"):
                    raise
                runtime.queue.settle(
                    receipt_id, owner, runtime.collector, state="refused", reason=exc.reason
                )
                return runtime.queue.report(receipt_id)
        progress = resume_submission(runtime.store, runtime.collector, owner)
        if progress.state == "complete":
            runtime.queue.settle(receipt_id, owner, runtime.collector, state="complete")
        else:
            runtime.queue.blocked(receipt_id, owner, progress.reason or "submission_incomplete")
        return runtime.queue.report(receipt_id)
    except CRMRefused as exc:
        runtime.queue.blocked(receipt_id, owner, exc.reason)
        return runtime.queue.report(receipt_id)
    except Exception:
        runtime.queue.blocked(receipt_id, owner, "poll_work_refused")
        return runtime.queue.report(receipt_id)
