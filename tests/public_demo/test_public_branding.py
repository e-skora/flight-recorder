"""P-01/P-02: first-party identity and one accessible brand announcement."""

import re
import struct
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.public_demo import create_public_demo
from tests.acceptance.test_readme import element, visible
from tests.public_demo.conftest import DECISION_URL, owned_copy


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture(scope="module")
def public(built_snapshot, tmp_path_factory):
    path = owned_copy(built_snapshot, tmp_path_factory.mktemp("branding"))
    with TestClient(create_public_demo(path)) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    ["/", "/demo", "/about", "/insights", "/accounts/novasignal-ai", DECISION_URL, "/contact/sent"],
)
def test_brand_and_disclosure_are_visible_once_per_location(public, path):
    html = public.get(path).text
    for brand_id in ("header-brand", "footer-brand"):
        brand = element(html, brand_id)
        assert visible(brand) == "GTM Flight Recorder"
        tags = Tags(brand).tags
        link = next((tag, attrs) for tag, attrs in Tags(html).tags if attrs.get("id") == brand_id)
        assert link[0] == "a" and link[1]["href"] == "/"
        images = [attrs for tag, attrs in tags if tag == "img"]
        assert len(images) == 1 and images[0]["alt"] == ""
        assert images[0]["src"].startswith("/static/brand/")
    notice = element(html, "public-demo-notice")
    collapsed = re.sub(r"<details\b.*?</details>", "", notice, flags=re.S)
    assert "Public read-only demo." in visible(collapsed)
    assert "All data here is synthetic" in visible(collapsed)


def test_icons_are_served_at_the_declared_dimensions(public):
    links = [attrs for tag, attrs in Tags(public.get("/").text).tags if tag == "link"]
    for size in (16, 32, 48, 180):
        link = next(a for a in links if a.get("sizes") == f"{size}x{size}")
        response = public.get(link["href"])
        assert response.status_code == 200
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", response.content[16:24]) == (size, size)
    for name in ("capsule.svg", "capsule-dark.svg", "capsule-mono.svg"):
        response = public.get(f"/static/brand/{name}")
        assert response.status_code == 200
        assert "<script" not in response.text and "<image" not in response.text


def test_local_mode_does_not_get_public_brand_assets(built_snapshot, tmp_path):
    with TestClient(create_app(owned_copy(built_snapshot, tmp_path))) as client:
        html = client.get("/").text
    assert "/static/brand/" not in html
    assert 'class="product">GTM Flight Recorder</p>' in html
