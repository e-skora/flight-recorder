"""D-018 piece 1: a snapshot changed after it was built is refused. Test 10.

Each case alters its own owned copy of a freshly built release snapshot, never
a shared file and never `fixtures/`. Where a table is append-only, the case
drops that table's guard trigger on its own copy only, makes the change, and
recreates the trigger from its stored definition, so the schema text is
exactly as built and the only difference is the data (the control case proves
the trigger round trip alone changes nothing). No production code disables a
trigger.

Every case first asserts **content sensitivity directly**: the altered copy's
content identity differs from the clean copy's, computed on valid inputs and
independent of the pinned value. Then startup refuses the copy with a named
content-identity error, and the copy's full state and file bytes are identical
before and after the refusal.

INV-01 (the recorded past is immutable: a rewritten event, projection or
account is not served), INV-04 (evidence and projections are append-only
versions: a deleted or edited row is detected), INV-05 (identity is content,
never a label: the stored payload is hashed as stored and never recomputed),
INV-10 (only what was recorded is served as having happened) and INV-11 (the
snapshot is exactly what the collector wrote).
"""

import sqlite3

import pytest

from flight_recorder.public_demo import (
    ContentIdentityMismatch,
    SnapshotTablesMismatch,
    content_identity,
    create_public_demo,
    open_read_only,
)
from tests.public_demo.conftest import (
    EXPECTED_CONTENT_IDENTITY,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)

pytestmark = pytest.mark.invariant

CANONICAL_DECISION = "evt-novasignal-04-decision-recorded"


def identity_of(path) -> str:
    engine = open_read_only(path)
    try:
        with engine.connect() as conn:
            return content_identity(conn)
    finally:
        engine.dispose()


def alter(path, statements, *, unguard=()):
    """Run `statements` on this owned copy, with the named triggers dropped and
    recreated from their stored SQL around them."""
    connection = sqlite3.connect(path)
    try:
        saved = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
            ).fetchone()[0]
            for name in unguard
        }
        for name in unguard:
            connection.execute(f"DROP TRIGGER {name}")
        for statement, parameters in statements:
            cursor = connection.execute(statement, parameters)
            assert cursor.rowcount != 0 or statement.lstrip().upper().startswith("CREATE")
        for sql in saved.values():
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


CASES = {
    "a_account_name_changed": (
        [
            (
                "UPDATE accounts SET name = ? WHERE account_ref = ?",
                ("NovaSignal A1", "novasignal-ai"),
            )
        ],
        (),
    ),
    "b_event_payload_changed_hash_kept": (
        [
            (
                "UPDATE events SET payload = replace(payload, '\"Series B\"', '\"Series C\"') "
                "WHERE event_id = ?",
                ("evt-novasignal-02-evidence-enrichment",),
            )
        ],
        ("events_no_update",),
    ),
    "c1_decision_context_value_changed": (
        [
            (
                "UPDATE decision_context SET value_text = '185' "
                "WHERE decision_event_id = ? AND input_key = 'employee_count'",
                (CANONICAL_DECISION,),
            )
        ],
        ("decision_context_no_update",),
    ),
    "c2_outcome_attribution_field_changed": (
        [
            (
                "UPDATE outcome_attributions SET status = 'unresolved' "
                "WHERE attribution_event_id = "
                "(SELECT min(attribution_event_id) FROM outcome_attributions)",
                (),
            )
        ],
        ("outcome_attributions_no_update",),
    ),
    "d_projection_row_deleted": (
        [
            (
                "DELETE FROM persona_selections WHERE event_id = "
                "(SELECT max(event_id) FROM persona_selections "
                "WHERE account_ref != 'novasignal-ai')",
                (),
            )
        ],
        ("persona_selections_no_delete",),
    ),
    "e_extra_table_added": ([("CREATE TABLE visitor_notes (note TEXT)", ())], ()),
}


@pytest.fixture
def clean(built_snapshot, tmp_path):
    return owned_copy(built_snapshot, tmp_path, "clean.db")


def test_the_payload_change_really_leaves_the_stored_hash_as_it_was(clean, tmp_path):
    """Case b's premise: only the payload changes; the stored hash does not."""
    altered = owned_copy(clean, tmp_path, "b.db")
    statements, unguard = CASES["b_event_payload_changed_hash_kept"]
    alter(altered, statements, unguard=unguard)
    query = "SELECT canonical_hash, payload FROM events WHERE event_id = ?"
    event = ("evt-novasignal-02-evidence-enrichment",)
    before = sqlite3.connect(clean).execute(query, event).fetchone()
    after = sqlite3.connect(altered).execute(query, event).fetchone()
    assert before[0] == after[0]
    assert before[1] != after[1] and '"Series C"' in after[1]


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_changed_copy_is_detected_and_refused_and_left_as_it_was(case, clean, tmp_path):
    statements, unguard = CASES[case]
    altered = owned_copy(clean, tmp_path, f"{case}.db")
    alter(altered, statements, unguard=unguard)

    # Content sensitivity, on valid inputs and independent of the pin. An
    # unlisted table is refused by the identity computation itself.
    clean_identity = identity_of(clean)
    if case == "e_extra_table_added":
        with pytest.raises(SnapshotTablesMismatch):
            identity_of(altered)
    else:
        assert identity_of(altered) != clean_identity, f"{case}: the identity did not move"

    state, sha256 = full_state(altered), file_sha256(altered)
    with pytest.raises(ContentIdentityMismatch) as caught:
        create_public_demo(altered)
    assert_unchanged(altered, state, sha256)
    if case == "e_extra_table_added":
        assert isinstance(caught.value, SnapshotTablesMismatch)
        assert caught.value.unexpected == ["visitor_notes"]
    else:
        assert caught.value.computed not in (None, EXPECTED_CONTENT_IDENTITY)


def test_the_trigger_round_trip_alone_changes_nothing(clean, tmp_path):
    """Control: dropping and recreating every guard trigger used above, with no
    data change, leaves the identity as built, so each case above is detected
    by its data change and not by the trigger handling."""
    copy = owned_copy(clean, tmp_path, "control.db")
    triggers = sorted({t for _, unguard in CASES.values() for t in unguard})
    alter(copy, [], unguard=triggers)
    assert identity_of(copy) == identity_of(clean) == EXPECTED_CONTENT_IDENTITY
    assert create_public_demo(copy).state.public_demo is True


def test_the_untouched_copy_still_starts(clean):
    state, sha256 = full_state(clean), file_sha256(clean)
    assert create_public_demo(clean).state.public_demo is True
    assert_unchanged(clean, state, sha256)
