"""`PRODUCT.md` §4.4: one screen shows everything recorded about one decision.

Every field asserted here is read back from the seeded ledger or loaded from
`fixtures/canonical/`, never retyped, so a test failure means the page stopped
showing what was recorded rather than that a constant drifted.

The four recorded input states are the point of the context table (INV-03):
`consumed`, `available but ignored`, `unavailable` and `absent` each get a
named test, and each is asserted as cell *text*, because a class attribute or a
color is not a distinction a reader can rely on (`PRODUCT.md` §11).

Outcomes render at their recorded scope -- observations recorded for the
account -- with a recorded action or decision reference shown as a recorded,
unvalidated reference and no attribution status claimed (D-012, INV-08). The
two helpers at the top of this file are the assertion contract for the three
new page-test modules: assertions are made against one identified region or one
identified table's cells, never against the whole page.
"""

import copy
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from flight_recorder.ledger.schema import decisions, events, logic_artifacts
from tests.acceptance.test_ac_04_isolation import SECOND_ACCOUNT
from tests.acceptance.test_trace_ordering import KIND_ORDER, _kinds
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    canonical_by_type,
    canonical_evidence_ids,
    canonical_raw,
    evidence_version_row,
    register_artifacts,
    seed_all,
)

ACCOUNT_REF = "novasignal-ai"
ABSENT_DECISION_ID = "evt-test-decision-absent-employee-count"
SECOND_DECISION_ID = "evt-test-decision-second-copy"


# --- The assertion contract -------------------------------------------------


class _Region(HTMLParser):
    """The inner HTML of the element carrying one id, nested elements included."""

    def __init__(self, element_id: str):
        super().__init__(convert_charrefs=True)
        self.element_id = element_id
        self.tag: str | None = None
        self.depth = 0
        self.found = False
        self.pieces: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.tag is None:
            if not self.found and dict(attrs).get("id") == self.element_id:
                self.tag, self.depth, self.found = tag, 1, True
            return
        if tag == self.tag:
            self.depth += 1
        self.pieces.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if self.tag is None:
            return
        if tag == self.tag:
            self.depth -= 1
            if self.depth == 0:
                self.tag = None
                return
        self.pieces.append(f"</{tag}>")

    def handle_data(self, data):
        if self.tag is not None:
            self.pieces.append(data)


def element(html: str, element_id: str) -> str:
    """The inner HTML of `#element_id`. Raises when the element is absent."""
    parser = _Region(element_id)
    parser.feed(html)
    parser.close()
    if not parser.found:
        raise AssertionError(f"no element with id {element_id!r} in the rendered page")
    return "".join(parser.pieces)


def has_element(html: str, element_id: str) -> bool:
    parser = _Region(element_id)
    parser.feed(html)
    parser.close()
    return parser.found


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def rows(html: str, table_id: str) -> list[list[str]]:
    """Each body row of `#table_id` as its cells' visible text, stripped."""
    parser = _Rows()
    parser.feed(element(html, table_id))
    parser.close()
    return parser.rows


def row_for(html: str, table_id: str, key: str) -> list[str]:
    return next(row for row in rows(html, table_id) if row[0] == key)


# --- Fixtures and ledger reads ----------------------------------------------


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def decision_url(decision_event_id: str = DECISION_EVENT_ID, account_ref: str = ACCOUNT_REF) -> str:
    return f"/accounts/{account_ref}/decisions/{decision_event_id}"


def page(harness: Harness, decision_event_id: str = DECISION_EVENT_ID, query: str = "") -> str:
    response = harness.client.get(decision_url(decision_event_id) + query)
    assert response.status_code == 200, response.status_code
    return response.text


def decision_row(harness: Harness, decision_event_id: str = DECISION_EVENT_ID):
    with harness.engine.connect() as conn:
        return conn.execute(
            select(decisions).where(decisions.c.decision_event_id == decision_event_id)
        ).first()


def event_row(harness: Harness, event_id: str):
    with harness.engine.connect() as conn:
        return conn.execute(select(events).where(events.c.event_id == event_id)).first()


def artifact_row(harness: Harness, artifact_hash: str):
    with harness.engine.connect() as conn:
        return conn.execute(
            select(logic_artifacts).where(logic_artifacts.c.artifact_hash == artifact_hash)
        ).first()


def canonical_decision_payload() -> dict:
    return canonical_by_type("decision.recorded")["payload"]


def consumed_contribution(input_key: str) -> int:
    return next(
        used["contribution"]
        for used in canonical_decision_payload()["consumed_inputs"]
        if used["input_key"] == input_key
    )


def absent_variant_envelope() -> tuple[dict, int]:
    """The canonical decision without `employee_count`, and the score that leaves.

    The key is dropped from both `historical_context` and `consumed_inputs`, so
    the historical artifact still has a factor for it and the preserved context
    has no row: exactly the `absent` state (INV-03). The recorded score is the
    canonical score minus that factor's recorded contribution, derived here
    rather than typed.
    """
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = ABSENT_DECISION_ID
    payload = envelope["payload"]
    payload["historical_context"] = [
        entry for entry in payload["historical_context"] if entry["input_key"] != "employee_count"
    ]
    payload["consumed_inputs"] = [
        used for used in payload["consumed_inputs"] if used["input_key"] != "employee_count"
    ]
    score = payload["result"]["score"] - consumed_contribution("employee_count")
    payload["result"] = {
        "score": score,
        "threshold": payload["result"]["threshold"],
        "output": "DO_NOT_PRIORITIZE",
    }
    return envelope, score


# --- §4.4: every recorded field ---------------------------------------------


def test_the_decision_page_shows_every_field_section_4_4_requires(seeded):
    recorded = decision_row(seeded)
    event = event_row(seeded, DECISION_EVENT_ID)
    artifact = artifact_row(seeded, recorded.artifact_hash)
    html = page(seeded)

    summary = element(html, "decision-summary")
    assert recorded.decision_class == "account_prioritization"
    assert recorded.decision_class in summary
    assert element(html, "decision-output") == recorded.output == "PRIORITIZE"
    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    assert (recorded.score, recorded.threshold) == (86, 75)
    assert recorded.decision_boundary in summary
    assert event.occurred_at in summary and event.recorded_at in summary
    assert event.source in summary
    assert recorded.workflow_version == "v4.2"
    assert recorded.workflow_version in summary

    identity = element(html, "logic-identity")
    assert recorded.logic_version == "v3.2"
    assert recorded.logic_version in identity
    assert recorded.artifact_hash in identity
    assert len(recorded.artifact_hash) == 64
    assert artifact.artifact_id in identity
    assert artifact.artifact_schema_version in identity
    assert artifact.evaluator_version == "evaluator-v1"
    assert artifact.evaluator_version in identity

    ruleset = rows(html, "ruleset-table")
    assert [row[0] for row in ruleset] == [
        f["key"]
        for f in canonical_by_type("logic_artifact.registered")["payload"]["artifact"]["factors"]
    ]
    assert element(html, "ruleset").count("immutable") == 1


def test_confidence_is_shown_as_not_provided(seeded):
    summary = element(page(seeded), "decision-summary")
    assert "Confidence: not provided" in summary
    assert "%" not in summary


def test_every_preserved_input_appears_with_its_state_value_and_provenance(seeded):
    html = page(seeded)
    table = {row[0]: row for row in rows(html, "context-table")}
    ids = canonical_evidence_ids()

    expected_values = {
        entry["input_key"]: entry["value"]
        for entry in canonical_decision_payload()["historical_context"]
    }
    assert expected_values["employee_count"] == 184
    assert expected_values["website_intent"] is None

    for key, value in expected_values.items():
        row = table[key]
        assert row[1] == ("no value recorded" if value is None else str(value)), key

    for key, evidence_version_id in ids.items():
        evidence = evidence_version_row(seeded, evidence_version_id)
        row = table[key]
        assert evidence_version_id.endswith("-v1"), key
        assert row[4] == evidence_version_id, key
        assert row[5] == evidence.source, key
        assert row[6] == (evidence.observed_at or "not recorded"), key
        assert row[7] == evidence.available_at, key

    for used in canonical_decision_payload()["consumed_inputs"]:
        assert table[used["input_key"]][3] == str(used["contribution"]), used["input_key"]
    assert {row[3] for row in table.values() if row[2] == "consumed"} == {
        "25",
        "20",
        "18",
        "15",
        "8",
    }

    for ignored in ("verified_integration_pressure", "head_of_platform_start_date"):
        assert table[ignored][3] == "not consumed", ignored

    unavailable = table["website_intent"]
    assert unavailable[2] == "unavailable"
    assert unavailable[4] == "none" and unavailable[5] == "none"
    assert unavailable[6] == "not recorded" and unavailable[7] == "not recorded"


def test_the_recorded_input_states_render_as_words(seeded):
    table = {row[0]: row for row in rows(page(seeded), "context-table")}
    consumed = [used["input_key"] for used in canonical_decision_payload()["consumed_inputs"]]
    assert len(consumed) == 5
    for key in consumed:
        assert table[key][2] == "consumed", key
    for key in ("verified_integration_pressure", "head_of_platform_start_date"):
        assert table[key][2] == "available but ignored", key
    assert table["website_intent"][2] == "unavailable"


def test_an_input_the_historical_artifact_references_but_the_context_lacks_renders_as_absent(
    seeded,
):
    envelope, score = absent_variant_envelope()
    assert seeded.post(envelope).status_code == 201
    html = page(seeded, ABSENT_DECISION_ID)

    table = {row[0]: row for row in rows(html, "context-table")}
    absent = table["employee_count"]
    assert absent[2] == "absent"
    assert absent[3] == "not consumed"
    assert absent[1] == "no value recorded"
    assert absent[4] == "none" and absent[5] == "none"
    assert absent[6] == "not recorded" and absent[7] == "not recorded"

    preserved = [entry["input_key"] for entry in envelope["payload"]["historical_context"]]
    assert len(preserved) == 7
    for key in preserved:
        assert key in table, key
    assert set(table) == {*preserved, "employee_count"}

    summary = element(html, "decision-summary")
    assert element(html, "decision-score-threshold") == f"score {score} / threshold 75"
    assert score == 61
    assert element(html, "decision-output") == "DO_NOT_PRIORITIZE"
    assert "DO_NOT_PRIORITIZE" in summary


# --- Explanation, action, outcomes ------------------------------------------


def test_the_explanation_is_present_verbatim_and_marked_as_not_evidence(seeded):
    html = page(seeded)
    explanation = canonical_decision_payload()["explanation"]
    region = element(html, "explanation")

    assert explanation in region
    assert "not evidence or logic" in region
    assert "not the authoritative record" in region
    assert explanation not in element(html, "evidence-context")


def test_the_recorded_action_and_cost_are_shown(seeded):
    payload = canonical_by_type("action.recorded")["payload"]
    action_event = canonical_by_type("action.recorded")
    html = page(seeded)
    row = rows(html, "actions-table")[0]

    assert row[0] == payload["action_type"]
    assert row[1] == str(payload["play_id"]) == "14"
    assert row[2] == payload["target_persona"] == "Head of Platform"
    assert row[3] == payload["status"] == "sent"
    assert row[4] == f"{payload['cost']} {payload['currency']}" == "1.42 USD"
    assert row[5].startswith(action_event["occurred_at"][:19])
    assert "attribut" not in element(html, "actions").lower()


def test_the_recorded_outcome_is_shown_with_its_window_and_observations(seeded):
    payload = canonical_by_type("outcome.evaluated")["payload"]
    html = page(seeded)
    region = element(html, "outcomes")
    row = rows(html, "outcomes-table")[0]

    assert row[0] == f"{payload['window_days']} days" == "90 days"
    assert row[1] == "reply: no meeting: no opportunity: no"
    for observation in ("reply: no", "meeting: no", "opportunity: no"):
        assert observation in region
    outcome_event = canonical_by_type("outcome.evaluated")
    assert row[2].startswith(outcome_event["occurred_at"][:19])
    assert row[3].startswith(outcome_event["recorded_at"][:19])
    assert payload["action_event_id"] in row[4]
    assert "evt-novasignal-06-action-recorded" in row[4]
    assert "Recorded account outcomes" in region


def test_no_attribution_status_is_claimed(seeded):
    html = page(seeded)
    region = element(html, "outcomes")
    row = rows(html, "outcomes-table")[0]

    assert "Attribution not yet evaluated." in region
    assert (
        "No policy-based link between this outcome and this decision has been established."
        in region
    )
    assert "(recorded reference, not validated by an attribution policy)" in row[4]
    assert row[5] == (
        "Attribution not yet evaluated. No policy-based link between this outcome and "
        "this decision has been established."
    )
    for forbidden in ("direct", "inferred", "unresolved"):
        assert forbidden not in row[5], forbidden

    # The section's lede denies attribution; no *row* may claim it.
    for cell in row:
        for claim in ("resulted from", "credited to", "attributable to", "belongs to"):
            assert claim not in cell, (claim, cell)

    assert "D-007" not in html and "D-012" not in html
    assert re.search(r"\bphases?\b", html, re.IGNORECASE) is None
    assert re.search(r"\broadmap\b", html, re.IGNORECASE) is None


def test_an_account_outcome_is_not_claimed_for_a_decision_that_did_not_produce_it(seeded):
    envelope = copy.deepcopy(canonical_by_type("decision.recorded"))
    envelope["event_id"] = SECOND_DECISION_ID
    if seeded.post(envelope).status_code != 201:
        envelope, _ = absent_variant_envelope()
        envelope["event_id"] = SECOND_DECISION_ID
        assert seeded.post(envelope).status_code == 201

    html = page(seeded, SECOND_DECISION_ID)
    assert "No action recorded for this decision." in element(html, "actions")

    region = element(html, "outcomes")
    row = rows(html, "outcomes-table")[0]
    assert row[0] == "90 days"
    assert DECISION_EVENT_ID in row[4]
    assert SECOND_DECISION_ID not in row[4]
    assert "the decision on this page" not in row[4]
    assert "Attribution not yet evaluated." in region
    for cell in row:
        for claim in ("resulted from", "credited to", "attributable to", "belongs to"):
            assert claim not in cell, (claim, cell)


def test_a_decision_with_no_action_and_no_outcome_says_so(harness):
    register_artifacts(harness)
    for index in range(4):
        assert harness.post_raw(canonical_raw(index)).status_code == 201

    html = page(harness)
    assert "No action recorded for this decision." in element(html, "actions")
    assert "No outcome recorded for this account." in element(html, "outcomes")
    assert "Attribution not yet evaluated." not in element(html, "outcomes")
    assert not has_element(html, "replay-integrity-failure")


# --- Structure, keyboard, routing --------------------------------------------


def test_the_page_is_keyboard_usable_and_states_are_text(seeded):
    html = page(seeded)

    assert "onclick" not in html
    assert 'role="button"' not in html
    assert html.count('tabindex="') == 1
    assert 'tabindex="-1"' in html
    assert "<script" not in html

    assert html.count("<table") == html.count("<caption>")
    assert html.count("<table") >= 5

    selector = element(html, "current-logic-selector")
    assert '<form method="get"' in selector
    assert '<label for="current-artifact">' in selector
    assert '<select name="current" id="current-artifact">' in selector
    assert '<button type="submit">' in selector

    for state in ("consumed", "available but ignored", "unavailable"):
        assert state in element(html, "context-table")


def test_the_sections_are_top_level_and_do_not_nest(seeded):
    html = page(seeded)
    for section_id in (
        "decision-summary",
        "logic-identity",
        "ruleset",
        "evidence-context",
        "explanation",
        "actions",
        "outcomes",
        "replay-panel",
    ):
        region = element(html, section_id)
        assert "<section" not in region, section_id


def test_unknown_account_unknown_decision_and_wrong_account_are_404(seeded):
    for envelope in SECOND_ACCOUNT:
        assert seeded.post(envelope).status_code == 201

    assert seeded.client.get(decision_url(DECISION_EVENT_ID, "nope")).status_code == 404
    assert seeded.client.get(decision_url("evt-nope")).status_code == 404
    assert seeded.client.get(decision_url(DECISION_EVENT_ID, "_system")).status_code == 404
    assert seeded.client.get(decision_url(DECISION_EVENT_ID, "driftlane-labs")).status_code == 404


def test_the_trace_page_links_to_the_decision_page(seeded):
    trace = seeded.client.get(f"/accounts/{ACCOUNT_REF}")
    assert trace.status_code == 200
    assert f'href="{decision_url()}"' in trace.text
    assert _kinds(trace.text) == KIND_ORDER
    assert trace.text.count('<a href="/accounts/novasignal-ai/decisions/') == 1
