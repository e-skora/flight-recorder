"""D-018 piece 1: the public demo serves every journey page read-only from a
snapshot, computes replay live, shows first-use guidance only in public mode,
and reports its identity. Tests 1, 2, 7 and 9 of the task.

Maps to D-018's acceptance: the browser demo is usable without login (test 1),
canonical replay stays correct and is computed by the actual engine (test 2,
INV-06: the counterfactual is computed on demand and never stored), first-use
instructions work (test 7), and restart preserves the intended dataset (test 9,
the health identity). INV-01 and INV-11: every request here is a read; the
snapshot's full state and file bytes are compared before and after.
"""

import re

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.cli import cmd_register_current_logic, cmd_reset, cmd_seed_dataset
from flight_recorder.public_demo import (
    PINNED_CONTENT_IDENTITY,
    content_identity,
    create_public_demo,
    open_read_only,
    schedule_digest,
)
from flight_recorder.replay.counterfactual import compare, replay
from tests.acceptance.test_readme import demo_steps, element, has_element, page_text, visible
from tests.public_demo.conftest import (
    DECISION_EVENT_ID,
    DECISION_URL,
    EXPECTED_CONTENT_IDENTITY,
    SNAPSHOT_EVENT_COUNT,
    SOURCE_URL,
    V5_1_HASH,
    V5_2_HASH,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)

PUBLIC_READS = (
    "/",
    "/?q=nova",
    "/accounts/novasignal-ai",
    DECISION_URL,
    f"{DECISION_URL}?current={V5_1_HASH}",
    "/insights",
    "/static/style.css",
    "/healthz",
)


@pytest.fixture(scope="module")
def snapshot(built_snapshot, tmp_path_factory):
    return owned_copy(built_snapshot, tmp_path_factory.mktemp("public-reads"))


@pytest.fixture(scope="module")
def public(snapshot):
    with TestClient(create_public_demo(snapshot)) as client:
        yield client


@pytest.fixture(scope="module")
def local(tmp_path_factory):
    """A local application over its own recipe-built database, in this process."""
    path = tmp_path_factory.mktemp("local-app") / "local.db"
    assert cmd_reset(path) == 0
    assert cmd_seed_dataset(path) == 0
    assert cmd_register_current_logic(path) == 0
    with TestClient(create_app(path)) as client:
        yield client


def score(html: str, element_id: str) -> str:
    return visible(element(html, element_id))


def contribution_rows(html: str) -> list[list[str]]:
    table = element(html, "contributions-table")
    return [
        [visible(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.S)]
        for row in re.findall(r'<tr class="change-[^"]*">(.*?)</tr>', table, re.S)
    ]


# --- Test 1: public reads ------------------------------------------------------------


def test_every_public_read_answers_200_and_changes_nothing(public, snapshot):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    for url in PUBLIC_READS:
        response = public.get(url)
        assert response.status_code == 200, (url, response.status_code)
    assert_unchanged(snapshot, state, sha256)


def test_the_canonical_page_renders_86_to_72_by_default_and_86_to_51_under_v5_1(public):
    default = public.get(DECISION_URL).text
    assert score(default, "original-score") == "86"
    assert score(default, "counterfactual-score") == "72"
    assert V5_2_HASH in element(default, "counterfactual-artifact-hash")

    explicit = public.get(f"{DECISION_URL}?current={V5_1_HASH}").text
    assert score(explicit, "original-score") == "86"
    assert score(explicit, "counterfactual-score") == "51"
    assert V5_1_HASH in element(explicit, "counterfactual-artifact-hash")


@pytest.mark.parametrize(
    ("url", "status", "detail"),
    [
        (f"{DECISION_URL}?current=not-a-hash", 400, "current must be 64 lowercase hexadecimal"),
        (f"{DECISION_URL}?current={'A' * 64}", 400, "current must be 64 lowercase hexadecimal"),
        ("/accounts/no-such-account", 404, "unknown account"),
        ("/accounts/novasignal-ai/decisions/evt-no-such-decision", 404, "unknown decision"),
        ("/accounts/_system", 404, "unknown account"),
    ],
)
def test_bad_requests_answer_a_named_4xx_never_a_5xx(public, url, status, detail):
    response = public.get(url)
    assert response.status_code == status, (url, response.status_code)
    assert detail in response.json()["detail"]


# --- Test 2: replay is computed live by the engine -----------------------------------


def registered_hashes(snapshot) -> list[str]:
    engine = open_read_only(snapshot)
    try:
        with engine.connect() as conn:
            return [
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT artifact_hash FROM logic_artifacts "
                    "WHERE decision_class = 'account_prioritization' ORDER BY artifact_hash"
                )
            ]
    finally:
        engine.dispose()


def test_every_rendered_replay_equals_the_engine_on_the_same_snapshot(public, snapshot):
    """Default, explicit v5.1 and every other registered artifact: the page shows
    exactly what `compare(replay(...))` computes now, so no constant can pass."""
    hashes = registered_hashes(snapshot)
    assert {V5_1_HASH, V5_2_HASH} <= set(hashes)
    engine = open_read_only(snapshot)
    try:
        cases = [(DECISION_URL, V5_2_HASH)] + [(f"{DECISION_URL}?current={h}", h) for h in hashes]
        seen_scores = set()
        for url, artifact_hash in cases:
            with engine.connect() as conn:
                expected = compare(replay(conn, DECISION_EVENT_ID, artifact_hash))
            html = public.get(url).text
            assert score(html, "original-score") == str(expected.original_score), url
            assert score(html, "counterfactual-score") == str(expected.counterfactual_score), url
            assert score(html, "counterfactual-threshold") == str(
                expected.counterfactual_threshold
            ), url
            assert score(html, "counterfactual-output") == str(expected.counterfactual_output), url
            rendered = contribution_rows(html)
            assert [row[0] for row in rendered] == [c.key for c in expected.contributions], url
            assert [row[5] for row in rendered] == [
                str(c.counterfactual_contribution) for c in expected.contributions
            ], url
            seen_scores.add(expected.counterfactual_score)
        assert {72, 51, 86} <= seen_scores
    finally:
        engine.dispose()


# --- Test 7: first use, public mode only ---------------------------------------------

NOTICE = "public-demo-notice"
START = "start-here"


def test_the_notice_renders_on_every_public_page_and_the_start_block_on_the_list(public):
    for url in ("/", "/?q=nova", "/accounts/novasignal-ai", DECISION_URL, "/insights"):
        html = public.get(url).text
        notice = page_text(element(html, NOTICE))
        assert "Public read-only demo." in notice
        assert "synthetic" in notice
        assert "computed on demand" in notice and "never stored" in notice
        assert f'href="{SOURCE_URL}"' in element(html, NOTICE)
        assert has_element(html, START) == (url in ("/", "/?q=nova"))


def test_the_start_block_links_work(public):
    html = public.get("/").text
    block = element(html, START)
    links = dict(re.findall(r'<a id="([^"]+)" href="([^"]+)"', block))
    assert links["start-canonical-decision"] == DECISION_URL
    assert links["start-replay-comparison"] == f"{DECISION_URL}#replay-comparison"
    assert links["start-source"] == SOURCE_URL
    for key in ("start-canonical-decision", "start-replay-comparison"):
        path, _, fragment = links[key].partition("#")
        response = public.get(path)
        assert response.status_code == 200
        if fragment:
            assert has_element(response.text, fragment)


def test_public_wording_stays_inside_the_claim_rules(public):
    for url in ("/", DECISION_URL):
        html = public.get(url).text
        text = page_text(element(html, NOTICE)).lower()
        if url == "/":
            text += " " + page_text(element(html, START)).lower()
        for word in ("real-time", "production", "validated", "causal", "live customer data"):
            assert word not in text, word
        assert "—" not in text  # no em dash in public-facing copy


def test_neither_renders_in_local_mode_and_the_apps_render_independently(public, local):
    """A public app and a local app in one process: the flag lives on each
    app's own state, never on the shared template environment."""
    for _ in range(2):  # interleaved both ways
        for url in ("/", DECISION_URL, "/insights"):
            local_html = local.get(url).text
            public_html = public.get(url).text
            assert not has_element(local_html, NOTICE), url
            assert not has_element(local_html, START), url
            assert "Public read-only demo" not in local_html
            assert has_element(public_html, NOTICE), url


def test_a_local_app_created_after_the_public_app_shows_no_public_content(snapshot, tmp_path):
    with TestClient(create_public_demo(snapshot)) as public_client:
        assert has_element(public_client.get("/").text, NOTICE)
        path = tmp_path / "later-local.db"
        assert cmd_reset(path) == 0
        with TestClient(create_app(path)) as later_local:
            html = later_local.get("/").text
            assert not has_element(html, NOTICE)
            assert not has_element(html, START)


def test_every_readme_demo_cue_renders_in_public_mode(public):
    steps = demo_steps()
    assert len(steps) == 8
    for step in steps:
        response = public.get(step.path)
        assert response.status_code == 200, step
        assert step.cue in page_text(response.text), step
        if step.fragment:
            assert has_element(response.text, step.fragment), step


# --- Test 9: health ------------------------------------------------------------------


def test_health_reports_the_pinned_identity_and_a_separately_labelled_schedule_digest(
    public, snapshot
):
    body = public.get("/healthz").json()
    assert PINNED_CONTENT_IDENTITY == EXPECTED_CONTENT_IDENTITY
    assert body["content_identity"] == EXPECTED_CONTENT_IDENTITY
    assert body["event_count"] == SNAPSHOT_EVENT_COUNT
    assert body["read_only"] is True and body["synthetic"] is True
    engine = open_read_only(snapshot)
    try:
        with engine.connect() as conn:
            assert body["schedule_digest"] == schedule_digest(conn)
            assert body["content_identity"] == content_identity(conn)
    finally:
        engine.dispose()
    assert body["schedule_digest"] != body["content_identity"]
    assert "not the admission identity" in body["schedule_digest_note"]


def test_head_healthz_answers_without_a_body_and_without_writing(public, snapshot):
    state, sha256 = full_state(snapshot), file_sha256(snapshot)
    response = public.head("/healthz")
    assert response.status_code == 200
    assert response.content == b""
    assert_unchanged(snapshot, state, sha256)
