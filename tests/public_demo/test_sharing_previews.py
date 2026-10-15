"""Sharing crawlers receive complete metadata without running JavaScript."""

import re
import struct
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from flight_recorder.app import create_app
from flight_recorder.public_demo import create_public_demo
from tests.public_demo.conftest import DECISION_URL, owned_copy
from tests.public_demo.test_public_branding import Tags


@pytest.fixture(scope="module")
def public(built_snapshot, tmp_path_factory):
    path = owned_copy(built_snapshot, tmp_path_factory.mktemp("sharing"))
    with TestClient(create_public_demo(path)) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    ["/", "/demo", "/about", "/insights", "/accounts/novasignal-ai", DECISION_URL],
)
def test_public_pages_have_complete_sharing_previews(public, path):
    html = public.get(path).text
    tags = Tags(html).tags
    metadata = {
        attrs.get("property", attrs.get("name")): attrs["content"]
        for tag, attrs in tags
        if tag == "meta" and "content" in attrs
    }
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert metadata["og:title"] == title
    assert metadata["twitter:title"] == title
    assert metadata["og:description"] == metadata["description"]
    assert metadata["twitter:description"] == metadata["description"]
    assert "synthetic" in metadata["description"].lower()
    assert "read-only" in metadata["description"]
    assert metadata["og:type"] == "website"
    assert metadata["og:site_name"] == "GTM Flight Recorder"
    assert metadata["og:url"] == f"https://flight-recorder.app{path}"
    assert (
        next(a["href"] for t, a in tags if t == "link" and a.get("rel") == "canonical")
        == (metadata["og:url"])
    )
    assert metadata["twitter:card"] == "summary_large_image"
    assert metadata["twitter:image"] == metadata["og:image"]
    assert metadata["twitter:image:alt"] == metadata["og:image:alt"]
    assert metadata["og:image:alt"]
    image_url = urlsplit(metadata["og:image"])
    assert image_url.scheme == "https" and image_url.netloc == "flight-recorder.app"
    response = public.get(image_url.path)
    assert response.status_code == 200
    assert response.headers["content-type"] == metadata["og:image:type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    dimensions = struct.unpack(">II", response.content[16:24])
    assert dimensions == (1200, 630)
    assert dimensions == (int(metadata["og:image:width"]), int(metadata["og:image:height"]))


def test_preview_urls_do_not_use_request_host_or_tracking_parameters(public):
    html = public.get("/?utm_source=sharing", headers={"host": "untrusted.example"}).text
    tags = Tags(html).tags
    url = next(a["content"] for t, a in tags if t == "meta" and a.get("property") == "og:url")
    assert url == "https://flight-recorder.app/"


def test_local_mode_does_not_advertise_public_site(built_snapshot, tmp_path):
    with TestClient(create_app(owned_copy(built_snapshot, tmp_path))) as client:
        html = client.get("/").text
    assert "og:" not in html
    assert "twitter:" not in html
    assert 'rel="canonical"' not in html
