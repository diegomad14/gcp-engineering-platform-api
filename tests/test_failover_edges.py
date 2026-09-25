"""Edge coverage for the failover, cost and preflight accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eng_platform_api.config import config, load_config
from eng_platform_api.main import app
from eng_platform_api.models import CostPeriod, CostSummary, DeploymentItem
from eng_platform_api.services import (
    cloud_build,
    deployment_executions,
    deployment_store,
    github_actions_quota,
)

client = TestClient(app)
tmp_store = Path(tempfile.mkdtemp(prefix="failover_edges_")) / "deployments.json"


@pytest.fixture(autouse=True)
def isolated_store():
    with (
        mock.patch(
            "eng_platform_api.services.deployment_store._DEFAULT_STORE_PATH", tmp_store
        ),
        mock.patch("eng_platform_api.services.deployment_store._COLLECTION", ""),
    ):
        tmp_store.unlink(missing_ok=True)
        deployment_executions._memory.clear()
        yield
        tmp_store.unlink(missing_ok=True)


def _item(item_id: str = "90") -> DeploymentItem:
    created = datetime.now(timezone.utc).isoformat()
    return DeploymentItem(
        id=item_id,
        service_name="cgm-artemis-api",
        repository="diegomad14/cgm-artemis-api",
        tag="v1.42.0",
        sha="a" * 40,
        status="QUEUED",
        current_stage="queued",
        created_at=created,
        updated_at=created,
    )


def test_unfinished_listing_and_key_lookup():
    running = _item("91")
    done = _item("92").model_copy(update={"status": "SUCCEEDED"})
    deployment_store.save(running, "running-key")
    deployment_store.save(done, "done-key")

    unfinished = deployment_store.list_unfinished()
    assert [item.id for item in unfinished] == ["91"]
    assert deployment_store.idempotency_key_for("91") == "running-key"
    assert deployment_store.idempotency_key_for("missing") == ""


def test_unfinished_listing_reads_firestore_records():
    class _Snapshot:
        def __init__(self, value):
            self.value = value

        def to_dict(self):
            return dict(self.value)

    class _Collection:
        def stream(self):
            return [_Snapshot({**_item("93").model_dump(), "idempotency_key": "k"})]

        def document(self, key):
            record = {**_item("93").model_dump(), "idempotency_key": "k"}
            return mock.Mock(get=lambda: mock.Mock(exists=True, to_dict=lambda: record))

    with mock.patch.object(
        deployment_store, "_firestore_collection", return_value=_Collection()
    ):
        assert [item.id for item in deployment_store.list_unfinished()] == ["93"]
        assert deployment_store.idempotency_key_for("93") == "k"


def test_refresh_records_deployment_build_minutes(monkeypatch):
    item = _item("94")
    deployment_executions.reserve(
        item.id,
        provider="cloud_build",
        fingerprint="fingerprint",
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )
    deployment_executions.save(item.id, build_id="build-1")
    started = datetime.now(timezone.utc)
    build = {
        "id": "build-1",
        "status": "SUCCESS",
        "createTime": started.isoformat(),
        "startTime": (started + timedelta(seconds=10)).isoformat(),
        "finishTime": (started + timedelta(seconds=190)).isoformat(),
    }
    with mock.patch.object(cloud_build, "get_build", return_value=build):
        cloud_build.refresh(item)

    record = deployment_executions.get(item.id)
    assert record["build_minutes_estimate"] == 3.0
    assert record["cost_category"] == "deployment"
    assert record["build_finished_at"].startswith("20")


def test_billing_preflight_reports_inconclusive(monkeypatch, caplog):
    monkeypatch.setattr(config.github, "billing_owner", "")
    monkeypatch.setattr(config.github, "token", "")
    with caplog.at_level(logging.WARNING):
        assert github_actions_quota.current_usage(force=True) is None
    assert "github_actions_quota_preflight_inconclusive" in caplog.text

    monkeypatch.setattr(config.github, "billing_owner", "owner")
    monkeypatch.setattr(config.github, "token", "token")
    caplog.clear()
    with (
        mock.patch.object(
            github_actions_quota.httpx, "get", side_effect=RuntimeError()
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert github_actions_quota.current_usage(force=True) is None
    assert "billing_lookup_failed" in caplog.text


def test_cost_summary_exposes_cloud_build_usage(monkeypatch):
    period = CostPeriod(start="2026-09-01", end="2026-09-30", days=30)
    monkeypatch.setattr(
        "eng_platform_api.routers.costs.billing.get_cost_summary",
        lambda **_: CostSummary(period=period),
    )
    response = client.get("/api/costs/summary")
    assert response.status_code == 200
    body = response.json()
    assert "cloud_build" in body
    assert body["cloud_build"]["month"]


def test_paired_web_sha_must_be_a_commit(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_ARTEMIS_WEB_SHA", "not-a-sha")
    with pytest.raises(ValueError):
        load_config()
    monkeypatch.setenv("ENG_PLATFORM_ARTEMIS_WEB_SHA", "d" * 40)
    assert load_config().release_orchestrator.artemis_web_sha == "d" * 40


def test_build_request_carries_the_paired_web_sha(monkeypatch):
    from eng_platform_api.services import catalog

    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "e" * 40)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(config.cloud_build, "project_id", "cgm-assistant-prod")
    monkeypatch.setattr(
        config.cloud_build,
        "service_account",
        "projects/p/serviceAccounts/deploy@p.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        config.cloud_build,
        "executor_image",
        "us-central1-docker.pkg.dev/p/r/executor@sha256:" + "f" * 64,
    )
    monkeypatch.setattr(
        config.cloud_build,
        "repositories",
        {
            "cgm-artemis-api": (
                "projects/p/locations/us-central1/connections/c/repositories/r"
            )
        },
    )
    service = catalog.get_service("cgm-artemis-api")
    item = _item("95")
    request = cloud_build.build_request(item, service)
    env = " ".join(request["steps"][0]["env"])
    assert f"CGM_PAIRED_WEB_SHA={'e' * 40}" in env
