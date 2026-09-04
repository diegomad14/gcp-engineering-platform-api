"""OIDC-bound internal interface for the engine's trusted release workflow."""

import json
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..services import (
    central_releases,
    github_deployments,
    release_authorization,
    release_plan,
)

router = APIRouter(
    prefix="/api/internal/central-releases",
    tags=["internal"],
    responses={
        401: {"description": "Invalid workflow identity or authorization"},
        409: {"description": "Release state conflict"},
        422: {"description": "Invalid request"},
        503: {"description": "Release integration unavailable"},
    },
)


def _failure(exc):
    if isinstance(exc, release_authorization.ReleaseAuthorizationError):
        return HTTPException(401, "Invalid release authorization or workflow identity")
    if isinstance(exc, release_plan.ReleasePlanError):
        return HTTPException(409, "Release state conflict")
    return HTTPException(503, "Release integration unavailable")


async def _body(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 100_000:
            raise HTTPException(422, "Invalid release request")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise HTTPException(422, "Invalid release request") from None


@router.post("/consume")
async def consume(request: Request):
    data = await _body(request)
    if set(data) != {"token"} or not isinstance(data["token"], str):
        raise HTTPException(422, "Invalid release request")
    try:
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            central_releases.consume,
            data["token"],
            request.headers.get("x-github-oidc", ""),
        )
    except Exception as exc:
        raise _failure(exc) from None


@router.get("/{execution_id}/source")
def source(execution_id: str, request: Request):
    try:
        execution_id = str(UUID(execution_id))
        _, record = central_releases.authorized_execution(
            execution_id, request.headers.get("x-github-oidc", "")
        )
        plan = record["plan"]
        repo = github_deployments.github_client().get_repo(plan["repository"])
        archive = repo.get_archive_link("tarball", ref=plan["sha"])
        parsed = urlparse(archive)
        if parsed.scheme != "https" or parsed.hostname != "codeload.github.com":
            raise RuntimeError("Unexpected source archive host")
        # urllib does not log the private redirect URL or authorization headers.
        upstream = urlopen(archive, timeout=30)  # nosec B310 -- fixed HTTPS host checked above
    except Exception as exc:
        raise _failure(exc) from None

    def chunks():
        try:
            with upstream:
                while chunk := upstream.read(65536):
                    yield chunk
        except Exception:
            raise RuntimeError("Release source transfer failed") from None

    return StreamingResponse(
        chunks(), media_type="application/gzip", headers={"Cache-Control": "no-store"}
    )


@router.get("/{execution_id}/context")
def context(execution_id: str, request: Request):
    try:
        _, record = central_releases.authorized_execution(
            str(UUID(execution_id)), request.headers.get("x-github-oidc", "")
        )
        return {"plan": record["plan"]}
    except Exception as exc:
        raise _failure(exc) from None


@router.post("/{execution_id}/result")
async def report(execution_id: str, request: Request):
    data = await _body(request)
    allowed = {
        "status",
        "image_digest",
        "candidate_revision",
        "production_revision",
        "runtime_snapshot",
    }
    if not set(data).issubset(allowed) or "status" not in data:
        raise HTTPException(422, "Invalid release result")
    try:
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(
            central_releases.report,
            str(UUID(execution_id)),
            request.headers.get("x-github-oidc", ""),
            data,
        )
    except Exception as exc:
        raise _failure(exc) from None
    return {"accepted": True}


@router.post("/{execution_id}/checkpoint")
async def checkpoint(execution_id: str, request: Request):
    data = await _body(request)
    try:
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(
            central_releases.checkpoint,
            str(UUID(execution_id)),
            request.headers.get("x-github-oidc", ""),
            data,
        )
    except Exception as exc:
        raise _failure(exc) from None
    return {"accepted": True}
