"""Keep synchronous release events ordered without occupying the API worker pool."""

from asyncio import CancelledError, Future, get_running_loop, shield
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Callable, ParamSpec, TypeVar


_P = ParamSpec("_P")
_T = TypeVar("_T")

# These handlers previously ran serially on the event loop within each process.
# Retain that ordering on one dedicated worker; queued events must not consume
# AnyIO tokens needed by unrelated synchronous routes and authorization checks.
# This is process-local, not a replacement for durable cross-replica claims.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="release-events")


def _consume_cancelled_request_result(future: Future) -> None:
    # The caller is gone, but admitted processing still completes in order.
    # Retrieve failures without logging potentially sensitive provider details.
    if not future.cancelled():
        future.exception()


async def run_event_processing(
    function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Await ordered event processing, preserving caller context and exceptions."""
    context = copy_context()
    call = partial(function, *args, **kwargs)
    future = get_running_loop().run_in_executor(_executor, context.run, call)
    try:
        # Once the body has been read and work admitted, a disconnected request
        # must not cancel queued side effects that previously ran synchronously.
        return await shield(future)
    except CancelledError:
        future.add_done_callback(_consume_cancelled_request_result)
        raise
