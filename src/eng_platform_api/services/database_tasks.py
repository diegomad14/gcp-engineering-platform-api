"""Cloud Tasks dispatch contains opaque IDs only, with fixed OIDC authority."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from ..config import config
from .database_registry import DatabaseUnavailable

_test_dispatcher: Any = None


def enqueue(kind: str, identity: str, *, schedule_at: float | None = None) -> None:
    if kind not in {"query", "export", "sort", "cleanup"} or not re.fullmatch(
        r"[a-f0-9]{32}", identity
    ):
        raise DatabaseUnavailable("Invalid database task identity")
    try:
        if _test_dispatcher is not None:
            _test_dispatcher(kind, identity, schedule_at)
            return
        from google.cloud import tasks_v2

        settings = config.databases
        queue = getattr(settings, f"{'export' if kind == 'sort' else kind}_queue")
        if not all(
            (
                settings.project_id,
                settings.queue_location,
                queue,
                settings.worker_service_account,
                settings.api_origin,
            )
        ) or not settings.api_origin.startswith("https://"):
            raise DatabaseUnavailable("Database task dispatch is unavailable")
        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(settings.project_id, settings.queue_location, queue)
        suffix = (
            f"-expiry-{math.ceil(schedule_at)}"
            if kind == "cleanup" and schedule_at is not None
            else ""
        )
        task: dict[str, Any] = {
            "name": f"{parent}/tasks/{kind}-{identity}{suffix}",
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{settings.api_origin}/api/internal/database-executions/{kind}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"id": identity}).encode(),
                "oidc_token": {
                    "service_account_email": settings.worker_service_account,
                    "audience": settings.api_origin,
                },
            },
            "dispatch_deadline": {"seconds": 275},
        }
        if schedule_at is not None:
            task["schedule_time"] = {"seconds": math.ceil(schedule_at)}
        try:
            client.create_task(
                request={"parent": parent, "task": task}, timeout=4, retry=None
            )
        except Exception as exc:
            if exc.__class__.__name__ != "AlreadyExists":
                raise
    except Exception:
        raise DatabaseUnavailable("Database task dispatch is unavailable") from None
