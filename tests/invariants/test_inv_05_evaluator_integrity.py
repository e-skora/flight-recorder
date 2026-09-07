"""INV-05 at the database: hash and evaluator identity are verified before replay.

Two cases, both against a real seeded ledger:

(a) the runtime evaluator is not the one that produced the decision. Exact
    replay must fail before any evaluation happens, rather than producing a
    plausible number under different code.
(b) a `logic_artifacts` row whose columns are internally consistent and whose
    identity the collector accepted, but whose stored `artifact_hash` is not the
    hash of its own content. A label is metadata; the content hash is identity.
"""

import copy
import json

import pytest
from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_hash, canonical_text
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF, events, logic_artifacts
from flight_recorder.logic import evaluator as evaluator_module
from flight_recorder.replay import reconstruct as reconstruct_module
from flight_recorder.replay.reconstruct import IntegrityFailure, reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    account_envelope_paths,
    load_json,
    seed_all,
    system_envelope_paths,
)

pytestmark = pytest.mark.invariant

MISLABELED_HASH = "0" * 64
MISLABELED_ID = "logic-account-prioritization-v3.2-mislabeled"
MISLABELED_VERSION = "v3.2-mislabeled"


def refuse_to_evaluate(*_args, **_kwargs):
    raise AssertionError("evaluation ran before the integrity check completed")


def test_a_different_runtime_evaluator_fails_before_any_evaluation(harness, monkeypatch):
    for response in seed_all(harness):
        assert response.status_code == 201

    monkeypatch.setattr(evaluator_module, "EVALUATOR_VERSION", "evaluator-v2")
    monkeypatch.setattr(reconstruct_module, "evaluate", refuse_to_evaluate)

    with harness.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        reconstruct(conn, DECISION_EVENT_ID)

    assert caught.value.field == "evaluator_version"
    assert (caught.value.stored, caught.value.recomputed) == ("evaluator-v1", "evaluator-v2")


def register_mislabeled_artifact(harness) -> str:
    """Insert an artifact row whose stored hash is not its content's hash.

    INSERT is permitted (only UPDATE and DELETE are blocked) and foreign keys
    are on, so the row needs a real `events` row under `_system` to point at.
    The content is the canonical `v3.2` text with only the two identity fields
    changed, so the row's columns and its content agree with each other -- the
    only thing wrong is the hash the ledger filed it under.
    """
    with harness.engine.connect() as conn:
        real_text = conn.execute(
            select(logic_artifacts.c.artifact_json).where(logic_artifacts.c.logic_version == "v3.2")
        ).scalar_one()

    content = json.loads(real_text)
    content["artifact_id"] = MISLABELED_ID
    content["logic_version"] = MISLABELED_VERSION
    text = canonical_text(content)
    source_event_id = "evt-system-logic-artifact-v3.2-mislabeled"

    with harness.engine.begin() as conn:
        conn.execute(
            events.insert().values(
                event_id=source_event_id,
                schema_version="1",
                event_type="logic_artifact.registered",
                source="direct-insert-for-inv-05",
                account_ref=SYSTEM_ACCOUNT_REF,
                occurred_at="2026-01-12T09:00:02.000000Z",
                recorded_at="2026-01-12T09:00:02.000000Z",
                canonical_hash=canonical_hash({"mislabeled": source_event_id}),
                payload=canonical_text({"artifact": content}),
            )
        )
        conn.execute(
            logic_artifacts.insert().values(
                artifact_hash=MISLABELED_HASH,
                artifact_id=MISLABELED_ID,
                logic_version=MISLABELED_VERSION,
                decision_class=content["decision_class"],
                artifact_schema_version=content["artifact_schema_version"],
                evaluator_version=content["evaluator_version"],
                artifact_json=text,
                source_event_id=source_event_id,
            )
        )
    return canonical_hash(content)


def test_an_artifact_row_filed_under_the_wrong_hash_fails_before_any_evaluation(
    harness, monkeypatch
):
    for path in system_envelope_paths():
        assert harness.post_raw(path.read_bytes()).status_code == 201
    true_hash = register_mislabeled_artifact(harness)

    for path in account_envelope_paths():
        envelope = copy.deepcopy(load_json(path))
        if envelope["event_type"] == "decision.recorded":
            envelope["payload"]["logic_artifact"] = {
                "logic_version": MISLABELED_VERSION,
                "artifact_id": MISLABELED_ID,
                "artifact_hash": MISLABELED_HASH,
                "evaluator_version": "evaluator-v1",
            }
        response = harness.post(envelope)
        # The collector accepts it: the hash resolves to a registered row and
        # every identity field matches that row's columns.
        assert response.status_code == 201, (envelope["event_id"], response.json())

    monkeypatch.setattr(reconstruct_module, "evaluate", refuse_to_evaluate)
    with harness.engine.connect() as conn, pytest.raises(IntegrityFailure) as caught:
        reconstruct(conn, DECISION_EVENT_ID)

    assert caught.value.field == "artifact_hash"
    assert caught.value.stored == MISLABELED_HASH
    assert caught.value.recomputed == true_hash
