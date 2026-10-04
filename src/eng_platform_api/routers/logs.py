"""Authenticated read-only runtime logs, with private filters in a JSON body."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from functools import partial
from typing import Callable

import anyio

from ..config import config
from ..models import ServiceLogsRequest, ServiceLogsResponse
from ..security import log_auth_configured, require_log_reader
from ..services import log_budget, runtime_logs


class LogsDeadlineRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            # Start before body parsing/dependency resolution, including any
            # thread-pool queue. Delivery after returning the ASGI response is
            # outside our control; the browser allows another five seconds.
            deadline = log_budget.Deadline.after(config.logs.request_timeout_seconds)
            request.state.log_deadline = deadline
            try:
                with anyio.fail_after(deadline.remaining()):
                    result = await handler(request)
                    deadline.remaining()
                    return result
            except TimeoutError:
                # The synchronous owner may still be inside SDK/ADC or a call.
                # Cancelling this wait must never finalize its reservation.
                return JSONResponse(
                    {"detail": "Runtime log request timed out"},
                    status_code=503,
                    headers={
                        "Cache-Control": "no-store",
                        "Pragma": "no-cache",
                        "Retry-After": "60",
                    },
                )
            finally:
                deadline.cancelled.set()

        return bounded


router = APIRouter(
    prefix="/api/catalog/services", tags=["logs"], route_class=LogsDeadlineRoute
)


async def private_log_request(request: Request) -> None:
    """Keep filters out of URLs/access logs, and reject cross-origin requests."""
    if request.query_params:
        raise HTTPException(
            status_code=422, detail="Log filters must be in the JSON body"
        )
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(status_code=415, detail="A JSON body is required")
    if (
        request.headers.get("x-requested-with") != "EngineeringPlatform"
        or request.headers.get("origin") != config.auth.frontend_url
    ):
        raise HTTPException(status_code=403, detail="Invalid log request origin")


def _read_logs(
    service_id: str,
    filters: ServiceLogsRequest,
    deadline: log_budget.Deadline,
    reader: str,
    session_guard: Callable[[], str] | None = None,
) -> ServiceLogsResponse:
    # Catalog loading, queueing, client construction and RPC work all run off
    # the event loop. A slow SDK/ADC callback cannot hold the HTTP wait open.
    deadline.remaining()
    try:
        items = runtime_logs.resources()
    except (OSError, ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=503, detail="Runtime log catalog is unavailable"
        ) from None
    deadline.remaining()
    resource = next((item for item in items if item.service_id == service_id), None)
    if resource is None:
        raise HTTPException(status_code=404, detail="Service not found")
    if (
        not resource.can_read(reader)
        or config.mock_mode
        or not log_auth_configured()
        or reader.lower() not in config.logs.allowed_logins
    ):
        raise HTTPException(
            status_code=403, detail="You are not allowed to view these logs"
        )
    if (resource.kind == "cloud_run_job" and filters.revision is not None) or (
        resource.kind != "cloud_run_job"
        and (filters.execution is not None or filters.task_index is not None)
    ):
        raise HTTPException(
            status_code=422, detail="Filter does not apply to this runtime"
        )

    def authorize(*, allow_disabled: bool = False) -> None:
        # This callback is also run immediately before SDK dispatch, after
        # coordination AND cold client/ADC construction. A response-time check
        # alone cannot prevent a read after access was revoked during setup.
        deadline.remaining()
        if session_guard is not None and session_guard().lower() != reader.lower():
            raise HTTPException(
                status_code=401, detail="Sign in with GitHub to view logs"
            )
        try:
            current = next(
                (
                    item
                    for item in runtime_logs.resources()
                    if item.service_id == service_id
                ),
                None,
            )
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(
                status_code=503, detail="Runtime log catalog is unavailable"
            ) from None
        if (
            current is None
            or current.key != resource.key
            or current.policy_fingerprint != resource.policy_fingerprint
            or not current.can_read(reader)
            or config.mock_mode
            or not log_auth_configured()
            or reader.lower() not in config.logs.allowed_logins
            or (not allow_disabled and not config.logs.enabled)
        ):
            raise HTTPException(
                status_code=403, detail="You are not allowed to view these logs"
            )

    result = runtime_logs.read_logs(
        resource, items, deadline=deadline, authorize=authorize, **filters.model_dump()
    )
    # Authorization is never cached, including already constructed responses.
    authorize(allow_disabled=result.status == "disabled")
    deadline.remaining()
    return result


@router.post(
    "/{service_id}/logs",
    response_model=ServiceLogsResponse,
    dependencies=[Depends(private_log_request)],
)
async def service_logs(
    service_id: str,
    filters: ServiceLogsRequest,
    request: Request,
    response: Response,
    _reader: str = Depends(require_log_reader),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if await request.is_disconnected():
        raise HTTPException(status_code=499, detail="Client disconnected")
    result = await anyio.to_thread.run_sync(
        partial(
            _read_logs,
            service_id,
            filters,
            request.state.log_deadline,
            _reader,
            partial(require_log_reader, request),
        ),
        abandon_on_cancel=True,
    )
    require_log_reader(request)
    return result
