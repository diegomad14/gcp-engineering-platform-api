"""Authenticated engine callbacks, intentionally absent from the public schema."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException

from ..config import config
from ..models import DeploymentExecutionEvent, DeploymentStatus
from ..services import (
    cloud_build,
    deployment_executions,
    deployment_store,
    github_deployments,
)

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _verify_identity(authorization: str | None) -> None:
    if config.mock_mode:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Cloud Build identity is required")
    if not config.cloud_build.callback_service_account:
        raise HTTPException(
            status_code=503, detail="Cloud Build callback identity is not configured"
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_oauth2_token

        audience = config.github.platform_api_url
        claims = verify_oauth2_token(
            authorization.removeprefix("Bearer "), Request(), audience
        )
        if claims.get("email") != config.cloud_build.callback_service_account:
            raise ValueError("unexpected service account")
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid Cloud Build identity"
        ) from exc


@router.post("/deployments/{deployment_id}/events", include_in_schema=False)
def accept_event(
    deployment_id: str,
    payload: DeploymentExecutionEvent,
    authorization: str | None = Header(default=None),
):
    _verify_identity(authorization)
    execution = deployment_executions.get(deployment_id)
    item = deployment_store.get(deployment_id)
    if execution is None or item is None:
        raise HTTPException(status_code=404, detail="Unknown deployment execution")
    if execution.get("provider") != "cloud_build":
        raise HTTPException(
            status_code=409, detail="Deployment is not Cloud Build managed"
        )
    if execution.get("fingerprint") != payload.fingerprint:
        raise HTTPException(status_code=403, detail="Execution fingerprint mismatch")
    if execution.get("build_id") != payload.build_id:
        raise HTTPException(status_code=403, detail="Cloud Build identity mismatch")
    try:
        build = cloud_build.get_build(payload.build_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Unable to verify Cloud Build"
        ) from exc
    substitutions = build.get("substitutions", {})
    if (
        substitutions.get("_REQUEST_FINGERPRINT") != payload.fingerprint
        or substitutions.get("_DEPLOYMENT_ID") != deployment_id
        or substitutions.get("_RELEASE_SHA") != item.sha
    ):
        raise HTTPException(
            status_code=403, detail="Cloud Build does not match deployment"
        )
    event, accepted = deployment_executions.accept_event(
        deployment_id,
        payload.sequence,
        stage=payload.stage,
        stage_status=payload.status,
        image_digest=payload.image_digest,
    )
    if not accepted:
        return {"accepted": True, "event_sequence": event["event_sequence"]}
    stages = {stage.key: stage for stage in item.stages}
    stage = stages.get(payload.stage)
    if stage:
        stage.status = payload.status
        if payload.status == "running":
            stage.started_at = datetime.now(timezone.utc).isoformat()
        elif payload.status in {"succeeded", "failed"}:
            stage.completed_at = datetime.now(timezone.utc).isoformat()
        if payload.error:
            stage.details = payload.error
        item.stages = list(stages.values())
    item.candidate_revision = payload.candidate_revision or item.candidate_revision
    item.candidate_url = payload.candidate_url or item.candidate_url
    item.production_revision = payload.production_revision or item.production_revision
    item.production_url = payload.production_url or item.production_url
    item.updated_at = datetime.now(timezone.utc).isoformat()
    if payload.status == "failed":
        item.status, item.current_stage, item.error = (
            "FAILED",
            payload.stage,
            payload.error,
        )
    elif payload.status == "succeeded" and payload.stage == "validate-production":
        item.status, item.current_stage, item.error = "SUCCEEDED", "complete", ""
    elif payload.status == "succeeded" and payload.stage == "rollback":
        item.status, item.current_stage, item.error = "ROLLED_BACK", "rollback", ""
    else:
        status_by_stage: dict[str, DeploymentStatus] = {
            "verify-release": "VERIFYING_RELEASE",
            "build": "BUILDING",
            "deploy-candidate": "DEPLOYING_CANDIDATE",
            "validate-candidate": "VALIDATING_CANDIDATE",
            "promote": "PROMOTING",
            "validate-production": "VALIDATING_PRODUCTION",
            "rollback": "ROLLING_BACK",
        }
        item.status = status_by_stage.get(payload.stage, item.status)
        item.current_stage = payload.stage
    deployment_store.save(item, "")
    github_state = "in_progress"
    if item.status in {"SUCCEEDED", "ROLLED_BACK"}:
        github_state = "success"
    elif item.status in {"FAILED", "ROLLBACK_FAILED"}:
        github_state = "failure"
    github_deployments.set_managed_status(
        item,
        state=github_state,
        description=(item.error or f"Deployment stage: {item.current_stage}"),
        log_url=str(execution.get("log_url", "")),
        environment_url=item.production_url,
    )
    return {"accepted": True, "event_sequence": event["event_sequence"]}
