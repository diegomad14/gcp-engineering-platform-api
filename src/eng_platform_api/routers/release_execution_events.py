"""Private authenticated callbacks for quality and release preparation."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..config import config
from ..models import ReleaseExecutionEvent
from ..services import (
    github_release_control,
    quality_store,
    release_cloud_build,
    release_executions,
    release_reconciler,
    release_workflow_identity,
)

router = APIRouter(prefix="/api/internal/release-executions", tags=["internal"])
_SEMVER_TAG = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class SourceTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_run_id: str = Field(min_length=1, max_length=128)


class ResolveExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=256)
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-fA-F]{40}$")
    operation: str = Field(pattern=r"^(pr_quality|main_release)$")


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Execution identity is required")
    return token


def _verify_google(token: str, *, expected_identity: str = "") -> dict:
    if config.mock_mode:
        return {
            "email": expected_identity
            or config.release_orchestrator.callback_service_account
            or "test",
            "sub": "test",
        }
    expected = expected_identity or config.release_orchestrator.callback_service_account
    if not expected:
        raise HTTPException(
            status_code=503, detail="Callback identity is not configured"
        )
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.id_token import verify_oauth2_token

        claims = verify_oauth2_token(
            token, GoogleRequest(), config.github.platform_api_url
        )
        if claims.get("email") != expected:
            raise ValueError("unexpected service account")
        return claims
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid managed build identity"
        ) from exc


def _verify_cloud_build(execution: dict, provider_run_id: str) -> dict:
    bound_build_id = str(execution.get("build_id", ""))
    if bound_build_id and bound_build_id != provider_run_id:
        raise HTTPException(status_code=403, detail="Cloud Build identity mismatch")
    try:
        build = release_cloud_build.get_build(provider_run_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Unable to verify Cloud Build"
        ) from exc
    substitutions = build.get("substitutions", {})
    source = build.get("source", {}).get("connectedRepository", {})
    expected_repository = config.cloud_build.repositories.get(
        str(execution["service_name"]), ""
    )
    if (
        str(build.get("id", "")) != provider_run_id
        or not expected_repository
        or source.get("repository") != expected_repository
        or source.get("revision") != execution.get("head_sha")
    ):
        raise HTTPException(status_code=403, detail="Cloud Build source mismatch")
    expected = {
        "_EXECUTION_ID": execution["execution_id"],
        "_REQUEST_FINGERPRINT": execution["fingerprint"],
        "_SERVICE_NAME": execution["service_name"],
        "_REPOSITORY": execution["repository"],
        "_HEAD_SHA": execution["head_sha"],
        "_BASE_SHA": execution["base_sha"],
        "_EXECUTOR_DIGEST": execution["executor_digest"],
        "_OPERATION": execution["operation"],
        "_PROFILE_SHA256": execution["profile_hash"],
    }
    if execution.get("operation") == "main_release":
        expected.update(
            {
                "_PLANNER_SHA256": execution["planner_hash"],
                "_PLANNER_DIGEST": config.release_orchestrator.release_planner_image,
            }
        )
    if any(substitutions.get(key) != value for key, value in expected.items()):
        raise HTTPException(status_code=403, detail="Cloud Build source mismatch")
    account = str(build.get("serviceAccount", ""))
    expected_account = config.release_orchestrator.service_account
    if not account or not expected_account or account != expected_account:
        raise HTTPException(
            status_code=403, detail="Cloud Build service account mismatch"
        )
    if not bound_build_id:
        try:
            release_cloud_build._bind(str(execution["execution_id"]), build)
        except (
            KeyError,
            ValueError,
            release_cloud_build.ReleaseCloudBuildError,
        ) as exc:
            raise HTTPException(
                status_code=409, detail="Cloud Build binding conflict"
            ) from exc
    return build


def _verify_provider(
    execution: dict, provider_run_id: str, authorization: str | None
) -> None:
    token = _bearer(authorization)
    if execution.get("provider") == "cloud_build":
        _verify_google(token)
        _verify_cloud_build(execution, provider_run_id)
        return
    try:
        claims = release_workflow_identity.verify(token, execution)
    except release_workflow_identity.ReleaseWorkflowIdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if claims.get("run_id") != provider_run_id:
        raise HTTPException(status_code=403, detail="GitHub workflow run mismatch")
    if execution.get("operation") == "pr_quality":
        try:
            run = github_release_control.workflow_run(
                str(execution["repository"]), int(provider_run_id)
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Unable to verify GitHub run"
            ) from exc
        if (
            str(getattr(run, "event", "")) != "pull_request_target"
            or str(getattr(run, "display_title", ""))
            != f"eng-platform-quality-{execution['head_sha']}"
            or not release_workflow_identity.matches_workflow_path(
                "pr_quality", str(getattr(run, "path", ""))
            )
        ):
            raise HTTPException(status_code=403, detail="GitHub workflow SHA mismatch")


def _verify_event_token(execution: dict, token: str | None) -> None:
    expected_hash = str(execution.get("event_token_hash", ""))
    if (
        not token
        or len(token) > 512
        or len(expected_hash) != 64
        or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected_hash
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid execution event token")


def _verify_report(execution: dict, payload: ReleaseExecutionEvent) -> str:
    if payload.report is None:
        return ""
    report = payload.report
    if (
        report.service_name != execution.get("service_name")
        or report.repository != execution.get("repository")
        or report.commit_sha.lower() != execution.get("head_sha")
        or report.base_sha.lower() != execution.get("base_sha")
        or report.policy_version != "oss-v2"
    ):
        raise HTTPException(status_code=403, detail="Quality report identity mismatch")
    try:
        return quality_store.save_pending_report(
            str(execution["execution_id"]),
            report,
            expected_hash=payload.report_hash,
        )
    except quality_store.QualityEvidenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _verify_plan(execution: dict, payload: ReleaseExecutionEvent) -> dict | None:
    plan = payload.release_plan
    if plan is None:
        return None
    if execution.get("operation") != "main_release":
        raise HTTPException(
            status_code=403, detail="PR executions cannot publish releases"
        )
    if plan.config_hash != execution.get("planner_hash"):
        raise HTTPException(status_code=403, detail="Release planner hash mismatch")
    if plan.release_type == "none":
        if plan.git_tag or plan.next_version:
            raise HTTPException(
                status_code=422, detail="No-release plan contains a tag"
            )
    elif (
        not _SEMVER_TAG.fullmatch(plan.git_tag)
        or plan.git_tag.removeprefix("v") != plan.next_version
    ):
        raise HTTPException(status_code=422, detail="Release plan is not semantic")
    return plan.model_dump(mode="json")


@router.post("/{execution_id}/events", include_in_schema=False)
async def accept_event(
    execution_id: str,
    payload: ReleaseExecutionEvent,
    request: Request,
    authorization: str | None = Header(default=None),
    x_eng_platform_event_token: str | None = Header(default=None),
):
    content_length = request.headers.get("content-length", "0")
    try:
        if int(content_length) > 1_000_000:
            raise HTTPException(status_code=413, detail="Release event exceeds 1 MB")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid content length") from exc
    if len(await request.body()) > 1_000_000:
        raise HTTPException(status_code=413, detail="Release event exceeds 1 MB")
    execution = release_executions.get(execution_id)
    if execution is None or payload.execution_id != execution_id:
        raise HTTPException(status_code=404, detail="Unknown release execution")
    if payload.fingerprint != execution.get("fingerprint"):
        raise HTTPException(status_code=403, detail="Execution fingerprint mismatch")
    _verify_provider(execution, payload.provider_run_id, authorization)
    _verify_event_token(execution, x_eng_platform_event_token)
    if payload.sequence <= int(execution.get("event_sequence", 0)):
        return {
            "accepted": False,
            "event_sequence": execution.get("event_sequence", 0),
            "status": execution.get("status"),
        }
    quality_result = payload.status in {"quality_passed", "quality_failed"}
    release_result = payload.status in {"release_planned", "no_release"}
    if quality_result != (payload.report is not None):
        raise HTTPException(
            status_code=422,
            detail="Quality completion must contain exactly one quality report",
        )
    if release_result != (payload.release_plan is not None):
        raise HTTPException(
            status_code=422,
            detail="Release completion must contain exactly one release plan",
        )
    try:
        report_hash = _verify_report(execution, payload)
        release_plan = _verify_plan(execution, payload)
    except HTTPException as exc:
        if exc.status_code == 409:
            try:
                release_executions.save(
                    execution_id, status="unknown", error=str(exc.detail)[:1000]
                )
            except ValueError:
                pass
        raise
    changes: dict[str, Any] = {
        "provider_run_id": payload.provider_run_id,
        "engine_event_status": payload.status,
        "error": payload.error,
    }
    if report_hash:
        changes["pending_report_hash"] = report_hash
    if release_plan is not None:
        changes["release_plan"] = release_plan
    release_phase_failure = (
        payload.status == "failed"
        and execution.get("operation") == "main_release"
        and bool(execution.get("pending_report_hash"))
        and execution.get("engine_event_status") == "quality_passed"
    )
    if release_phase_failure:
        # Quality has already emitted authenticated, immutable candidate
        # evidence. Keep the execution reconcilable until the provider becomes
        # terminal, then commit quality and fail only release preparation.
        changes["status"] = "running_quality"
        changes["release_engine_failed"] = True
        changes["release_error"] = payload.error or "Release planner failed"
    elif payload.status == "failed":
        changes["status"] = "failed"
    else:
        changes["status"] = "running_quality"
    try:
        updated, accepted = release_executions.accept_event(
            execution_id, payload.sequence, **changes
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if accepted and updated.get("status") != "failed":
        try:
            updated = release_reconciler.reconcile(execution_id)
        except (ValueError, quality_store.QualityEvidenceConflict) as exc:
            updated = release_executions.save(
                execution_id, status="unknown", error=str(exc)
            )
    return {
        "accepted": accepted,
        "event_sequence": updated.get("event_sequence", payload.sequence),
        "status": updated.get("status"),
    }


@router.post("/resolve", include_in_schema=False)
def resolve_execution(
    payload: ResolveExecutionRequest,
    authorization: str | None = Header(default=None),
):
    execution = release_executions.find(
        payload.repository, payload.head_sha, payload.operation
    )
    if execution is None or execution.get("provider") != "github_actions":
        raise HTTPException(
            status_code=404, detail="Release execution is not available"
        )
    try:
        claims = release_workflow_identity.verify(_bearer(authorization), execution)
    except release_workflow_identity.ReleaseWorkflowIdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if execution.get("operation") == "pr_quality":
        try:
            run = github_release_control.workflow_run(
                str(execution["repository"]), int(claims["run_id"])
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Unable to verify GitHub run"
            ) from exc
        if (
            str(getattr(run, "event", "")) != "pull_request_target"
            or str(getattr(run, "display_title", ""))
            != f"eng-platform-quality-{execution['head_sha']}"
            or not release_workflow_identity.matches_workflow_path(
                "pr_quality", str(getattr(run, "path", ""))
            )
        ):
            raise HTTPException(status_code=403, detail="GitHub workflow SHA mismatch")
    current_run = str(execution.get("provider_run_id", ""))
    if current_run and current_run != claims["run_id"]:
        raise HTTPException(
            status_code=409, detail="Release execution already has a run"
        )
    execution = release_executions.save(
        str(execution["execution_id"]),
        provider_run_id=claims["run_id"],
        github_run_id=int(claims["run_id"]),
    )
    return {
        "execution_id": execution["execution_id"],
        "fingerprint": execution["fingerprint"],
        "base_sha": execution["base_sha"],
        "profile_hash": execution["profile_hash"],
        "executor_image": execution["executor_digest"],
        "planner_image": (
            config.release_orchestrator.release_planner_image
            if execution["operation"] == "main_release"
            else ""
        ),
        "planner_hash": execution.get("planner_hash", ""),
    }


@router.post("/{execution_id}/source-token", include_in_schema=False)
def issue_source_token(
    execution_id: str,
    payload: SourceTokenRequest,
    authorization: str | None = Header(default=None),
):
    execution = release_executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Unknown release execution")
    if payload.fingerprint != execution.get("fingerprint"):
        raise HTTPException(status_code=403, detail="Execution fingerprint mismatch")
    _verify_provider(execution, payload.provider_run_id, authorization)
    if not release_executions.claim_source_token(execution_id):
        raise HTTPException(status_code=409, detail="Source token was already issued")
    try:
        access_token, expires_at = github_release_control.installation_read_token(
            str(execution["repository"])
        )
    except Exception as exc:
        # The one-time claim intentionally remains consumed after an uncertain
        # mint. Operators reconcile it instead of creating multiple credentials.
        raise HTTPException(
            status_code=502, detail="Unable to mint source token"
        ) from exc
    return {"token": access_token, "expires_at": expires_at}


@router.post("/{execution_id}/event-token", include_in_schema=False)
def issue_event_token(
    execution_id: str,
    payload: SourceTokenRequest,
    authorization: str | None = Header(default=None),
):
    execution = release_executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Unknown release execution")
    if payload.fingerprint != execution.get("fingerprint"):
        raise HTTPException(status_code=403, detail="Execution fingerprint mismatch")
    _verify_provider(execution, payload.provider_run_id, authorization)
    event_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(event_token.encode()).hexdigest()
    if not release_executions.claim_event_token(execution_id, token_hash=token_hash):
        raise HTTPException(status_code=409, detail="Event token was already issued")
    return {"event_token": event_token}
