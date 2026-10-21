"""INV-01/04/09/11: canonical identity rejects Salesforce aliases before state."""

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.adapters.crm.test_identity import api, target

pytestmark = pytest.mark.invariant


def suffix(base):
    # Independent specification oracle; never calls the production ID helper.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    return "".join(
        alphabet[sum(1 << j for j, c in enumerate(base[i : i + 5]) if c in string.ascii_uppercase)]
        for i in (0, 5, 10)
    )


@given(st.text(alphabet=string.ascii_letters + string.digits, min_size=12, max_size=12))
def test_only_case_safe_canonical_account_spelling_is_accepted(tail):
    base = "001" + tail
    canonical = base + suffix(base)
    assert target(record_id=canonical).document["record_id"] == canonical
    for alias in (base, canonical[:-1] + ("Z" if canonical[-1] != "Z" else "Y")):
        with pytest.raises(api().CRMRefused):
            target(record_id=alias)
    letter = next((i for i, c in enumerate(base) if c in string.ascii_letters), None)
    if letter is not None:
        alias = canonical[:letter] + canonical[letter].swapcase() + canonical[letter + 1 :]
        with pytest.raises(api().CRMRefused):
            target(record_id=alias)
