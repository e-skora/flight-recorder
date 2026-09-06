"""INV-01 at the database: every projected historical record is append-only.

The present must not rewrite the past even through direct SQL, so each
projection table carries `BEFORE UPDATE` and `BEFORE DELETE` triggers. A
Hypothesis case additionally ingests the canonical nine in any valid order and
asserts the projections are identical whichever order they arrived in.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from flight_recorder.ledger.schema import PROJECTION_TABLES
from tests.conftest import Harness, canonical_envelope_paths, seed_all

pytestmark = pytest.mark.invariant

TABLE_IDS = [table.name for table in PROJECTION_TABLES]

#: Each canonical envelope's index and the indices it must follow. The two
#: registrations and discovery are independent; everything else has a real
#: dependency the collector enforces.
DEPENDENCIES = {
    0: (),  # logic artifact v3.2
    1: (),  # logic artifact v5.1
    2: (),  # account.discovered
    3: (2,),  # evidence (enrichment)
    4: (2,),  # evidence (integration pressure)
    5: (0, 3, 4),  # decision: needs its artifact and both evidence events
    6: (5,),  # persona.selected
    7: (5,),  # action.recorded
    8: (7,),  # outcome.evaluated
}


def _first_writable_column(table):
    """A non-primary-key column, so the UPDATE is a genuine mutation attempt."""
    return next(c for c in table.columns if not c.primary_key)


@pytest.mark.parametrize("table", PROJECTION_TABLES, ids=TABLE_IDS)
def test_update_on_a_projection_table_is_refused(harness, table):
    seed_all(harness)
    before = harness.projection_rows()
    assert before[table.name], f"{table.name} must have rows for this test to mean anything"
    column = _first_writable_column(table)
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(update(table).values({column: "tampered"}))
    assert harness.projection_rows() == before


@pytest.mark.parametrize("table", PROJECTION_TABLES, ids=TABLE_IDS)
def test_delete_on_a_projection_table_is_refused(harness, table):
    seed_all(harness)
    before = harness.projection_rows()
    assert before[table.name]
    with pytest.raises(IntegrityError, match="INV-01"), harness.engine.begin() as conn:
        conn.execute(delete(table))
    assert harness.projection_rows() == before


@st.composite
def valid_orders(draw):
    """A random topological order of the canonical nine.

    Built by repeatedly choosing among the envelopes whose dependencies are
    already placed, so every generated order is admissible by construction.
    """
    placed: list[int] = []
    remaining = set(DEPENDENCIES)
    while remaining:
        ready = sorted(e for e in remaining if set(DEPENDENCIES[e]) <= set(placed))
        chosen = draw(st.sampled_from(ready))
        placed.append(chosen)
        remaining.remove(chosen)
    return placed


@given(valid_orders())
def test_any_valid_ingestion_order_produces_identical_projections(tmp_path_factory, order):
    paths = canonical_envelope_paths()

    reference = Harness(tmp_path_factory.mktemp("inv01-ref"))
    seed_all(reference)
    expected = reference.projection_rows()

    shuffled = Harness(tmp_path_factory.mktemp("inv01-shuffled"))
    for index in order:
        response = shuffled.post_raw(paths[index].read_bytes())
        assert response.status_code == 201, (paths[index].name, response.json())

    actual = shuffled.projection_rows()
    # `decisions.ingest_sequence` records arrival order, which legitimately
    # differs; every other projected value is order-independent.
    for table_name in expected:
        if table_name == "decisions":
            continue
        assert actual[table_name] == expected[table_name], table_name
    assert [r[:-1] for r in actual["decisions"]] == [r[:-1] for r in expected["decisions"]]
