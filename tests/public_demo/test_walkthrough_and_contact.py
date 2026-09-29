"""D-021, public site task 2: the personal walkthrough video and the Contact form
on Home, the `/contact/sent` page, and the startup validation of both settings.
Checks E.1 to E.5 of the task.

Invariants named: INV-01 and INV-11 (the public app stays read-only: every write
method to `/contact/sent` is refused before routing and the snapshot's full state
and file bytes are unchanged afterwards; the form posts to the external provider,
never to this app, so no submission reaches the ledger or any store here).

The markup is read with the standard library's HTML parser, tag by tag, so an
attribute in another order, a second element or a stray script is seen as such
rather than slipping past a substring search.
"""

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.public_demo import (
    CONTACT_FORM_ACCESS_KEY,
    PERSONAL_WALKTHROUGH_POSTER_URL,
    PERSONAL_WALKTHROUGH_VIDEO_SHA256,
    PERSONAL_WALKTHROUGH_VIDEO_URL,
    ContactKeyRefused,
    SiteConfigurationRefused,
    WalkthroughAddressRefused,
    create_public_demo,
)
from tests.acceptance.test_readme import element, has_element, page_text, visible
from tests.public_demo.conftest import (
    DECISION_URL,
    assert_unchanged,
    file_sha256,
    full_state,
    owned_copy,
)

#: The accepted values, written out here independently of the production
#: constants, so a changed constant is caught too.
VIDEO_URL = "https://media.flight-recorder.app/flight-recorder-walkthrough-2026-09-27-1080p.mp4"
POSTER_URL = "https://media.flight-recorder.app/poster-2026-09-27.jpg"
VIDEO_SHA256 = "19c24c9372a5f27866c5baeeaa979e158dfc6aa3410f5c53e62438463318dabe"
ACCESS_KEY = "f2ed1fb0-7c9f-4eee-8d7b-3a2f4add6490"
CAPTION = (
    "Elias Skora walks through one decision: its preserved evidence, the rules it ran, "
    "and a replay under newer rules."
)
MEDIA_LINE = "The video file is served from media.flight-recorder.app."
FALLBACK = "Your browser cannot play this video. Download the video instead."
PROVIDER_LINE = (
    "Sending goes through Web3Forms, which processes and keeps your name, email and message "
    "under its own terms; this site does not receive or store it."
)
HIDDEN_FIELDS = {
    "access_key": ACCESS_KEY,
    "subject": "Flight Recorder contact",
    "from_name": "Flight Recorder website",
    "redirect": "https://flight-recorder.app/contact/sent",
}
PUBLIC_PAGES = ("/", "/demo", "/about", "/accounts/novasignal-ai", DECISION_URL, "/insights")
REFUSED_METHODS = ("POST", "PUT", "PATCH", "DELETE")
FORBIDDEN_TAGS = ("script", "iframe", "embed", "object")
WATCH_ACTION = (
    '<a id="watch-the-walkthrough" href="#walkthrough" class="button button-secondary">'
    "Watch the walkthrough</a>"
)


class _Tags(HTMLParser):
    """Every start tag, in document order, with its attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def all_tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser.tags


def tags(html: str, name: str) -> list[dict[str, str | None]]:
    return [attrs for tag, attrs in all_tags(html) if tag == name]


def main_text(html: str) -> str:
    return visible(element(html, "main"))


@pytest.fixture(scope="module")
def snapshot(built_snapshot, tmp_path_factory):
    return owned_copy(built_snapshot, tmp_path_factory.mktemp("walkthrough-contact"))


@pytest.fixture(scope="module")
def public(snapshot):
    with TestClient(create_public_demo(snapshot)) as client:
        yield client


@pytest.fixture(scope="module")
def home_html(public) -> str:
    return public.get("/").text


def client_for(snapshot, **settings) -> TestClient:
    return TestClient(create_public_demo(snapshot, **settings))


def test_the_constants_are_the_accepted_values():
    assert PERSONAL_WALKTHROUGH_VIDEO_URL == VIDEO_URL
    assert PERSONAL_WALKTHROUGH_POSTER_URL == POSTER_URL
    assert PERSONAL_WALKTHROUGH_VIDEO_SHA256 == VIDEO_SHA256
    assert CONTACT_FORM_ACCESS_KEY == ACCESS_KEY


# --- E.1: the player ---------------------------------------------------------------------


def test_home_has_exactly_one_native_player_with_the_accepted_attributes(home_html):
    (video,) = tags(home_html, "video")
    assert set(video) == {"id", "controls", "preload", "playsinline", "poster", "aria-label"}
    assert video["preload"] == "metadata"
    assert video["poster"] == POSTER_URL
    assert video["aria-label"] == "Personal walkthrough by Elias Skora"
    for attribute in ("autoplay", "muted", "loop"):
        assert attribute not in video, attribute
    (source,) = tags(home_html, "source")
    assert source == {"src": VIDEO_URL, "type": "video/mp4"}


def test_the_player_sits_in_the_walkthrough_section_with_its_fallback(home_html):
    section = element(home_html, "walkthrough")
    player = section[section.index("<video") : section.index("</video>")]
    assert visible(player) == FALLBACK
    assert f'<a href="{VIDEO_URL}">Download the video</a>' in player
    assert '<div class="walkthrough-frame">' in section


def test_the_caption_download_link_and_media_line_sit_beside_the_player(home_html):
    section = element(home_html, "walkthrough")
    assert visible(element(section, "walkthrough-caption")) == CAPTION
    assert f'<a id="walkthrough-download" href="{VIDEO_URL}">Download the video</a>' in section
    assert visible(element(section, "walkthrough-media-line")) == MEDIA_LINE
    markers = ("<video", 'id="walkthrough-caption"', 'id="walkthrough-download"', 'id="contact"')
    positions = [section.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "loom" not in home_html.lower()


def test_watch_the_walkthrough_links_to_the_section(home_html):
    assert WATCH_ACTION in element(home_html, "home-intro")
    assert has_element(home_html, "walkthrough")


@pytest.mark.parametrize("url", (*PUBLIC_PAGES, "/contact/sent"))
def test_no_public_page_carries_script_iframe_embed_or_object(public, url):
    html = public.get(url).text
    found = [name for name, _ in all_tags(html) if name in FORBIDDEN_TAGS]
    assert found == [], (url, found)
    for name, attrs in all_tags(html):
        for attribute in ("autoplay", "muted", "loop"):
            assert attribute not in attrs, (url, name, attribute)


def test_query_parameters_do_not_change_the_media_addresses(public):
    html = public.get("/", params={"video": "https://evil.example/x.mp4", "poster": "x"}).text
    (source,) = tags(html, "source")
    assert source["src"] == VIDEO_URL
    assert tags(html, "video")[0]["poster"] == POSTER_URL
    assert "evil.example" not in html


# --- E.2: validation at startup ----------------------------------------------------------

BAD_ADDRESSES = {
    "http": "http://media.flight-recorder.app/x.mp4",
    "another host": "https://videos.example.com/x.mp4",
    "r2.dev": "https://pub-0123456789abcdef.r2.dev/x.mp4",
    "query string": "https://media.flight-recorder.app/x.mp4?v=2",
    "fragment": "https://media.flight-recorder.app/x.mp4#t=10",
    "empty path": "https://media.flight-recorder.app/",
    "host suffix": "https://media.flight-recorder.app.example.com/x.mp4",
    "credentials": "https://media.flight-recorder.app@example.com/x.mp4",
    "uppercase scheme": "HTTPS://media.flight-recorder.app/x.mp4",
}


@pytest.mark.parametrize("case", BAD_ADDRESSES)
@pytest.mark.parametrize("role", ["video", "poster"])
def test_a_bad_media_address_refuses_startup_with_the_named_error(snapshot, role, case):
    setting = "walkthrough_url" if role == "video" else "walkthrough_poster_url"
    with pytest.raises(WalkthroughAddressRefused) as refused:
        create_public_demo(snapshot, **{setting: BAD_ADDRESSES[case]})
    assert refused.value.role == role
    assert isinstance(refused.value, SiteConfigurationRefused)
    assert "https://media.flight-recorder.app/" in str(refused.value)


def test_the_settings_are_checked_before_the_snapshot_is_opened(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(WalkthroughAddressRefused):
        create_public_demo(missing, walkthrough_url=BAD_ADDRESSES["r2.dev"])
    with pytest.raises(ContactKeyRefused):
        create_public_demo(missing, contact_access_key="-" * 36)


BAD_KEYS = {
    "36 hyphens": "-" * 36,
    "36 letters": "a" * 36,
    "uppercase": ACCESS_KEY.upper(),
    "a missing group": "f2ed1fb0-7c9f-4eee-3a2f4add6490",
    "a hyphen in a wrong position": "f2ed1fb-07c9f-4eee-8d7b-3a2f4add6490",
    "an extra character": ACCESS_KEY + "0",
    "a trailing newline": ACCESS_KEY + "\n",
    "surrounding spaces": f" {ACCESS_KEY} ",
    "no hyphens": ACCESS_KEY.replace("-", ""),
    "a non-hex letter": "g" + ACCESS_KEY[1:],
}


@pytest.mark.parametrize("case", BAD_KEYS)
def test_a_malformed_contact_key_refuses_startup_with_the_named_error(snapshot, case):
    with pytest.raises(ContactKeyRefused) as refused:
        create_public_demo(snapshot, contact_access_key=BAD_KEYS[case])
    assert isinstance(refused.value, SiteConfigurationRefused)
    assert "8-4-4-4-12" in str(refused.value)


def test_the_configured_key_and_addresses_pass(snapshot):
    app = create_public_demo(
        snapshot,
        walkthrough_url=VIDEO_URL,
        walkthrough_poster_url=POSTER_URL,
        contact_access_key=ACCESS_KEY,
    )
    assert app.state.public_walkthrough == {"video_url": VIDEO_URL, "poster_url": POSTER_URL}
    assert app.state.public_contact["access_key"] == ACCESS_KEY


# --- E.3: absence and independence -------------------------------------------------------


def assert_no_walkthrough(html: str) -> None:
    assert not has_element(html, "walkthrough")
    assert not has_element(html, "watch-the-walkthrough")
    assert "Watch the walkthrough" not in page_text(html)
    assert tags(html, "video") == [] and tags(html, "source") == []
    assert "media.flight-recorder.app" not in html


def assert_no_contact(html: str) -> None:
    assert not has_element(html, "contact")
    assert tags(html, "form") == []
    assert "Contact" not in main_text(html)
    assert "web3forms" not in html.lower()


def assert_contact(html: str) -> None:
    assert has_element(html, "contact")
    assert len(tags(html, "form")) == 1


def assert_walkthrough(html: str) -> None:
    assert has_element(html, "walkthrough") and has_element(html, "watch-the-walkthrough")
    assert len(tags(html, "video")) == 1


@pytest.mark.parametrize("absent", [None, ""])
def test_no_walkthrough_renders_no_section_and_no_action_while_contact_still_renders(
    snapshot, absent
):
    with client_for(snapshot, walkthrough_url=absent) as client:
        html = client.get("/").text
        assert_no_walkthrough(html)
        assert_contact(html)
        assert has_element(html, "home-contact")
        assert client.get("/contact/sent").status_code == 200


@pytest.mark.parametrize("absent", [None, ""])
def test_no_contact_key_renders_no_control_and_no_route_while_the_walkthrough_renders(
    snapshot, absent
):
    with client_for(snapshot, contact_access_key=absent) as client:
        html = client.get("/").text
        assert_no_contact(html)
        assert_walkthrough(html)
        assert client.get("/contact/sent").status_code == 404
        assert "/contact/sent" not in html


def test_both_absent_renders_neither_and_both_present_renders_both(snapshot, home_html, public):
    with client_for(snapshot, walkthrough_url=None, contact_access_key=None) as client:
        html = client.get("/").text
        assert_no_walkthrough(html)
        assert_no_contact(html)
        assert client.get("/contact/sent").status_code == 404
    assert_walkthrough(home_html)
    assert_contact(home_html)
    assert public.get("/contact/sent").status_code == 200


# --- E.4: the contact markup ------------------------------------------------------------


def test_home_has_exactly_one_form_and_it_posts_to_the_provider(home_html):
    (form,) = tags(home_html, "form")
    assert form["action"] == "https://api.web3forms.com/submit"
    assert form["method"] == "post"
    assert "mailto:" not in home_html


def test_the_contact_control_is_a_closed_details_box_with_a_contact_summary(home_html):
    box = element(home_html, "contact")
    (details,) = [d for d in tags(home_html, "details") if d.get("id") == "contact"]
    assert details == {"id": "contact", "class": "contact"}
    assert '<summary class="button button-secondary">Contact</summary>' in box
    assert box.index("<summary") < box.index("<form")


def test_the_hidden_fields_carry_exactly_the_accepted_values(home_html):
    hidden = {i["name"]: i["value"] for i in tags(home_html, "input") if i.get("type") == "hidden"}
    assert hidden == HIDDEN_FIELDS


def test_the_honeypot_is_present_hidden_and_unchecked(home_html):
    (honeypot,) = [i for i in tags(home_html, "input") if i.get("name") == "botcheck"]
    assert honeypot == {
        "type": "checkbox",
        "name": "botcheck",
        "class": "hidden",
        "style": "display:none",
        "tabindex": "-1",
        "autocomplete": "off",
    }


def test_the_three_visible_fields_are_labelled_and_required(home_html):
    form = element(home_html, "contact-form")
    assert {label["for"] for label in tags(form, "label")} == {
        "contact-name",
        "contact-email",
        "contact-message",
    }
    for field_id, label in (
        ("contact-name", "Your name"),
        ("contact-email", "Your email"),
        ("contact-message", "Message"),
    ):
        assert f'<label for="{field_id}">{label}</label>' in form
    fields = {
        attrs["id"]: attrs
        for attrs in tags(form, "input") + tags(form, "textarea")
        if "id" in attrs
    }
    assert set(fields) == {"contact-name", "contact-email", "contact-message"}
    assert fields["contact-name"]["name"] == "name" and fields["contact-name"]["type"] == "text"
    assert fields["contact-email"]["name"] == "email" and fields["contact-email"]["type"] == "email"
    assert fields["contact-message"]["name"] == "message"
    assert fields["contact-message"]["maxlength"] == "4000"
    for field in fields.values():
        assert "required" in field
    names = [attrs.get("name") for attrs in tags(form, "input") + tags(form, "textarea")]
    assert sorted(names) == sorted([*HIDDEN_FIELDS, "botcheck", "name", "email", "message"])


def test_the_submit_button_and_the_provider_line(home_html):
    form = element(home_html, "contact-form")
    assert tags(form, "button") == [{"type": "submit"}]
    assert '<button type="submit">Send message</button>' in form
    assert visible(element(home_html, "contact-provider")) == PROVIDER_LINE
    box = element(home_html, "contact")
    assert box.index("</form>") < box.index('id="contact-provider"')


def test_the_new_copy_keeps_the_claim_rules(home_html, public):
    for html in (home_html, public.get("/contact/sent").text):
        text = page_text(html)
        assert "—" not in text
        for word in ("real-time", "production", "validated", "live customer data"):
            assert word not in text.lower(), word


# --- E.5: the landing page --------------------------------------------------------------


def test_contact_sent_answers_with_its_heading_line_and_both_links(public):
    response = public.get("/contact/sent")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert visible(element(html, "contact-sent-heading")) == "Message sent"
    assert (
        visible(element(html, "contact-sent-line"))
        == "Thanks. Your message is on its way to Elias."
    )
    assert (
        '<a id="contact-sent-home" href="/" class="button button-secondary">Back to Home</a>'
        in html
    )
    assert (
        '<a id="contact-sent-demo" href="/demo" class="button button-primary">Try the demo</a>'
        in html
    )
    assert tags(html, "form") == [] and tags(html, "video") == []
    assert has_element(html, "site-footer") and has_element(html, "public-demo-notice")


def test_contact_sent_head_behaves_like_every_other_site_page(public):
    status = public.head("/contact/sent").status_code
    assert status == public.head("/").status_code == public.head("/about").status_code


def test_every_write_method_to_contact_sent_is_refused_and_nothing_changes(snapshot, tmp_path):
    copy = owned_copy(snapshot, tmp_path)
    state, sha256 = full_state(copy), file_sha256(copy)
    with TestClient(create_public_demo(copy)) as client:
        for method in REFUSED_METHODS:
            for body in (b"name=a&email=b%40c.test&message=hello", b'{"probe": true}'):
                response = client.request(method, "/contact/sent", content=body)
                assert response.status_code == 405, (method, response.status_code)
                assert response.headers["allow"] == "GET, HEAD", method
                assert "read-only" in response.json()["detail"]
                assert_unchanged(copy, state, sha256)
        assert client.get("/contact/sent").status_code == 200
    assert_unchanged(copy, state, sha256)


def test_local_mode_has_no_contact_route_no_form_and_no_player(snapshot, tmp_path):
    with TestClient(create_app(owned_copy(snapshot, tmp_path, "local.db"))) as local:
        assert local.get("/contact/sent").status_code == 404
        html = local.get("/").text
        assert not has_element(html, "contact") and not has_element(html, "walkthrough")
        assert tags(html, "video") == []
        assert "web3forms" not in html.lower()
