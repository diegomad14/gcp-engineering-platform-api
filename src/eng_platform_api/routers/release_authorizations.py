"""Internal release authorization endpoint used by GitHub Actions."""

from fastapi import APIRouter, HTTPException, Request, status

from ..config import config

from ..models import (
    ReleaseAuthorizationConsumeRequest,
    ReleaseAuthorizationConsumeResponse,
)
from ..services import (
    release_authorization,
    release_authorization_store,
    workflow_identity,
)

router = APIRouter(prefix="/api/internal/release-authorizations", tags=["internal"])


@router.post(
    "/consume",
    response_model=ReleaseAuthorizationConsumeResponse,
    status_code=status.HTTP_200_OK,
)
def consume_authorization(
    payload: ReleaseAuthorizationConsumeRequest, request: Request
):
    try:
        claims = release_authorization.verify(
            payload.token,
            {
                "repository": payload.repository,
                "service_name": payload.service_name,
                "tag": payload.tag,
                "sha": payload.sha,
                "github_deployment_id": payload.github_deployment_id,
                "kind": payload.kind,
            },
        )
        identity = {}
        # Legacy callers omit this field; central callers must bind it server-side.
        if (claims.get("execution_repository") or payload.target_revision) and (
            claims.get("target_revision", "") != payload.target_revision
        ):
            raise release_authorization.ReleaseAuthorizationError(
                "Rollback target mismatch"
            )
        if claims.get("execution_repository"):
            if claims.get("requested_by", "").lower() not in config.auth.allowed_logins:
                raise release_authorization.ReleaseAuthorizationError(
                    "Operator authorization was revoked"
                )
            if claims.get("configuration_hash") != payload.configuration_hash:
                raise release_authorization.ReleaseAuthorizationError(
                    "Configuration mismatch"
                )
            identity = workflow_identity.verify(
                request.headers.get("x-github-oidc", ""),
                claims["execution_repository"],
                payload.kind,
            )
        consumed = release_authorization_store.consume(
            str(claims["jti"]),
            {
                "repository": payload.repository,
                "service_name": payload.service_name,
                "tag": payload.tag,
                "sha": payload.sha,
                "github_deployment_id": payload.github_deployment_id,
                "kind": payload.kind,
                "exp": claims["exp"],
                "workflow_identity": identity,
                "requested_by": claims.get("requested_by", ""),
                "configuration_hash": claims.get("configuration_hash", ""),
                "target_revision": payload.target_revision,
            },
            require_durable=bool(claims.get("execution_repository")),
        )
    except (release_authorization.ReleaseAuthorizationError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid release authorization",
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
    return ReleaseAuthorizationConsumeResponse(jti=str(claims["jti"]))
