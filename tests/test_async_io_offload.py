"""Blocking provider work must leave the ASGI loop free without early replies."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from eng_platform_api import main
from eng_platform_api.config import config
from eng_platform_api.routers import auth, github_events, release_execution_events
from eng_platform_api.routers import secrets as secret_routes
from eng_platform_api.services import mcp_auth


class BlockingIO:
    """Release a fake synchronous provider explicitly, never on a timing guess."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = get_ident()
        self.entered = asyncio.Event()
        self.release = Event()

    def __call__(self):
        # Fail immediately rather than deadlock if the regression returns.
        self.loop.call_soon_threadsafe(self.entered.set)
        assert get_ident() != self.loop_thread, "Provider blocked the ASGI loop"
        assert self.release.wait(10), "Test did not release the fake provider"


def block_mock(stub, gate):
    result = stub.return_value

    def blocked(*args, **kwargs):
        gate()
        return result

    stub.side_effect = blocked


@pytest.fixture
async def browser():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
    ) as client:
        yield client


async def while_blocked(browser, operation, gate):
    task = asyncio.create_task(operation)
    try:
        # These timeouts are deadlock guards, not performance thresholds.
        await asyncio.wait_for(gate.entered.wait(), 5)
        assert not task.done(), "Request finished before its provider returned"
        response = await asyncio.wait_for(browser.get("/"), 5)
        assert response.status_code == 200
        assert response.json()["service"] == "Engineering Platform API"
        assert not task.done(), "Request acknowledged unfinished provider work"
    finally:
        gate.release.set()
        result = await asyncio.wait_for(task, 5)
    return result


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(config.release_orchestrator, "webhook_secret", "test-secret")
    monkeypatch.setattr(config.github, "installation_id", "123")
    monkeypatch.setattr(config, "mock_mode", False)
    calls = Mock()
    stages = {
        "catalog": (github_events.catalog, "get_services_by_repository", [object()]),
        "identity": (github_events, "verify_webhook_identity", True),
        "receive": (
            github_events.github_webhooks,
            "receive",
            ({"status": "received"}, True),
        ),
        "orchestrate": (
            github_events.release_orchestrator,
            "handle_push",
            [{"execution_id": "execution-1"}],
        ),
        "complete": (github_events.github_webhooks, "complete", None),
    }
    for name, (owner, attribute, result) in stages.items():
        stub = Mock(return_value=result)
        calls.attach_mock(stub, name)
        monkeypatch.setattr(owner, attribute, stub)
    payload = {
        "repository": {"full_name": "owner/repo", "id": 42},
        "installation": {"id": 123},
    }
    return calls, payload


def webhook_headers(body, **overrides):
    return {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest(),
        **overrides,
    }


@pytest.mark.parametrize(
    "stage", ["catalog", "identity", "receive", "orchestrate", "complete"]
)
async def test_webhook_io_leaves_other_requests_responsive_and_waits_for_completion(
    browser, webhook, stage
):
    calls, payload = webhook
    gate = BlockingIO()
    block_mock(getattr(calls, stage), gate)
    body = json.dumps(payload).encode()
    response = await while_blocked(
        browser,
        browser.post(
            "/api/internal/github/events", content=body, headers=webhook_headers(body)
        ),
        gate,
    )
    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "duplicate": False,
        "execution_ids": ["execution-1"],
    }
    assert [call[0] for call in calls.mock_calls] == [
        "catalog",
        "identity",
        "receive",
        "orchestrate",
        "complete",
    ]
    calls.receive.assert_called_once_with(
        delivery_id="delivery-1", event="push", repository="owner/repo", body=body
    )
    calls.complete.assert_called_once_with(
        "delivery-1", outcome="accepted", execution_ids=["execution-1"]
    )


@pytest.mark.parametrize("event", ["pull_request", "workflow_run"])
async def test_webhook_keeps_event_dispatch(browser, webhook, monkeypatch, event):
    calls, payload = webhook
    monkeypatch.setattr(
        github_events.release_orchestrator, f"handle_{event}", calls.orchestrate
    )
    body = json.dumps(payload).encode()
    response = await browser.post(
        "/api/internal/github/events",
        content=body,
        headers=webhook_headers(body, **{"X-GitHub-Event": event}),
    )
    assert response.status_code == 200
    calls.orchestrate.assert_called_once_with(payload, delivery_id="delivery-1")


@pytest.mark.parametrize(
    ("failure", "status", "expected_calls"),
    [
        ("disabled", 404, []),
        ("content_length", 400, []),
        ("declared_size", 413, []),
        ("actual_size", 413, []),
        ("signature", 401, []),
        ("json", 400, []),
        ("payload", 400, []),
        ("repository", 403, ["catalog"]),
        ("identity", 403, ["catalog", "identity"]),
        ("installation", 403, ["catalog", "identity"]),
        ("delivery", 409, ["catalog", "identity", "receive"]),
        ("event", 400, ["catalog", "identity", "receive"]),
        (
            "orchestrate",
            409,
            ["catalog", "identity", "receive", "orchestrate", "complete"],
        ),
    ],
)
async def test_webhook_keeps_fail_closed_order(
    browser, webhook, monkeypatch, failure, status, expected_calls
):
    calls, payload = webhook
    if failure == "disabled":
        monkeypatch.setattr(config.release_orchestrator, "enabled", False)
    elif failure == "repository":
        calls.catalog.return_value = []
    elif failure == "identity":
        calls.identity.return_value = False
    elif failure == "installation":
        payload["installation"]["id"] = 999
    elif failure == "delivery":
        calls.receive.side_effect = ValueError("Delivery payload changed")
    elif failure == "orchestrate":
        calls.orchestrate.side_effect = (
            github_events.release_orchestrator.ReleaseOrchestratorError("rejected")
        )
    body = json.dumps(payload).encode()
    if failure == "json":
        body = b"{"
    elif failure == "payload":
        body = b"[]"
    elif failure == "actual_size":
        body = b"x" * 2_000_001
    headers = webhook_headers(body)
    if failure == "content_length":
        headers["Content-Length"] = "invalid"
    elif failure == "declared_size":
        headers["Content-Length"] = "2000001"
    elif failure == "actual_size":
        headers["Content-Length"] = "0"
    elif failure == "signature":
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    elif failure == "event":
        headers["X-GitHub-Event"] = "unsupported"
    response = await browser.post(
        "/api/internal/github/events", content=body, headers=headers
    )
    assert response.status_code == status
    assert [call[0] for call in calls.mock_calls] == expected_calls
    if failure == "orchestrate":
        calls.complete.assert_called_once_with(
            "delivery-1", outcome="rejected", error="rejected"
        )


async def test_processed_webhook_replay_does_not_reexecute(browser, webhook):
    calls, payload = webhook
    calls.receive.return_value = ({"status": "processed"}, False)
    body = json.dumps(payload).encode()
    response = await browser.post(
        "/api/internal/github/events", content=body, headers=webhook_headers(body)
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "duplicate": True}
    calls.orchestrate.assert_not_called()
    calls.complete.assert_not_called()


@pytest.mark.parametrize("stage", ["get", "verify", "accept", "reconcile"])
async def test_release_callback_offloads_io_in_authenticated_order(
    browser, monkeypatch, stage
):
    fingerprint = "f" * 64
    state = {
        "execution_id": "execution-1",
        "fingerprint": fingerprint,
        "event_sequence": 0,
        "status": "running_quality",
        "event_token_hash": hashlib.sha256(b"test-event-token").hexdigest(),
    }
    updated = {**state, "event_sequence": 1}
    calls = Mock()
    stages = {
        "get": (release_execution_events.release_executions, "get", state),
        "verify": (release_execution_events, "_verify_provider", None),
        "accept": (
            release_execution_events.release_executions,
            "accept_event",
            (updated, True),
        ),
        "reconcile": (
            release_execution_events.release_reconciler,
            "reconcile",
            updated,
        ),
    }
    for name, (owner, attribute, value) in stages.items():
        stub = Mock(return_value=value)
        calls.attach_mock(stub, name)
        monkeypatch.setattr(owner, attribute, stub)
    gate = BlockingIO()
    block_mock(getattr(calls, stage), gate)
    response = await while_blocked(
        browser,
        browser.post(
            "/api/internal/release-executions/execution-1/events",
            json={
                "execution_id": "execution-1",
                "provider_run_id": "build-1",
                "fingerprint": fingerprint,
                "sequence": 1,
                "status": "running_quality",
            },
            headers={
                "Authorization": "Bearer test-identity",
                "X-Eng-Platform-Event-Token": "test-event-token",
            },
        ),
        gate,
    )
    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "event_sequence": 1,
        "status": "running_quality",
    }
    assert [call[0] for call in calls.mock_calls] == [
        "get",
        "verify",
        "accept",
        "reconcile",
    ]
    calls.verify.assert_called_once_with(state, "build-1", "Bearer test-identity")


@pytest.mark.parametrize("name", ["can_view_catalog", "can_query_databases"])
async def test_session_capability_does_not_block_other_requests(
    browser, monkeypatch, name
):
    gate = BlockingIO()
    capability = Mock(return_value=True)
    block_mock(capability, gate)
    monkeypatch.setattr(auth, name, capability)
    response = await while_blocked(browser, browser.get("/api/auth/me"), gate)
    assert response.status_code == 200
    assert response.json()[name] is True
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("outcome", ["allowed", "revoked", "source_changed"])
async def test_post_response_catalog_revalidation_is_awaited_and_fail_closed(
    browser, monkeypatch, outcome
):
    gate = BlockingIO()
    source = ("/catalog.json", "f" * 64)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/catalog/services",
            "headers": [],
            "session": {},
        }
    )
    request.state.private_catalog_source = source
    calls = []

    async def next_response(received):
        assert received is request
        calls.append("handler")
        return JSONResponse({"private": "metadata"})

    def revalidate(received):
        assert received is request
        calls.append("revalidate")
        gate()
        if outcome == "revoked":
            raise HTTPException(403, "You are not allowed to view this metadata")

    def current_source():
        calls.append("source")
        return ("/changed.json", "a" * 64) if outcome == "source_changed" else source

    monkeypatch.setattr(main, "require_private_metadata_request", revalidate)
    monkeypatch.setattr(main, "catalog_source_identity", current_source)
    monkeypatch.setattr(main, "is_private_metadata_request", lambda _: True)
    response = await while_blocked(
        browser, main.record_request_duration(request, next_response), gate
    )
    expected_status = {"allowed": 200, "revoked": 403, "source_changed": 503}[outcome]
    assert response.status_code == expected_status
    assert calls == (
        ["handler", "revalidate"]
        if outcome == "revoked"
        else ["handler", "revalidate", "source"]
    )
    if outcome != "allowed":
        assert b"private" not in response.body
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Cookie"
    assert float(response.headers["x-process-time-ms"]) >= 0


@pytest.mark.parametrize("stage", ["select", "publish"])
async def test_secret_readiness_and_publish_are_awaited_off_loop(
    browser, monkeypatch, stage
):
    secret = SimpleNamespace(key="API_PASSWORD", editable=True)
    service = SimpleNamespace(operational_secrets=[secret])
    calls = Mock()
    calls.select.return_value = service
    calls.publish.return_value = {"status": "SAVED", "generation": 1}
    monkeypatch.setattr(secret_routes, "selected_service", calls.select)
    monkeypatch.setattr(secret_routes.operational_secrets, "publish", calls.publish)
    gate = BlockingIO()
    block_mock(getattr(calls, stage), gate)
    operation_id = "00000000-0000-4000-8000-000000000001"
    response = await while_blocked(
        browser,
        browser.post(
            "/api/services/example/secrets/API_PASSWORD/versions",
            json={"value": "synthetic-secret", "generation": 0},
            headers={
                "Origin": config.auth.frontend_url,
                "X-Requested-With": "EngineeringPlatform",
                "Idempotency-Key": operation_id,
            },
        ),
        gate,
    )
    assert response.status_code == 201
    assert response.json() == {"status": "SAVED", "generation": 1}
    assert response.headers["cache-control"] == "no-store"
    assert [call[0] for call in calls.mock_calls] == ["select", "publish"]
    calls.publish.assert_called_once_with(
        service, secret, "synthetic-secret", operation_id, 0, "diegomad14"
    )


@pytest.mark.parametrize("stage", ["state", "consent"])
async def test_oauth_callback_offloads_state_and_consent_io(
    browser, monkeypatch, stage
):
    monkeypatch.setattr(config.mcp, "enabled", True)
    calls = Mock()
    calls.state.return_value = True
    calls.complete = AsyncMock(return_value="https://platform.example/mcp/consent")
    calls.consent.return_value = "synthetic-browser-nonce"
    monkeypatch.setattr(mcp_auth, "owns_pending_state", calls.state)
    monkeypatch.setattr(
        mcp_auth.provider, "complete_github_authorization", calls.complete
    )
    monkeypatch.setattr(mcp_auth.provider, "bind_consent_browser", calls.consent)
    gate = BlockingIO()
    block_mock(getattr(calls, stage), gate)
    response = await while_blocked(
        browser,
        browser.get("/api/auth/callback?state=synthetic-state&code=synthetic-code"),
        gate,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "https://platform.example/mcp/consent"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert (
        "eng_platform_mcp_consent=synthetic-browser-nonce"
        in response.headers["set-cookie"]
    )
    assert [call[0] for call in calls.mock_calls] == ["state", "complete", "consent"]
