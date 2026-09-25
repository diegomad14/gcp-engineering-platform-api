"""Failover for GitHub dispatches that are accepted but never start."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from eng_platform_api.config import config, load_config
from eng_platform_api.models import DeploymentItem
from eng_platform_api.services import (
    cloud_build,
    deployment_commands,
    deployment_executions,
    deployment_store,
    executor_circuits,
    github_deployments,
    release_cloud_build,
    release_executions,
    release_reconciler,
)

tmp_store = Path(tempfile.mkdtemp(prefix="failover_test_")) / "deployments.json"


@pytest.fixture(autouse=True)
def isolated_state():
    with (
        mock.patch(
            "eng_platform_api.services.deployment_store._DEFAULT_STORE_PATH", tmp_store
        ),
        mock.patch("eng_platform_api.services.deployment_store._COLLECTION", ""),
    ):
        tmp_store.unlink(missing_ok=True)
        deployment_executions._memory.clear()
        executor_circuits._memory.clear()
        release_executions._memory.clear()
        yield
        tmp_store.unlink(missing_ok=True)


def _stalled(item_id: str = "42", *, minutes_ago: int = 5) -> DeploymentItem:
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return DeploymentItem(
        id=item_id,
        service_name="cgm-artemis-web",
        repository="diegomad14/cgm-artemis-web",
        tag="v1.37.1",
        sha="a" * 40,
        status="QUEUED",
        current_stage="queued",
        github_deployment_id=int(item_id),
        created_at=created,
        updated_at=created,
    )


def _execution_for(item: DeploymentItem) -> None:
    deployment_executions.reserve(
        item.id,
        provider="github_actions",
        fingerprint="fingerprint",
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )


def test_stalled_dispatch_falls_back_to_cloud_build(monkeypatch):
    item = _stalled()
    deployment_store.save(item, "failover-key")
    _execution_for(item)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(config.cloud_build, "enabled_services", ("cgm-artemis-web",))
    monkeypatch.setattr(
        config.cloud_build, "cloud_build_only_services", ("cgm-artemis-web",)
    )
    submitted = item.model_copy(
        update={"status": "QUEUED", "current_stage": "queued", "error": ""}
    )
    with (
        mock.patch.object(github_deployments, "refresh", return_value=item),
        mock.patch.object(
            deployment_commands.github_release_control,
            "repository_is_private",
            return_value=True,
        ),
        mock.patch.object(
            deployment_commands.github_release_control,
            "set_repository_execution_mode",
        ),
        mock.patch.object(deployment_commands, "_require_release_quality"),
        mock.patch.object(deployment_commands, "_require_orchestrated_release"),
        mock.patch.object(cloud_build, "submit", return_value=submitted) as submit,
        mock.patch.object(github_deployments, "set_managed_status"),
    ):
        result = deployment_commands.reconcile_stalled_dispatches()

    assert result["reconciled"] == 1
    assert result["items"][0]["result"] == "cloud_build_fallback"
    submit.assert_called_once()
    assert executor_circuits.is_open("diegomad14") is True
    assert deployment_store.idempotency_key_for(item.id) == "failover-key"


def test_stalled_dispatch_without_fallback_is_surfaced(monkeypatch):
    item = _stalled("43")
    deployment_store.save(item, "no-fallback-key")
    _execution_for(item)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(config.cloud_build, "enabled_services", ("other-service",))
    with (
        mock.patch.object(github_deployments, "refresh", return_value=item),
        mock.patch.object(cloud_build, "submit") as submit,
    ):
        result = deployment_commands.reconcile_stalled_dispatches()

    assert result["items"][0]["result"] == "no_fallback_configured"
    submit.assert_not_called()
    assert deployment_store.get(item.id).status == "FAILED"


def test_recent_dispatch_is_left_alone(monkeypatch):
    item = _stalled("44", minutes_ago=0)
    deployment_store.save(item, "recent-key")
    _execution_for(item)
    with mock.patch.object(github_deployments, "refresh", return_value=item):
        result = deployment_commands.reconcile_stalled_dispatches()
    assert result["reconciled"] == 0
    assert deployment_store.get(item.id).status == "QUEUED"


def _waiting_execution() -> dict:
    fingerprint = release_executions.fingerprint(
        repository="diegomad14/cgm-artemis-web",
        service_name="cgm-artemis-web",
        operation="pr_quality",
        head_sha="a" * 40,
        base_sha="b" * 40,
        profile_hash="profile-v1",
        executor_digest="quality@sha256:" + "c" * 64,
        policy_hash="policy-v1",
        planner_hash="",
    )
    execution, _ = release_executions.reserve(
        fingerprint_value=fingerprint,
        repository="diegomad14/cgm-artemis-web",
        service_name="cgm-artemis-web",
        operation="pr_quality",
        head_sha="a" * 40,
        base_sha="b" * 40,
        branch="feature/test",
        profile_hash="profile-v1",
        executor_digest="quality@sha256:" + "c" * 64,
        policy_hash="policy-v1",
        delivery_id="delivery-1",
    )
    stale = (
        datetime.now(timezone.utc)
        - timedelta(
            seconds=config.release_orchestrator.release_dispatch_timeout_seconds + 60
        )
    ).isoformat()
    return release_executions.save(execution["execution_id"], created_at=stale)


def test_waiting_release_times_out_to_cloud_build():
    execution = _waiting_execution()
    moved = dict(execution, provider="cloud_build", status="running_quality")
    with mock.patch.object(release_cloud_build, "submit", return_value=moved) as submit:
        result = release_reconciler.reconcile(execution["execution_id"])

    submit.assert_called_once()
    assert result["provider"] == "cloud_build"
    assert executor_circuits.is_open("diegomad14") is True


def test_monthly_usage_counts_deployment_minutes():
    deployment_executions._memory["x"] = {
        "provider": "cloud_build",
        "build_finished_at": "2026-09-25T18:00:00+00:00",
        "build_minutes_estimate": 4.5,
    }
    usage = release_executions.monthly_cloud_build_usage("2026-09")
    assert usage["deployment_minutes"] == 4.5
    assert usage["total_minutes"] >= 4.5


def test_dispatch_timeouts_must_be_positive(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_DEPLOY_DISPATCH_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        load_config()
