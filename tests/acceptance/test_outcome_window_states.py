"""INV-08, INV-09: eight outcome and attribution conditions stay distinguishable.

Open window, missing outcome data, negative evaluated outcome, unknown
observation, heuristic `inferred`, `unresolved`, unevaluated attribution, and
a named evaluation or integrity failure are each built through the collector
(the integrity failure by corrupting the ledger around it) and read back from
the rendered decision page as text only. The last three are never conflated.
An open window with a false observation is not a negative result, and
advancing the clock creates no evaluation.
"""

import pytest
from sqlalchemy import func, select

from flight_recorder.attribution import policy
from flight_recorder.ledger.schema import events
from tests.acceptance.test_decision_detail_page import element, has_element, page, rows
from tests.conftest import (
    ACCOUNT_REF,
    OUTCOME_EVENT_ID,
    FixedClock,
    Harness,
    attribute_at,
    attribute_ledger,
    attribution_rows,
    canonical_by_type,
    insert_ambiguous_attribution,
    max_sequence,
    outcome_row,
    outcome_v2_envelope,
    post_created,
    seed_all,
    seed_and_attribute,
    seed_through_decision,
)

OPENED = canonical_by_type("action.recorded")["occurred_at"]
CLOSES = canonical_by_type("outcome.evaluated")["occurred_at"]

NOT_YET_EVALUATED = "Attribution not yet evaluated."
COULD_NOT_BE_ESTABLISHED = "Attribution could not be established"
NO_OUTCOME = ("no outcome recorded", None, None, None)


def v2(event_id: str, *, state: str, observed_at: str, **payload) -> dict:
    return outcome_v2_envelope(
        event_id,
        observed_at=observed_at,
        window_opened_at=OPENED,
        window_closes_at=CLOSES,
        evaluation_state=state,
        **payload,
    )


ALL_FALSE = {"reply": False, "meeting": False, "opportunity": False}
REPLIED = {"reply": True, "meeting": False, "opportunity": False}


def describe_row(row: list[str]) -> tuple:
    """`(outcome recorded, window, observations, attribution)` from a row's text.

    window: `open` / `closed` / `length only`; observations: `unknown` when any
    is unknown, `all no`, or `other`; attribution: exactly one of
    `unevaluated` / `failure` / `unresolved` / `inferred heuristic` / `direct`.
    """
    window_cell, observations, attribution = row[0], row[1], row[5]
    if window_cell.startswith("window open"):
        window = "open"
    elif window_cell.startswith("window closed"):
        window = "closed"
    else:
        assert window_cell.split(" superseded")[0].endswith("days"), window_cell
        window = "length only"
    if "unknown" in observations:
        observed = "unknown"
    elif observations == "reply: no meeting: no opportunity: no":
        observed = "all no"
    else:
        observed = "other"
    markers = {
        "unevaluated": attribution.startswith(NOT_YET_EVALUATED),
        "failure": attribution.startswith(COULD_NOT_BE_ESTABLISHED),
        "unresolved": attribution.startswith(policy.STATUS_UNRESOLVED),
        "inferred heuristic": attribution.startswith(f"{policy.STATUS_INFERRED} heuristic"),
        "direct": attribution.startswith(policy.STATUS_DIRECT),
    }
    (label,) = [name for name, present in markers.items() if present]
    return ("outcome recorded", window, observed, label)


def reading(html: str, pick=lambda row: True) -> tuple:
    """The condition the decision page shows for the one row `pick` selects."""
    if not has_element(html, "outcomes-table"):
        assert "No outcome recorded for this account." in element(html, "outcomes")
        return NO_OUTCOME
    (row,) = [row for row in rows(html, "outcomes-table") if pick(row)]
    return describe_row(row)


def is_v2(row: list[str]) -> bool:
    return row[0].startswith("window ")


def is_v1(row: list[str]) -> bool:
    return not is_v2(row)


# --- The eight conditions -------------------------------------------------------------


def open_window(harness: Harness) -> tuple:
    seed_through_decision(harness)
    post_created(harness, v2("evt-test-o-open", state="open", observed_at=OPENED, **ALL_FALSE))
    return reading(page(harness))


def missing_outcome_data(harness: Harness) -> tuple:
    seed_through_decision(harness)
    return reading(page(harness))


def negative_evaluated(harness: Harness) -> tuple:
    seed_through_decision(harness)
    post_created(harness, v2("evt-test-o-closed", state="closed", observed_at=CLOSES, **ALL_FALSE))
    return reading(page(harness))


def unknown_observation(harness: Harness) -> tuple:
    seed_through_decision(harness)
    post_created(
        harness,
        v2("evt-test-o-unknown", state="closed", observed_at=CLOSES, meeting=False),
    )
    return reading(page(harness))


def heuristic_inferred(harness: Harness) -> tuple:
    for response in seed_all(harness):
        assert response.status_code == 201
    post_created(
        harness,
        v2("evt-test-o-inferred", state="open", observed_at="2026-06-01T00:00:00Z", **REPLIED),
    )
    attribute_ledger(harness)
    return reading(page(harness), is_v2)


def unresolved(harness: Harness) -> tuple:
    seed_through_decision(harness)
    post_created(
        harness,
        v2("evt-test-o-unresolved", state="open", observed_at="2026-06-01T00:00:00Z", **REPLIED),
    )
    attribute_ledger(harness)
    return reading(page(harness))


def unevaluated_attribution(harness: Harness) -> tuple:
    for response in seed_all(harness):
        assert response.status_code == 201
    return reading(page(harness))


def integrity_failure(harness: Harness) -> tuple:
    seed_and_attribute(harness)
    post_created(
        harness,
        v2("evt-test-o-other", state="open", observed_at="2026-06-01T00:00:00Z", **REPLIED),
    )
    attribute_ledger(harness)
    other = attribution_rows(harness)[1]
    insert_ambiguous_attribution(harness, other.attribution_event_id)
    return reading(page(harness), is_v1)


CONDITIONS = {
    "open window": (open_window, ("outcome recorded", "open", "all no", "unevaluated")),
    "missing outcome data": (missing_outcome_data, NO_OUTCOME),
    "negative evaluated": (
        negative_evaluated,
        ("outcome recorded", "closed", "all no", "unevaluated"),
    ),
    "unknown observation": (
        unknown_observation,
        ("outcome recorded", "closed", "unknown", "unevaluated"),
    ),
    "heuristic inferred": (
        heuristic_inferred,
        ("outcome recorded", "open", "other", "inferred heuristic"),
    ),
    "unresolved": (unresolved, ("outcome recorded", "open", "other", "unresolved")),
    "unevaluated attribution": (
        unevaluated_attribution,
        ("outcome recorded", "length only", "all no", "unevaluated"),
    ),
    "integrity failure": (
        integrity_failure,
        ("outcome recorded", "length only", "all no", "failure"),
    ),
}


@pytest.mark.parametrize("name", list(CONDITIONS))
def test_each_condition_renders_as_itself(tmp_path, name):
    build, expected = CONDITIONS[name]
    assert build(Harness(tmp_path)) == expected


def test_the_eight_conditions_are_eight_distinct_readings():
    readings = [expected for _, expected in CONDITIONS.values()]
    assert len(set(readings)) == 8


def test_unresolved_unevaluated_and_failure_are_never_conflated(tmp_path_factory):
    regions = {}
    for name in ("unresolved", "unevaluated attribution", "integrity failure"):
        harness = Harness(tmp_path_factory.mktemp(name.replace(" ", "-")))
        CONDITIONS[name][0](harness)
        regions[name] = element(page(harness), "outcomes")

    assert '<span class="attribution-status">unresolved</span>' in regions["unresolved"]
    assert NOT_YET_EVALUATED not in regions["unresolved"]
    assert COULD_NOT_BE_ESTABLISHED not in regions["unresolved"]

    assert NOT_YET_EVALUATED in regions["unevaluated attribution"]
    assert "attribution-status" not in regions["unevaluated attribution"]
    assert COULD_NOT_BE_ESTABLISHED not in regions["unevaluated attribution"]

    assert COULD_NOT_BE_ESTABLISHED in regions["integrity failure"]
    assert "ambiguous_effective_selection" in regions["integrity failure"]
    # The failing row claims neither a result nor the absence of one.
    (row,) = [r for r in rows(regions["integrity failure"], "outcomes-table") if is_v1(r)]
    assert NOT_YET_EVALUATED not in row[5]
    assert policy.STATUS_UNRESOLVED not in row[5]


def test_an_evaluation_failure_is_raised_by_name_and_never_returned_as_a_result(harness):
    for response in seed_all(harness):
        assert response.status_code == 201
    with pytest.raises(policy.AttributionError) as failure:
        attribute_at(harness, OUTCOME_EVENT_ID, max_sequence(harness) - 1)
    assert isinstance(failure.value, policy.CutoffExcludesOutcome)
    assert failure.value.reason == "ingest_cutoff_excludes_the_outcome"
    assert attribution_rows(harness) == []


# --- An open window is not a negative result, and time evaluates nothing --------------


def test_an_open_window_with_false_observations_reads_as_nothing_recorded_yet(harness):
    open_window(harness)
    html = page(harness)
    (row,) = rows(html, "outcomes-table")
    assert row[0].startswith("window open")
    region = " ".join(element(html, "outcomes").replace("<em>", "").replace("</em>", "").split())
    assert (
        "In an open window, no means nothing was recorded as of the stated time, "
        "not a negative result." in region
    )
    trace = harness.client.get(f"/accounts/{ACCOUNT_REF}").text
    assert "no reply recorded yet" in trace
    assert "no meeting recorded yet" in trace


def test_advancing_the_clock_creates_no_evaluation(harness):
    """The attribution command runs with a clock years past the window's close.
    It records an attribution and nothing else: the observation stays open, no
    outcome event is appended, and the page still reads the recorded state."""
    open_window(harness)
    before = tuple(outcome_row(harness, "evt-test-o-open"))
    assert reading(page(harness)) == CONDITIONS["open window"][1]

    clock = FixedClock()
    clock.advance(days=3 * 365)
    run = attribute_ledger(harness, clock=clock)
    assert [s.envelope["event_type"] for s in run.submissions] == ["outcome.attributed"]

    assert tuple(outcome_row(harness, "evt-test-o-open")) == before
    with harness.engine.connect() as conn:
        outcome_events = conn.execute(
            select(func.count())
            .select_from(events)
            .where(events.c.event_type == "outcome.evaluated")
        ).scalar_one()
    assert outcome_events == 1
    # Still open, still all no; only the attribution cell now holds a result.
    assert reading(page(harness)) == ("outcome recorded", "open", "all no", "unresolved")
