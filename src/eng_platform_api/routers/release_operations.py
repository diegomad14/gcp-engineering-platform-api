"""Private scheduler/admin operations for the release fallback."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..config import config
from ..security import require_deployer
from ..services import release_executions, release_orchestrator, release_reconciler
from .release_execution_events import _bearer, _verify_google

router = APIRouter(prefix="/api/internal/release-operations", tags=["internal"])


class ReconcileGitHubRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1, max_length=128)
    operation: str = Field(pattern=r"^(pr_quality|main_release)$")
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-fA-F]{40}$")
    base_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-fA-F]{40}$")
    github_run_id: int = Field(gt=0)
    paired_github_run_id: int | None = Field(default=None, gt=0)


class SupersedeExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


@router.post("/reconcile", include_in_schema=False)
def reconcile_due(authorization: str | None = Header(default=None)):
    _verify_google(
        _bearer(authorization),
        expected_identity=config.release_orchestrator.reconciler_service_account,
    )
    results = []
    for execution in release_executions.list_due(limit=100):
        execution_id = str(execution["execution_id"])
        try:
            updated = release_reconciler.reconcile(execution_id)
            results.append({"execution_id": execution_id, "status": updated["status"]})
        except Exception as exc:
            try:
                updated = release_executions.save(
                    execution_id, status="unknown", error=str(exc)[:1000]
                )
                results.append(
                    {"execution_id": execution_id, "status": updated["status"]}
                )
            except Exception:
                results.append({"execution_id": execution_id, "status": "error"})
    return {"reconciled": len(results), "items": results}


@router.post("/canaries/{execution_id}/approve", include_in_schema=False)
def approve_canary(execution_id: str, identity: str = Depends(require_deployer)):
    try:
        release_orchestrator.approve_canary(execution_id, approved_by=identity)
        return release_reconciler.reconcile(execution_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Unknown release execution"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/executions/{execution_id}/supersede", include_in_schema=False)
def supersede_execution(
    execution_id: str,
    payload: SupersedeExecutionRequest,
    identity: str = Depends(require_deployer),
):
    """Retire a planned release that will never be published."""
    try:
        execution = release_orchestrator.supersede(
            execution_id, superseded_by=identity, reason=payload.reason
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Unknown release execution"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "execution_id": execution["execution_id"],
        "status": execution["status"],
        "superseded_by": execution.get("superseded_by"),
    }


@router.post("/github-actions/probe", include_in_schema=False)
def request_probe(identity: str = Depends(require_deployer)):
    try:
        circuit = release_orchestrator.request_health_probe(requested_by=identity)
    except (ValueError, release_orchestrator.ReleaseOrchestratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "accepted": True,
        "state": circuit.get("state"),
        "repository": circuit.get("probe", {}).get("repository"),
    }


@router.post("/reconcile-github-run", include_in_schema=False)
def reconcile_github_run(
    payload: ReconcileGitHubRunRequest,
    identity: str = Depends(require_deployer),
):
    try:
        execution = release_orchestrator.reconcile_verified_github_run(
            service_name=payload.service_name,
            operation=payload.operation,
            head_sha=payload.head_sha,
            base_sha=payload.base_sha,
            run_id=payload.github_run_id,
            paired_run_id=payload.paired_github_run_id,
            requested_by=identity,
        )
    except release_orchestrator.ReleaseOrchestratorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": True, "execution_id": execution["execution_id"]}
