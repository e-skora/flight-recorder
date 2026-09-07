"""INV-01, generatively: no sequence of later evidence changes the reconstruction.

AC-18 asks for generative cases that inject post-decision evidence. This one
appends a random, valid sequence of later evidence versions -- new values,
optional supersession of the versions the decision consumed -- and requires the
reconstruction of the canonical decision to be identical after every single
append, not merely at the end.
"""

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_recorder.replay.reconstruct import reconstruct
from tests.conftest import (
    DECISION_EVENT_ID,
    Harness,
    evidence_envelope,
    seed_all,
)

pytestmark = pytest.mark.invariant

#: The closed schema-v1 evidence vocabulary, with a strategy for each type's
#: value and any extra fields it carries.
VALUES = {
    "employee_count": st.integers(min_value=1, max_value=200_000),
    "industry": st.sampled_from(
        ["B2B AI Software", "Industrial Automation", "Fintech", "Developer Tools"]
    ),
    "headquarters_country": st.sampled_from(["United States", "Canada", "Germany", "Japan"]),
    "open_platform_engineering_roles": st.integers(min_value=0, max_value=200),
    "funding_event": st.sampled_from(["Seed", "Series A", "Series B", "Series C"]),
    "head_of_platform_start_date": st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 8, 1)),
    "verified_integration_pressure": st.sampled_from(["LOW", "MEDIUM", "HIGH"]),
}

#: The canonical `-v1` version of each type, minted before the boundary and
#: therefore a legal supersession target for anything appended later.
ORIGINAL = {
    "employee_count": "ev-novasignal-employee-count-v1",
    "industry": "ev-novasignal-industry-v1",
    "headquarters_country": "ev-novasignal-headquarters-country-v1",
    "open_platform_engineering_roles": "ev-novasignal-open-platform-engineering-roles-v1",
    "funding_event": "ev-novasignal-funding-event-v1",
    "head_of_platform_start_date": "ev-novasignal-head-of-platform-start-date-v1",
    "verified_integration_pressure": "ev-novasignal-verified-integration-pressure-v1",
}

BOUNDARY_DAY = date(2026, 4, 17)


@st.composite
def later_evidence(draw):
    evidence_type = draw(st.sampled_from(sorted(VALUES)))
    return {
        "evidence_type": evidence_type,
        "value": draw(VALUES[evidence_type]),
        "observed_at": draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 8, 1))),
        "supersedes": draw(st.booleans()),
    }


def build_item(index: int, drawn: dict, latest: dict[str, str]) -> dict:
    """One `evidence.recorded` item, valid for the schema-v1 evidence vocabulary."""
    evidence_type = drawn["evidence_type"]
    value = drawn["value"]
    item = {
        "evidence_version_id": f"ev-novasignal-{evidence_type.replace('_', '-')}-later-{index}",
        "evidence_type": evidence_type,
        "value": value.isoformat() if isinstance(value, date) else value,
    }
    if evidence_type in ("funding_event", "head_of_platform_start_date"):
        item["observed_at"] = drawn["observed_at"].isoformat()
    if evidence_type == "verified_integration_pressure":
        item["basis"] = ["generated for the INV-01 property test"]
    if drawn["supersedes"]:
        item["supersedes_evidence_version_id"] = latest[evidence_type]
    return item


@given(st.lists(later_evidence(), min_size=1, max_size=4))
def test_appending_later_evidence_never_changes_the_reconstruction(tmp_path_factory, appends):
    harness = Harness(tmp_path_factory.mktemp("inv01-reconstruction"))
    for response in seed_all(harness):
        assert response.status_code == 201

    with harness.engine.connect() as conn:
        original = reconstruct(conn, DECISION_EVENT_ID)
    assert original.result.score == 86

    latest = dict(ORIGINAL)
    for index, drawn in enumerate(appends):
        item = build_item(index, drawn, latest)
        # Every append is recorded well after `T(d)`, in arrival order, so each
        # supersession target is already available.
        occurred_at = (BOUNDARY_DAY + timedelta(days=14 + index)).isoformat() + "T09:00:00Z"
        response = harness.post(
            evidence_envelope(f"evt-novasignal-later-{index}", [item], occurred_at=occurred_at)
        )
        assert response.status_code == 201, (item, response.json())
        latest[drawn["evidence_type"]] = item["evidence_version_id"]

        with harness.engine.connect() as conn:
            assert reconstruct(conn, DECISION_EVENT_ID) == original
