"""Local-release authorization and shared execution-control endpoints.

These endpoints only reserve state and record intent/results.  They do not call
GitHub, Artifact Registry, Cloud Run, Actions, or SanPlat.
"""

from fastapi import APIRouter, HTTPException, status

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
)
from ..services import (
    execution_control_store,
    release_authorization,
    release_authorization_store,
)

router = APIRouter(prefix="/api/internal/release-execution", tags=["internal"])


def _execution_http_error(exc: Exception) -> HTTPException:
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
):
    """Consume a platform-issued local-CLI ticket exactly once."""
    try:
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
        if payload.actor_id.lower() not in config.auth.allowed_logins:
            raise release_authorization.ReleaseAuthorizationError(
                "Operator authorization was revoked"
            )
        consumed = release_authorization_store.consume(
            str(claims["jti"]),
            {
                "authorization_mode": release_authorization.LOCAL_EXECUTION_MODE,
                "actor_id": payload.actor_id,
                "requested_by": payload.actor_id,
                "release_id": payload.release_id,
                "repository": payload.repository,
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
    except release_authorization.ReleaseAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid local release authorization",
        ) from exc
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
def acquire_lease(payload: ExecutionLeaseAcquireRequest):
    try:
        return execution_control_store.acquire_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/renew", response_model=ExecutionLease)
def renew_lease(payload: ExecutionLeaseRenewRequest):
    try:
        return execution_control_store.renew_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/release", response_model=ExecutionLease)
def release_lease(payload: ExecutionLeaseReleaseRequest):
    try:
        return execution_control_store.release_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/leases/reconcile", response_model=ExecutionLease)
def reconcile_lease(payload: ExecutionLeaseReconcileRequest):
    try:
        return execution_control_store.reconcile_lease(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/intents", response_model=ExecutionIntent)
def create_intent(payload: ExecutionIntentCreateRequest):
    try:
        return execution_control_store.create_intent(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/intents/result", response_model=ExecutionIntent)
def record_intent_result(payload: ExecutionIntentResultRequest):
    try:
        return execution_control_store.record_intent_result(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc


@router.post("/intents/reconcile", response_model=ExecutionIntent)
def reconcile_intent(payload: ExecutionIntentReconcileRequest):
    try:
        return execution_control_store.reconcile_intent(payload)
    except Exception as exc:
        raise _execution_http_error(exc) from exc
