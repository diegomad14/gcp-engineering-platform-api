"""Read-only Cloud Build inventory; never imports a submit or release reconciler.

Only the private ledger is written. Functional execution states are untouched.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from ..config import config
from . import cloud_build_usage_store as store
from . import deployment_executions, release_executions
from . import gcp_billing_bigquery as billing
from .cloud_build_policy import validate_submission

logger = logging.getLogger(__name__)
API = "https://cloudbuild.googleapis.com/v1"
REGIONS = ("us-central1", "global")
TERMINAL = {"SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT", "INTERNAL_ERROR", "EXPIRED"}
# Retained for historical/external builds, never for generating submissions.
RATES = {
    "DEFAULT": 0.006,
    "E2_STANDARD_2": 0.006,
    "E2_HIGHCPU_8": 0.0156,
    "E2_HIGHCPU_32": 0.0624,
    "E2_MEDIUM": 0.003,
}


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Build timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def _execution_link(build: dict[str, Any]) -> tuple[str, str]:
    substitutions = build.get("substitutions", {})
    for field, category, loader, sha_field in (
        ("_DEPLOYMENT_ID", "deployment", deployment_executions.get, "sha"),
        ("_EXECUTION_ID", "release_quality", release_executions.get, "head_sha"),
    ):
        execution_id = str(substitutions.get(field, ""))
        if not execution_id:
            continue
        execution = loader(execution_id) or {}
        source = build.get("source", {}).get("connectedRepository", {})
        build_ids = {
            execution.get("build_id"),
            *(a.get("build_id") for a in execution.get("build_attempts", [])),
        }
        if (
            execution.get("provider") == "cloud_build"
            and build.get("id") in build_ids
            and execution.get("fingerprint")
            and substitutions.get("_REQUEST_FINGERPRINT") == execution["fingerprint"]
            and substitutions.get("_SERVICE_NAME") == execution.get("service_name")
            and substitutions.get("_REPOSITORY") == execution.get("repository")
            # Historical connections can be renamed/replaced. The bound build
            # ID, fingerprint, repository, service and exact SHA are authoritative.
            and source.get("repository")
            and source.get("revision") == execution.get(sha_field)
        ):
            return category, execution_id
    return "other", ""


def normalize(
    project: str, region: str, build: dict[str, Any], now: datetime
) -> dict[str, Any]:
    build_id = str(build.get("id", ""))
    if not build_id or "/" in build_id or build.get("projectId", project) != project:
        raise ValueError("Build identity does not match the inventory project")
    name = f"projects/{project}/locations/{region}/builds/{build_id}"
    if build.get("name", name) != name:
        # Cloud Build returns numeric project names even when queried by ID.
        # Its projectId must independently match; location and build ID stay exact.
        parts = str(build.get("name", "")).split("/")
        if not (
            len(parts) == 6
            and parts[0] == "projects"
            and parts[1].isdigit()
            and build.get("projectId") == project
            and parts[2:] == ["locations", region, "builds", build_id]
        ):
            raise ValueError("Build location does not match inventory")
    category, execution_id = _execution_link(build)
    started, finished = build.get("startTime"), build.get("finishTime")
    terminal = build.get("status") in TERMINAL
    complete = bool(terminal and (finished or not started))
    allocations: dict[str, float] = {}
    seconds = 0.0
    if started and finished:
        start, end = parse(started), parse(finished)
        if end < start:
            raise ValueError("Build finish precedes start")
        seconds = (end - start).total_seconds()
        cursor = start
        while cursor < end:
            month = cursor.strftime("%Y-%m")
            next_month = month_bounds(month)[1]
            stop = min(end, next_month)
            allocations[month] = (stop - cursor).total_seconds() / 60
            cursor = stop
    # Keep zero-duration/pending builds visible in their creation month.
    if not allocations:
        allocations[parse(str(build["createTime"])).strftime("%Y-%m")] = 0.0
    options = build.get("options", {})
    machine = options.get("machineType", "DEFAULT")
    # Build responses include an empty pool object even when no pool was requested.
    policy_request = {
        "timeout": build.get("timeout", "1800s"),
        "options": dict(options),
    }
    if not options.get("pool"):
        policy_request["options"].pop("pool", None)
    try:
        validate_submission(policy_request)
        violation = False
    except ValueError:
        violation = True
    return {
        "kind": "build",
        "key": hashlib.sha256(name.encode()).hexdigest(),
        "project_id": project,
        "region": region,
        "build_id": build_id,
        "machine_type": machine,
        "status": str(build.get("status", "UNKNOWN")),
        "created_at": build["createTime"],
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": seconds,
        "monthly_minutes": allocations,
        "complete": complete,
        "category": category,
        "execution_id": execution_id,
        "policy_violation": violation,
        "minute_price_usd": RATES.get(machine),
        "observed_at": now.isoformat(),
    }


class Inventory:
    def __init__(self, project: str):
        self.project = project
        credentials, _ = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        self.session = AuthorizedSession(credentials)

    def page(
        self, region: str, since: str, until: str, token: str = ""
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{API}/projects/{self.project}/locations/{region}/builds",
            params={
                "filter": f'create_time>="{since}" AND create_time<="{until}"',
                "pageSize": "100",
                "pageToken": token,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get(self, region: str, build_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{API}/projects/{self.project}/locations/{region}/builds/{build_id}",
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


def aggregate(month: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    minutes = {"deployment": 0.0, "release_quality": 0.0, "other": 0.0}
    estimates = dict.fromkeys(minutes, 0.0)
    counts = {
        "build_count": 0,
        "pending_build_count": 0,
        "policy_violation_count": 0,
        "unpriced_build_count": 0,
    }
    for row in rows:
        if month not in row["monthly_minutes"]:
            continue
        counts["build_count"] += 1
        counts["policy_violation_count"] += int(row["policy_violation"])
        if not row["complete"]:
            counts["pending_build_count"] += 1
            continue
        value = row["monthly_minutes"][month]
        category = row["category"]
        minutes[category] += value
        rate = row.get("minute_price_usd")
        if rate is None:
            counts["unpriced_build_count"] += 1
        else:
            estimates[category] += value * rate
    project_minutes = sum(minutes.values())
    thresholds = list(config.release_orchestrator.usage_alert_minutes)
    return {
        "month": month,
        "project_id": config.cloud_build.project_id,
        "release_minutes": round(minutes["release_quality"], 3),
        "deployment_minutes": round(minutes["deployment"], 3),
        "total_minutes": round(minutes["deployment"] + minutes["release_quality"], 3),
        "other_minutes": round(minutes["other"], 3),
        "project_minutes": round(project_minutes, 3),
        "estimated_cost_usd": round(
            estimates["deployment"] + estimates["release_quality"], 6
        ),
        "project_estimated_cost_usd": round(sum(estimates.values()), 6),
        "minute_price_usd": 0.006,
        "alert_scope": "project",
        "alert_thresholds": thresholds,
        "alert_thresholds_reached": [t for t in thresholds if project_minutes >= t],
        **counts,
    }


def query_billing(project: str, month: str) -> dict[str, Any]:
    table = billing._billing_table_exists()
    if not table:
        raise ValueError("Billing export is unavailable")
    start, end = month_bounds(month)
    query = f"""
    SELECT SUM(cost) AS cost,
      SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
      MAX(export_time) AS export_time
    FROM `{table}`
    WHERE project.id = @project AND service.description = 'Cloud Build'
      AND usage_start_time >= @start AND usage_start_time < @end
      AND cost_type = 'regular'
    """
    params = [
        bigquery.ScalarQueryParameter("project", "STRING", project),
        bigquery.ScalarQueryParameter("start", "TIMESTAMP", start),
        bigquery.ScalarQueryParameter("end", "TIMESTAMP", end),
    ]
    row = next(
        iter(
            bigquery.Client(project=project)
            .query(query, job_config=bigquery.QueryJobConfig(query_parameters=params))
            .result()
        )
    )
    cost, credits = float(row.cost or 0), float(row.credits or 0)
    return {
        "billed_cost_usd": round(cost, 6),
        "billed_credits_usd": round(credits, 6),
        "billed_net_cost_usd": round(cost + credits, 6),
        "billing_exported_at": row.export_time.isoformat() if row.export_time else None,
    }


def refresh_billing(month: str, now: datetime) -> None:
    key = "_billing-" + month
    if not store.claim(key, now, 3600):
        return
    try:
        value = query_billing(config.cloud_build.project_id, month)
        store.update(
            key,
            lambda old: {
                **old,
                **value,
                "updated_at": now.isoformat(),
                "failed": False,
            },
        )
    except Exception:
        store.update(key, lambda old: {**old, "failed": True})
        logger.warning("cloud_build_billing_unavailable month=%s", month)


def materialize(
    month: str, now: datetime, *, complete_inventory: bool
) -> dict[str, Any]:
    value = aggregate(month, store.rows())
    billing_value = store.get("_billing-" + month) or {}
    for key in (
        "billed_cost_usd",
        "billed_credits_usd",
        "billed_net_cost_usd",
        "billing_exported_at",
    ):
        value[key] = billing_value.get(key)
    value.update(
        updated_at=now.isoformat(),
        data_status="fresh" if complete_inventory else "partial",
        billing_updated_at=billing_value.get("updated_at"),
        billing_data_status=("stale" if billing_value.get("failed") else "fresh")
        if billing_value.get("updated_at")
        else "unavailable",
    )
    store.update(
        "_summary-" + month,
        lambda old: value if old.get("updated_at", "") <= value["updated_at"] else old,
    )
    for threshold in value["alert_thresholds_reached"]:
        key = f"_alert-{month}-{threshold}"
        if store.claim(key, now, 370 * 86400):
            logger.warning(
                "cloud_build_usage_threshold month=%s threshold_minutes=%s observed_minutes=%.3f",
                month,
                threshold,
                value["project_minutes"],
            )
    return value


_last_good: dict[str, dict[str, Any]] = {}


def summary(month: str, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or store.utc_now()
    try:
        value = store.get("_summary-" + month)
    except Exception:
        value = _last_good.get(month)
        return {**value, "data_status": "stale"} if value else None
    if value:
        value = dict(value)
        if (now - parse(value["updated_at"])).total_seconds() > 1800:
            value["data_status"] = "stale"
        if (
            value.get("billing_updated_at")
            and (now - parse(value["billing_updated_at"])).total_seconds() > 7200
        ):
            value["billing_data_status"] = "stale"
        _last_good[month] = value
    return value


def reconcile() -> dict[str, Any]:
    """Bounded scheduler sweep; independent of feature flag and execution status."""
    now = store.utc_now()
    if config.mock_mode or not config.cloud_build.enabled:
        return {"skipped": True}
    month = now.strftime("%Y-%m")
    touched = {month}
    complete = True
    try:
        if not store.claim("_collector", now, 900):
            return {"skipped": True}
        inventory = Inventory(config.cloud_build.project_id)
        for region in REGIONS:
            key = "_cursor-" + region
            state = store.get(key) or {}
            if not state.get("until"):
                watermark = (
                    parse(state["watermark"]) - timedelta(hours=24)
                    if state.get("watermark")
                    else month_bounds(month)[0]
                )
                state = {
                    "since": watermark.isoformat(),
                    "until": now.isoformat(),
                    "token": "",
                }
            for _ in range(5):
                page = inventory.page(
                    region, state["since"], state["until"], state.get("token", "")
                )
                for build in page.get("builds", []):
                    row = normalize(inventory.project, region, build, now)
                    store.put_build(row)
                    touched.update(row["monthly_minutes"])
                token = page.get("nextPageToken", "")
                state = (
                    {**state, "token": token}
                    if token
                    else {"watermark": state["until"]}
                )
                store.update(key, lambda _old: state)
                if not token:
                    break
            if state.get("token"):
                complete = False
        pending = sorted(
            (row for row in store.rows() if not row["complete"]),
            key=lambda row: row["observed_at"],
        )
        for row in pending[:100]:
            build = inventory.get(row["region"], row["build_id"])
            updated = normalize(inventory.project, row["region"], build, now)
            store.put_build(updated)
            touched.update(updated["monthly_minutes"])
        for target in touched:
            refresh_billing(target, now)
            materialize(target, now, complete_inventory=complete)
        return {"reconciled": True, "complete": complete}
    except Exception:
        logger.exception("cloud_build_usage_reconciliation_failed")
        return {"reconciled": False}


def backfill(month: str, *, apply: bool = False) -> dict[str, Any]:
    """Read provider inventory. Dry-run never writes ledger, checkpoints or billing."""
    now = store.utc_now()
    start, end = month_bounds(month)
    inventory = Inventory(config.cloud_build.project_id)
    rows: dict[str, dict[str, Any]] = {}
    for region in REGIONS:
        token = ""
        while True:
            page = inventory.page(
                region,
                (start - timedelta(days=2)).isoformat(),
                min(now, end).isoformat(),
                token,
            )
            for build in page.get("builds", []):
                row = normalize(inventory.project, region, build, now)
                rows[row["key"]] = row
            token = page.get("nextPageToken", "")
            if not token:
                break
    result = aggregate(month, list(rows.values()))
    if apply:
        for row in rows.values():
            store.put_build(row)
        refresh_billing(month, now)
        result = materialize(month, now, complete_inventory=True)
    return {"applied": apply, **result}
