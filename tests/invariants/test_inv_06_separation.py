"""INV-06: the original and the counterfactual cannot be blurred.

Four proofs against a real seeded ledger:

1. A counterfactual is a different type from a reconstruction, carries its own
   fixed label on the value, is never equal to the original it contains, and
   keeps its numbers under `result` so a caller must name which side it reads.
2. `replay` plus `compare` execute no write, append no event, read no `events`
   or `accounts` row, and read `evidence_versions` by preserved id only; no
   table, row, or event anywhere holds a counterfactual (D-011).
3. Generatively (AC-18, INV-01): a random current artifact -- random weights,
   threshold, dropped factors, an optional factor over an unavailable input --
   registered through the collector never moves the original and always reads
   the sealed `-v1` versions.
4. The decision-detail page, which renders a counterfactual on every load,
   writes nothing either: three loads leave every row, every event, and the
   reconstruction untouched, and no table holds a counterfactual (3B).
"""

import re
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import inspect

from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.ledger.schema import PROJECTION_TABLES
from flight_recorder.logic.evaluator import InputState, evaluate
from flight_recorder.replay.counterfactual import (
    COUNTERFACTUAL_LABEL,
    ORIGINAL_LABEL,
    Counterfactual,
    compare,
    replay,
)
from flight_recorder.replay.reconstruct import Reconstruction, reconstruct
from tests.acceptance.test_decision_detail_page import decision_url
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    assert_same_reconstruction,
    canonical_boundary,
    canonical_context,
    canonical_evidence_ids,
    captured_statements,
    decision_rows,
    derived_artifact_envelope,
    factor,
    logic_artifact,
    register_derived_artifact,
    replay_under,
    seed_all,
    v5_1_hash,
)
from tests.invariants.test_inv_09_visible_failure_states import (
    mutated_artifact,
    register_unsupported_rule_artifact,
)

pytestmark = pytest.mark.invariant

#: The nine projection tables that exist on `main`; nothing holds a counterfactual.
PROJECTION_TABLE_NAMES = {
    "evidence_versions",
    "logic_artifacts",
    "decisions",
    "decision_context",
    "decision_consumed_inputs",
    "persona_selections",
    "actions",
    "outcomes",
    "outcome_attributions",
}
#: The only tables the counterfactual path may read.
READABLE = {
    "decisions",
    "logic_artifacts",
    "decision_context",
    "decision_consumed_inputs",
    "evidence_versions",
}
FORBIDDEN_TABLE = re.compile(r"\b(events|accounts)\b", re.IGNORECASE)
TABLE_REFERENCE = re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.IGNORECASE)


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


# --- 1. Type and label --------------------------------------------------------


def test_a_counterfactual_is_a_different_type_with_its_own_label(seeded):
    cf = replay_under(seeded, v5_1_hash())

    assert not isinstance(cf, Reconstruction)
    assert not issubclass(Counterfactual, Reconstruction)
    assert Counterfactual.__mro__ == (Counterfactual, object)
    assert isinstance(cf.original, Reconstruction)
    assert cf != cf.original
    assert cf.original != cf

    assert cf.label == COUNTERFACTUAL_LABEL == "counterfactual"
    c = compare(cf)
    assert c.original_label == ORIGINAL_LABEL == "original"
    assert c.counterfactual_label == COUNTERFACTUAL_LABEL == "counterfactual"

    # The numbers live under `result` on both sides; nothing at the top level
    # lets a caller read a score without naming which side it belongs to.
    assert not hasattr(cf, "score")
    assert not hasattr(cf, "output")
    assert not hasattr(cf.original, "score")
    assert (cf.result.score, cf.original.result.score) == (51, 86)

    for forged in (ORIGINAL_LABEL, "", "Counterfactual", "counterfactual "):
        with pytest.raises(ValueError):
            replace(cf, label=forged)
    assert replace(cf, label=COUNTERFACTUAL_LABEL) == cf


# --- 2. Nothing written, nothing persisted, evidence by preserved id only -------


def test_replay_writes_nothing_appends_no_event_and_leaves_every_row_untouched(seeded):
    snapshot_before = seeded.snapshot()
    rows_before = decision_rows(seeded)
    events_before = seeded.event_count()
    with seeded.engine.connect() as conn:
        before = reconstruct(conn, DECISION_EVENT_ID)

    with captured_statements(seeded.engine) as statements:
        with seeded.engine.connect() as conn:
            cf = replay(conn, DECISION_EVENT_ID, v5_1_hash())
            compare(cf)
    assert cf.result.score == 51

    # The listener sees real SQL text, so the negative matches below mean something.
    assert [s for s in statements if "decision_context" in s]
    assert [s for s in statements if "evidence_versions" in s]
    for statement in statements:
        head = statement.strip().upper()
        assert not head.startswith(("INSERT", "UPDATE", "DELETE")), statement
        assert not FORBIDDEN_TABLE.search(statement), statement
        assert set(TABLE_REFERENCE.findall(statement)) <= READABLE, statement
        if "evidence_versions" in statement:
            assert "evidence_versions.evidence_version_id = ?" in statement, statement
            for forbidden in (
                "supersedes_evidence_version_id",
                "account_ref",
                "evidence_type",
                "ORDER BY",
            ):
                assert forbidden not in statement, statement

    assert seeded.snapshot() == snapshot_before
    assert decision_rows(seeded) == rows_before
    assert seeded.event_count() == events_before
    with seeded.engine.connect() as conn:
        assert_same_reconstruction(reconstruct(conn, DECISION_EVENT_ID), before)

    # No table, row, or event exists that could hold a counterfactual.
    assert {table.name for table in PROJECTION_TABLES} == PROJECTION_TABLE_NAMES
    names = set(inspect(seeded.engine).get_table_names())
    assert names == {"accounts", "events", *PROJECTION_TABLE_NAMES}
    assert not [n for n in names if "counterfactual" in n or "replay" in n]


# --- 3. Random current logic never moves the original (AC-18) -----------------

KEYS = [f["key"] for f in logic_artifact("v5.1")["factors"]]
WEBSITE_INTENT = {"key": "website_intent", "rule": "website_intent equals 'HIGH'", "weight": 0}


@st.composite
def current_logic(draw):
    return {
        "weights": {key: draw(st.integers(min_value=-60, max_value=60)) for key in KEYS},
        "threshold": draw(st.integers(min_value=0, max_value=150)),
        "dropped": draw(st.sets(st.sampled_from(KEYS), max_size=5)),
        "add_website_intent": draw(st.booleans()),
        "website_intent_weight": draw(st.integers(min_value=-60, max_value=60)),
        "n": draw(st.integers(min_value=0, max_value=10**9)),
    }


def drawn_envelope(drawn: dict) -> dict:
    factors = [
        {**f, "weight": drawn["weights"][f["key"]]}
        for f in logic_artifact("v5.1")["factors"]
        if f["key"] not in drawn["dropped"]
    ]
    if drawn["add_website_intent"]:
        factors.append({**WEBSITE_INTENT, "weight": drawn["website_intent_weight"]})
    n = drawn["n"]
    return derived_artifact_envelope(
        f"logic-account-prioritization-hyp-{n}",
        f"hyp-{n}",
        factors,
        event_id=f"evt-system-logic-artifact-hyp-{n}",
        threshold=drawn["threshold"],
    )


@given(drawn=current_logic())
def test_random_current_logic_never_moves_the_original_and_always_reads_the_sealed_versions(
    tmp_path_factory, drawn
):
    harness = Harness(tmp_path_factory.mktemp("inv06-separation"))
    for response in seed_all(harness):
        assert response.status_code == 201
    with harness.engine.connect() as conn:
        before = reconstruct(conn, DECISION_EVENT_ID)
    assert before.result.score == 86
    count_before, accounts_before, projections_before = harness.snapshot()

    envelope = drawn_envelope(drawn)
    drawn_hash = register_derived_artifact(harness, envelope)
    remaining = [key for key in KEYS if key not in drawn["dropped"]]
    assert remaining

    cf = replay_under(harness, drawn_hash)
    pure = evaluate(
        LogicArtifact.model_validate(envelope["payload"]["artifact"]),
        canonical_context(),
        canonical_boundary(),
    )

    assert cf.label == "counterfactual"
    assert cf.current_logic_version == f"hyp-{drawn['n']}"
    assert cf.current_artifact_hash == drawn_hash
    ids = canonical_evidence_ids()
    for key in remaining:
        evaluated = factor(cf.result, key)
        assert evaluated.evidence_version_id == ids[key], key
        assert evaluated.input_state is InputState.CONSUMED, key
        assert evaluated.matched == factor(pure, key).matched, key
        assert evaluated.contribution == (drawn["weights"][key] if evaluated.matched else 0), key
    if drawn["add_website_intent"]:
        website_intent = factor(cf.result, "website_intent")
        assert website_intent.input_state is InputState.UNAVAILABLE
        assert website_intent.contribution == 0
        assert website_intent.evidence_version_id is None
    assert len(cf.result.factors) == len(remaining) + int(drawn["add_website_intent"])

    assert cf.result.score == sum(f.contribution for f in cf.result.factors)
    assert cf.result.threshold == drawn["threshold"]
    expected_output = "PRIORITIZE" if cf.result.score >= drawn["threshold"] else "DO_NOT_PRIORITIZE"
    assert cf.result.output == expected_output
    c = compare(cf)
    assert c.score_delta == cf.result.score - 86
    assert c.score_delta == sum(change.contribution_delta for change in c.contributions)
    assert set(cf.result.context_states) == {entry.key for entry in canonical_context()}
    assert len(cf.result.context_states) == 8

    with harness.engine.connect() as conn:
        assert_same_reconstruction(reconstruct(conn, DECISION_EVENT_ID), before)

    count_after, accounts_after, projections_after = harness.snapshot()
    assert count_after == count_before + 1
    assert accounts_after == accounts_before
    for name, rows in projections_before.items():
        if name == "logic_artifacts":
            assert len(projections_after[name]) == len(rows) + 1
            assert set(rows) < set(projections_after[name])
        else:
            assert projections_after[name] == rows, name


# --- 4. The decision page persists nothing either (3B) ------------------------


def test_viewing_the_decision_page_persists_no_counterfactual(seeded):
    """Three page loads -- default, an explicit hash, an unregistered hash --
    write nothing, append no event, and move no row. The counterfactual the
    panel renders is computed on demand every time and is stored nowhere
    (D-011, INV-06)."""
    snapshot_before = seeded.snapshot()
    rows_before = decision_rows(seeded)
    events_before = seeded.event_count()
    with seeded.engine.connect() as conn:
        before = reconstruct(conn, DECISION_EVENT_ID)

    url = decision_url()
    # The page runs on the application's own engine, not the harness's.
    with captured_statements(seeded.app.state.engine) as statements:
        for query in ("", f"?current={v5_1_hash()}", "?current=" + "0" * 64):
            assert seeded.client.get(url + query).status_code == 200

    # The listener saw real SQL, so the negative matches below mean something.
    assert [s for s in statements if "decision_context" in s]
    assert [s for s in statements if "logic_artifacts" in s]
    for statement in statements:
        assert not statement.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")), statement

    assert seeded.snapshot() == snapshot_before
    assert decision_rows(seeded) == rows_before
    assert seeded.event_count() == events_before

    names = set(inspect(seeded.engine).get_table_names())
    assert names == {"accounts", "events", *PROJECTION_TABLE_NAMES}
    assert not [n for n in names if "counterfactual" in n or "replay" in n]

    with seeded.engine.connect() as conn:
        assert_same_reconstruction(reconstruct(conn, DECISION_EVENT_ID), before)


def test_viewing_a_failing_decision_page_persists_nothing_either(seeded):
    """A page that cannot replay writes no more than one that can.

    Two failure pages are loaded: an artifact whose rule the grammar does not
    support, and one whose `missing_value_behavior` the evaluator does not
    implement. Both render a named failure at 200, and the failure path --
    including the second `reconstruct` that establishes where the failure arose
    -- is read-only like every other page path (INV-06, INV-09).
    """
    unsupported_rule_hash = register_unsupported_rule_artifact(seeded)
    bad_behavior_hash = mutated_artifact(
        seeded,
        "test-bad-missing-value",
        "v5.1-test-bad-missing-value",
        "evt-system-logic-artifact-test-bad-missing-value",
        lambda artifact: artifact.update(missing_value_behavior="guess_the_value"),
    )

    snapshot_before = seeded.snapshot()
    rows_before = decision_rows(seeded)
    events_before = seeded.event_count()

    url = decision_url()
    with captured_statements(seeded.app.state.engine) as statements:
        for artifact_hash in (unsupported_rule_hash, bad_behavior_hash):
            response = seeded.client.get(url + f"?current={artifact_hash}")
            assert response.status_code == 200, response.status_code
            assert 'id="replay-integrity-failure"' in response.text

    assert [s for s in statements if "logic_artifacts" in s]
    for statement in statements:
        assert not statement.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")), statement

    assert seeded.snapshot() == snapshot_before
    assert decision_rows(seeded) == rows_before
    assert seeded.event_count() == events_before
