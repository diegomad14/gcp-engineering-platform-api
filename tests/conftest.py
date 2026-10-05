"""Explicit test operators; production has no implicit authorized user."""

import pytest

from eng_platform_api.config import config
from eng_platform_api.services import (
    executor_circuits,
    github_webhooks,
    release_executions,
)


@pytest.fixture(autouse=True)
def configured_test_operator(monkeypatch):
    # Unit tests must never discover ambient Google credentials or touch a real
    # Firestore project. Individual integration-shape tests opt out explicitly.
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
    executor_circuits._memory.clear()
    github_webhooks._memory.clear()
    release_executions._memory.clear()


@pytest.fixture(autouse=True)
def mcp_durable_test_store(request, monkeypatch):
    if request.module.__name__.split(".")[-1] not in {
        "test_mcp",
        "test_mcp_cost_consent",
        "test_private_metadata_access",
        "test_cost_alerts",
        "test_mcp_database",
    }:
        yield
        return
    from eng_platform_api.services import database_job_store
    from tests.mcp_helpers import Control

    monkeypatch.setattr(config.mcp, "enabled", True)
    with database_job_store.testing_backend(Control()) as backend:
        yield backend
