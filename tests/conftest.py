"""Explicit test operators; production has no implicit authorized user."""

import pytest

from eng_platform_api.config import config


@pytest.fixture(autouse=True)
def configured_test_operator(monkeypatch):
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
