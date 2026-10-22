"""Inert placeholder. A future reviewed control increment owns opt-in live execution."""

import pytest


def test_salesforce_live_requires_reviewed_controls_and_exact_external_authority():
    pytest.skip(
        "Live execution is not implemented here. Reviewed controls and fresh exact external "
        "read/write authority are required; environment variables cannot activate this placeholder."
    )
