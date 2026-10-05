"""OIDC-only delivery surface; task bodies contain no SQL or credentials."""

from __future__ import annotations

from functools import partial
import json
from typing import Callable

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import config
from ..services import database_jobs, database_views
from ..services.database_registry import DatabaseUnavailable

router = APIRouter(prefix="/api/internal/database-executions", tags=["internal"])


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


def verify_worker(request: Request) -> None:
    settings = config.databases
    if (
        config.mock_mode
        or not settings.worker_service_account
        or not settings.api_origin
    ):
        raise HTTPException(503, "Database worker is unavailable")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 8192:
        raise HTTPException(401, "Database worker identity is required")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.id_token import verify_oauth2_token

        transport = GoogleRequest()

        def bounded_transport(url, method="GET", body=None, headers=None, **kwargs):
            return transport(url, method=method, body=body, headers=headers, timeout=4)

        claims = verify_oauth2_token(token, bounded_transport, settings.api_origin)
        if (
            claims.get("email") != settings.worker_service_account
            or claims.get("email_verified") is not True
        ):
            raise ValueError("Unexpected worker")
    except Exception:
        raise HTTPException(401, "Invalid database worker identity") from None


async def _body(request: Request) -> dict:
    if (
        request.query_params
        or request.headers.get("content-type", "").split(";", 1)[0]
        != "application/json"
    ):
        raise HTTPException(422, "Invalid database worker request")
    raw = bytearray()
    try:
        with anyio.fail_after(5):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 256:
                    raise ValueError("Body too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value
    except (ValueError, UnicodeError, TimeoutError):
        raise HTTPException(422, "Invalid database worker request") from None


@router.post("/{operation}")
async def worker(operation: str, request: Request):
    await anyio.to_thread.run_sync(partial(verify_worker, request))
    if operation not in {"query", "export", "sort", "cleanup", "sweep"}:
        raise HTTPException(404, "Database worker operation not found")
    body = await _body(request)
    run: Callable[[], None]
    try:
        if operation == "sweep":
            if body:
                raise ValueError("Empty body required")
            run = database_jobs.sweep
        else:
            value = TaskRequest.model_validate(body)
            operations: dict[str, Callable[[str], None]] = {
                "query": database_jobs.run_query,
                "export": database_jobs.run_export,
                "sort": database_views.run_sort,
                "cleanup": database_jobs.cleanup,
            }
            run = partial(operations[operation], value.id)
        with anyio.fail_after(270):
            await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
        return Response(status_code=204)
    except (DatabaseUnavailable, TimeoutError):
        raise HTTPException(503, "Database worker operation is unavailable") from None
    except (ValidationError, ValueError):
        raise HTTPException(422, "Invalid database worker request") from None
