"""`PRODUCT.md` §4.5 on screen: the replay panel over 3A's `replay` and `compare`.

The panel renders the engine's `Comparison` and adds no semantics: both logic
identities, both scores, thresholds and outputs, the signed delta, one row per
contribution change with the classification word verbatim, and every input the
current logic expects that `H(d)` lacks, with `unavailable` and `absent` kept
distinct (AC-02, AC-06, INV-03).

The current artifact is always an explicitly selected hash. The default is
resolved *by logic version* to exactly one registered artifact and then used by
hash; zero or several artifacts carrying that version is a named state with no
automatic fallback, and nothing is ever chosen by recency (D-011).

Between `test_the_canonical_comparison_renders_...` and
`test_selecting_an_artifact_explicitly_...` every one of the five `change`
words -- `added`, `removed`, `unchanged`, `reweighted`, `changed` -- is
asserted as rendered cell text.
"""

import copy
from dataclasses import dataclass
from html.parser import HTMLParser

import pytest

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.replay.counterfactual import COUNTERFACTUAL_LABEL, ORIGINAL_LABEL
from tests.acceptance.test_decision_detail_page import (
    decision_url,
    element,
    has_element,
    page,
    row_for,
    rows,
)
from tests.conftest import (
    canonical_by_type,
    canonical_raw,
    derived_artifact_envelope,
    logic_artifact,
    register_derived_artifact,
    seed_all,
    system_raw,
    v5_1_hash,
)

DID_NOT_OCCUR = "This decision did not occur."
DUPLICATE_LABEL_ID = "logic-account-prioritization-v5.1-duplicate-label"

#: The `<select>` placeholder that holds the control whenever no registered
#: artifact is selected, so the browser never displays the first registered
#: option as an accidental default.
PLACEHOLDER_TEXT = "No registered artifact is chosen. Select one."

#: The recorded-sections lede, which claims nothing about computation.
LEDE = (
    "The recorded sections show stored decision data. The replay panel can compare "
    "that preserved context under selected logic without changing the record."
)

#: The one sentence that says a replay was computed. It renders on the success
#: path only, inside `#replay-comparison`.
COMPUTED_CLAIM = "computed on demand when this page was opened"

UNSUPPORTED_RULE_TEXT = "employee_count is quite large"


@dataclass(frozen=True)
class SelectOption:
    """One rendered `<option>`: what a reader of the control actually sees."""

    value: str
    disabled: bool
    selected: bool
    text: str


class _Options(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.options: list[SelectOption] = []
        self._attrs: dict | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "option":
            self._attrs, self._text = dict(attrs), []

    def handle_endtag(self, tag):
        if tag == "option" and self._attrs is not None:
            self.options.append(
                SelectOption(
                    value=self._attrs.get("value") or "",
                    disabled="disabled" in self._attrs,
                    selected="selected" in self._attrs,
                    text="".join(self._text).strip(),
                )
            )
            self._attrs = None

    def handle_data(self, data):
        if self._attrs is not None:
            self._text.append(data)


def artifact_select_options(html: str) -> list[SelectOption]:
    """Every `<option>` of `#current-artifact`, in rendered order."""
    parser = _Options()
    parser.feed(element(html, "current-artifact"))
    parser.close()
    return parser.options


def assert_placeholder_holds_the_control(html: str) -> None:
    """No registered artifact is shown as chosen; the placeholder is."""
    assert '<select name="current" id="current-artifact" required>' in html
    options = artifact_select_options(html)
    placeholder = options[0]
    assert placeholder.value == ""
    assert placeholder.disabled and placeholder.selected
    assert placeholder.text == PLACEHOLDER_TEXT
    assert "No registered artifact is chosen" in placeholder.text
    assert [option for option in options[1:] if option.selected] == []


def assert_no_placeholder(html: str) -> None:
    for option in artifact_select_options(html):
        assert option.value != "", option
        assert not option.disabled, option
    assert PLACEHOLDER_TEXT not in html


@pytest.fixture
def seeded(harness):
    for response in seed_all(harness):
        assert response.status_code == 201, response.json()
    return harness


def decision_artifact_hash() -> str:
    return canonical_by_type("decision.recorded")["payload"]["logic_artifact"]["artifact_hash"]


def removed_changed_envelope() -> dict:
    """`v5.1` with `headquarters_country` dropped and two rule texts changed.

    The same construction as `tests/acceptance/test_ac_02_counterfactual.py`,
    duplicated locally on purpose: this file owns its own fixtures.
    """
    factors = []
    for original in logic_artifact("v5.1")["factors"]:
        edited = dict(original)
        if edited["key"] == "headquarters_country":
            continue
        if edited["key"] == "employee_count":
            edited["rule"] = "employee_count between 100 and 300 inclusive"
        if edited["key"] == "open_platform_engineering_roles":
            edited["rule"] = "open_platform_engineering_roles at least 10"
        factors.append(edited)
    assert [f["weight"] for f in factors] == [25, 20, 4, 15, -21]
    return derived_artifact_envelope(
        "logic-account-prioritization-test-removed-changed",
        "test-removed-changed",
        factors,
        event_id="evt-system-logic-artifact-test-removed-changed",
    )


def unsupported_rule_envelope() -> dict:
    """`v5.1` with the first factor's rule written outside the rule grammar.

    The same construction as
    `tests/invariants/test_inv_09_visible_failure_states.py`, duplicated
    locally on purpose. Its own `logic_version` keeps the default resolution by
    `v5.1` unambiguous.
    """
    factors = copy.deepcopy(logic_artifact("v5.1")["factors"])
    assert factors[0]["key"] == "employee_count"
    factors[0]["rule"] = UNSUPPORTED_RULE_TEXT
    return derived_artifact_envelope(
        "test-unsupported-rule",
        "v5.1-test-unsupported-rule",
        factors,
        event_id="evt-system-logic-artifact-test-unsupported-rule",
    )


def missing_inputs_envelope() -> dict:
    """The six `v5.1` factors plus one `unavailable` and one `absent` key.

    The same construction as `tests/acceptance/test_ac_06_missing_inputs.py`,
    duplicated locally on purpose.
    """
    factors = [
        *logic_artifact("v5.1")["factors"],
        {"key": "website_intent", "rule": "website_intent equals 'HIGH'", "weight": 30},
        {"key": "partner_referral", "rule": "partner_referral equals 'YES'", "weight": 30},
    ]
    return derived_artifact_envelope(
        "logic-account-prioritization-test-missing-inputs",
        "test-missing-inputs",
        factors,
        event_id="evt-system-logic-artifact-test-missing-inputs",
    )


# --- The default selection ----------------------------------------------------


def test_the_default_selection_is_the_registered_v5_1_by_hash(seeded):
    selector = element(page(seeded), "current-logic-selector")

    assert v5_1_hash() in selector
    assert len(v5_1_hash()) == 64
    assert "logic version v5.1" in selector
    assert "the default was resolved by logic version v5.1" in selector


# --- The canonical comparison -------------------------------------------------


def test_the_canonical_comparison_renders_with_both_labels_and_the_whole_difference(seeded):
    html = page(seeded)
    comparison = element(html, "replay-comparison")

    assert ORIGINAL_LABEL in comparison and COUNTERFACTUAL_LABEL in comparison
    assert element(html, "original-score") == "86"
    assert element(html, "counterfactual-score") == "51"
    assert element(html, "score-delta") == "-35"
    assert element(html, "original-threshold") == "75"
    assert element(html, "counterfactual-threshold") == "75"
    assert element(html, "original-output") == "PRIORITIZE"
    assert element(html, "counterfactual-output") == "DO_NOT_PRIORITIZE"
    assert element(html, "output-changed") == "output changed: yes"
    assert decision_artifact_hash() in element(html, "original-artifact-hash")
    assert v5_1_hash() in element(html, "counterfactual-artifact-hash")
    assert element(html, "original-evaluator-version") == "evaluator-v1"
    assert element(html, "counterfactual-evaluator-version") == "evaluator-v1"
    assert element(html, "original-logic-version") == "v3.2"
    assert element(html, "counterfactual-logic-version") == "v5.1"

    contributions = rows(html, "contributions-table")
    assert len(contributions) == 6

    funding = row_for(html, "contributions-table", "funding_event")
    assert funding[1] == "reweighted"
    assert (funding[3], funding[5], funding[6]) == ("18", "4", "-14")

    pressure = row_for(html, "contributions-table", "verified_integration_pressure")
    assert pressure[1] == "added"
    assert (pressure[2], pressure[4]) == ("ignored", "consumed")
    assert (pressure[5], pressure[6]) == ("-21", "-21")

    unchanged = [row[0] for row in contributions if row[1] == "unchanged"]
    assert sorted(unchanged) == [
        "employee_count",
        "headquarters_country",
        "industry",
        "open_platform_engineering_roles",
    ]

    assert "None." in element(html, "missing-inputs-table")
    assert rows(html, "missing-inputs-table") == [["None."]]


def test_the_panel_states_that_the_counterfactual_did_not_occur(seeded):
    panel = element(page(seeded), "replay-panel")

    assert DID_NOT_OCCUR in panel
    assert panel.index(DID_NOT_OCCUR) < panel.index('id="replay-comparison"')
    assert "never stored" in panel
    assert "computed on demand" in panel


# --- Explicit selection -------------------------------------------------------


def test_selecting_an_artifact_explicitly_changes_only_the_counterfactual_side(seeded):
    artifact_hash = register_derived_artifact(seeded, removed_changed_envelope())
    html = page(seeded, query=f"?current={artifact_hash}")

    classifications = {row[0]: row[1] for row in rows(html, "contributions-table")}
    assert classifications == {
        "employee_count": "changed",
        "funding_event": "reweighted",
        "headquarters_country": "removed",
        "industry": "unchanged",
        "open_platform_engineering_roles": "changed",
        "verified_integration_pressure": "added",
    }

    assert element(html, "counterfactual-score") == "28"
    assert element(html, "score-delta") == "-58"
    assert element(html, "original-score") == "86"
    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"
    assert element(html, "decision-output") == "PRIORITIZE"


def test_missing_inputs_render_with_unavailable_and_absent_distinct(seeded):
    artifact_hash = register_derived_artifact(seeded, missing_inputs_envelope())
    html = page(seeded, query=f"?current={artifact_hash}")

    missing = {row[0]: row for row in rows(html, "missing-inputs-table")}
    assert set(missing) == {"website_intent", "partner_referral"}
    assert missing["website_intent"][1] == "unavailable"
    assert missing["partner_referral"][1] == "absent"
    for row in missing.values():
        assert row[2] == "No present-day value was substituted."

    assert element(html, "counterfactual-score") == "51"


def test_replaying_under_the_decisions_own_artifact_is_still_labeled_counterfactual(seeded):
    html = page(seeded, query=f"?current={decision_artifact_hash()}")

    assert element(html, "original-score") == "86"
    assert element(html, "counterfactual-score") == "86"
    assert element(html, "score-delta") == "0"
    assert element(html, "output-changed") == "output changed: no"
    assert COUNTERFACTUAL_LABEL in element(html, "replay-comparison")
    assert DID_NOT_OCCUR in element(html, "replay-panel")


# --- Named states where no default can be resolved ----------------------------


def test_a_missing_default_logic_version_is_named_as_such(harness):
    assert harness.post_raw(system_raw(0)).status_code == 201
    for index in range(4):
        assert harness.post_raw(canonical_raw(index)).status_code == 201

    html = page(harness)
    assert "Default replay logic v5.1 is not registered for this decision class." in element(
        html, "replay-no-selection"
    )
    assert not has_element(html, "replay-comparison")
    assert not has_element(html, "replay-integrity-failure")
    assert element(html, "decision-score-threshold") == "score 86 / threshold 75"

    v32_hash = canonical_hash(logic_artifact("v3.2"))
    selector = element(html, "current-logic-selector")
    assert f'value="{v32_hash}"' in selector
    assert "v3.2" in selector

    explicit = page(harness, query=f"?current={v32_hash}")
    assert element(explicit, "score-delta") == "0"
    assert element(explicit, "counterfactual-logic-version") == "v3.2"


def test_two_artifacts_carrying_the_same_logic_version_force_an_explicit_selection(seeded):
    envelope = derived_artifact_envelope(
        DUPLICATE_LABEL_ID,
        "v5.1",
        logic_artifact("v5.1")["factors"],
        event_id="evt-system-logic-artifact-v5.1-duplicate-label",
    )
    duplicate_hash = register_derived_artifact(seeded, envelope)
    assert duplicate_hash != v5_1_hash()

    html = page(seeded)
    no_selection = element(html, "replay-no-selection")
    assert "More than one registered artifact carries logic version v5.1" in no_selection
    assert "must be selected explicitly" in no_selection
    assert v5_1_hash() in no_selection and duplicate_hash in no_selection
    assert not has_element(html, "replay-comparison")
    assert not has_element(html, "replay-integrity-failure")

    explicit = page(seeded, query=f"?current={v5_1_hash()}")
    assert element(explicit, "counterfactual-score") == "51"
    assert element(explicit, "score-delta") == "-35"


# --- The panel and the outcomes section stay apart (INV-10) -------------------


def test_the_replay_panel_renders_no_outcome_data(seeded):
    html = page(seeded)
    panel = element(html, "replay-panel")
    outcomes = element(html, "outcomes")

    for outcome_marker in (
        'id="outcomes-table"',
        "reply:",
        "meeting:",
        "opportunity:",
        "Evaluation window",
        "Recorded reference",
        "Attribution",
    ):
        assert outcome_marker not in panel, outcome_marker

    for panel_marker in (
        'id="replay-comparison"',
        'id="contributions-table"',
        'id="missing-inputs-table"',
        "counterfactual",
        "score delta",
    ):
        assert panel_marker not in outcomes, panel_marker


def test_the_second_registration_does_not_disturb_the_canonical_fixture(seeded):
    """The duplicate-label artifact is a copy with its own identity."""
    envelope = derived_artifact_envelope(
        DUPLICATE_LABEL_ID,
        "v5.1",
        copy.deepcopy(logic_artifact("v5.1")["factors"]),
        event_id="evt-system-logic-artifact-v5.1-duplicate-label",
    )
    assert envelope["payload"]["artifact"]["artifact_id"] == DUPLICATE_LABEL_ID
    assert logic_artifact("v5.1")["artifact_id"] == "logic-account-prioritization-v5.1"
    assert canonical_hash(envelope["payload"]["artifact"]) != v5_1_hash()
    assert decision_url() == "/accounts/novasignal-ai/decisions/evt-novasignal-04-decision-recorded"


# --- The control never shows an accidental default ----------------------------
#
# The `<select>` must never display a registered artifact the page did not
# select. In the three states where nothing registered is selected -- an
# unregistered hash, a missing default, an ambiguous default -- a disabled,
# empty-valued placeholder holds the control, and `required` refuses a
# submission that leaves it. A registered artifact that fails evaluation is a
# real selection and stays selected.


def test_an_unregistered_selection_shows_a_disabled_placeholder(seeded):
    unregistered = "0" * 64
    html = page(seeded, query=f"?current={unregistered}")

    assert_placeholder_holds_the_control(html)

    selector = element(html, "current-logic-selector")
    assert "Selected:" in selector
    assert "an artifact that is not registered for this decision class" in selector
    assert unregistered in selector
    assert "No comparison used it." in selector


def test_the_missing_default_state_shows_the_placeholder(harness):
    assert harness.post_raw(system_raw(0)).status_code == 201
    for index in range(4):
        assert harness.post_raw(canonical_raw(index)).status_code == 201

    html = page(harness)
    assert_placeholder_holds_the_control(html)

    v32_hash = canonical_hash(logic_artifact("v3.2"))
    registered = {option.value: option for option in artifact_select_options(html)}
    assert v32_hash in registered
    assert not registered[v32_hash].selected

    assert "Default replay logic v5.1 is not registered for this decision class." in element(
        html, "replay-no-selection"
    )


def test_the_ambiguous_default_state_shows_the_placeholder(seeded):
    envelope = derived_artifact_envelope(
        DUPLICATE_LABEL_ID,
        "v5.1",
        logic_artifact("v5.1")["factors"],
        event_id="evt-system-logic-artifact-v5.1-duplicate-label",
    )
    duplicate_hash = register_derived_artifact(seeded, envelope)

    html = page(seeded)
    assert_placeholder_holds_the_control(html)

    registered = {option.value: option for option in artifact_select_options(html)}
    assert not registered[v5_1_hash()].selected
    assert not registered[duplicate_hash].selected

    no_selection = element(html, "replay-no-selection")
    assert "More than one registered artifact carries logic version v5.1" in no_selection
    assert v5_1_hash() in no_selection and duplicate_hash in no_selection


def test_a_registered_artifact_that_fails_stays_selected(seeded):
    artifact_hash = register_derived_artifact(seeded, unsupported_rule_envelope())

    html = page(seeded, query=f"?current={artifact_hash}")

    registered = {option.value: option for option in artifact_select_options(html)}
    assert registered[artifact_hash].selected
    assert_no_placeholder(html)
    assert "UnsupportedRule" in element(html, "replay-integrity-failure")


def test_the_canonical_selection_is_unchanged(seeded):
    html = page(seeded)

    registered = {option.value: option for option in artifact_select_options(html)}
    assert registered[v5_1_hash()].selected
    assert [value for value, option in registered.items() if option.selected] == [v5_1_hash()]
    assert_no_placeholder(html)

    assert element(html, "original-score") == "86"
    assert element(html, "counterfactual-score") == "51"
    assert element(html, "score-delta") == "-35"


# --- The introduction claims nothing about computation ------------------------


def test_the_recorded_sections_lede_makes_no_computation_claim(seeded):
    success = page(seeded)
    assert LEDE in element(success, "decision-summary")
    assert COMPUTED_CLAIM in element(success, "replay-comparison")
    assert success.count(COMPUTED_CLAIM) == 1

    failing_hash = register_derived_artifact(seeded, unsupported_rule_envelope())
    failure = page(seeded, query=f"?current={failing_hash}")
    assert LEDE in element(failure, "decision-summary")
    assert has_element(failure, "replay-integrity-failure")
    assert COMPUTED_CLAIM not in failure

    register_derived_artifact(
        seeded,
        derived_artifact_envelope(
            DUPLICATE_LABEL_ID,
            "v5.1",
            logic_artifact("v5.1")["factors"],
            event_id="evt-system-logic-artifact-v5.1-duplicate-label",
        ),
    )
    no_selection = page(seeded)
    assert LEDE in element(no_selection, "decision-summary")
    assert has_element(no_selection, "replay-no-selection")
    assert COMPUTED_CLAIM not in no_selection
