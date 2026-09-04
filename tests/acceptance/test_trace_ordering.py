"""Trace ordering (INV-02 tie-break) and the trace page (INV-10 labels)."""

import copy
import re

from sqlalchemy import select

from flight_recorder.ledger.schema import events
from flight_recorder.web.routes import trace_query
from tests.conftest import canonical_by_type, canonical_envelopes, canonical_raw, seed_all

KIND_ORDER = ["EVENT", "EVIDENCE", "EVIDENCE", "DECISION", "EVENT", "ACTION", "OUTCOME"]


def _kinds(html: str) -> list[str]:
    return re.findall(r'<span class="kind">([A-Z]+)</span>', html)


def test_seeded_trace_has_seven_rows_in_canonical_order(harness):
    seed_all(harness)
    account_ref = canonical_envelopes()[0]["account_ref"]
    with harness.engine.connect() as conn:
        rows = conn.execute(trace_query(account_ref)).all()
    assert [r.event_id for r in rows] == [e["event_id"] for e in canonical_envelopes()]

    page = harness.client.get(f"/accounts/{account_ref}")
    assert page.status_code == 200
    assert _kinds(page.text) == KIND_ORDER


def test_same_occurred_at_orders_by_ingest_sequence(harness):
    harness.post_raw(canonical_raw(0))
    base = canonical_by_type("evidence.recorded")
    ids = []
    for i in range(3):
        env = copy.deepcopy(base)
        env["event_id"] = f"evt-same-instant-{i}"
        env["payload"]["items"] = [
            {
                "evidence_version_id": f"ev-same-instant-{i}",
                "evidence_type": "open_platform_engineering_roles",
                "value": i,
            }
        ]
        assert harness.post(env).status_code == 201
        ids.append(env["event_id"])
    with harness.engine.connect() as conn:
        rows = conn.execute(trace_query(env["account_ref"])).all()
        sequences = conn.execute(
            select(events.c.event_id, events.c.ingest_sequence).where(events.c.event_id.in_(ids))
        ).all()
    assert [r.event_id for r in rows[1:]] == ids
    assert len({r.occurred_at for r in rows[1:]}) == 1
    assert sorted(sequences, key=lambda r: r.ingest_sequence) == [
        (i, s) for i, s in sorted(sequences, key=lambda r: ids.index(r.event_id))
    ]


def test_pages_show_time_labels_and_synthetic_banner(harness):
    seed_all(harness)
    account_ref = canonical_envelopes()[0]["account_ref"]
    home = harness.client.get("/")
    trace = harness.client.get(f"/accounts/{account_ref}")
    for page in (home, trace):
        assert page.status_code == 200
        assert "synthetic-banner" in page.text
        assert "simulated" in page.text and "fictional" in page.text
        assert "Merge" not in page.text
        assert "RelayBridge" in page.text
    assert f'href="/accounts/{account_ref}"' in home.text
    assert "Occurred at" in trace.text and "Recorded at" in trace.text
    assert trace.text.count("Occurred at:") == 7
    assert trace.text.count("Recorded at:") == 7
    assert 'lang="en"' in trace.text and "skip-link" in trace.text


def test_unknown_account_is_404(client):
    assert client.get("/accounts/nobody").status_code == 404
