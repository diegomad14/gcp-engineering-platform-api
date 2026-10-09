"""Offline races around release intent, dispatch and callback replay boundaries."""

from concurrent.futures import Future as ConcurrentFuture, ThreadPoolExecutor
from contextvars import ContextVar
from functools import wraps
from threading import Barrier, BoundedSemaphore, Event, RLock
from unittest.mock import Mock

import anyio
import asyncio
import hashlib

import httpx
import pytest
from fastapi import HTTPException

from eng_platform_api.main import app
from eng_platform_api.config import config
from eng_platform_api.models import ReleaseExecutionEvent
from eng_platform_api.routers import github_events
from eng_platform_api.services import event_processing
from eng_platform_api.routers import release_execution_events as events
from eng_platform_api.services import release_cloud_build as cloud_build
from eng_platform_api.services import release_executions as executions
from tests.test_release_executions import _Collection, _reserve


@pytest.fixture(params=["memory", "transactional_double"])
def execution_store(request, monkeypatch):
    """Exercise both branches with a deterministic, atomic Firestore double."""
    if request.param == "memory":
        monkeypatch.setattr(executions, "_collection", lambda: None)
        return
    from google.cloud import firestore

    collection = _Collection()
    lock = RLock()

    def transactional(function):
        @wraps(function)
        def atomic(*args, **kwargs):
            with lock:
                return function(*args, **kwargs)

        return atomic

    monkeypatch.setattr(firestore, "transactional", transactional)
    monkeypatch.setattr(executions, "_collection", lambda: collection)


def test_concurrent_intents_reserve_one_execution(execution_store):
    ready = Barrier(2)

    def reserve():
        ready.wait(timeout=5)
        return _reserve(provider="cloud_build")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(created for _, created in results) == 1
    assert len({value["execution_id"] for value, _ in results}) == 1


def test_concurrent_submission_creates_only_one_build(execution_store, monkeypatch):
    execution, _ = _reserve(provider="cloud_build")
    execution_id = execution["execution_id"]
    matching = Barrier(2)
    dispatched, release, loser_finished = Event(), Event(), Event()
    requests = []
    monkeypatch.setattr(cloud_build, "require_managed", lambda service: service)

    def no_existing_build(_):
        matching.wait(timeout=5)
        return None

    def create(*args, **kwargs):
        requests.append(kwargs["json"])
        dispatched.set()
        assert release.wait(5), "Test did not release the fake Cloud Build POST"
        return Mock(
            status_code=200,
            json=lambda: {
                "metadata": {
                    "build": {
                        "id": "single-build",
                        "status": "QUEUED",
                        "substitutions": {"_REQUEST_FINGERPRINT": execution_id},
                    }
                }
            },
        )

    monkeypatch.setattr(cloud_build, "_matching_build", no_existing_build)
    monkeypatch.setattr(
        cloud_build, "build_request", lambda *_: {"intent": execution_id}
    )
    monkeypatch.setattr(cloud_build, "validate_submission", lambda _: None)
    monkeypatch.setattr(cloud_build, "_session", lambda: Mock(post=create))
    monkeypatch.setattr(cloud_build, "_location", lambda: "offline/builds")

    def submit():
        try:
            return cloud_build.submit(execution_id, object())
        except cloud_build.ReleaseCloudBuildError as exc:
            loser_finished.set()
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        try:
            assert dispatched.wait(5)
            assert loser_finished.wait(5)
            assert len(requests) == 1
            assert executions.get(execution_id)["status"] == "submitting"
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    successes = [result for result in results if isinstance(result, dict)]
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == len(errors) == 1
    assert successes[0]["build_id"] == "single-build"
    # Existing behavior: the concurrent loser reports a reconciliation conflict;
    # it must not retry dispatch while the winner's POST is still unfinished.
    assert str(errors[0]) == "Release build submission is being reconciled"
    assert executions.get(execution_id)["build_id"] == "single-build"
    assert requests == [{"intent": execution_id}]


async def test_concurrent_duplicate_callbacks_accept_and_reconcile_once(
    execution_store, monkeypatch
):
    execution, _ = _reserve(provider="cloud_build")
    execution_id = execution["execution_id"]
    executions.save(
        execution_id,
        build_id="single-build",
        provider_run_id="single-build",
        event_token_hash=hashlib.sha256(b"offline-event-token").hexdigest(),
    )
    # Both callbacks have read sequence=0 before either reaches atomic acceptance.
    verified = Barrier(2)

    def verify_provider(*_):
        verified.wait(timeout=5)

    monkeypatch.setattr(events, "_verify_provider", verify_provider)
    reconcile = Mock(side_effect=executions.get)
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)
    payload = {
        "execution_id": execution_id,
        "provider_run_id": "single-build",
        "fingerprint": execution_id,
        "sequence": 1,
        "status": "running_quality",
    }
    # Bypass the process-local HTTP queue to model two separate replicas.
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                asyncio.to_thread(
                    events._accept_event,
                    execution_id,
                    ReleaseExecutionEvent(**payload),
                    "Bearer offline-identity",
                    "offline-event-token",
                )
                for _ in range(2)
            )
        ),
        timeout=10,
    )
    assert sorted(result["accepted"] for result in results) == [False, True]
    assert all(result["event_sequence"] == 1 for result in results)
    reconcile.assert_called_once_with(execution_id)
    assert executions.get(execution_id)["event_sequence"] == 1


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [
        ("webhook", "webhook"),
        ("callback", "callback"),
        ("webhook", "callback"),
        ("callback", "webhook"),
    ],
)
async def test_http_events_share_one_ordered_worker_and_leave_ui_pool_free(
    monkeypatch, first_kind, second_kind
):
    execution, _ = _reserve(provider="cloud_build")
    execution_id = execution["execution_id"]
    executions.save(
        execution_id,
        build_id="single-build",
        provider_run_id="single-build",
        event_token_hash=hashlib.sha256(b"offline-event-token").hexdigest(),
    )
    monkeypatch.setattr(config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(config.release_orchestrator, "webhook_secret", "")
    monkeypatch.setattr(config.github, "installation_id", "")
    monkeypatch.setattr(
        github_events.catalog, "get_services_by_repository", lambda _: [object()]
    )
    dispatch = Mock(return_value=[execution])
    monkeypatch.setattr(github_events.release_orchestrator, "handle_push", dispatch)
    monkeypatch.setattr(events, "_verify_provider", Mock())
    reconcile = Mock(side_effect=executions.get)
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    loop = asyncio.get_running_loop()
    entered, queued = asyncio.Event(), asyncio.Event()
    release = Event()
    timeline = []
    submit_count = 0
    original_submit = event_processing._executor.submit

    def record_submission(*args, **kwargs):
        nonlocal submit_count
        future = original_submit(*args, **kwargs)
        submit_count += 1
        if submit_count == 2:
            loop.call_soon_threadsafe(queued.set)
        return future

    monkeypatch.setattr(event_processing._executor, "submit", record_submission)

    def tracked(kind, function):
        def run(*args, **kwargs):
            timeline.append(f"{kind}:start")
            if len(timeline) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(10), "Test did not release the first event"
            result = function(*args, **kwargs)
            timeline.append(f"{kind}:finish")
            return result

        return run

    monkeypatch.setattr(
        github_events,
        "_process_event",
        tracked("webhook", github_events._process_event),
    )
    monkeypatch.setattr(
        events, "_accept_event", tracked("callback", events._accept_event)
    )

    async def request(browser, kind):
        if kind == "webhook":
            return await browser.post(
                "/api/internal/github/events",
                json={"repository": {"full_name": "owner/repository"}},
                headers={
                    "X-GitHub-Delivery": "offline-delivery",
                    "X-GitHub-Event": "push",
                },
            )
        return await browser.post(
            f"/api/internal/release-executions/{execution_id}/events",
            json={
                "execution_id": execution_id,
                "provider_run_id": "single-build",
                "fingerprint": execution_id,
                "sequence": 1,
                "status": "running_quality",
            },
            headers={
                "Authorization": "Bearer offline-identity",
                "X-Eng-Platform-Event-Token": "offline-event-token",
            },
        )

    limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    tasks = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as browser:
            tasks.append(asyncio.create_task(request(browser, first_kind)))
            await asyncio.wait_for(entered.wait(), 5)
            tasks.append(asyncio.create_task(request(browser, second_kind)))
            await asyncio.wait_for(queued.wait(), 5)
            assert timeline == [f"{first_kind}:start"]
            assert all(not task.done() for task in tasks)
            health = await asyncio.wait_for(browser.get("/health"), 5)
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert all(not task.done() for task in tasks)
            assert timeline == [f"{first_kind}:start"]
    finally:
        release.set()
        try:
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        finally:
            limiter.total_tokens = original_tokens
    assert [response.status_code for response in responses] == [200, 200]
    assert timeline == [
        f"{first_kind}:start",
        f"{first_kind}:finish",
        f"{second_kind}:start",
        f"{second_kind}:finish",
    ]
    assert dispatch.call_count == (1 if "webhook" in {first_kind, second_kind} else 0)
    assert reconcile.call_count == (1 if "callback" in {first_kind, second_kind} else 0)
    if first_kind == second_kind == "webhook":
        assert [response.json()["duplicate"] for response in responses] == [False, True]
    if first_kind == second_kind == "callback":
        assert [response.json()["accepted"] for response in responses] == [True, False]


def test_event_worker_preserves_context_across_separate_event_loops():
    value = ContextVar("event-worker-test", default="unset")

    def read_and_mutate():
        result = value.get()
        value.set("worker-local")
        return result

    for expected in ("first-loop", "second-loop"):
        token = value.set(expected)
        try:
            assert (
                asyncio.run(event_processing.run_event_processing(read_and_mutate))
                == expected
            )
            assert value.get() == expected
        finally:
            value.reset(token)


async def test_event_worker_propagates_errors_and_remains_available():
    error = ValueError("offline-event-error")

    def fail():
        raise error

    with pytest.raises(ValueError) as raised:
        await event_processing.run_event_processing(fail)
    assert raised.value is error
    assert (
        await event_processing.run_event_processing(
            lambda *, result: result, result="next"
        )
        == "next"
    )


@pytest.mark.parametrize("raises", [False, True])
async def test_cancelled_queued_request_keeps_admitted_work_in_order(
    monkeypatch, raises
):
    loop = asyncio.get_running_loop()
    entered, queued = asyncio.Event(), asyncio.Event()
    release = Event()
    timeline = []
    original_submit = event_processing._executor.submit
    submissions = 0
    unhandled = []
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    def submit(*args, **kwargs):
        nonlocal submissions
        future = original_submit(*args, **kwargs)
        submissions += 1
        if submissions == 2:
            loop.call_soon_threadsafe(queued.set)
        return future

    monkeypatch.setattr(event_processing._executor, "submit", submit)

    def first():
        timeline.append("first:start")
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        timeline.append("first:finish")
        return "first"

    def cancelled_request():
        timeline.append("cancelled:processed")
        if raises:
            raise ValueError("offline-provider-failure")
        return "cancelled"

    first_task = asyncio.create_task(event_processing.run_event_processing(first))
    cancelled_task = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancelled_task = asyncio.create_task(
            event_processing.run_event_processing(cancelled_request)
        )
        await asyncio.wait_for(queued.wait(), 5)
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task
        assert timeline == ["first:start"]
        release.set()
        assert await asyncio.wait_for(first_task, 5) == "first"
        await asyncio.wait_for(
            event_processing.run_event_processing(lambda: timeline.append("last")),
            5,
        )
        assert timeline == [
            "first:start",
            "first:finish",
            "cancelled:processed",
            "last",
        ]
        assert unhandled == []
    finally:
        release.set()
        await asyncio.wait_for(first_task, 5)
        if cancelled_task is not None:
            await asyncio.gather(cancelled_task, return_exceptions=True)
        loop.set_exception_handler(original_handler)


class ControlledEventExecutor:
    """Hold actual concurrent futures without dispatching threads or using sleeps."""

    def __init__(self):
        self.calls = []
        self.submitted = asyncio.Queue()

    def submit(self, function, *args):
        future = ConcurrentFuture()
        self.calls.append((future, function, args))
        self.submitted.put_nowait(future)
        return future

    def finish(self, index):
        future, function, args = self.calls[index]
        if future.set_running_or_notify_cancel():
            try:
                result = function(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)


async def test_event_admission_is_bounded_and_cancelled_waiters_retain_slots(
    monkeypatch,
):
    assert event_processing._MAX_EVENTS == 32
    executor = ControlledEventExecutor()
    monkeypatch.setattr(event_processing, "_executor", executor)
    monkeypatch.setattr(event_processing, "_slots", BoundedSemaphore(32))
    processed, rejected = Mock(return_value="processed"), Mock()
    tasks = [
        asyncio.create_task(event_processing.run_event_processing(processed))
        for _ in range(32)
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(executor.submitted.get() for _ in range(32))), 5
        )
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert all(not future.done() for future, _, _ in executor.calls)
        with pytest.raises(HTTPException) as error:
            await event_processing.run_event_processing(rejected)
        assert error.value.status_code == 503
        assert error.value.detail == "Event processing is temporarily unavailable"
        assert error.value.headers == {"Retry-After": "5"}
        assert len(executor.calls) == 32
        rejected.assert_not_called()
        processed.assert_not_called()

        executor.finish(0)
        replacement = asyncio.create_task(
            event_processing.run_event_processing(processed)
        )
        tasks.append(replacement)
        await asyncio.wait_for(executor.submitted.get(), 5)
        assert len(executor.calls) == 33
        with pytest.raises(HTTPException):
            await event_processing.run_event_processing(rejected)
        rejected.assert_not_called()
    finally:
        for index, (future, _, _) in enumerate(executor.calls):
            if not future.done():
                executor.finish(index)
        await asyncio.gather(*tasks, return_exceptions=True)
    assert processed.call_count == 33


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_event_slot_returns_only_when_concurrent_future_finishes(
    monkeypatch, outcome
):
    executor = ControlledEventExecutor()
    monkeypatch.setattr(event_processing, "_executor", executor)
    monkeypatch.setattr(event_processing, "_slots", BoundedSemaphore(1))
    error = ValueError("offline-provider-error")
    operation = Mock(return_value="done")
    if outcome == "error":
        operation.side_effect = error
    task = asyncio.create_task(event_processing.run_event_processing(operation))
    future = await asyncio.wait_for(executor.submitted.get(), 5)
    with pytest.raises(HTTPException):
        await event_processing.run_event_processing(lambda: "rejected")
    if outcome == "cancel":
        assert future.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operation.assert_not_called()
    else:
        executor.finish(0)
        if outcome == "error":
            with pytest.raises(ValueError) as raised:
                await task
            assert raised.value is error
        else:
            assert await task == "done"
    replacement = asyncio.create_task(
        event_processing.run_event_processing(lambda: "recovered")
    )
    await asyncio.wait_for(executor.submitted.get(), 5)
    executor.finish(1)
    assert await replacement == "recovered"


async def test_failed_event_submission_returns_its_slot(monkeypatch):
    monkeypatch.setattr(event_processing, "_slots", BoundedSemaphore(1))
    failed = RuntimeError("offline-executor-unavailable")
    monkeypatch.setattr(
        event_processing, "_executor", Mock(submit=Mock(side_effect=failed))
    )
    with pytest.raises(RuntimeError) as raised:
        await event_processing.run_event_processing(lambda: "not-submitted")
    assert raised.value is failed

    executor = ControlledEventExecutor()
    monkeypatch.setattr(event_processing, "_executor", executor)
    task = asyncio.create_task(
        event_processing.run_event_processing(lambda: "recovered")
    )
    await asyncio.wait_for(executor.submitted.get(), 5)
    executor.finish(0)
    assert await task == "recovered"


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_event_slot_returns_after_request_loop_has_closed(monkeypatch, outcome):
    executor = ControlledEventExecutor()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(event_processing, "_executor", executor)
    monkeypatch.setattr(event_processing, "_slots", slots)
    operation = Mock(return_value="done")
    if outcome == "error":
        operation.side_effect = ValueError("offline-provider-error")

    async def admit_and_disconnect():
        task = asyncio.create_task(event_processing.run_event_processing(operation))
        await asyncio.wait_for(executor.submitted.get(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(admit_and_disconnect())
    assert not slots.acquire(blocking=False)
    future = executor.calls[0][0]
    if outcome == "cancel":
        assert future.cancel()
        operation.assert_not_called()
    else:
        executor.finish(0)
    assert slots.acquire(blocking=False)
    slots.release()

    # A new request loop uses the same semaphore after the old loop has closed.
    recovered = ControlledEventExecutor()
    monkeypatch.setattr(event_processing, "_executor", recovered)

    async def next_request():
        task = asyncio.create_task(
            event_processing.run_event_processing(lambda: "recovered")
        )
        await asyncio.wait_for(recovered.submitted.get(), 5)
        recovered.finish(0)
        assert await task == "recovered"

    asyncio.run(next_request())


async def test_running_cancelled_event_retains_slot_until_worker_returns(monkeypatch):
    monkeypatch.setattr(event_processing, "_slots", BoundedSemaphore(1))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()
    submitted = []
    original_submit = event_processing._executor.submit

    def capture(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        submitted.append(future)
        return future

    monkeypatch.setattr(event_processing._executor, "submit", capture)

    def run():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        return "finished"

    task = asyncio.create_task(event_processing.run_event_processing(run))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert submitted[0].running()
        with pytest.raises(HTTPException):
            await event_processing.run_event_processing(lambda: "rejected")
        assert len(submitted) == 1
    finally:
        release.set()
        if submitted:
            assert (
                await asyncio.wait_for(asyncio.wrap_future(submitted[0]), 5)
                == "finished"
            )
        await asyncio.gather(task, return_exceptions=True)
    assert (
        await event_processing.run_event_processing(lambda: "recovered") == "recovered"
    )


@pytest.mark.parametrize("kind", ["webhook", "callback"])
async def test_full_event_queue_returns_generic_http_503_before_processing(
    monkeypatch, kind
):
    monkeypatch.setattr(config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(event_processing, "_slots", BoundedSemaphore(0))
    submit = Mock(side_effect=AssertionError("A full queue must not submit work"))
    monkeypatch.setattr(event_processing._executor, "submit", submit)
    webhook, callback = Mock(), Mock()
    monkeypatch.setattr(github_events, "_process_event", webhook)
    monkeypatch.setattr(events, "_accept_event", callback)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as browser:
        if kind == "webhook":
            response = await browser.post(
                "/api/internal/github/events",
                json={"repository": {"full_name": "owner/repo"}},
            )
        else:
            response = await browser.post(
                "/api/internal/release-executions/offline/events",
                json={
                    "execution_id": "offline",
                    "provider_run_id": "build",
                    "fingerprint": "f" * 64,
                    "sequence": 1,
                    "status": "running_quality",
                },
            )
        assert response.status_code == 503
        assert response.json() == {
            "detail": "Event processing is temporarily unavailable"
        }
        assert response.headers["retry-after"] == "5"
        assert (await browser.get("/health")).status_code == 200
    submit.assert_not_called()
    webhook.assert_not_called()
    callback.assert_not_called()
