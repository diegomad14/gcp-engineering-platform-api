"""HMAC-authenticated GitHub App events for CI/release orchestration."""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request

from ..config import config
from ..services import catalog, github_webhooks, release_orchestrator

router = APIRouter(prefix="/api/internal/github", tags=["internal"])


@router.post("/events", include_in_schema=False)
async def github_event(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    if not config.release_orchestrator.enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    length = request.headers.get("content-length", "0")
    try:
        if int(length) > 2_000_000:
            raise HTTPException(status_code=413, detail="GitHub event exceeds 2 MB")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid content length") from exc
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(status_code=413, detail="GitHub event exceeds 2 MB")
    if not github_webhooks.verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid GitHub event JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid GitHub event payload")
    repository = str(payload.get("repository", {}).get("full_name", ""))
    if not repository or not catalog.get_services_by_repository(repository):
        raise HTTPException(status_code=403, detail="Repository is not managed")
    installation_id = str(payload.get("installation", {}).get("id", ""))
    if config.github.installation_id and installation_id != str(
        config.github.installation_id
    ):
        raise HTTPException(status_code=403, detail="GitHub installation mismatch")
    try:
        delivery, created = github_webhooks.receive(
            delivery_id=x_github_delivery,
            event=x_github_event,
            repository=repository,
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created and delivery.get("status") == "processed":
        return {"accepted": True, "duplicate": True}
    try:
        if x_github_event == "pull_request":
            executions = release_orchestrator.handle_pull_request(
                payload, delivery_id=x_github_delivery
            )
        elif x_github_event == "push":
            executions = release_orchestrator.handle_push(
                payload, delivery_id=x_github_delivery
            )
        elif x_github_event == "workflow_run":
            executions = release_orchestrator.handle_workflow_run(
                payload, delivery_id=x_github_delivery
            )
        else:
            raise HTTPException(status_code=400, detail="Unsupported GitHub event")
    except release_orchestrator.ReleaseOrchestratorError as exc:
        github_webhooks.complete(
            x_github_delivery, outcome="rejected", error=str(exc)[:500]
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    github_webhooks.complete(
        x_github_delivery,
        outcome="accepted",
        execution_ids=[item["execution_id"] for item in executions],
    )
    return {
        "accepted": True,
        "duplicate": not created,
        "execution_ids": [item["execution_id"] for item in executions],
    }
