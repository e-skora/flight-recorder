"""Shared fixtures: temp SQLite per test, in-process client, canonical fixture access."""

import copy
import json
import os
import uuid
import warnings
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from sqlalchemy import event, func, select

from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    attribute,
    ledger_maximum,
    load_outcome,
)
from flight_recorder.attribution.service import build_envelope, run_attribution
from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact, format_utc
from flight_recorder.fixtures import canonical_envelope_paths, load_json, logic_artifact_path
from flight_recorder.ledger.database import reset_database
from flight_recorder.ledger.schema import (
    PROJECTION_TABLES,
    SYSTEM_ACCOUNT_REF,
    accounts,
    decision_consumed_inputs,
    decision_context,
    decisions,
    events,
    evidence_versions,
    outcome_attributions,
    outcomes,
)
from flight_recorder.logic.evaluator import ContextInput, InputState
from flight_recorder.logic.rules import parse_boundary
from flight_recorder.replay.counterfactual import replay
from flight_recorder.replay.reconstruct import ConsumedInputRow

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated",
)
from fastapi.testclient import TestClient  # noqa: E402

from flight_recorder.app import create_app  # noqa: E402

# Hypothesis profiles: modest locally, larger in CI (HYPOTHESIS_PROFILE=ci).
settings.register_profile(
    "default",
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

COLLECTOR_URL = "/api/v1/decision-events"
JSON_HEADERS = {"content-type": "application/json"}


class Harness:
    """A fresh SQLite file, its engine, and an in-process client."""

    def __init__(self, directory: Path, raise_server_exceptions: bool = True):
        self.db_path = directory / f"{uuid.uuid4().hex}.db"
        self.engine = reset_database(self.db_path)
        self.app = create_app(self.db_path)
        self.collector = self.app.state.collector
        self.client = TestClient(self.app, raise_server_exceptions=raise_server_exceptions)

    def post_raw(self, body: bytes | str):
        if isinstance(body, str):
            body = body.encode("utf-8")
        return self.client.post(COLLECTOR_URL, content=body, headers=JSON_HEADERS)

    def post(self, envelope: dict):
        return self.post_raw(json.dumps(envelope))

    def event_count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(events)).scalar_one()

    def account_rows(self) -> list[tuple]:
        with self.engine.connect() as conn:
            query = select(accounts).order_by(accounts.c.account_ref)
            return [tuple(r) for r in conn.execute(query)]

    def projection_rows(self) -> dict[str, list[tuple]]:
        """Every row of every projection table, keyed by table name."""
        with self.engine.connect() as conn:
            return {
                table.name: [
                    tuple(r)
                    for r in conn.execute(select(table).order_by(*table.primary_key.columns))
                ]
                for table in PROJECTION_TABLES
            }

    def snapshot(self) -> tuple[int, list[tuple], dict[str, list[tuple]]]:
        return self.event_count(), self.account_rows(), self.projection_rows()

    def is_empty(self) -> bool:
        """No event, no account, and no projected row anywhere."""
        count, account_rows, projections = self.snapshot()
        return count == 0 and account_rows == [] and not any(projections.values())


@pytest.fixture
def harness(tmp_path) -> Harness:
    return Harness(tmp_path)


@pytest.fixture
def client(harness: Harness) -> TestClient:
    return harness.client


def canonical_envelopes() -> list[dict]:
    """All canonical envelopes, including the two `_system` registrations."""
    return [load_json(p) for p in canonical_envelope_paths()]


def account_envelope_paths() -> list[Path]:
    """The canonical envelopes belonging to the NovaSignal AI account."""
    return [
        p for p in canonical_envelope_paths() if load_json(p)["account_ref"] != SYSTEM_ACCOUNT_REF
    ]


def system_envelope_paths() -> list[Path]:
    """The canonical envelopes submitted under the `_system` principal."""
    return [
        p for p in canonical_envelope_paths() if load_json(p)["account_ref"] == SYSTEM_ACCOUNT_REF
    ]


def account_envelopes() -> list[dict]:
    return [load_json(p) for p in account_envelope_paths()]


def canonical_by_type(event_type: str) -> dict:
    return next(e for e in canonical_envelopes() if e["event_type"] == event_type)


def canonical_raw(index: int) -> bytes:
    """Raw bytes of the index-th *account* envelope (0 = account.discovered)."""
    return account_envelope_paths()[index].read_bytes()


def system_raw(index: int) -> bytes:
    """Raw bytes of the index-th `_system` envelope (0 = v3.2, 1 = v5.1)."""
    return system_envelope_paths()[index].read_bytes()


def register_artifacts(harness: "Harness") -> None:
    """Submit both logic-artifact registrations; prerequisite for any decision."""
    for path in system_envelope_paths():
        assert harness.post_raw(path.read_bytes()).status_code == 201


TEST_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def local_fixture(name: str) -> dict:
    """A non-canonical fixture used by one test file, not part of the demo seed."""
    return load_json(TEST_FIXTURES_DIR / name)


def logic_artifact(version: str) -> dict:
    return load_json(logic_artifact_path(version))


def seed_all(harness: Harness) -> list:
    return [harness.post_raw(p.read_bytes()) for p in canonical_envelope_paths()]


def reversed_keys(obj):
    """Recursively reverse dict key order; a genuinely different raw layout."""
    if isinstance(obj, dict):
        return {k: reversed_keys(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [reversed_keys(v) for v in obj]
    return obj


def reformatted(envelope: dict) -> bytes:
    """Same content, reversed keys, indented with tabs, trailing newline."""
    return (json.dumps(reversed_keys(envelope), indent="\t") + "\n").encode("utf-8")


# --- Phase 2B: evaluator, reconstruction ------------------------------------

DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"


def logic_artifact_model(version: str = "v3.2") -> LogicArtifact:
    """The canonical artifact of `version`, through the strict schema-v1 model."""
    return LogicArtifact.model_validate(logic_artifact(version))


def canonical_boundary() -> datetime:
    """`T(d)` of the canonical decision, from the fixture."""
    return parse_boundary(canonical_by_type("decision.recorded")["payload"]["decision_boundary"])


def canonical_observed_at() -> dict[str, date]:
    """`observed_at` per evidence version id, from the canonical evidence events.

    The dates a temporal rule reads come from the evidence versions, never from
    the decision payload, so the tests source them the same way.
    """
    return {
        item["evidence_version_id"]: date.fromisoformat(item["observed_at"])
        for envelope in canonical_envelopes()
        if envelope["event_type"] == "evidence.recorded"
        for item in envelope["payload"]["items"]
        if "observed_at" in item
    }


def canonical_context() -> tuple[ContextInput, ...]:
    """`H(d)` of the canonical decision, hand-built from the fixtures.

    Ordered by input key, matching how the reconstruction loads it.
    """
    observed = canonical_observed_at()
    entries = canonical_by_type("decision.recorded")["payload"]["historical_context"]
    return tuple(
        ContextInput(
            key=entry["input_key"],
            availability=entry["availability"],
            value=entry["value"],
            evidence_version_id=entry.get("evidence_version_id"),
            observed_at=observed.get(entry.get("evidence_version_id")),
        )
        for entry in sorted(entries, key=lambda e: e["input_key"])
    )


def canonical_consumed_rows() -> tuple[ConsumedInputRow, ...]:
    """`U(d)` of the canonical decision as stored comparison rows."""
    consumed = canonical_by_type("decision.recorded")["payload"]["consumed_inputs"]
    return tuple(
        ConsumedInputRow(
            input_key=used["input_key"],
            evidence_version_id=used["evidence_version_id"],
            contribution=used["contribution"],
        )
        for used in sorted(consumed, key=lambda u: u["input_key"])
    )


def replace_context(
    context: tuple[ContextInput, ...], key: str, replacement: ContextInput | None
) -> tuple[ContextInput, ...]:
    """`context` with `key` replaced, or dropped entirely when `replacement` is None."""
    return tuple(
        replacement if entry.key == key else entry
        for entry in context
        if replacement is not None or entry.key != key
    )


def evidence_envelope(
    event_id: str,
    items: list[dict],
    *,
    occurred_at: str,
    account_ref: str = "novasignal-ai",
    source: str = "clay-sim-later",
) -> dict:
    """A well-formed `evidence.recorded` envelope. Test-only, never canonical."""
    return {
        "schema_version": "1",
        "event_id": event_id,
        "event_type": "evidence.recorded",
        "source": source,
        "account_ref": account_ref,
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "payload": {"items": items},
    }


@contextmanager
def captured_statements(engine):
    """Every SQL statement the engine executes inside the block.

    Used to prove that reconstruction writes nothing and reads no `events` or
    `accounts` row (INV-01, INV-02).
    """
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)


# --- Phase 2C: corrections, the decision boundary ---------------------------
#
# Shared by `test_ac_05_corrections.py`, `test_ac_03_boundary.py`,
# `test_inv_02_boundary.py` and the 2C additions to
# `test_inv_04_evidence_versions.py`.

#: Every field of a `Reconstruction` other than `result`.
RECONSTRUCTION_FIELDS = (
    "decision_event_id",
    "artifact_hash",
    "logic_version",
    "evaluator_version",
    "decision_boundary",
    "stored_artifact_hash",
    "recomputed_artifact_hash",
    "runtime_evaluator_version",
)

#: Every field of an `EvaluationResult`.
RESULT_FIELDS = ("score", "threshold", "output", "ignored_inputs", "factors", "context_states")


def stored_form(text: str) -> str:
    """The persisted D-010 form of an ISO-8601 instant: microseconds and `Z`.

    The same normalization the collector applies (`Timestamp` -> `format_utc`),
    so a test can state an instant in any aware spelling and still know the
    exact text the ledger holds for it.
    """
    return format_utc(datetime.fromisoformat(text))


def decision_rows(harness: Harness, decision_event_id: str = DECISION_EVENT_ID):
    """Every projected row belonging to one decision, keyed by table name."""
    with harness.engine.connect() as conn:
        return {
            table.name: [
                tuple(row)
                for row in conn.execute(
                    select(table)
                    .where(table.c.decision_event_id == decision_event_id)
                    .order_by(*table.primary_key.columns)
                )
            ]
            for table in (decisions, decision_context, decision_consumed_inputs)
        }


def evidence_version_row(harness: Harness, evidence_version_id: str):
    """One `evidence_versions` row by primary key, or None."""
    with harness.engine.connect() as conn:
        return conn.execute(
            select(evidence_versions).where(
                evidence_versions.c.evidence_version_id == evidence_version_id
            )
        ).first()


def factor(result, key: str):
    """The evaluated factor for `key`."""
    return next(f for f in result.factors if f.key == key)


def consumed_versions(result) -> dict[str, str]:
    """`input_key -> evidence_version_id` for every consumed factor."""
    return {
        f.key: f.evidence_version_id for f in result.factors if f.input_state is InputState.CONSUMED
    }


def assert_same_reconstruction(after, before) -> None:
    """`after` equals `before` field by field, including every factor's
    `evidence_version_id`, and then as a whole."""
    for field in RECONSTRUCTION_FIELDS:
        assert getattr(after, field) == getattr(before, field), field
    for field in RESULT_FIELDS:
        assert getattr(after.result, field) == getattr(before.result, field), field
    assert dict(after.result.context_states) == dict(before.result.context_states)
    for after_factor, before_factor in zip(
        after.result.factors, before.result.factors, strict=True
    ):
        assert after_factor == before_factor, after_factor.key
        assert after_factor.evidence_version_id == before_factor.evidence_version_id
    assert after == before


def decision_envelope_with(
    event_id: str,
    *,
    input_key: str,
    value,
    evidence_version_id: str,
    contribution: int,
    score: int,
    output: str,
    boundary: str | None = None,
) -> dict:
    """The canonical `decision.recorded` envelope with one input re-pointed.

    Everything is the canonical decision except: `event_id`; the
    `historical_context` and `consumed_inputs` entries for `input_key`, which
    now preserve `value` from `evidence_version_id` with `contribution`; the
    recorded `result`; and, when `boundary` is given, `occurred_at`,
    `recorded_at` and `payload.decision_boundary`, all set to that one spelling.
    """
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = event_id
    payload = envelope["payload"]
    entry = next(e for e in payload["historical_context"] if e["input_key"] == input_key)
    entry.update(value=value, availability="available", evidence_version_id=evidence_version_id)
    used = next(u for u in payload["consumed_inputs"] if u["input_key"] == input_key)
    used.update(value=value, evidence_version_id=evidence_version_id, contribution=contribution)
    payload["result"] = {
        "score": score,
        "threshold": payload["result"]["threshold"],
        "output": output,
    }
    if boundary is not None:
        envelope["occurred_at"] = boundary
        envelope["recorded_at"] = boundary
        payload["decision_boundary"] = boundary
    return envelope


# --- Phase 3A: the counterfactual -------------------------------------------
#
# Shared by `test_ac_02_counterfactual.py`, `test_ac_06_missing_inputs.py`,
# `test_inv_06_separation.py` and the 3A additions to `test_ac_03_boundary.py`,
# `test_ac_04_isolation.py` and `test_inv_02_boundary.py`.

#: Every field of a `Counterfactual` other than `original` and `result`.
COUNTERFACTUAL_FIELDS = (
    "label",
    "decision_event_id",
    "decision_boundary",
    "current_artifact_hash",
    "current_logic_version",
    "current_evaluator_version",
    "stored_artifact_hash",
    "recomputed_artifact_hash",
    "runtime_evaluator_version",
)


def v5_1_hash() -> str:
    """The canonical `v5.1` artifact's content hash, derived, never typed."""
    return canonical_hash(logic_artifact("v5.1"))


def canonical_evidence_ids() -> dict[str, str]:
    """`input_key -> evidence_version_id` for every available entry of `H(d)`."""
    return {
        entry.key: entry.evidence_version_id
        for entry in canonical_context()
        if entry.evidence_version_id is not None
    }


def derived_artifact_envelope(
    artifact_id: str,
    logic_version: str,
    factors: list[dict],
    *,
    event_id: str,
    evaluator_version: str | None = None,
    threshold: int | None = None,
) -> dict:
    """A test-only artifact derived from canonical `v5.1`, registered under `_system`.

    Same evaluator, schema, missing-value behavior and output mapping as `v5.1`
    unless overridden; its own identity. Never canonical.
    """
    content = copy.deepcopy(logic_artifact("v5.1"))
    content["artifact_id"] = artifact_id
    content["logic_version"] = logic_version
    content["factors"] = copy.deepcopy(factors)
    if evaluator_version is not None:
        content["evaluator_version"] = evaluator_version
    if threshold is not None:
        content["threshold"] = threshold
    content["activation"] = {
        "activated_at": "2026-05-05T09:00:00.000000Z",
        "deactivated_at": None,
        "status": "current",
    }
    return {
        "schema_version": "1",
        "event_id": event_id,
        "event_type": "logic_artifact.registered",
        "source": "relaybridge-logic-registry",
        "account_ref": SYSTEM_ACCOUNT_REF,
        "occurred_at": "2026-05-05T09:00:00Z",
        "recorded_at": "2026-05-05T09:00:00Z",
        "payload": {"artifact": content},
    }


def register_derived_artifact(harness: Harness, envelope: dict) -> str:
    """Register a test-only artifact through the collector; returns its content hash."""
    response = harness.post(envelope)
    assert response.status_code == 201, (envelope["event_id"], response.json())
    return canonical_hash(envelope["payload"]["artifact"])


def replay_under(harness: Harness, artifact_hash: str, decision_event_id: str = DECISION_EVENT_ID):
    """`R(Lc, H(d))` through the application's replay path, on a fresh connection."""
    with harness.engine.connect() as conn:
        return replay(conn, decision_event_id, artifact_hash)


def assert_same_counterfactual(after, before) -> None:
    """`after` equals `before` field by field -- the counterfactual's own fields,
    the original inside it, every result field, every factor's
    `evidence_version_id` -- and then as a whole."""
    for field in COUNTERFACTUAL_FIELDS:
        assert getattr(after, field) == getattr(before, field), field
    assert_same_reconstruction(after.original, before.original)
    for field in RESULT_FIELDS:
        assert getattr(after.result, field) == getattr(before.result, field), field
    assert dict(after.result.context_states) == dict(before.result.context_states)
    for after_factor, before_factor in zip(
        after.result.factors, before.result.factors, strict=True
    ):
        assert after_factor == before_factor, after_factor.key
        assert after_factor.evidence_version_id == before_factor.evidence_version_id
    assert after == before


def assert_same_comparison(after, before) -> None:
    """`after` equals `before` entry by entry and then as a whole."""
    for after_change, before_change in zip(after.contributions, before.contributions, strict=True):
        assert after_change == before_change, after_change.key
        assert after_change.evidence_version_id == before_change.evidence_version_id
    assert after.missing_inputs == before.missing_inputs
    assert after == before


# --- Phase 4A: outcome schema v2 and attribution ----------------------------
#
# Shared by `test_ac_16_attribution.py`, `test_attribution_ingest.py`,
# `test_outcome_window_states.py`, `test_attribution_compatibility.py`,
# `test_inv_08_attribution_boundaries.py`, `test_inv_08_supersession.py`,
# `test_outcome_schema_versions.py`, and the setup of the parametrized tests
# in `test_inv_01_projections_append_only.py`.

#: Canonical identities, read from the fixtures rather than retyped.
ACCOUNT_REF = canonical_by_type("account.discovered")["account_ref"]
ACTION_EVENT_ID = canonical_by_type("action.recorded")["event_id"]
OUTCOME_EVENT_ID = canonical_by_type("outcome.evaluated")["event_id"]


class FixedClock:
    """An injectable clock that moves only when a test advances it."""

    def __init__(self, start: datetime | None = None):
        self.now = start if start is not None else datetime.fromisoformat("2026-09-11T12:00:00Z")

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


def attribute_ledger(harness: Harness, *, reevaluate: bool = False, clock=None):
    """One run of the attribution command against the harness's application,
    submitted over the same in-process HTTP collector boundary as `seed`."""
    return run_attribution(
        harness.app, reevaluate=reevaluate, clock=clock if clock is not None else FixedClock()
    )


def seed_and_attribute(harness: Harness, clock=None):
    """The canonical nine through the collector, then one attribution run."""
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    run = attribute_ledger(harness, clock=clock)
    assert [s.http_status for s in run.submissions] == [201], [s.body for s in run.submissions]
    return run


def max_sequence(harness: Harness) -> int:
    with harness.engine.connect() as conn:
        return ledger_maximum(conn)


def attribution_rows(harness: Harness) -> list:
    """Every `outcome_attributions` row, in ingest order."""
    with harness.engine.connect() as conn:
        return conn.execute(
            select(outcome_attributions)
            .join(events, events.c.event_id == outcome_attributions.c.attribution_event_id)
            .order_by(events.c.ingest_sequence)
        ).all()


def outcome_row(harness: Harness, outcome_event_id: str):
    with harness.engine.connect() as conn:
        return conn.execute(
            select(outcomes).where(outcomes.c.outcome_event_id == outcome_event_id)
        ).first()


def event_hash(harness: Harness, event_id: str) -> str:
    with harness.engine.connect() as conn:
        return conn.execute(
            select(events.c.canonical_hash).where(events.c.event_id == event_id)
        ).scalar_one()


def attribute_at(harness: Harness, outcome_event_id: str, cutoff: int | None = None):
    """`outcome-attribution-v1` computed directly, at `cutoff` or the ledger maximum."""
    with harness.engine.connect() as conn:
        return attribute(
            conn,
            outcome_event_id,
            cutoff=cutoff if cutoff is not None else ledger_maximum(conn),
        )


def discovery_envelope(account_ref: str, *, name: str | None = None) -> dict:
    """A second account's `account.discovered`, derived from the canonical one."""
    envelope = copy.deepcopy(canonical_by_type("account.discovered"))
    envelope.update(
        event_id=f"evt-{account_ref}-discovered",
        account_ref=account_ref,
        payload={"name": name or account_ref, "domain": f"{account_ref}.example"},
    )
    return envelope


def action_envelope(
    event_id: str,
    *,
    occurred_at: str,
    status: str = "sent",
    decision_event_id: str = DECISION_EVENT_ID,
    account_ref: str = ACCOUNT_REF,
    recorded_at: str | None = None,
) -> dict:
    """The canonical `action.recorded` with its identity, time and status replaced."""
    envelope = copy.deepcopy(canonical_by_type("action.recorded"))
    envelope.update(
        event_id=event_id,
        account_ref=account_ref,
        occurred_at=occurred_at,
        recorded_at=recorded_at if recorded_at is not None else occurred_at,
    )
    envelope["payload"].update(status=status, decision_event_id=decision_event_id)
    return envelope


def decision_copy_envelope(event_id: str, *, boundary: str, account_ref: str = ACCOUNT_REF) -> dict:
    """The canonical decision re-recorded under a new id at a later boundary.

    Its preserved evidence was available at the canonical boundary, so any
    later boundary still satisfies the collector's availability rule.
    """
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope.update(
        event_id=event_id,
        account_ref=account_ref,
        occurred_at=boundary,
        recorded_at=boundary,
    )
    envelope["payload"]["decision_boundary"] = boundary
    return envelope


def outcome_v2_envelope(
    event_id: str,
    *,
    observed_at: str,
    window_opened_at: str,
    window_closes_at: str,
    evaluation_state: str,
    account_ref: str = ACCOUNT_REF,
    recorded_at: str | None = None,
    source: str = "crm-sim",
    **payload,
) -> dict:
    """A schema-v2 `outcome.evaluated`. Keyword `payload` entries are added as
    given (observations, source claims, a supersession link); anything not
    given is omitted, which v2 reads as unknown or absent."""
    return {
        "schema_version": "2",
        "event_id": event_id,
        "event_type": "outcome.evaluated",
        "source": source,
        "account_ref": account_ref,
        "occurred_at": observed_at,
        "recorded_at": recorded_at if recorded_at is not None else observed_at,
        "payload": {
            "window_opened_at": window_opened_at,
            "window_closes_at": window_closes_at,
            "evaluation_state": evaluation_state,
            "observed_at": observed_at,
            **payload,
        },
    }


def outcome_v1_envelope(
    event_id: str,
    *,
    action_event_id: str,
    occurred_at: str,
    account_ref: str = ACCOUNT_REF,
) -> dict:
    """The canonical v1 outcome under a new id, naming `action_event_id`."""
    envelope = copy.deepcopy(canonical_by_type("outcome.evaluated"))
    envelope.update(
        event_id=event_id,
        account_ref=account_ref,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
    )
    envelope["payload"]["action_event_id"] = action_event_id
    return envelope


def post_created(harness: Harness, *envelopes: dict) -> None:
    for envelope in envelopes:
        response = harness.post(envelope)
        assert response.status_code == 201, (envelope["event_id"], response.json())


def attribution_envelope(
    harness: Harness,
    outcome_event_id: str,
    *,
    cutoff: int | None = None,
    supersedes: str | None = None,
    clock=None,
    policy_version: str | None = None,
) -> dict:
    """A policy-correct `outcome.attributed` envelope, built as the command
    builds it but not submitted, so a test can submit or mutate it by hand."""
    with harness.engine.connect() as conn:
        at = cutoff if cutoff is not None else ledger_maximum(conn)
        result = attribute(
            conn,
            outcome_event_id,
            cutoff=at,
            **({"policy_version": policy_version} if policy_version else {}),
        )
        account_ref = load_outcome(conn, outcome_event_id, cutoff=at).account_ref
    now = (clock if clock is not None else FixedClock())()
    return build_envelope(
        result,
        account_ref=account_ref,
        attributed_at=now,
        recorded_at=now,
        supersedes_attribution_event_id=supersedes,
    )


def append_unrelated(harness: Harness, count: int = 1) -> None:
    """Raise the ledger maximum with events no attribution depends on."""
    for _ in range(count):
        post_created(harness, discovery_envelope(f"filler-{uuid.uuid4().hex[:12]}"))


def insert_ambiguous_attribution(harness: Harness, other_attribution_event_id: str) -> str:
    """Corrupt the ledger *around* the collector: a second effective result for
    the canonical outcome, inserted directly (the append-only triggers refuse
    UPDATE and DELETE, not INSERT). Its supersession link names another
    outcome's result, so both it and the genuine result are unsuperseded.
    Used only to prove that selection raises instead of picking."""
    corrupt_id = "evt-test-corrupt-attribution"
    stamp = "2026-09-11T12:00:00.000000Z"
    with harness.engine.begin() as conn:
        conn.execute(
            events.insert().values(
                event_id=corrupt_id,
                schema_version="1",
                event_type="outcome.attributed",
                source="test-corruption",
                account_ref=ACCOUNT_REF,
                occurred_at=stamp,
                recorded_at=stamp,
                canonical_hash="0" * 64,
                payload="{}",
            )
        )
        conn.execute(
            outcome_attributions.insert().values(
                attribution_event_id=corrupt_id,
                account_ref=ACCOUNT_REF,
                source_event_id=corrupt_id,
                outcome_event_id=OUTCOME_EVENT_ID,
                policy_version=POLICY_VERSION,
                method="explicit_source_reference",
                window_days=90,
                resolved_action_event_id=ACTION_EVENT_ID,
                resolved_decision_event_id=DECISION_EVENT_ID,
                status="direct",
                reason="valid_source_action_reference",
                attributed_at=stamp,
                ingest_cutoff=10**6,
                supersedes_attribution_event_id=other_attribution_event_id,
            )
        )
    return corrupt_id


def seed_through_decision(harness: Harness) -> None:
    """Both registrations plus the account envelopes up to and including the
    canonical decision: no persona, action or outcome."""
    register_artifacts(harness)
    for index in range(4):
        assert harness.post_raw(canonical_raw(index)).status_code == 201
