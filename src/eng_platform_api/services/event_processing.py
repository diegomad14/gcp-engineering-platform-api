"""Keep synchronous release events ordered without occupying the API worker pool."""

from asyncio import CancelledError, Future, get_running_loop, shield, wrap_future
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from threading import BoundedSemaphore
from typing import Callable, ParamSpec, TypeVar

from fastapi import HTTPException


_P = ParamSpec("_P")
_T = TypeVar("_T")

# These handlers previously ran serially on the event loop within each process.
# Retain that ordering on one dedicated worker; queued events must not consume
# AnyIO tokens needed by unrelated synchronous routes and authorization checks.
# This is process-local, not a replacement for durable cross-replica claims.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="release-events")
_MAX_EVENTS = 32
_slots = BoundedSemaphore(_MAX_EVENTS)


def _consume_cancelled_request_result(future: Future) -> None:
    # The caller is gone, but admitted processing still completes in order.
    # Retrieve failures without logging potentially sensitive provider details.
    if not future.cancelled():
        future.exception()


async def run_event_processing(
    function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Await ordered event processing, preserving caller context and exceptions."""
    loop = get_running_loop()
    slots = _slots
    if not slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Event processing is temporarily unavailable",
            headers={"Retry-After": "5"},
        )
    try:
        context = copy_context()
        call = partial(function, *args, **kwargs)
        submitted = _executor.submit(context.run, call)
    except BaseException:
        slots.release()
        raise
    # The concurrent future owns the permit, not its HTTP waiter. This callback
    # runs even if the request is cancelled or its event loop has already closed.
    submitted.add_done_callback(lambda _: slots.release())
    future = wrap_future(submitted, loop=loop)
    try:
        # Once the body has been read and work admitted, a disconnected request
        # must not cancel queued side effects that previously ran synchronously.
        return await shield(future)
    except CancelledError:
        future.add_done_callback(_consume_cancelled_request_result)
        raise
