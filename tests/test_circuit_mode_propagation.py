"""Circuit evidence never rewrites repository hints or selects another provider."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.models import CatalogService
from eng_platform_api.services import executor_circuits as circuits
from eng_platform_api.services import release_orchestrator as orchestrator

REPOSITORY = "owner/private"


@pytest.fixture
def routing(monkeypatch):
    monkeypatch.setattr(orchestrator.config.cloud_build, "enabled", True)
    monkeypatch.setattr(orchestrator.config.cloud_build, "new_builds_enabled", True)
    monkeypatch.setattr(orchestrator.config.github, "billing_owner", "")
    service = CatalogService(
        service_name="example-service",
        repository=REPOSITORY,
        owner="platform",
        project_id="test-project",
        region="us-central1",
        release={"executor": None},
    )
    catalog, private, hints = mock.Mock(), mock.Mock(), mock.Mock()
    monkeypatch.setattr(orchestrator.catalog, "get_services", catalog)
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", private
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "set_repository_execution_mode", hints
    )
    return SimpleNamespace(
        service=service, catalog=catalog, private=private, hints=hints
    )


def _open():
    orchestrator._open_circuit(
        repository=REPOSITORY,
        run_id="42",
        reason="github_actions_billing_rejection",
        evidence="quota",
    )


@pytest.mark.parametrize("mode", ["auto", "cloud_build", "github_actions"])
@pytest.mark.parametrize("default", [None, "github_actions", "cloud_build"])
def test_service_default_is_independent_of_account_circuit(
    monkeypatch, routing, mode, default
):
    monkeypatch.setattr(
        orchestrator.config.release_orchestrator, "private_executor_mode", mode
    )
    routing.service.release.executor = default
    _open()
    for _ in range(8):
        assert orchestrator._provider(routing.service) == (default or "github_actions")
    assert circuits.get("owner")["run_id"] == "42"
    routing.catalog.assert_not_called()
    routing.private.assert_not_called()
    routing.hints.assert_not_called()


def test_concurrent_default_selection_does_not_wait_for_or_rewrite_hints(routing):
    _open()
    routing.hints.side_effect = AssertionError("No routing mutation")
    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(
            pool.map(lambda _: orchestrator._provider(routing.service), range(40))
        )
    assert values == ["github_actions"] * 40
    routing.catalog.assert_not_called()
    routing.hints.assert_not_called()


def test_open_circuit_persistence_failure_does_not_write_hints(monkeypatch, routing):
    monkeypatch.setattr(
        circuits, "open_circuit", mock.Mock(side_effect=RuntimeError("storage error"))
    )
    with pytest.raises(RuntimeError, match="storage error"):
        _open()
    routing.catalog.assert_not_called()
    routing.hints.assert_not_called()


def test_default_selection_does_not_depend_on_circuit_store(monkeypatch, routing):
    read = mock.Mock(side_effect=RuntimeError("storage error"))
    monkeypatch.setattr(circuits, "get", read)
    assert orchestrator._provider(routing.service) == "github_actions"
    read.assert_not_called()
    routing.hints.assert_not_called()


def test_closed_circuit_cannot_be_reopened_by_default_selection(routing):
    _open()
    circuits.request_probe(
        "owner", repository=REPOSITORY, workflow="health.yml", requested_by="admin"
    )
    circuits.record_probe(
        "owner",
        repository=REPOSITORY,
        workflow="health.yml",
        run_id="43",
        conclusion="success",
        jobs_started=1,
    )
    closed = circuits.close_after_successful_probe("owner", run_id="43")
    assert orchestrator._provider(routing.service) == "github_actions"
    assert circuits.get("owner") == closed
    routing.hints.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "invalid",
        ["invalid"],
        {"retry_after": "invalid"},
        {"retry_after": None},
        {"retry_after": {}},
        {"retry_after": float("nan")},
        {"retry_after": float("inf")},
        {"retry_after": float("-inf")},
        {"retry_after": 10**1000},
        {"retry_after": 10**12},
    ],
)
def test_legacy_propagation_metadata_cannot_change_routing_or_be_rewritten(
    routing, metadata
):
    _open()
    circuits._memory[circuits._id("owner")]["mode_propagation"] = metadata
    before = repr(circuits.get("owner"))
    assert orchestrator._provider(routing.service) == "github_actions"
    assert repr(circuits.get("owner")) == before
    routing.catalog.assert_not_called()
    routing.hints.assert_not_called()


def test_paused_build_policy_does_not_change_github_default(monkeypatch, routing):
    monkeypatch.setattr(orchestrator.config.cloud_build, "new_builds_enabled", False)
    _open()
    assert circuits.is_open("owner")
    assert orchestrator._provider(routing.service) == "github_actions"
    routing.hints.assert_not_called()
