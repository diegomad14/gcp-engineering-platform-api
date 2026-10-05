"""Shared throttling of economic GitHub hints never caches circuit safety."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.models import CatalogService
from eng_platform_api.services import executor_circuits as circuits
from eng_platform_api.services import release_orchestrator as orchestrator


REPOSITORY = "owner/private"


@pytest.fixture
def propagation(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(circuits.time, "time", lambda: now[0])
    monkeypatch.setattr(orchestrator.config.github, "billing_owner", "")
    monkeypatch.setattr(orchestrator.config.cloud_build, "repositories", {})
    service = CatalogService(
        service_name="example-service",
        management_mode="managed",
        repository=REPOSITORY,
        owner="platform",
        project_id="test-project",
        region="us-central1",
    )
    services = mock.Mock(
        return_value=SimpleNamespace(
            services=[service, SimpleNamespace(repository="owner/other")]
        )
    )
    private = mock.Mock(return_value=True)
    set_mode = mock.Mock()
    monkeypatch.setattr(orchestrator.catalog, "get_services", services)
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", private
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "set_repository_execution_mode", set_mode
    )
    return SimpleNamespace(
        now=now, service=service, catalog=services, private=private, set_mode=set_mode
    )


def _open():
    orchestrator._open_circuit(
        repository=REPOSITORY, run_id="42", reason="billing", evidence="quota"
    )


def test_repeated_open_and_provider_calls_skip_catalog_fanout(propagation):
    _open()
    for _ in range(8):
        _open()
        assert orchestrator._provider(propagation.service) == "cloud_build"
    assert propagation.catalog.call_count == 1
    assert propagation.set_mode.call_count == 2
    assert circuits.get("owner")["run_id"] == "42"
    # Success is refreshed eventually, so added repos and later changes recover.
    propagation.now[0] = 1600.0
    assert orchestrator._provider(propagation.service) == "cloud_build"
    assert propagation.catalog.call_count == 2
    assert propagation.set_mode.call_count == 4


def test_partial_failure_is_retried_without_blocking_provider(propagation):
    def fail_one(repository, mode):
        if repository == "owner/other":
            raise RuntimeError("temporary GitHub failure")

    propagation.set_mode.side_effect = fail_one
    _open()
    assert propagation.set_mode.call_count == 2
    assert circuits.is_open("owner")
    propagation.set_mode.side_effect = None
    propagation.now[0] = 1059.0
    assert orchestrator._provider(propagation.service) == "cloud_build"
    assert propagation.set_mode.call_count == 2
    propagation.now[0] = 1060.0
    assert orchestrator._provider(propagation.service) == "cloud_build"
    assert propagation.set_mode.call_count == 4
    propagation.set_mode.assert_any_call("owner/other", "cloud_build")


def test_concurrent_provider_calls_do_not_wait_for_or_duplicate_sweep(propagation):
    circuits.open_circuit("owner", reason="billing")
    entered = Event()
    release = Event()

    def set_mode(repository, mode):
        entered.set()
        assert release.wait(timeout=5)

    propagation.set_mode.side_effect = set_mode
    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(orchestrator._provider, propagation.service)
        try:
            assert entered.wait(timeout=5)
            others = [
                pool.submit(orchestrator._provider, propagation.service)
                for _ in range(4)
            ]
            assert [task.result(timeout=3) for task in others] == ["cloud_build"] * 4
            assert propagation.catalog.call_count == 1
        finally:
            release.set()
        assert first.result(timeout=3) == "cloud_build"
    assert propagation.set_mode.call_count == 2


def test_stale_open_snapshot_cannot_reopen_closed_circuit(propagation):
    stale, _ = circuits.open_circuit("owner", reason="billing")
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
    orchestrator._propagate_open_circuit("owner", stale)
    assert circuits.get("owner") == closed
    propagation.catalog.assert_not_called()
    propagation.set_mode.assert_not_called()


def test_open_circuit_persistence_failure_does_not_write_hints(
    monkeypatch, propagation
):
    monkeypatch.setattr(
        circuits, "open_circuit", mock.Mock(side_effect=RuntimeError("storage error"))
    )
    with pytest.raises(RuntimeError, match="storage error"):
        _open()
    propagation.catalog.assert_not_called()
    propagation.set_mode.assert_not_called()


def test_circuit_read_failure_does_not_select_github(monkeypatch, propagation):
    monkeypatch.setattr(
        circuits, "get", mock.Mock(side_effect=RuntimeError("storage error"))
    )
    with pytest.raises(RuntimeError, match="storage error"):
        orchestrator._provider(propagation.service)
    propagation.set_mode.assert_not_called()


@pytest.mark.parametrize("failure", ["catalog", "claim", "finish"])
def test_hint_infrastructure_failures_preserve_open_provider_and_retry(
    monkeypatch, propagation, failure
):
    circuits.open_circuit("owner", reason="billing")
    with monkeypatch.context() as patch:
        if failure == "catalog":
            patch.setattr(
                orchestrator.catalog,
                "get_services",
                mock.Mock(side_effect=RuntimeError("catalog error")),
            )
        else:
            patch.setattr(
                circuits,
                f"{failure}_mode_propagation",
                mock.Mock(side_effect=RuntimeError("lease error")),
            )
        assert orchestrator._provider(propagation.service) == "cloud_build"
    assert circuits.is_open("owner")
    propagation.now[0] += 300
    assert orchestrator._provider(propagation.service) == "cloud_build"
    propagation.set_mode.assert_any_call(REPOSITORY, "cloud_build")


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
def test_malformed_propagation_metadata_is_repaired_without_changing_provider(
    propagation, metadata
):
    circuits.open_circuit("owner", reason="billing")
    circuits._memory[circuits._id("owner")]["mode_propagation"] = metadata
    assert orchestrator._provider(propagation.service) == "cloud_build"
    assert propagation.catalog.call_count == 1
    assert propagation.set_mode.call_count == 2
    assert circuits.get("owner")["state"] == "open"
    assert circuits.get("owner")["mode_propagation"]["retry_after"] == 1600.0
