"""Internal release authorization endpoint used by GitHub Actions."""

from fastapi import APIRouter, HTTPException, status

from ..models import (
    ReleaseAuthorizationConsumeRequest,
    ReleaseAuthorizationConsumeResponse,
)
from ..services import release_authorization, release_authorization_store

router = APIRouter(prefix="/api/internal/release-authorizations", tags=["internal"])


@router.post(
    "/consume",
    response_model=ReleaseAuthorizationConsumeResponse,
    status_code=status.HTTP_200_OK,
)
def consume_authorization(payload: ReleaseAuthorizationConsumeRequest):
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
            },
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
