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

    if request.module.__name__.split(".")[-1] == "test_private_metadata_access":
        from eng_platform_api.services import mcp_store

        monkeypatch.setattr(mcp_store, "_collection", lambda _: None)

    monkeypatch.setattr(config.mcp, "enabled", True)
    with database_job_store.testing_backend(Control()) as backend:
        yield backend


@pytest.fixture(autouse=True)
def approved_cloud_build_transport_tests(request, monkeypatch):
    # Existing transport/state suites explicitly simulate a future approved
    # activation. Runtime and pause-policy tests remain closed by default.
    if request.module.__name__.split(".")[-1] in {
        "test_cloud_build",
        "test_cloud_build_execution",
        "test_deployment_selector",
        "test_deployment_executions",
        "test_release_cloud_build",
        "test_release_concurrency",
        "test_release_executions",
        "test_release_orchestrator",
        "test_release_quality_bootstrap",
        "test_release_quality_bootstrap_store",
        "test_release_operations",
        "test_release_orchestrator_routes",
        "test_release_reconciler",
        "test_github_rejection_policy",
        "test_private_executor_policy",
        "test_explicit_provider_selection",
    }:
        monkeypatch.setattr(config.cloud_build, "enabled", True)
        monkeypatch.setattr(config.cloud_build, "new_builds_enabled", True)
