"""`PRODUCT.md` §4.2 at dataset scale: NovaSignal AI is pinned and the list is filterable.

Evidence executed: the canonical pin read from the canonical fixture and the
stored account row; the name filter as a parameterized, case-insensitive,
literal substring (`%` and `_` are characters, not LIKE wildcards); the shown
and total counts read from `accounts_query()`; the system principal never
listed; the home page's existing guarantees (AC-12, the synthetic banner).
"""

import re
from html import unescape

from sqlalchemy import func, select

from flight_recorder.fixtures import canonical_account
from flight_recorder.ledger.schema import SYSTEM_ACCOUNT_REF, accounts, accounts_query
from tests.acceptance.test_decision_detail_page import element, has_element
from tests.conftest import (
    Harness,
    discovery_envelope,
    post_created,
    seed_all,
    seed_dataset,
    small_dataset_config,
)


def home(harness: Harness, query: str = "") -> str:
    response = harness.client.get("/" + query)
    assert response.status_code == 200, response.status_code
    return response.text


def text(html: str, element_id: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", element(html, element_id))).split())


def listed(html: str) -> list[str]:
    """The account refs linked from `ul.account-list`, in page order; [] when absent."""
    found = re.search(r'<ul class="account-list">(.*?)</ul>', html, re.DOTALL)
    if found is None:
        return []
    return re.findall(r'<li>\s*<a href="/accounts/([^"]+)">', found.group(1))


def stored_accounts(harness: Harness) -> list:
    with harness.engine.connect() as conn:
        return conn.execute(accounts_query().order_by(accounts.c.name)).all()


def total_accounts(harness: Harness) -> int:
    with harness.engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(accounts_query().subquery())
        ).scalar_one()


def test_the_canonical_account_is_pinned_and_the_list_is_filterable(harness):
    seed_dataset(harness, config=small_dataset_config())
    canonical_ref, canonical_name = canonical_account()
    (stored,) = [row for row in stored_accounts(harness) if row.account_ref == canonical_ref]
    assert stored.name == canonical_name
    total = total_accounts(harness)
    pin = f'<a href="/accounts/{canonical_ref}">{stored.name}</a>'

    html = home(harness)
    assert pin in element(html, "canonical-account")
    assert text(html, "canonical-account") == f"Canonical demo account: {stored.name}"
    assert text(html, "account-count") == f"Showing {total} of {total} accounts"
    form = re.search(r'<form method="get" action="/" id="account-filter">.*?</form>', html, re.S)
    assert form is not None
    assert '<label for="account-query">' in form.group(0)
    assert '<input type="search" name="q" id="account-query"' in form.group(0)
    assert '<button type="submit">' in form.group(0)
    assert len(listed(html)) == total

    for query in ("?q=nova", "?q=NOVA"):
        html = home(harness, query)
        assert listed(html) == [canonical_ref], query
        typed = query.removeprefix("?q=")
        assert text(html, "account-count") == f"Showing 1 of {total} accounts matching “{typed}”"
        assert f'id="account-query" value="{typed}"' in html
        assert pin in element(html, "canonical-account")

    for query in ("?q=", "?q=%20"):
        html = home(harness, query)
        assert len(listed(html)) == total, query
        assert text(html, "account-count") == f"Showing {total} of {total} accounts"
        assert pin in element(html, "canonical-account")

    html = home(harness, "?q=zzz-no-such-account")
    assert has_element(html, "account-no-match")
    assert text(html, "account-count") == (
        f"Showing 0 of {total} accounts matching “zzz-no-such-account”"
    )
    assert listed(html) == []
    assert 'class="account-list"' not in html
    assert pin in element(html, "canonical-account")

    html = home(harness, "?q=%27%20OR%201%3D1%20--")
    assert listed(html) == []
    assert has_element(html, "account-no-match")
    assert pin in element(html, "canonical-account")


def test_like_wildcards_match_literally(harness):
    seed_dataset(harness, config=small_dataset_config())
    post_created(harness, discovery_envelope("wild-card", name="Wild_Card 100% Co"))
    total = total_accounts(harness)

    html = home(harness, "?q=%25")
    assert listed(html) == ["wild-card"]
    assert text(html, "account-count") == f"Showing 1 of {total} accounts matching “%”"

    assert listed(home(harness, "?q=_card")) == ["wild-card"]
    assert listed(home(harness, "?q=100%25%20co")) == ["wild-card"]

    underscored = [row.account_ref for row in stored_accounts(harness) if "_" in row.name]
    assert underscored
    assert listed(home(harness, "?q=_")) == underscored


def test_the_pin_is_absent_without_the_canonical_account_and_the_system_account_never_lists(
    harness, tmp_path
):
    post_created(harness, discovery_envelope("filler-only"))
    html = home(harness)
    assert not has_element(html, "canonical-account")
    assert text(html, "account-count") == "Showing 1 of 1 accounts"

    canonical = Harness(tmp_path)
    for response in seed_all(canonical):
        assert response.status_code == 201, response.json()
    for query in ("", "?q=_sys"):
        assert SYSTEM_ACCOUNT_REF not in home(canonical, query), query


def test_the_home_page_keeps_its_existing_guarantees(harness):
    seed_all(harness)
    canonical_ref, _ = canonical_account()
    html = home(harness)
    assert "synthetic-banner" in html
    assert "simulated" in html and "fictional" in html
    assert "RelayBridge" in html
    assert "Merge" not in html
    assert f'href="/accounts/{canonical_ref}"' in html
    assert '<nav class="site-nav" aria-label="Site">' in html
