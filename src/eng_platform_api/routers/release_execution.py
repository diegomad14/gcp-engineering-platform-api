"""Local-release authorization and shared execution-control endpoints.

These endpoints only reserve state and record intent/results.  They do not call
GitHub, Artifact Registry, Cloud Run, Actions, or SanPlat.
"""

import re

from fastapi import APIRouter, HTTPException, Request, status

from ..config import config
from ..models import (
    ExecutionIntent,
    ExecutionIntentCreateRequest,
    ExecutionIntentReconcileRequest,
    ExecutionIntentResultRequest,
    ExecutionLease,
    ExecutionLeaseAcquireRequest,
    ExecutionLeaseReconcileRequest,
    ExecutionLeaseReleaseRequest,
    ExecutionLeaseRenewRequest,
    ReleaseExecutionAuthorizationConsumeRequest,
    ReleaseExecutionAuthorizationConsumeResponse,
    ReleaseExecutionAuthorizationIssueRequest,
    ReleaseExecutionAuthorizationIssueResponse,
)
from ..services import (
    catalog,
    execution_control_store,
    local_release_policy,
    release_authorization,
    release_authorization_store,
)
from ..security import require_deployer
from .quality import get_quality_report

router = APIRouter(prefix="/api/internal/release-execution", tags=["internal"])


def _require_context_actor(request: Request, actor_id: str) -> str:
    """Bind every control-plane call to the authenticated platform identity."""
    identity = require_deployer(request)
    if identity.lower() != actor_id.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated deployer does not own this release execution",
        )
    return identity


def _context_actor_for_lease(scope_key: str) -> str:
    lease = execution_control_store.get_lease(scope_key)
    if lease is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found"
        )
    return lease.context.actor_id


def _context_actor_for_intent(intent_id: str) -> str:
    intent = execution_control_store.get_intent(intent_id)
    if intent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found"
        )
    return intent.context.actor_id


def _validate_capability(payload: ReleaseExecutionAuthorizationIssueRequest) -> None:
    """Resolve all executable identity from the registered single-service catalog."""
    local_release_policy.require_enabled(payload.service_name)
    if payload.operation == "publish":
        local_release_policy.require_publication_handoff(payload.repository)
    service = catalog.get_service(payload.service_name)
    if service is None:
        raise ValueError("Service is not registered in the platform catalog")
    if service.repository != payload.repository:
        raise ValueError("Capability repository does not match the catalog service")
    expected_target = (
        f"{payload.repository}:oss-v2"
        if payload.operation == "publish"
        else f"{service.project_id}/{service.region}/{service.service_name}"
    )
    if payload.target != expected_target:
        raise ValueError("Capability destination does not match the catalog service")
    if not service.deployment_ready:
        raise ValueError("Service is not deployment-ready in the catalog")
    if service.release_policy != "oss-v2" or service.quality.policy_version != "oss-v2":
        raise ValueError("Service does not use the required oss-v2 release policy")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", payload.tag):
        raise ValueError("Capability tag must be a release SemVer tag")
    report = get_quality_report(
        payload.service_name, payload.source_sha, for_release=True
    )
    if (
        report is None
        or report.repository != payload.repository
        or report.commit_sha.lower() != payload.source_sha.lower()
        or report.policy_version != "oss-v2"
        or report.quality_gate_status != "PASSED"
    ):
        raise ValueError(
            "Capability requires exact passed oss-v2 evidence for the source SHA"
        )


@router.post(
    "/authorizations/issue",
    response_model=ReleaseExecutionAuthorizationIssueResponse,
)
def issue_local_authorization(
    payload: ReleaseExecutionAuthorizationIssueRequest, request: Request
):
    """Issue one authenticated, catalog-bound local release capability."""
    actor_id = require_deployer(request)
    try:
        _validate_capability(payload)
        token, claims = release_authorization.issue(
            repository=payload.repository,
            service_name=payload.service_name,
            tag=payload.tag,
            sha=payload.source_sha,
            github_deployment_id=0,
            requested_by=actor_id,
            kind="deploy" if payload.operation != "rollback" else "rollback",
            release_id=payload.release_id,
            artifact_digest=payload.artifact_digest,
            target=payload.target,
            operation=payload.operation,
            audience=release_authorization.LOCAL_AUDIENCE,
            execution_mode=release_authorization.LOCAL_EXECUTION_MODE,
            configuration_hash=payload.configuration_hash,
            capability_issued=True,
        )
    except (ValueError, release_authorization.ReleaseAuthorizationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release authorization issuer unavailable",
        ) from exc
    return ReleaseExecutionAuthorizationIssueResponse(
        token=token, expires_at=int(claims["exp"])
    )


def _execution_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, execution_control_store.AuthorizationRequired):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Platform release authorization is required",
        )
    if isinstance(exc, RuntimeError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shared release execution store unavailable",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(exc),
    )


@router.post(
    "/authorizations/consume",
    response_model=ReleaseExecutionAuthorizationConsumeResponse,
)
def consume_local_authorization(
    payload: ReleaseExecutionAuthorizationConsumeRequest,
    request: Request,
):
    """Consume a platform-issued local-CLI ticket exactly once."""
    try:
        actor_id = _require_context_actor(request, payload.actor_id)
        claims = release_authorization.verify(
            payload.token,
            {
                "requested_by": payload.actor_id,
                "release_id": payload.release_id,
                "repository": payload.repository,
                "sha": payload.source_sha,
                "tag": payload.tag,
                "artifact_digest": payload.artifact_digest,
                "target": payload.target,
                "operation": payload.operation,
                "configuration_hash": payload.configuration_hash,
            },
            audience=release_authorization.LOCAL_AUDIENCE,
        )
        if claims.get("execution_mode") != release_authorization.LOCAL_EXECUTION_MODE:
            raise release_authorization.ReleaseAuthorizationError(
                "Unsupported local execution mode"
            )
        capability_service = str(claims.get("service_name", ""))
        if not capability_service:
            raise release_authorization.ReleaseAuthorizationError(
                "Capability has no single-service identity"
            )
        local_release_policy.require_enabled(capability_service)
        if payload.operation == "publish":
            local_release_policy.require_publication_handoff(payload.repository)
        if payload.service_name and payload.service_name != capability_service:
            raise release_authorization.ReleaseAuthorizationError(
                "Capability service does not match release execution"
            )
        if payload.release_group_id != "":
            raise release_authorization.ReleaseAuthorizationError(
                "Release execution groups are not supported"
            )
        if claims.get("requested_by", "").lower() != actor_id.lower():
            raise release_authorization.ReleaseAuthorizationError(
                "Capability actor does not match authenticated deployer"
            )
        # Tickets issued through the legacy Actions path remain usable only in
        # explicit mock fixtures. A non-mock CLI must start with the OAuth
        # authenticated capability endpoint above.
        if not config.mock_mode and claims.get("capability_issued") is not True:
            raise release_authorization.ReleaseAuthorizationError(
                "Local capability was not issued to an authenticated deployer"
            )
        consumed = release_authorization_store.consume(
            str(claims["jti"]),
            {
                "authorization_mode": release_authorization.LOCAL_EXECUTION_MODE,
                "actor_id": payload.actor_id,
                "requested_by": payload.actor_id,
                "release_id": payload.release_id,
                "repository": payload.repository,
                "service_name": capability_service,
                "release_group_id": "",
                "source_sha": payload.source_sha,
                "tag": payload.tag,
                "artifact_digest": payload.artifact_digest,
                "target": payload.target,
                "operation": payload.operation,
                "configuration_hash": payload.configuration_hash,
                "exp": claims["exp"],
            },
            require_durable=not config.mock_mode,
        )
    except local_release_policy.LocalReleasePolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except release_authorization.ReleaseAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid local release authorization",
        ) from exc
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release authorization store unavailable",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release authorization store unavailable",
        ) from exc
    if not consumed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Release authorization has already been consumed",
        )
    return ReleaseExecutionAuthorizationConsumeResponse(
        jti=str(claims["jti"]),
        actor_id=payload.actor_id,
        expires_at=int(claims["exp"]),
    )


@router.post("/leases/acquire", response_model=ExecutionLease)
def acquire_lease(payload: ExecutionLeaseAcquireRequest, request: Request):
    try:
        _require_context_actor(request, payload.actor_id)
        local_release_policy.require_enabled(payload.service_name)
        if payload.operation == "publish":
            local_release_policy.require_publication_handoff(payload.repository)
        return execution_control_store.acquire_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/renew", response_model=ExecutionLease)
def renew_lease(payload: ExecutionLeaseRenewRequest, request: Request):
    try:
        _require_context_actor(request, _context_actor_for_lease(payload.scope_key))
        return execution_control_store.renew_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/release", response_model=ExecutionLease)
def release_lease(payload: ExecutionLeaseReleaseRequest, request: Request):
    try:
        _require_context_actor(request, _context_actor_for_lease(payload.scope_key))
        return execution_control_store.release_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/reconcile", response_model=ExecutionLease)
def reconcile_lease(payload: ExecutionLeaseReconcileRequest, request: Request):
    try:
        _require_context_actor(request, _context_actor_for_lease(payload.scope_key))
        return execution_control_store.reconcile_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.get("/leases/{scope_key:path}", response_model=ExecutionLease)
def get_lease(scope_key: str, request: Request):
    lease = execution_control_store.get_lease(scope_key)
    if lease is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found"
        )
    _require_context_actor(request, lease.context.actor_id)
    return lease


@router.post("/intents", response_model=ExecutionIntent)
def create_intent(payload: ExecutionIntentCreateRequest, request: Request):
    try:
        _require_context_actor(request, payload.actor_id)
        local_release_policy.require_enabled(payload.service_name)
        if payload.operation == "publish":
            local_release_policy.require_publication_handoff(payload.repository)
        return execution_control_store.create_intent(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/intents/result", response_model=ExecutionIntent)
def record_intent_result(payload: ExecutionIntentResultRequest, request: Request):
    try:
        _require_context_actor(request, _context_actor_for_intent(payload.intent_id))
        return execution_control_store.record_intent_result(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/intents/reconcile", response_model=ExecutionIntent)
def reconcile_intent(payload: ExecutionIntentReconcileRequest, request: Request):
    try:
        _require_context_actor(request, _context_actor_for_intent(payload.intent_id))
        return execution_control_store.reconcile_intent(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.get("/intents/{intent_id}", response_model=ExecutionIntent)
def get_intent(intent_id: str, request: Request):
    intent = execution_control_store.get_intent(intent_id)
    if intent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found"
        )
    _require_context_actor(request, intent.context.actor_id)
    return intent
