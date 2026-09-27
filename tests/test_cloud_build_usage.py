from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from eng_platform_api.config import config
from eng_platform_api.routers import costs
from eng_platform_api.services import cloud_build_usage as usage
from eng_platform_api.services import cloud_build_usage_store as store

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
QUERY_BILLING = usage.query_billing


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    store._memory.clear()
    usage._last_good.clear()
    monkeypatch.setattr(store, "_collection", lambda: None)
    monkeypatch.setattr(store, "utc_now", lambda: NOW)
    monkeypatch.setattr(config.cloud_build, "project_id", "test-project")
    monkeypatch.setattr(
        usage,
        "query_billing",
        lambda *_: {
            "billed_cost_usd": 7.54,
            "billed_credits_usd": -1.0,
            "billed_net_cost_usd": 6.54,
        },
    )


def build(**updates):
    return {
        "id": "build-1",
        "projectId": "test-project",
        "status": "SUCCESS",
        "createTime": "2026-09-25T00:00:00Z",
        "startTime": "2026-09-25T00:01:00Z",
        "finishTime": "2026-09-25T00:11:00Z",
        "timeout": "1800s",
        "options": {"logging": "CLOUD_LOGGING_ONLY", "pool": {}},
        **updates,
    }


def normalized(**updates):
    return usage.normalize("test-project", "us-central1", build(**updates), NOW)


@pytest.mark.parametrize("status", sorted(usage.TERMINAL))
def test_all_terminal_outcomes_are_metered_even_without_engine_callbacks(status):
    row = normalized(status=status)
    store.put_build(row)
    store.put_build(row)
    summary = usage.aggregate("2026-09", store.rows())
    assert summary["build_count"] == 1
    assert summary["other_minutes"] == 10
    assert summary["project_minutes"] == 10
    assert summary["project_estimated_cost_usd"] == 0.06
    assert summary["pending_build_count"] == 0
    assert row["policy_violation"] is False


@pytest.mark.parametrize("status", ["WORKING", "SUCCESS"])
def test_final_callback_without_finish_is_pending_then_completed(status):
    row = normalized(status=status, finishTime=None)
    store.put_build(row)
    assert usage.aggregate("2026-09", store.rows())["pending_build_count"] == 1
    store.put_build(normalized())
    # Late callback/poll may not downgrade a metered build.
    store.put_build({**row, "observed_at": (NOW + timedelta(minutes=1)).isoformat()})
    assert usage.aggregate("2026-09", store.rows())["project_minutes"] == 10


def test_never_started_build_has_zero_duration_and_no_false_spending():
    row = normalized(status="EXPIRED", startTime=None, finishTime=None)
    assert row["complete"]
    assert row["monthly_minutes"] == {"2026-09": 0}


def test_numeric_resource_name_uses_verified_project_id():
    name = "projects/546821492326/locations/us-central1/builds/build-1"
    assert normalized(name=name)["key"] == normalized()["key"]
    with pytest.raises(ValueError):
        normalized(name=name.replace("us-central1", "global"))


def test_attempts_allocate_to_each_utc_month_and_unknown_price_is_visible():
    row = normalized(
        startTime="2026-09-30T23:59:00Z",
        finishTime="2026-10-01T00:02:00Z",
        options={"machineType": "UNKNOWN"},
    )
    retry = normalized(
        id="retry",
        createTime="2026-10-01T00:00:00Z",
        startTime="2026-10-01T00:03:00Z",
        finishTime="2026-10-01T00:04:00Z",
    )
    assert row["monthly_minutes"] == {"2026-09": 1, "2026-10": 2}
    result = usage.aggregate("2026-10", [row, retry])
    assert result["project_minutes"] == 3
    assert result["unpriced_build_count"] == 1
    assert result["policy_violation_count"] == 1
    assert usage.aggregate("2026-11", [row])["build_count"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "a/b"},
        {"projectId": "different"},
        {"name": "projects/different/builds/x"},
        {"finishTime": "2026-09-24T00:00:00Z"},
        {"startTime": "2026-09-25T00:00:00"},
    ],
)
def test_invalid_provider_metadata_is_not_metered(changes):
    with pytest.raises(ValueError):
        normalized(**changes)


@pytest.mark.parametrize(
    "category,field,sha_field,module",
    [
        ("deployment", "_DEPLOYMENT_ID", "sha", usage.deployment_executions),
        ("release_quality", "_EXECUTION_ID", "head_sha", usage.release_executions),
    ],
)
def test_execution_link_requires_bound_identity_not_just_tags(
    monkeypatch, category, field, sha_field, module
):
    execution = {
        "provider": "cloud_build",
        "build_id": "new-attempt",
        "previous_build_ids": ["build-1"],
        "fingerprint": "fp",
        "service_name": "service",
        "repository": "owner/repo",
        sha_field: "a" * 40,
    }
    monkeypatch.setattr(module, "get", lambda _: execution)
    values = {
        "substitutions": {
            field: "execution",
            "_REQUEST_FINGERPRINT": "fp",
            "_SERVICE_NAME": "service",
            "_REPOSITORY": "owner/repo",
        },
        "source": {
            "connectedRepository": {
                "repository": "old-connection",
                "revision": "a" * 40,
            }
        },
    }
    row = normalized(**values)
    assert row["category"] == category
    assert row["execution_id"] == "execution"
    assert usage.aggregate("2026-09", [row])["total_minutes"] == 10
    execution["fingerprint"] = "wrong"
    assert normalized(**values)["category"] == "other"
    assert (
        normalized(tags=["eng-platform", "deployment-execution"])["category"] == "other"
    )


def test_ledger_concurrency_old_observation_and_preserved_identity():
    row = {**normalized(), "category": "deployment", "execution_id": "verified"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.put_build, [row] * 32))
    assert len(store.rows()) == 1
    older = {
        **row,
        "observed_at": (NOW - timedelta(days=1)).isoformat(),
        "duration_seconds": 0,
    }
    assert store.put_build(older)["duration_seconds"] == 600
    assert store.put_build(normalized())["category"] == "deployment"
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: store.claim("lease", NOW, 900), range(16))) == 1
    assert store.claim("lease", NOW + timedelta(seconds=900), 900)


class FakeInventory:
    project = "test-project"

    def __init__(self, project):
        assert project == self.project
        self.calls = []

    def page(self, region, since, until, token=""):
        self.calls.append((region, since, until, token))
        if region == "global":
            return {}
        return (
            {"builds": [build()], "nextPageToken": "page-2"}
            if not token
            else {"builds": [build(id="build-2", status="CANCELLED")]}
        )

    def get(self, region, build_id):
        return build(id=build_id)


def test_backfill_dry_run_then_idempotent_apply_does_not_change_executions(monkeypatch):
    monkeypatch.setattr(usage, "Inventory", FakeInventory)
    assert usage.backfill("2026-09")["project_minutes"] == 20
    assert store._memory == {}
    first = usage.backfill("2026-09", apply=True)
    second = usage.backfill("2026-09", apply=True)
    assert first == second
    assert first["billed_net_cost_usd"] == 6.54
    assert usage.summary("2026-09", NOW)["project_minutes"] == 20


def test_scheduler_is_independent_of_read_flag_and_finishes_old_pending(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "usage_enabled", False)
    inventory = FakeInventory("test-project")
    monkeypatch.setattr(usage, "Inventory", lambda _: inventory)
    store.put_build(normalized(id="old-pending", status="WORKING", finishTime=None))
    assert usage.reconcile() == {"reconciled": True, "complete": True}
    assert usage.summary("2026-09", NOW)["project_minutes"] == 30
    assert usage.reconcile() == {"skipped": True}
    assert store.get("_cursor-us-central1")["watermark"] == NOW.isoformat()
    monkeypatch.setattr(store, "utc_now", lambda: NOW + timedelta(minutes=15))
    assert usage.reconcile()["reconciled"]
    assert inventory.calls[3][1] == (NOW - timedelta(hours=24)).isoformat()


def test_scheduler_resumes_checkpoint_after_interrupted_page(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    inventory = FakeInventory("test-project")
    original = inventory.page
    inventory.page = Mock(
        side_effect=[original("us-central1", "", ""), RuntimeError("network")]
    )
    monkeypatch.setattr(usage, "Inventory", lambda _: inventory)
    assert usage.reconcile() == {"reconciled": False}
    assert store.get("_cursor-us-central1")["token"] == "page-2"
    inventory.page = original
    monkeypatch.setattr(store, "utc_now", lambda: NOW + timedelta(minutes=15))
    assert usage.reconcile()["reconciled"]
    assert len(store.rows()) == 2


def test_scheduler_bounds_pages_and_publishes_partial_status(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    inventory = FakeInventory("test-project")
    inventory.page = Mock(return_value={"builds": [build()], "nextPageToken": "more"})
    monkeypatch.setattr(usage, "Inventory", lambda _: inventory)
    assert usage.reconcile() == {"reconciled": True, "complete": False}
    assert inventory.page.call_count == 10
    assert usage.summary("2026-09", NOW)["data_status"] == "partial"


def test_billing_throttles_failures_and_preserves_last_success(monkeypatch):
    usage.refresh_billing("2026-09", NOW)
    loader = Mock(side_effect=RuntimeError("billing down"))
    monkeypatch.setattr(usage, "query_billing", loader)
    usage.refresh_billing("2026-09", NOW + timedelta(minutes=59))
    loader.assert_not_called()
    later = NOW + timedelta(hours=1)
    usage.refresh_billing("2026-09", later)
    usage.refresh_billing("2026-09", later)
    assert loader.call_count == 1
    result = usage.materialize("2026-09", later, complete_inventory=True)
    assert result["billed_net_cost_usd"] == 6.54
    assert result["billing_data_status"] == "stale"


def test_stale_snapshot_and_no_fake_zero_on_store_failure(monkeypatch):
    assert usage.summary("2026-09", NOW) is None
    usage.refresh_billing("2026-09", NOW)
    usage.materialize("2026-09", NOW, complete_inventory=True)
    result = usage.summary("2026-09", NOW + timedelta(hours=3))
    assert result["data_status"] == result["billing_data_status"] == "stale"
    monkeypatch.setattr(store, "get", Mock(side_effect=RuntimeError("offline")))
    assert usage.summary("2026-09", NOW)["data_status"] == "stale"
    assert usage.summary("2026-10", NOW) is None


def test_project_threshold_includes_external_builds_once(monkeypatch):
    row = normalized()
    row["monthly_minutes"]["2026-09"] = 2251
    store.put_build(row)
    warning = Mock()
    monkeypatch.setattr(usage.logger, "warning", warning)
    for _ in range(2):
        result = usage.materialize("2026-09", NOW, complete_inventory=True)
    assert result["total_minutes"] == 0
    assert result["alert_thresholds_reached"] == [2000, 2250]
    assert warning.call_count == 2


def test_router_rollout_flag_and_unavailable_snapshot(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "usage_enabled", False)
    assert costs._cloud_build_usage() is None
    monkeypatch.setattr(config.cloud_build, "usage_enabled", True)
    monkeypatch.setattr(usage, "summary", lambda month: None)
    assert costs._cloud_build_usage() is None


def test_inventory_transport_is_get_only(monkeypatch):
    monkeypatch.setattr(usage, "default", lambda **_: (object(), "p"))
    session = Mock()
    session.get.return_value.json.return_value = {"id": "build-1"}
    monkeypatch.setattr(usage, "AuthorizedSession", lambda _: session)
    inventory = usage.Inventory("test-project")
    inventory.page("global", "since", "until", "page")
    assert session.get.call_args.kwargs["params"]["pageToken"] == "page"
    assert (
        session.get.call_args.kwargs["params"]["filter"]
        == 'create_time>="since" AND create_time<="until"'
    )
    inventory.get("global", "build-1")
    session.post.assert_not_called()
    session.get.return_value.raise_for_status.side_effect = RuntimeError("denied")
    with pytest.raises(RuntimeError):
        inventory.get("global", "build-1")


def test_billing_query_uses_same_utc_usage_window(monkeypatch):
    original = QUERY_BILLING
    monkeypatch.setattr(usage.billing, "_billing_table_exists", lambda: "p.d.t")
    client = Mock()
    client.query.return_value.result.return_value = [
        Mock(cost=2.0, credits=-0.5, export_time=NOW)
    ]
    monkeypatch.setattr(usage.bigquery, "Client", lambda **_: client)
    assert original("test-project", "2026-09")["billed_net_cost_usd"] == 1.5
    sql = client.query.call_args.args[0]
    assert "usage_start_time >= @start" in sql
    assert "_PARTITIONTIME" not in sql
    monkeypatch.setattr(usage.billing, "_billing_table_exists", lambda: None)
    with pytest.raises(ValueError, match="unavailable"):
        original("test-project", "2026-09")
