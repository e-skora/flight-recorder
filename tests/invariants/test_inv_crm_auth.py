"""INV-01/09/11: strict identity, one-use auth state and no guessed authority."""

import string
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_recorder.adapters.crm.contracts import CRMRefused, CRMTarget
from tests.adapters.crm.test_oauth import (
    ACCOUNT,
    LOGIN,
    ORG,
    USER,
    Clock,
    configuration,
    fake_provider,
    feature,
    finish,
    private_store,
    start,
    token_response,
)

pytestmark = pytest.mark.invariant


@given(st.text(alphabet=string.ascii_letters + string.digits, min_size=12, max_size=12))
def test_source_identity_conversion_preserves_every_original_case_bit(tail):
    source = "00D" + tail
    canonical = feature("salesforce", "source_identity_id")(source, "00D")
    assert len(canonical) == 18 and canonical[:15] == source
    assert CRMTarget("salesforce", canonical, "Account", ACCOUNT).instance_id == canonical
    with pytest.raises(CRMRefused):
        configuration(expected_instance_id=source)


@given(
    st.sampled_from(["write_enabled", "code_secret_required", "refresh_secret_required"]),
    st.one_of(st.none(), st.integers(), st.floats(), st.text(max_size=32)),
)
def test_configuration_never_coerces_non_boolean_flags(field, value):
    with pytest.raises(CRMRefused):
        configuration(**{field: value})


@given(st.sampled_from(["\x00", "\n", "\r", "\x7f", "\x85"]), st.text(max_size=30))
@settings(deadline=None)
def test_invalid_callback_code_consumes_state_but_cannot_exchange(control, suffix):
    with tempfile.TemporaryDirectory(
        prefix="fr-oauth-state-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        config, clock = configuration(), Clock()
        store = private_store(Path(name))
        sessions, _, query = start(config, clock)
        http, requests = fake_provider(clock)
        callback = {"state": query["state"][0], "code": control + suffix}
        with pytest.raises(CRMRefused, match="invalid_oauth_callback"):
            finish(config, sessions, store, callback, http, clock)
        with pytest.raises(CRMRefused, match="oauth_state_invalid"):
            finish(
                config, sessions, store, {**callback, "code": "fictional-valid-code"}, http, clock
            )
        assert requests == []
        http.close()
        store.close()


@given(
    st.text(alphabet=string.ascii_letters + string.digits, min_size=12, max_size=12).filter(
        lambda tail: "00D" + tail != ORG[:15]
    )
)
@settings(deadline=None)
def test_foreign_source_org_success_never_installs_or_reads_account(tail):
    with tempfile.TemporaryDirectory(
        prefix="fr-oauth-org-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        config, clock = configuration(), Clock()
        store = private_store(Path(name))
        sessions, _, query = start(config, clock)
        response = token_response(id=f"{LOGIN}/id/00D{tail}/{USER[:15]}")
        http, requests = fake_provider(clock, token=response)
        with pytest.raises(CRMRefused):
            finish(
                config,
                sessions,
                store,
                {"state": query["state"][0], "code": "fictional-code"},
                http,
                clock,
            )
        assert len(requests) == 1 and requests[0].method == "POST"
        assert not list((Path(name) / "private-tokens").glob("*.json"))
        http.close()
        store.close()
