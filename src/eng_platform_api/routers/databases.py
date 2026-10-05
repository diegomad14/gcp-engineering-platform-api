"""Private database console. Request bodies and provider errors never echo SQL."""

from __future__ import annotations

from functools import partial
import json
from time import monotonic

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import config
from ..security import require_database_reader
from ..services import database_console, database_registry
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
    return {"databases": [item.public() for item in values]}


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
            database, payload.sql, payload.max_rows, authorize
        ),
    )
