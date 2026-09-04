"""Operator-only metadata and write-only secret publishing endpoints."""

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from ..config import config
from ..security import require_deployer
from ..services import catalog, operational_secrets

router = APIRouter(prefix="/api/services", tags=["secrets"])


def selected_service(service_name: str):
    service = catalog.get_service(service_name)
    if service is None:
        raise HTTPException(404, "Unknown service")
    return service


@router.get(
    "/{service_name}/secrets",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Operator not authorized"},
        404: {"description": "Unknown service"},
        503: {"description": "Secret metadata unavailable"},
    },
)
def list_secrets(service_name: str, request: Request, response: Response):
    require_deployer(request)
    service = selected_service(service_name)
    response.headers["Cache-Control"] = "no-store"
    try:
        return operational_secrets.metadata(service)
    except Exception:
        raise HTTPException(503, "Secret metadata is unavailable") from None


@router.post(
    "/{service_name}/secrets/{secret_key}/versions",
    status_code=201,
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Operator, origin or secret not permitted"},
        404: {"description": "Unknown service"},
        409: {"description": "Stale configuration or unresolved operation"},
        413: {"description": "Request exceeds size limit"},
        415: {"description": "JSON required"},
        422: {"description": "Invalid secret request"},
        503: {"description": "Secret save could not be confirmed"},
    },
)
async def save_secret(
    service_name: str, secret_key: str, request: Request, response: Response
):
    operator = require_deployer(request)
    if (
        request.headers.get("origin") != config.auth.frontend_url
        or request.headers.get("x-requested-with") != "EngineeringPlatform"
    ):
        raise HTTPException(403, "Invalid request origin")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON required")
    service = selected_service(service_name)
    secret = next(
        (item for item in service.operational_secrets if item.key == secret_key), None
    )
    if secret is None or not secret.editable:
        raise HTTPException(403, "Secret is not editable for this service")
    # Parse manually: FastAPI's default validation errors can echo sensitive input.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 100_000:
            raise HTTPException(413, "Secret request is too large")
    try:
        body = json.loads(raw)
        value = body["value"]
        generation = body["generation"]
        operation_id = str(UUID(request.headers.get("idempotency-key", "")))
        if (
            set(body) != {"value", "generation"}
            or not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > 65536
            or type(generation) is not int
            or generation < 0
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "Invalid secret request") from None
    response.headers["Cache-Control"] = "no-store"
    try:
        # Do not return or log provider exceptions: they may contain request data.
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            operational_secrets.publish,
            service,
            secret,
            value,
            operation_id,
            generation,
            operator,
        )
    except operational_secrets.ConfigurationConflict:
        raise HTTPException(
            409, "Refresh configuration; an operation may require reconciliation"
        ) from None
    except Exception:
        raise HTTPException(
            503, "Save could not be confirmed. Refresh status before retrying"
        ) from None
