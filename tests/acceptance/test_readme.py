"""The README's claims, checked against the fixtures, the rendered pages and the collector.

These tests keep the documentation true. They read repository files and render
pages in process; none needs a running server, and none asserts anything about
the images beyond file integrity.

**Scoping.** Every extraction is taken from the region that makes the claim: a
numeric claim comes from its own labelled table row inside its own section, and
a demo destination comes only from the demo-path table. The README legitimately
inlines the canonical decision envelope, which contains `86`, `75` and `v3.2`,
so a whole-document membership check would accept prose that gets those numbers
wrong. Fenced blocks are excluded from prose checks for the same reason.

Constructions shared with other test modules (`_Region`, `element`, `visible`)
are duplicated here, not imported, following the convention of the other page
test modules.
"""

import contextlib
import io
import json
import re
from html.parser import HTMLParser

import pytest

from flight_recorder.fixtures import REPO_ROOT
from tests.conftest import (
    Harness,
    canonical_by_type,
    canonical_raw,
    register_artifacts,
    register_current_logic,
    seed_dataset,
)

README_PATH = REPO_ROOT / "README.md"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCREENSHOT_PATHS = (
    "docs/screenshots/trace.png",
    "docs/screenshots/decision-replay.png",
    "docs/screenshots/insights.png",
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024

ACCOUNT_REF = "novasignal-ai"
DECISION_EVENT_ID = "evt-novasignal-04-decision-recorded"
DECISION_URL = f"/accounts/{ACCOUNT_REF}/decisions/{DECISION_EVENT_ID}"

#: The four destinations the demo path must reach, whatever else it names.
REQUIRED_DESTINATIONS = frozenset({"/", f"/accounts/{ACCOUNT_REF}", DECISION_URL, "/insights"})


# --- Reading the README -----------------------------------------------------


def readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```[^\n]*$", re.S | re.M)


def fenced_blocks(text_: str) -> list[tuple[str, str]]:
    """Every fenced block as `(language, body)`, in document order."""
    return [(m.group(1), m.group(2)) for m in FENCE.finditer(text_)]


def without_fences(text_: str) -> str:
    return FENCE.sub("", text_)


def prose(text_: str) -> str:
    """Everything a reader reads as sentences: fenced blocks, inline code and link
    and image targets removed, link text and image alt text kept.

    Language and phase checks run over this, never over the whole file: the
    inline envelope, the setup commands and the diagram source are data and
    command text, and a prose rule must never pressure them.
    """
    text_ = without_fences(text_)
    text_ = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text_)  # image: keep the alt
    text_ = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text_)  # link: keep the text
    text_ = re.sub(r"<https?://[^>]*>", "", text_)  # autolink
    text_ = re.sub(r"`[^`]*`", "", text_)  # inline code
    # Collapsed: the README hard-wraps, so a claim or a caveat may straddle a
    # line break and must still read as one phrase.
    return " ".join(text_.split())


def heading_level(line: str) -> int | None:
    match = re.match(r"^(#{1,6})\s", line)
    return len(match.group(1)) if match else None


def section(text_: str, heading: str) -> str:
    """The named heading's own text, up to the very next heading of any level.

    Its own text and no subsection's: a claim belongs to the region that makes
    it, and a subsection carries its own table and its own claims.
    """
    lines = text_.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    assert start is not None, f"no heading {heading!r} in the README"
    assert heading_level(heading) is not None, f"{heading!r} is not a heading"
    for index in range(start + 1, len(lines)):
        if heading_level(lines[index]) is not None:
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def strip_cell(cell: str) -> str:
    """A table cell as its plain value: backticks dropped, whitespace collapsed."""
    return " ".join(cell.replace("`", "").split())


def table_rows(region: str) -> tuple[list[str], list[list[str]]]:
    """The one pipe table in `region`, as `(header cells, body rows)`.

    A region holding no table, or more than one, is a documentation defect: the
    scoping rule depends on each claim living in exactly one place.
    """
    rows = [
        [strip_cell(c) for c in line.strip().strip("|").split("|")]
        for line in without_fences(region).splitlines()
        if line.strip().startswith("|")
    ]
    assert rows, "the region holds no table"
    separators = [i for i, row in enumerate(rows) if all(set(c) <= set("-: ") for c in row)]
    assert len(separators) == 1, f"expected one table in the region, found {len(separators)}"
    divider = separators[0]
    assert divider == 1, "the region's table block must begin with its own header row"
    return rows[0], rows[divider + 1 :]


def labelled(region: str, label: str) -> str:
    """The value cell of the row whose first cell is `label`, in that region's table."""
    _, rows = table_rows(region)
    matches = [row for row in rows if row[0] == label]
    assert len(matches) == 1, f"expected one row labelled {label!r}, found {len(matches)}"
    assert len(matches[0]) == 2, f"row {label!r} is not a two-column row"
    return matches[0][1]


def carries(cell: str, value: object) -> bool:
    """Whether the cell states `value` as a whole token rather than inside a longer
    number or word: `14` is carried by `#14`, and not by `140` or `v1.4`."""
    return re.search(rf"(?<![\w.]){re.escape(str(value))}(?![\w.])", cell) is not None


# --- The demo-path structure ------------------------------------------------


class DemoStep:
    def __init__(self, number: int, destination: str, cue: str):
        self.number = number
        self.destination = destination
        self.path, _, self.fragment = destination.partition("#")
        self.cue = cue

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"step {self.number} -> {self.destination}"


def demo_steps() -> list[DemoStep]:
    """The eight steps, read only from the demo-path table.

    Nothing else in the README can contribute a destination: a path inside the
    inline envelope, a source reference or a fixture path is not navigation.
    """
    header, rows = table_rows(section(readme(), "## The demo path"))
    assert header == ["Step", "What you say", "Destination", "Expected visible cue"], header
    return [DemoStep(int(row[0]), row[2], row[3]) for row in rows]


# --- Reading the rendered pages ---------------------------------------------


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


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []

    def handle_data(self, data):
        self.pieces.append(data)


def visible(fragment: str) -> str:
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    return " ".join("".join(parser.pieces).split())


def text(html: str, element_id: str) -> str:
    return visible(element(html, element_id))


def page_text(html: str) -> str:
    """The whole page as the text a viewer reads, whitespace-normalized."""
    return visible(html)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Harness:
    """One fully seeded dataset ledger, as the README's setup builds it.

    The README's order exactly: the dataset first, then the `v5.2` overlay the
    replay panel defaults to. Registering the overlay before the dataset is not
    a supported setup order and would make the seed refuse.
    """
    harness = Harness(tmp_path_factory.mktemp("readme-seed"))
    _, report = seed_dataset(harness)
    assert report.fresh
    register_current_logic(harness)
    return harness


@pytest.fixture(scope="module")
def decision_page(seeded: Harness) -> str:
    response = seeded.client.get(DECISION_URL)
    assert response.status_code == 200, response.status_code
    return response.text


@pytest.fixture(scope="module")
def insights_page(seeded: Harness) -> str:
    response = seeded.client.get("/insights")
    assert response.status_code == 200, response.status_code
    return response.text


# --- 1. Demo destinations resolve, per step ---------------------------------


def test_the_demo_path_is_eight_numbered_steps_covering_the_required_destinations():
    steps = demo_steps()
    assert [step.number for step in steps] == list(range(1, 9))
    assert steps[0].destination == "/", "the path must begin at the account list"
    assert REQUIRED_DESTINATIONS <= {step.path for step in steps}


def test_each_demo_step_destination_answers_200_with_its_own_cue(seeded: Harness):
    for step in demo_steps():
        response = seeded.client.get(step.path)
        assert response.status_code == 200, f"{step}: {response.status_code}"
        assert step.cue in page_text(response.text), (
            f"{step}: cue {step.cue!r} is not visible on {step.path}"
        )


def test_each_demo_step_fragment_names_an_element_that_exists(seeded: Harness):
    fragments = [step for step in demo_steps() if step.fragment]
    assert fragments, "at least one step should point at a fragment"
    for step in fragments:
        response = seeded.client.get(step.path)
        assert has_element(response.text, step.fragment), (
            f"{step}: no element with id {step.fragment!r} on {step.path}"
        )


# --- 2. Canonical facts match the fixtures ----------------------------------


def canonical_facts() -> dict[str, object]:
    """The canonical facts the README states, each read from its own fixture."""
    decision = canonical_by_type("decision.recorded")["payload"]
    action = canonical_by_type("action.recorded")["payload"]
    outcome = canonical_by_type("outcome.evaluated")["payload"]
    employees = next(
        item["value"]
        for item in decision["consumed_inputs"]
        if item["input_key"] == "employee_count"
    )
    return {
        "Decision boundary": decision["decision_boundary"],
        "Employees, preserved at the boundary": employees,
        "Workflow version": decision["workflow_version"],
        "Threshold": decision["result"]["threshold"],
        "Outbound play": action["play_id"],
        "Recorded synthetic cost": action["cost"],
        "Outcome evaluation window": outcome["window_days"],
    }


def test_every_canonical_fact_row_carries_its_fixture_value():
    region = section(readme(), "### Canonical decision facts")
    for label, value in canonical_facts().items():
        cell = labelled(region, label)
        assert carries(cell, value), f"row {label!r} reads {cell!r}, not the fixture {value!r}"


def test_the_cost_row_names_the_fixture_currency():
    region = section(readme(), "### Canonical decision facts")
    currency = canonical_by_type("action.recorded")["payload"]["currency"]
    cell = labelled(region, "Recorded synthetic cost")
    assert carries(cell, currency), f"the cost row reads {cell!r}, without {currency!r}"


def test_the_canonical_fact_table_states_exactly_those_facts():
    region = section(readme(), "### Canonical decision facts")
    _, rows = table_rows(region)
    assert {row[0] for row in rows} == set(canonical_facts())


# --- 3. Replay claims match the rendered page, including direction ----------

#: Every replay claim, bound to the element that renders it. `51` is a computed
#: replay result, not a stored property: `fixtures/canonical/logic-v5.1.json`
#: holds factors and a threshold and no score, so no fixture field supplies it.
REPLAY_CLAIMS = {
    "Original score": "original-score",
    "Counterfactual score": "counterfactual-score",
    "Score delta": "score-delta",
    "Original threshold": "original-threshold",
    "Counterfactual threshold": "counterfactual-threshold",
    "Original output": "original-output",
    "Counterfactual output": "counterfactual-output",
    "Output changed": "output-changed",
}


def test_every_replay_claim_equals_the_element_that_renders_it(decision_page: str):
    region = section(readme(), "### The replay comparison, as the page renders it")
    for label, element_id in REPLAY_CLAIMS.items():
        cell = labelled(region, label)
        rendered = text(decision_page, element_id)
        assert cell == rendered, f"row {label!r} reads {cell!r}; #{element_id} reads {rendered!r}"


def test_the_replay_table_states_exactly_those_claims():
    region = section(readme(), "### The replay comparison, as the page renders it")
    _, rows = table_rows(region)
    assert {row[0] for row in rows} == set(REPLAY_CLAIMS)


def test_the_replay_claims_are_read_from_inside_the_replay_comparison(decision_page: str):
    """The claims describe one region of the page, so they are read from it."""
    comparison = element(decision_page, "replay-comparison")
    for element_id in REPLAY_CLAIMS.values():
        assert has_element(comparison, element_id), f"#{element_id} is outside #replay-comparison"


# --- 4. Insights claims match the rendered page -----------------------------

INSIGHTS_CLAIMS = {
    "Observed 90-day opportunity rate, all decisions": "overall-rate-value",
    "recently_funded difference": "signal-recently_funded-difference",
    "verified_integration_pressure_high difference": (
        "signal-verified_integration_pressure_high-difference"
    ),
    "Workflow compared": "workflow-comparison-version",
    "Workflow compared against": "workflow-comparison-against",
    "Workflow difference": "workflow-comparison-difference",
}


def test_every_insights_claim_equals_the_element_that_renders_it(insights_page: str):
    region = section(readme(), "### What Insights shows at that cutoff")
    for label, element_id in INSIGHTS_CLAIMS.items():
        cell = labelled(region, label)
        rendered = text(insights_page, element_id)
        assert cell == rendered, f"row {label!r} reads {cell!r}; #{element_id} reads {rendered!r}"


def test_the_insights_table_states_exactly_those_claims():
    region = section(readme(), "### What Insights shows at that cutoff")
    _, rows = table_rows(region)
    assert {row[0] for row in rows} == set(INSIGHTS_CLAIMS)


def test_the_insights_section_keeps_the_language_descriptive():
    region = section(readme(), "### What Insights shows at that cutoff")
    assert "not causal" in region
    assert "observed" in region.lower()


# --- 5. The inline collector envelope is the canonical decision fixture -----


def collector_section() -> str:
    return section(readme(), "## Submitting a decision to the collector")


def inline_envelope() -> dict:
    """The one JSON block in the collector section, as parsed data."""
    region = collector_section()
    assert "fixtures/canonical/04-decision-recorded.json" in region, (
        "the collector section must name the fixture the envelope comes from"
    )
    blocks = [body for language, body in fenced_blocks(region) if language == "json"]
    assert len(blocks) == 1, f"expected one JSON block in the collector section, got {len(blocks)}"
    return json.loads(blocks[0])


def test_the_inline_envelope_is_the_canonical_decision_fixture():
    inline = inline_envelope()
    fixture = canonical_by_type("decision.recorded")
    assert inline["event_type"] == fixture["event_type"] == "decision.recorded"
    assert inline["event_id"] == fixture["event_id"]
    assert inline["schema_version"] == fixture["schema_version"]
    assert inline["payload"] == fixture["payload"]
    assert inline == fixture


def test_the_inline_envelope_result_and_logic_reference_are_the_fixture_values():
    inline = inline_envelope()["payload"]
    fixture = canonical_by_type("decision.recorded")["payload"]
    assert inline["result"] == fixture["result"]
    assert inline["logic_artifact"] == fixture["logic_artifact"]


def test_the_collector_section_names_the_live_schema_surfaces():
    region = collector_section()
    for surface in ("/docs", "/openapi.json", "src/flight_recorder/collector/schema.py"):
        assert surface in region, f"the collector section does not name {surface}"
    assert "POST /api/v1/decision-events" in region
    assert "application/json" in region


def test_the_collector_section_states_the_contract_it_documents():
    region = collector_section()
    for required in ("schema_version", "event_id", "occurred_at", "recorded_at", "payload"):
        assert required in region, f"the collector section does not name {required!r}"
    for status in ("200", "409", "422"):
        assert status in region, f"the collector section does not state the {status} answer"
    assert "canonical" in region.lower()


def test_the_collector_section_states_the_prerequisites_and_the_duplicate_answer():
    region = collector_section()
    for required in (
        "account.discovered",
        "evidence.recorded",
        "logic_artifact.registered",
        "seed-dataset",
        "idempotent duplicate",
        "not a creation",
    ):
        assert required in region, f"the collector section does not state {required!r}"


def test_the_clay_example_is_linked_and_marked_not_runnable():
    region = collector_section()
    assert "fixtures/examples/clay-http-step.json" in region
    assert "not runnable unchanged" in region
    assert "{{row_id}}" in region, "the placeholders must stay visible"
    assert "zero" in region, "the placeholder artifact hash must be called out"


# --- 6. The documented body works through the collector ---------------------


def test_the_documented_body_is_created_and_then_answers_an_identical_retry(harness: Harness):
    """The README's own envelope, through the collector, with its prerequisites met."""
    register_artifacts(harness)
    for index in range(3):  # account.discovered, then both evidence.recorded events
        assert harness.post_raw(canonical_raw(index)).status_code == 201

    body = json.dumps(inline_envelope()).encode("utf-8")
    created = harness.post_raw(body)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "created"
    after_creation = harness.snapshot()

    retry = harness.post_raw(body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "duplicate"
    assert harness.snapshot() == after_creation, "the retry left a second record"


def test_the_documented_body_answers_a_duplicate_against_the_seeded_demo(seeded: Harness):
    """What the README tells a reader to expect against the shipped seed."""
    body = json.dumps(inline_envelope()).encode("utf-8")
    before = seeded.snapshot()
    response = seeded.post_raw(body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "duplicate"
    assert seeded.snapshot() == before


# --- 7. The synthetic boundary is stated ------------------------------------


def test_the_synthetic_boundary_names_the_simulated_sources_and_the_fictional_names():
    region = section(readme(), "## Synthetic data")
    for source in ("Apollo", "Clay", "CRM", "outcome"):
        assert source in region, f"the disclosure does not name {source}"
    assert "simulated" in region
    for name in ("RelayBridge", "NovaSignal AI"):
        assert name in region, f"the disclosure does not name {name}"
    assert "fictional" in region
    assert "live customer system" in region


# --- 8. The Merge sentence is present and bounded ---------------------------

SENTENCE = re.compile(r"[^.!?]+[.!?]", re.S)


def test_merge_appears_in_exactly_one_bounded_sentence():
    sentences = [" ".join(sentence.split()) for sentence in SENTENCE.findall(prose(readme()))]
    mentions = [s for s in sentences if re.search(r"\bMerge\b", s)]
    assert len(mentions) == 1, f"Merge appears in {len(mentions)} sentences: {mentions}"
    assert "public evidence that the operating archetype" in mentions[0]
    assert "neither a customer relationship nor an unmet need" in mentions[0]


# --- 9. Setup commands are real commands ------------------------------------


def cli_accepts(subcommand: str) -> bool:
    """Whether `cli.main`'s own parser accepts this subcommand.

    Asked of the parser by parsing `<subcommand> --help`, which argparse answers
    with exit status 0 for a registered subcommand and 2 for anything else,
    before any handler runs. A renamed command therefore fails this test.
    """
    from flight_recorder import cli

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            cli.main([subcommand, "--help"])
        except SystemExit as exit_:
            return exit_.code == 0
    return False


def setup_section() -> str:
    return section(readme(), "## Setup")


def setup_subcommands() -> list[str]:
    """The `flight-recorder` subcommands the setup block tells a reader to run."""
    lines = [
        line.strip()
        for _, block in fenced_blocks(setup_section())
        for line in block.splitlines()
        if line.strip()
    ]
    assert lines, "the setup section holds no commands"
    found = [
        match.group(1)
        for line in lines
        for match in [re.search(r"uv run flight-recorder ([\w-]+)", line)]
        if match
    ]
    assert found, "the setup section runs no flight-recorder subcommand"
    return found


def test_the_parser_probe_rejects_an_unknown_subcommand():
    assert cli_accepts("no-such-subcommand") is False


def test_every_setup_subcommand_exists_in_the_cli_parser():
    for subcommand in setup_subcommands():
        assert cli_accepts(subcommand), f"cli.py's parser does not accept {subcommand!r}"


def test_the_setup_block_runs_uv_sync_and_the_three_documented_subcommands():
    lines = [
        line.strip() for _, block in fenced_blocks(setup_section()) for line in block.splitlines()
    ]
    assert "uv sync" in lines, "the setup block does not run `uv sync`"
    assert set(setup_subcommands()) >= {"reset", "seed-dataset", "serve"}


def test_the_setup_section_warns_about_the_database_path():
    region = setup_section()
    assert "FLIGHT_RECORDER_DB" in region
    assert "flight_recorder.db" in region
    assert re.search(r"reset\b[^.]*delete", region), (
        "the setup section does not say that `reset` deletes that file"
    )


def test_the_setup_section_names_the_pinned_python_version():
    region = setup_section()
    pinned = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned in region, f"the setup section does not name Python {pinned}"
    assert ".python-version" in region
    assert "uv" in region


def test_the_setup_section_distinguishes_seed_from_seed_dataset():
    region = setup_section()
    assert "the demo seed" in region
    assert "seed-dataset" in region


# --- 10. No unsupported positive claim --------------------------------------

#: Unsupported positives. Whole words only: `rate` sits inside `generated` and
#: `count` inside `account`, so a substring search would be nonsense.
BANNED = (
    r"causal lift",
    r"lift",
    r"proves",
    r"guaranteed",
    r"guarantee",
    r"production-ready",
    r"enterprise-grade",
    r"real-time",
    r"integrates with",
    r"autonomous",
)

#: Bounded negatives a truthful README needs. A blanket ban on the words above
#: would reject exactly the caveats the contract requires, so these exact
#: phrasings are removed before the check runs. This is a small explicit
#: allow-list, deliberately not a natural-language classifier.
PERMITTED_CAVEATS = (
    "descriptive, not causal",
    "not causal",
    "does not execute autonomous outreach",
    "no causal claims",
    "causal reading",
)

BANNED_PATTERN = tuple(rf"(?<![\w-]){pattern}(?![\w-])" for pattern in BANNED)


def banned_in(body: str) -> list[str]:
    return sorted(
        {
            match.group(0).lower()
            for pattern in BANNED_PATTERN
            for match in re.finditer(pattern, body, re.I)
        }
    )


def test_readme_prose_makes_no_unsupported_positive_claim():
    body = prose(readme())
    for caveat in PERMITTED_CAVEATS:
        body = re.sub(re.escape(caveat), " ", body, flags=re.I)
    assert not banned_in(body), f"unsupported positive claims in README prose: {banned_in(body)}"


def test_the_permitted_caveats_are_actually_used():
    """The allow-list exists for claims the README makes, not as a blanket escape."""
    body = prose(readme()).lower()
    assert [caveat for caveat in PERMITTED_CAVEATS if caveat.lower() in body], (
        "no bounded negative is stated; the allow-list is doing nothing"
    )


def test_the_language_check_rejects_a_positive_claim_and_permits_a_bounded_negative():
    assert banned_in("The funding signal shows a causal lift in opportunity creation.")
    assert banned_in("This is production-ready and integrates with Salesforce.")
    permitted = "The comparisons are descriptive, not causal."
    for caveat in PERMITTED_CAVEATS:
        permitted = re.sub(re.escape(caveat), " ", permitted, flags=re.I)
    assert not banned_in(permitted)


# --- 11. One diagram --------------------------------------------------------


def test_exactly_one_mermaid_diagram_covers_the_named_components():
    blocks = [body for language, body in fenced_blocks(readme()) if language == "mermaid"]
    assert len(blocks) == 1, f"expected exactly one mermaid fence, found {len(blocks)}"
    diagram = blocks[0]
    for named in (
        "POST /api/v1/decision-events",
        "events",
        "logic_artifacts",
        "reconstruct",
        "counterfactual",
        "insights",
        "/insights",
    ):
        assert named in diagram, f"the diagram does not name {named!r}"
    assert "reads only" in diagram, "the diagram does not show replay and analytics as reads"


# --- 12. The badge points at the workflow -----------------------------------

BADGE = re.compile(r"\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)")
WORKFLOW_SUFFIX = "actions/workflows/ci.yml"
REPOSITORY = "https://github.com/e-skora/flight-recorder/"


def workflow_jobs() -> list[str]:
    """The job names in `ci.yml`, read from the workflow file itself."""
    lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index("jobs:")
    return [
        match.group(1)
        for line in lines[start + 1 :]
        for match in [re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)]
        if match
    ]


def badge_region() -> str:
    return section(readme(), "# GTM Flight Recorder")


def test_the_badge_is_the_ci_workflow_badge_for_main_wrapped_in_a_link():
    badges = BADGE.findall(badge_region())
    assert len(badges) == 1, f"expected one badge, found {len(badges)}"
    alt, image, link = badges[0]
    assert alt == "ci"
    assert image.startswith(REPOSITORY), image
    assert image.endswith(f"{WORKFLOW_SUFFIX}/badge.svg?branch=main"), image
    assert link == f"{REPOSITORY}{WORKFLOW_SUFFIX}", link


def test_the_badge_sentence_names_every_job_the_workflow_runs():
    jobs = workflow_jobs()
    assert len(jobs) >= 3, jobs
    region = badge_region()
    for job in jobs:
        assert job in region, f"the badge sentence does not name the {job!r} job"
    assert "three" in region or str(len(jobs)) in region


def test_the_badge_sentence_states_the_invariant_command_the_workflow_runs():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "uv run pytest -m invariant" in workflow
    assert "HYPOTHESIS_PROFILE: ci" in workflow
    region = badge_region()
    assert "uv run pytest -m invariant" in region
    assert "HYPOTHESIS_PROFILE=ci" in region


def test_the_readme_claims_no_single_job_badge():
    body = readme().lower()
    for claim in (
        "invariants badge",
        "invariant badge",
        "badge for the invariants job",
        "badge for the invariant job",
        "badge reports the invariants job",
    ):
        assert claim not in body, f"the README claims a single-job badge: {claim!r}"
    assert "not a badge for the invariant suite alone" in readme()


# --- 13. Screenshot file integrity ------------------------------------------


@pytest.mark.parametrize("relative", SCREENSHOT_PATHS)
def test_each_screenshot_exists_is_a_png_under_the_cap_and_is_referenced(relative: str):
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing"
    data = path.read_bytes()
    assert data.startswith(PNG_MAGIC), f"{relative} does not begin with the PNG signature"
    assert len(data) <= MAX_SCREENSHOT_BYTES, f"{relative} is {len(data)} bytes"
    assert relative in readme(), f"{relative} is not referenced by the README"


def test_the_screenshot_section_states_the_capture_commit_and_calls_them_synthetic():
    region = section(readme(), "### Screenshots")
    assert re.search(r"\b[0-9a-f]{40}\b", region), "no capture commit is stated"
    assert "synthetic" in region.lower()
    for relative in SCREENSHOT_PATHS:
        assert relative in region, f"{relative} is not in the screenshot section"


# --- 14. No phase language --------------------------------------------------


def test_readme_prose_carries_no_phase_or_decision_identifiers():
    body = prose(readme())
    found = sorted(
        {
            match.group(0)
            for pattern in (r"[Pp]hase\s*\d", r"\bD-0\d\d\b", r"\bD-0\b", r"\b5[ABC]\b")
            for match in re.finditer(pattern, body)
        }
    )
    assert not found, f"internal phase or decision language in README prose: {found}"
