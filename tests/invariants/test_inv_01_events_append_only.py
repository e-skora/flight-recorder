"""INV-01 at the database: events are append-only; foreign keys are enforced."""

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError

from flight_recorder.ledger.schema import events
from tests.conftest import canonical_raw

pytestmark = pytest.mark.invariant


def _seeded(harness):
    assert harness.post_raw(canonical_raw(0)).status_code == 201
    return harness.engine


def test_update_on_events_is_refused(harness):
    engine = _seeded(harness)
    with pytest.raises(IntegrityError, match="INV-01"), engine.begin() as conn:
        conn.execute(update(events).values(source="tampered"))
    assert harness.event_count() == 1


def test_delete_on_events_is_refused(harness):
    engine = _seeded(harness)
    with pytest.raises(IntegrityError, match="INV-01"), engine.begin() as conn:
        conn.execute(delete(events))
    assert harness.event_count() == 1


def test_event_for_nonexistent_account_is_refused_by_foreign_key(harness):
    engine = _seeded(harness)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as conn:
        conn.execute(
            insert(events).values(
                event_id="evt-orphan",
                schema_version="1",
                event_type="persona.selected",
                source="test",
                account_ref="no-such-account",
                occurred_at="2026-04-17T10:06:00.000000Z",
                recorded_at="2026-04-17T10:06:00.000000Z",
                canonical_hash="0" * 64,
                payload="{}",
            )
        )
    assert harness.event_count() == 1
