"""Private database console. Request bodies and provider errors never echo SQL."""

from __future__ import annotations

from functools import partial
from typing import Literal
import json
from time import monotonic

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import config
from ..security import require_database_reader
from ..services import database_console, database_registry
from ..services import database_jobs, database_views
from ..services.database_sql import QueryRejected

router = APIRouter(prefix="/api/databases", tags=["databases"])
MAX_REQUEST_BYTES = 32_768


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sql: str = Field(min_length=1, max_length=30_000)
    max_rows: int = Field(default=100, ge=1, le=500)


async def private_request(request: Request) -> dict:
    if request.query_params:
        raise HTTPException(422, "Database requests must use the JSON body")
    if (
        request.headers.get("x-requested-with") != "EngineeringPlatform"
        or request.headers.get("origin") != config.auth.frontend_url
    ):
        raise HTTPException(403, "Invalid database request origin")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "A JSON body is required")
    raw = bytearray()
    try:
        length = request.headers.get("content-length")
        if length is not None and (int(length) < 0 or int(length) > MAX_REQUEST_BYTES):
            raise HTTPException(413, "Database request exceeds the size limit")
        with anyio.fail_after(5):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Database request exceeds the size limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value
    except TimeoutError:
        raise HTTPException(408, "Database request timed out") from None
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid database request") from None


def _visible(reader: str) -> list[database_registry.Database]:
    try:
        return [item for item in database_registry.databases() if item.can_read(reader)]
    except database_registry.DatabaseUnavailable:
        raise HTTPException(503, "Database registry is unavailable") from None


def _database(database_id: str, reader: str) -> database_registry.Database:
    result = next((item for item in _visible(reader) if item.id == database_id), None)
    if result is None:
        # Unknown and hidden connections share a response.
        raise HTTPException(404, "Database not found")
    return result


@router.get("")
async def list_databases(
    request: Request, reader: str = Depends(require_database_reader)
):
    if request.query_params:
        raise HTTPException(422, "Database request parameters are not supported")
    values = await anyio.to_thread.run_sync(partial(_visible, reader))
    require_database_reader(request)
    if _visible(reader) != values:
        raise HTTPException(403, "Database permission changed; retry the request")
    public = [item.public() for item in values]
    if config.databases.executions_enabled:
        await anyio.to_thread.run_sync(database_jobs.active_policy)
        require_database_reader(request)
        if _visible(reader) != values:
            raise HTTPException(403, "Database permission changed; retry the request")
        return {
            "databases": public,
            "workspace_enabled": True,
            "global_sort_enabled": True,
            "workspace_limits": {
                "timeout_seconds": 240,
                "page_sizes": [25, 50, 100],
                "retention_seconds": 3600,
            },
        }
    return {
        "databases": public,
        "workspace_enabled": False,
        "global_sort_enabled": False,
    }


async def _run(request: Request, database_id: str, reader: str, operation):
    started = monotonic()

    def run():
        database = _database(database_id, reader)

        def authorize():
            if monotonic() - started > 15:
                raise HTTPException(503, "Database request timed out")
            if (
                require_database_reader(request) != reader
                or _database(database_id, reader) != database
            ):
                raise HTTPException(
                    403, "Database permission changed; retry the request"
                )
            if config.databases.executions_enabled:
                database_jobs.active_policy()

        try:
            return database, operation(database, authorize)
        except QueryRejected:
            raise HTTPException(
                422, "Query is outside the supported read-only SQL language"
            ) from None
        except database_registry.DatabaseUnavailable:
            raise HTTPException(503, "Database operation is unavailable") from None

    try:
        with anyio.fail_after(15):
            database, result = await anyio.to_thread.run_sync(
                run, abandon_on_cancel=True
            )
        require_database_reader(request)
        if config.databases.executions_enabled:
            await anyio.to_thread.run_sync(database_jobs.active_policy)
            require_database_reader(request)
        if _database(database_id, reader) != database:
            raise HTTPException(403, "Database permission changed; retry the request")
        return result
    except TimeoutError:
        # The connection owner still rolls back/closes and owns its concurrency
        # slot until it finishes; abandoning the wait never releases that slot.
        raise HTTPException(503, "Database request timed out") from None


@router.post("/{database_id}/schema")
async def schema(
    database_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    if body:
        raise HTTPException(422, "Schema request must be an empty JSON object")
    return await _run(request, database_id, reader, database_console.read_schema)


@router.post("/{database_id}/query")
async def query(
    database_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    try:
        payload = QueryRequest.model_validate(body)
    except ValidationError:
        raise HTTPException(422, "Invalid database query request") from None
    return await _run(
        request,
        database_id,
        reader,
        lambda database, authorize: database_console.query(
            database,
            payload.sql,
            payload.max_rows,
            authorize,
            **(
                {
                    "admission": database_jobs.connection_slot(
                        database, "query", reader, authorize
                    )
                }
                if config.databases.executions_enabled
                else {}
            ),
        ),
    )


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sql: str = Field(min_length=1, max_length=30_000)
    client_request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class PageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_index: int = Field(default=0, ge=0, le=2_147_483_647)
    page_size: int = Field(default=100)
    view_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    view_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    column_key: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"]
    client_request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


def _payload(model, body):
    try:
        return model.model_validate(body)
    except ValidationError:
        raise HTTPException(422, "Invalid database execution request") from None


def _empty(body):
    if body:
        raise HTTPException(422, "Database request must be an empty JSON object")


async def _job_call(operation):
    try:
        with anyio.fail_after(15):
            return await anyio.to_thread.run_sync(operation, abandon_on_cancel=True)
    except QueryRejected:
        raise HTTPException(
            422, "Query is outside the supported read-only SQL language"
        ) from None
    except (database_registry.DatabaseUnavailable, TimeoutError):
        raise HTTPException(503, "Database operation is unavailable") from None


@router.post("/{database_id}/workspaces", status_code=201)
async def create_workspace(
    database_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    _empty(body)
    return await _job_call(
        partial(database_jobs.create_workspace, request, database_id)
    )


@router.get("/{database_id}/workspaces/{workspace_id}")
async def workspace(
    database_id: str,
    workspace_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
):
    if request.query_params:
        raise HTTPException(422, "Database request parameters are not supported")
    return await _job_call(
        partial(database_jobs.workspace, request, database_id, workspace_id)
    )


@router.post("/{database_id}/workspaces/{workspace_id}/executions", status_code=202)
async def create_execution(
    database_id: str,
    workspace_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    payload = _payload(ExecutionRequest, body)
    return await _job_call(
        partial(
            database_jobs.create_execution,
            request,
            database_id,
            workspace_id,
            payload.sql,
            payload.client_request_id,
        )
    )


@router.get("/{database_id}/workspaces/{workspace_id}/executions/{execution_id}")
async def execution(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
):
    if request.query_params:
        raise HTTPException(422, "Database request parameters are not supported")
    return await _job_call(
        partial(
            database_jobs.execution, request, database_id, workspace_id, execution_id
        )
    )


@router.post("/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/pages")
async def page(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    payload = _payload(PageRequest, body)
    if payload.page_size not in {25, 50, 100}:
        raise HTTPException(422, "Database page size must be 25, 50 or 100")
    return await _job_call(
        partial(
            database_jobs.page,
            request,
            database_id,
            workspace_id,
            execution_id,
            payload.page_index,
            payload.page_size,
            payload.view_id,
        )
    )


@router.post(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/cancel"
)
async def cancel(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    _empty(body)
    return await _job_call(
        partial(database_jobs.cancel, request, database_id, workspace_id, execution_id)
    )


@router.post(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/exports",
    status_code=202,
)
async def create_export(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    payload = _payload(ExportRequest, body)
    return await _job_call(
        partial(
            database_jobs.create_export,
            request,
            database_id,
            workspace_id,
            execution_id,
            payload.view_id,
        )
    )


@router.post(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/views",
    status_code=202,
)
async def create_view(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    payload = _payload(ViewRequest, body)
    return await _job_call(
        partial(
            database_views.create_view,
            request,
            database_id,
            workspace_id,
            execution_id,
            payload.column_key,
            payload.direction,
            payload.client_request_id,
        )
    )


@router.get(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/views/{view_id}"
)
async def get_view(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
):
    if request.query_params:
        raise HTTPException(422, "Database request parameters are not supported")
    return await _job_call(
        partial(
            database_views.view,
            request,
            database_id,
            workspace_id,
            execution_id,
            view_id,
        )
    )


@router.delete(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/views/{view_id}"
)
async def delete_view(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    _empty(body)
    return await _job_call(
        partial(
            database_views.delete_view,
            request,
            database_id,
            workspace_id,
            execution_id,
            view_id,
        )
    )


@router.get(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/exports/{export_id}"
)
async def export(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    export_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
):
    if request.query_params:
        raise HTTPException(422, "Database request parameters are not supported")
    return await _job_call(
        partial(
            database_jobs.export,
            request,
            database_id,
            workspace_id,
            execution_id,
            export_id,
        )
    )


@router.get(
    "/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/exports/{export_id}/file"
)
async def download(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    export_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
):
    if (
        request.query_params
        or request.headers.get("sec-fetch-site", "same-origin")
        not in {"same-origin", "none"}
        or request.headers.get("origin", config.auth.frontend_url)
        != config.auth.frontend_url
    ):
        raise HTTPException(403, "Invalid database download origin")
    stream, size = await _job_call(
        partial(
            database_jobs.open_download,
            request,
            database_id,
            workspace_id,
            execution_id,
            export_id,
        )
    )
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="query-{export_id}.xlsx"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{database_id}/workspaces/{workspace_id}/purge")
async def purge(
    database_id: str,
    workspace_id: str,
    request: Request,
    reader: str = Depends(require_database_reader),
    body: dict = Depends(private_request),
):
    _empty(body)
    return await _job_call(
        partial(database_jobs.purge_workspace, request, database_id, workspace_id)
    )
