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
    mcp_grants,
)

from ..services.resource_access import require_managed_service_name

router = APIRouter(prefix="/api/internal/release-authorizations", tags=["internal"])


@router.post(
    "/consume",
    response_model=ReleaseAuthorizationConsumeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def consume_authorization(
    payload: ReleaseAuthorizationConsumeRequest, request: Request
):
    try:
        expected = {
            "repository": payload.repository,
            "service_name": payload.service_name,
            "tag": payload.tag,
            "sha": payload.sha,
            "github_deployment_id": payload.github_deployment_id,
            "kind": payload.kind,
        }
        previous = None
        if payload.mode == "continue":
            claims, previous = release_authorization.verify_continuation(
                payload.token, expected
            )
        else:
            if payload.stage:
                raise release_authorization.ReleaseAuthorizationError(
                    "Consumption cannot select an approval stage"
                )
            claims = release_authorization.verify(payload.token, expected)
        continuation = claims.get("workflow_continuation") is True
        if claims.get("mcp_grant_id"):
            if claims.get("mcp_authority_policy") != mcp_grants.POLICY:
                raise release_authorization.ReleaseAuthorizationError(
                    "MCP policy changed"
                )
            mcp_grants.validate(claims["mcp_grant_id"], claims.get("requested_by"))
        identity = {}
        # Legacy callers omit this field; central callers must bind it server-side.
        if (
            continuation
            or claims.get("execution_repository")
            or payload.target_revision
        ) and (claims.get("target_revision", "") != payload.target_revision):
            raise release_authorization.ReleaseAuthorizationError(
                "Rollback target mismatch"
            )
        if continuation or claims.get("execution_repository"):
            if (
                not claims.get("mcp_grant_id")
                and claims.get("requested_by", "").lower()
                not in config.auth.allowed_logins
            ):
                raise release_authorization.ReleaseAuthorizationError(
                    "Operator authorization was revoked"
                )
            if claims.get("configuration_hash") != payload.configuration_hash:
                raise release_authorization.ReleaseAuthorizationError(
                    "Configuration mismatch"
                )
            if continuation:
                if not payload.service_name.startswith("cgm-artemis-") or claims.get(
                    "execution_repository"
                ):
                    raise release_authorization.ReleaseAuthorizationError(
                        "Invalid platform continuation capability"
                    )
                identity = workflow_identity.verify_platform(
                    request.headers.get("x-github-oidc", ""),
                    payload.repository,
                    payload.kind,
                    payload.tag,
                    payload.sha,
                    mode=payload.mode,
                    stage=payload.stage,
                )
            else:
                identity = workflow_identity.verify(
                    request.headers.get("x-github-oidc", ""),
                    claims["execution_repository"],
                    payload.kind,
                )
        require_managed_service_name(payload.service_name)
        context = {
            **expected,
            "exp": claims["exp"],
            "requested_by": claims.get("requested_by", ""),
            "configuration_hash": claims.get("configuration_hash", ""),
            "target_revision": payload.target_revision,
            "mcp_grant_id": claims.get("mcp_grant_id", ""),
            "mcp_authority_policy": claims.get("mcp_authority_policy", ""),
        }
        if payload.mode == "continue":
            if (
                previous is None
                or any(previous.get(key) != value for key, value in context.items())
                or previous.get("workflow_identity") != identity
            ):
                raise release_authorization.ReleaseAuthorizationError(
                    "Continuation does not match the consumed operation"
                )
            return ReleaseAuthorizationConsumeResponse(jti=str(claims["jti"]))
        consumed = release_authorization_store.consume(
            str(claims["jti"]),
            {
                **context,
                "workflow_identity": identity,
                "workflow_continuation": continuation,
            },
            require_durable=continuation or bool(claims.get("execution_repository")),
        )
    except HTTPException:
        raise
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
