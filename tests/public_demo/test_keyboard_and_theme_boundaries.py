"""D-018 piece 1, revision 4: keyboard access and the public theme boundary. Test 12.

Every horizontally scrolling table sits in a named region that the keyboard can
reach (`tabindex="0"`), with no positive tabindex anywhere, and the skip link
and `<main id="main" tabindex="-1">` stay as they were. The public theme's
hooks (the `public-demo` root class, the badge, the source link and the
current-page marker) and the public notices occur only in public mode; local
pages carry none of them. The browser proof that the keyboard reaches, scrolls
and leaves an overflowing region is the task's check 9, run separately.

Maps to D-018's acceptance "first-use instructions and readable layouts work"
and to INV-01 and INV-11 in the sense that none of this touches data: every
request here is a read.
"""

import re

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.public_demo import create_public_demo
from tests.public_demo.conftest import DECISION_URL, V5_1_HASH, owned_copy

PAGES = (
    "/",
    "/?q=nova",
    "/accounts/novasignal-ai",
    DECISION_URL,
    f"{DECISION_URL}?current={V5_1_HASH}",
    "/insights",
)
#: Markup that only the public app may render.
PUBLIC_HOOKS = (
    'class="public-demo"',
    'class="public-badge"',
    'class="site-nav-source"',
    'aria-current="page"',
    'id="public-demo-notice"',
    'id="start-here"',
)
WRAPPER = re.compile(r'<div class="table-scroll"[^>]*>')


@pytest.fixture(scope="module")
def clients(built_snapshot, tmp_path_factory):
    directory = tmp_path_factory.mktemp("theme")
    public_app = create_public_demo(owned_copy(built_snapshot, directory, "public.db"))
    local_app = create_app(owned_copy(built_snapshot, directory, "local.db"))
    with TestClient(public_app) as public, TestClient(local_app) as local:
        yield public, local


@pytest.mark.parametrize("url", PAGES)
def test_scroll_regions_are_named_and_focusable_without_positive_tabindex(clients, url):
    for client in clients:
        html = client.get(url).text
        assert '<a class="skip-link" href="#main">Skip to main content</a>' in html
        assert '<main id="main" tabindex="-1">' in html
        assert not re.search(r'tabindex="[1-9]', html)
        wrappers = WRAPPER.findall(html)
        for tag in wrappers:
            assert 'role="region"' in tag, tag
            assert 'tabindex="0"' in tag, tag
            assert re.search(r'aria-label="[^"]+ table"', tag), tag
        assert html.count('tabindex="') == 1 + len(wrappers)
        # Every table on the page is inside one of those regions.
        assert html.count("<table") == len(wrappers)


@pytest.mark.parametrize("url", PAGES)
def test_public_theme_hooks_occur_only_in_public_mode(clients, url):
    public, local = clients
    public_html, local_html = public.get(url).text, local.get(url).text
    assert '<html lang="en" class="public-demo">' in public_html
    assert '<html lang="en">' in local_html
    for hook in PUBLIC_HOOKS:
        assert hook not in local_html, (url, hook)
    for hook in PUBLIC_HOOKS[:3]:
        assert hook in public_html, (url, hook)
    local_nav = '<nav class="site-nav" aria-label="Site"><a href="/">Accounts</a> '
    assert local_nav + '<a href="/insights">Insights</a></nav>' in local_html


def test_the_public_nav_marks_the_current_page(clients):
    public, _ = clients
    home = public.get("/").text
    assert '<a href="/" aria-current="page" class="is-current">Home</a>' in home
    assert home.count('aria-current="page"') == 1
    demo = public.get("/demo").text
    assert '<a href="/demo" aria-current="page" class="is-current">Try the demo</a>' in demo
    assert demo.count('aria-current="page"') == 1
    insights = public.get("/insights").text
    assert '<a href="/insights" aria-current="page" class="is-current">Insights</a>' in insights
    assert insights.count('aria-current="page"') == 1
    about = public.get("/about").text
    assert '<a href="/about" aria-current="page" class="is-current">About</a>' in about
    assert about.count('aria-current="page"') == 1
    decision = public.get(DECISION_URL).text
    assert 'aria-current="page"' not in decision  # a section marker, not a page claim
    assert '<a href="/demo" class="is-current">Try the demo</a>' in decision
    assert decision.count("is-current") == 1


def test_the_public_theme_is_scoped_in_the_stylesheet(clients):
    """Every rule after the public-theme marker is scoped to html.public-demo."""
    public, _ = clients
    css = public.get("/static/style.css").text
    marker = "/* --- Public demo theme (flight-recorder.app)."
    assert css.count(marker) == 1
    theme = re.sub(r"/\*.*?\*/", "", css[css.index(marker) :], flags=re.S)
    selectors = []
    for block in re.findall(r"([^{}]+)\{", theme):
        block = block.strip()
        if block.startswith("@media"):
            continue
        selectors += [s.strip() for s in block.split(",") if s.strip()]
    assert selectors
    unscoped = [s for s in selectors if not s.startswith("html.public-demo")]
    assert unscoped == [], unscoped
    assert "prefers-reduced-motion: no-preference" in theme
