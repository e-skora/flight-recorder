"""INV-01/09/11: local token generations fence mutations and uncertain refreshes."""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flight_recorder.adapters.crm.contracts import CRMRefused
from flight_recorder.adapters.crm.secrets import TokenMaterial, TokenStore

pytestmark = pytest.mark.invariant
BINDING = "a" * 64


def fake(number):
    return TokenMaterial(f"fictional-access-{number}", f"fictional-refresh-{number}", None)


@given(st.lists(st.booleans(), min_size=1, max_size=20))
@settings(deadline=None)
def test_each_cas_changes_one_generation_and_stale_attempt_changes_nothing(actions):
    with tempfile.TemporaryDirectory(
        prefix="fr-secret-generative-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        store = TokenStore(Path(name))
        store.install("fictional", BINDING, fake(0))
        generation = 1
        for index, should_win in enumerate(actions, 1):
            before = {p.name: p.read_bytes() for p in Path(name).glob("*.json")}
            if should_win:
                result = store.replace(
                    "fictional", fake(index), binding=BINDING, expected_generation=generation
                )
                generation += 1
                assert result.generation == generation
                assert store.load("fictional", BINDING) == fake(index)
            else:
                with pytest.raises(CRMRefused, match="^stale_token_generation$"):
                    store.replace(
                        "fictional",
                        fake(index),
                        binding=BINDING,
                        expected_generation=generation + 1,
                    )
                assert {p.name: p.read_bytes() for p in Path(name).glob("*.json")} == before
            assert store.status("fictional", BINDING).generation == generation
        store.close()


@given(
    st.one_of(st.booleans(), st.none(), st.text(max_size=10), st.floats(), st.integers(max_value=0))
)
@settings(deadline=None)
def test_non_exact_positive_generation_cannot_mutate_store(generation):
    with tempfile.TemporaryDirectory(
        prefix="fr-secret-types-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        store = TokenStore(name)
        store.install("fictional", BINDING, fake(0))
        before = next(Path(name).glob("*.json")).read_bytes()
        with pytest.raises(CRMRefused, match="^stale_token_generation$"):
            store.begin_refresh("fictional", BINDING, expected_generation=generation)
        assert next(Path(name).glob("*.json")).read_bytes() == before
        assert store.load("fictional", BINDING) == fake(0)
        store.close()


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/ .", min_size=1, max_size=64))
@settings(deadline=None)
def test_exact_connection_identity_never_uses_path_components(connection):
    with tempfile.TemporaryDirectory(
        prefix="fr-secret-identity-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        store = TokenStore(name)
        store.install(connection, BINDING, fake(0))
        assert store.load(connection, BINDING) == fake(0)
        assert len(list(Path(name).glob("*.json"))) == 1
        with pytest.raises(CRMRefused, match="^token_material_missing$"):
            store.load(connection + "different", BINDING)
        store.close()


@given(st.booleans(), st.integers(min_value=1, max_value=10**20))
@settings(deadline=None)
def test_refresh_claim_is_single_use_even_after_reopen(finish, salt):
    with tempfile.TemporaryDirectory(
        prefix="fr-secret-refresh-", dir=Path(tempfile.gettempdir()).resolve()
    ) as name:
        store = TokenStore(name)
        store.install("fictional", BINDING, fake(0))
        claim = store.begin_refresh("fictional", BINDING, expected_generation=1)
        store.close()
        reopened = TokenStore(name)
        with pytest.raises(CRMRefused, match="^token_state_blocked$"):
            reopened.load("fictional", BINDING)
        result = (
            reopened.finish_refresh(claim, fake(salt)) if finish else reopened.fail_refresh(claim)
        )
        assert result.generation == 3
        before = next(Path(name).glob("*.json")).read_bytes()
        for action in [
            lambda: reopened.fail_refresh(claim),
            lambda: reopened.finish_refresh(claim, fake(salt + 1)),
        ]:
            with pytest.raises(CRMRefused, match="^stale_token_generation$"):
                action()
        assert next(Path(name).glob("*.json")).read_bytes() == before
        reopened.close()
