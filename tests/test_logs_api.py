"""HTTP and GitHub session boundaries for the runtime logs viewer."""

import asyncio
from base64 import b64encode
from datetime import datetime, timezone
import importlib
import json
from threading import Event
from unittest.mock import AsyncMock, Mock

import anyio
import httpx
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import pytest

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.models import ServiceLogsResponse
from eng_platform_api.services import log_budget, log_catalog, runtime_logs as logs
from tests.test_log_catalog import (
    SYNTHETIC_PRIVATE_NAMES,
    install_synthetic_private_catalog,
)

RESOURCE = logs.Resource(
    "example-api",
    "example-project",
    "us-central1",
    "cloud_run_service",
    enabled=True,
    allowed_logins=("reader",),
)
JOB = logs.Resource(
    "example-job",
    "example-project",
    "us-central1",
    "cloud_run_job",
    enabled=True,
    allowed_logins=("reader",),
)
HEADERS = {"Origin": "http://localhost:5173", "X-Requested-With": "EngineeringPlatform"}
PATH = "/api/catalog/services/example-api/logs"
REAL_READ_LOGS = logs.read_logs
REAL_FETCH_PAGE = logs.fetch_page


def client(login=None, provider="github_oauth"):
    result = TestClient(app)
    if login:
        session = {"github_login": login, "github_auth_provider": provider}
        middleware = next(
            item
            for item in app.user_middleware
            if item.cls.__name__ == "SessionMiddleware"
        )
        key = middleware.kwargs["secret_key"]
        result.cookies.set(
            "session",
            TimestampSigner(key).sign(b64encode(json.dumps(session).encode())).decode(),
        )
    return result


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.logs, "enabled", True)
    monkeypatch.setattr(config.logs, "allowed_logins", ("reader",))
    monkeypatch.setattr(
        config.auth, "session_secret", "synthetic-session-signing-secret-32-chars"
    )
    monkeypatch.setattr(config.auth, "github_client_id", "synthetic-client-id")
    monkeypatch.setattr(config.auth, "github_client_secret", "synthetic-client-secret")
    monkeypatch.setattr(config.auth, "frontend_url", "http://localhost:5173")
    monkeypatch.setattr(logs, "resources", lambda: [RESOURCE, JOB])
    monkeypatch.setattr(
        logs,
        "read_logs",
        Mock(
            return_value=ServiceLogsResponse(
                status="fresh",
                explorer_url=logs.explorer_url(RESOURCE),
                window_start=logs.iso(datetime.now(timezone.utc)),
            )
        ),
    )


def test_no_session_even_mock_or_trusted_iap_cannot_read(monkeypatch):
    for is_mock in (True, False):
        monkeypatch.setattr(config, "mock_mode", is_mock)
        monkeypatch.setattr(config.auth, "trust_iap_identity", True)
        response = client().post(
            PATH,
            json={},
            headers={
                **HEADERS,
                "X-Goog-Authenticated-User-Email": "accounts.google.com:reader",
            },
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    logs.read_logs.assert_not_called()


def test_deployer_permission_does_not_grant_log_reader():
    response = client("diegomad14").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == 403
    assert client("diegomad14").get("/api/auth/me").json()["can_view_logs"] is False
    logs.read_logs.assert_not_called()


@pytest.mark.parametrize("service_id", SYNTHETIC_PRIVATE_NAMES)
def test_private_resource_allows_only_synthetic_approved_reader(
    monkeypatch, tmp_path, service_id
):
    install_synthetic_private_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(logs, "resources", log_catalog.resources)
    # A separate globally allowed identity must still fail the resource ACL.
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader", "reader"))
    path = f"/api/catalog/services/{service_id}/logs"
    response = client("demo-reader").post(path, json={}, headers=HEADERS)
    assert response.status_code == 200
    logs.read_logs.assert_called_once()
    assert logs.read_logs.call_args.args[0].service_id == service_id
    logs.read_logs.reset_mock()
    response = client("reader").post(path, json={}, headers=HEADERS)
    assert response.status_code == 403
    logs.read_logs.assert_not_called()


def test_private_catalog_still_requires_global_reader_membership(monkeypatch, tmp_path):
    install_synthetic_private_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(logs, "resources", log_catalog.resources)
    monkeypatch.setattr(config.logs, "allowed_logins", ())
    reader = client("demo-reader")
    assert reader.get("/api/auth/me").json()["can_view_logs"] is False
    for service_id in SYNTHETIC_PRIVATE_NAMES:
        response = reader.post(
            f"/api/catalog/services/{service_id}/logs", json={}, headers=HEADERS
        )
        assert response.status_code == 403, service_id
    logs.read_logs.assert_not_called()


def test_private_catalog_global_off_never_reserves_or_reads_logs(monkeypatch, tmp_path):
    install_synthetic_private_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(logs, "resources", log_catalog.resources)
    monkeypatch.setattr(logs, "read_logs", REAL_READ_LOGS)
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
    monkeypatch.setattr(config.logs, "enabled", False)
    forbidden = Mock(side_effect=AssertionError("Global OFF must prevent upstream I/O"))
    monkeypatch.setattr(log_budget, "reserve", forbidden)
    monkeypatch.setattr(logs, "fetch_page", forbidden)
    reader = client("demo-reader")
    assert reader.get("/api/auth/me").json()["can_view_logs"] is False
    for service_id in SYNTHETIC_PRIVATE_NAMES:
        response = reader.post(
            f"/api/catalog/services/{service_id}/logs", json={}, headers=HEADERS
        )
        assert response.status_code == 200, service_id
        assert response.json()["status"] == "disabled", service_id
        assert response.json()["entries"] == [], service_id
    forbidden.assert_not_called()


def test_log_reader_is_separate_from_deployer():
    reader = client("ReAdEr")
    session = reader.get("/api/auth/me").json()
    assert session["can_view_logs"] is True
    assert session["can_deploy"] is False
    result = reader.post(
        PATH, json={"severity": "WARNING", "text": "synthetic needle"}, headers=HEADERS
    )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["status"] == "fresh"
    assert logs.read_logs.call_args.kwargs["text"] == "synthetic needle"
    assert logs.read_logs.call_args.kwargs["limit"] == 200


@pytest.mark.parametrize("provider", ["mock", "", "iap", None])
def test_mock_or_legacy_session_requires_real_oauth(provider):
    assert (
        client("reader", provider=provider)
        .post(PATH, json={}, headers=HEADERS)
        .status_code
        == 401
    )
    assert (
        client("reader", provider=provider).get("/api/auth/me").json()["can_view_logs"]
        is False
    )
    logs.read_logs.assert_not_called()


def test_unconfigured_secret_fails_closed(monkeypatch):
    monkeypatch.setattr(config.auth, "session_secret", "")
    assert client("reader").post(PATH, json={}, headers=HEADERS).status_code == 403
    assert client("reader").get("/api/auth/me").json()["can_view_logs"] is False


@pytest.mark.parametrize(
    "filters",
    [
        {"filter": "resource.type=*"},
        {"project": "evil-project"},
        {"page_token": "opaque"},
        {"limit": 201},
        {"limit": 0},
        {"lookback_minutes": 16},
        {"text": "x" * 201},
        {"severity": "ANY"},
        {"revision": '" OR true'},
        {"task_index": -1},
        {"execution": "other-job"},
        {"task_index": 0},
    ],
)
def test_arbitrary_upstream_query_and_invalid_filters_rejected(filters):
    result = client("reader").post(PATH, json=filters, headers=HEADERS)
    assert result.status_code == 422
    assert result.headers["cache-control"] == "no-store"
    logs.read_logs.assert_not_called()


def test_job_filters_and_unknown_resource():
    reader = client("reader")
    assert (
        reader.post(
            "/api/catalog/services/example-job/logs",
            json={"revision": "wrong"},
            headers=HEADERS,
        ).status_code
        == 422
    )
    assert (
        reader.post(
            "/api/catalog/services/example-job/logs",
            json={"execution": "example-job-1", "task_index": 1},
            headers=HEADERS,
        ).status_code
        == 200
    )
    assert (
        reader.post(
            "/api/catalog/services/missing/logs", json={}, headers=HEADERS
        ).status_code
        == 404
    )


def test_private_post_transport_rejects_query_get_media_and_cross_origin():
    reader = client("reader")
    for headers in (
        {},
        {"Origin": HEADERS["Origin"]},
        {**HEADERS, "Origin": "https://evil.example"},
    ):
        assert reader.post(PATH, json={}, headers=headers).status_code == 403
    assert reader.post(PATH, content="{}", headers=HEADERS).status_code == 415
    assert (
        reader.post(PATH + "?text=secret", json={}, headers=HEADERS).status_code == 422
    )
    assert reader.get(PATH, headers=HEADERS).status_code == 405
    logs.read_logs.assert_not_called()


def test_disabled_feature_has_no_upstream_and_capability_false(monkeypatch):
    monkeypatch.setattr(config.logs, "enabled", False)
    reader = client("reader")
    assert reader.get("/api/auth/me").json()["can_view_logs"] is False
    monkeypatch.setattr(logs, "read_logs", REAL_READ_LOGS)
    result = reader.post(PATH, json={}, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["status"] == "disabled"


def test_catalog_failure_lower_lookback_and_disconnect(monkeypatch):
    reader = client("reader")
    monkeypatch.setattr(
        logs, "resources", Mock(side_effect=ValueError("unsafe coordinates"))
    )
    assert reader.post(PATH, json={}, headers=HEADERS).status_code == 503
    monkeypatch.setattr(logs, "resources", lambda: [RESOURCE])
    monkeypatch.setattr(config.logs, "lookback_minutes", 5)
    assert reader.post(PATH, json={}, headers=HEADERS).status_code == 200
    logs.read_logs.reset_mock()
    monkeypatch.setattr(config.logs, "lookback_minutes", 15)
    monkeypatch.setattr(
        "starlette.requests.Request.is_disconnected", AsyncMock(return_value=True)
    )
    assert reader.post(PATH, json={}, headers=HEADERS).status_code == 499
    logs.read_logs.assert_not_called()


def test_log_filters_are_not_recorded_by_application_access_logger(caplog):
    with caplog.at_level("INFO", logger="eng_platform_api.requests"):
        client("reader").post(
            PATH, json={"text": "SYNTHETIC-SENSITIVE-NEEDLE"}, headers=HEADERS
        )
    assert "SYNTHETIC-SENSITIVE-NEEDLE" not in caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_secret", "x" * 31),
        ("github_client_id", ""),
        ("github_client_secret", ""),
    ],
)
def test_incomplete_or_weak_oauth_configuration_fails_closed(monkeypatch, field, value):
    monkeypatch.setattr(config.auth, field, value)
    reader = client("reader")
    assert reader.get("/api/auth/me").json()["can_view_logs"] is False
    assert reader.post(PATH, json={}, headers=HEADERS).status_code == 403
    logs.read_logs.assert_not_called()


@pytest.mark.asyncio
async def test_entire_endpoint_deadline_includes_saturated_worker_queue(monkeypatch):
    monkeypatch.setattr(config.logs, "request_timeout_seconds", 0.03)
    limiter = anyio.to_thread.current_default_thread_limiter()
    old_total = limiter.total_tokens
    limiter.total_tokens = 1
    await limiter.acquire()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies=client("reader").cookies,
        ) as browser:
            response = await asyncio.wait_for(
                browser.post(PATH, json={}, headers=HEADERS), 1
            )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "60"
        assert response.json() == {"detail": "Runtime log request timed out"}
        logs.read_logs.assert_not_called()
    finally:
        limiter.release()
        limiter.total_tokens = old_total


@pytest.fixture
def real_pipeline(monkeypatch):
    from tests.log_test_support import FirestoreDouble
    from eng_platform_api.services import log_budget

    # The 100 ms HTTP tests isolate blocked SDK construction/ADC or RPC. Warm
    # generated message imports before their tiny budget; no client is created.
    importlib.import_module("google.cloud.logging_v2.types")
    store = FirestoreDouble()
    monkeypatch.setattr(config.logs, "quota_project_id", "quota-project")
    monkeypatch.setattr(config.logs, "budget_project_id", "example-project")
    monkeypatch.setattr(config.logs, "budget_collection", "eng_platform_log_budget")
    monkeypatch.setattr(log_budget, "firestore_client", lambda _: store)
    monkeypatch.setattr(logs, "read_logs", REAL_READ_LOGS)
    monkeypatch.setattr(logs, "fetch_page", REAL_FETCH_PAGE)
    monkeypatch.setattr(logs, "_caches", {})
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_phase", ["client", "rpc"])
async def test_http_timeout_does_not_finish_a_blocked_worker_or_reuse_its_slot(
    monkeypatch, real_pipeline, blocking_phase
):
    entered, release, completed = Event(), Event(), Event()
    sdk = Mock()
    page = type("Page", (), {"entries": [], "next_page_token": ""})()
    pager = type("Pager", (), {"pages": [page]})()
    sdk.list_log_entries.return_value = pager
    monkeypatch.setattr(config.logs, "request_timeout_seconds", 0.1)

    def blocked():
        entered.set()
        assert release.wait(3)

    def initialize(scope):
        if blocking_phase == "client":
            blocked()
        return sdk

    def fetch(**kwargs):
        blocked()
        return pager

    def tracked(*args, **kwargs):
        try:
            return REAL_READ_LOGS(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(logs, "_logging_client", initialize)
    monkeypatch.setattr(logs, "read_logs", tracked)
    # For the short test total, retain valid room for dispatch and completion.
    monkeypatch.setattr(config.logs, "finish_timeout_seconds", 0.01)
    if blocking_phase == "rpc":
        sdk.list_log_entries.side_effect = fetch
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies=client("reader").cookies,
        ) as browser:
            response = await asyncio.wait_for(
                browser.post(PATH, json={}, headers=HEADERS), 1
            )
        assert entered.is_set()
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"detail": "Runtime log request timed out"}
        assert not completed.is_set()
        assert (
            real_pipeline.records[log_budget.document_id()]["attempts"][0][
                "completed_at"
            ]
            is None
        )
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 3)
    # A callback returning late cannot trigger another call or finalize outside
    # the total budget. Restarting a replica cannot recover this pending slot.
    assert sdk.list_log_entries.call_count == (1 if blocking_phase == "rpc" else 0)
    assert (
        real_pipeline.records[log_budget.document_id()]["attempts"][0]["completed_at"]
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_phase", ["client", "rpc"])
async def test_cancelling_http_wait_only_finishes_after_worker_has_returned(
    monkeypatch, real_pipeline, blocking_phase
):
    entered, release, completed = Event(), Event(), Event()
    sdk = Mock()
    page = type("Page", (), {"entries": [], "next_page_token": ""})()
    pager = type("Pager", (), {"pages": [page]})()
    sdk.list_log_entries.return_value = pager

    def blocked():
        entered.set()
        assert release.wait(3)

    def initialize(scope):
        if blocking_phase == "client":
            blocked()
        return sdk

    def fetch(**kwargs):
        blocked()
        return pager

    def tracked(*args, **kwargs):
        try:
            return REAL_READ_LOGS(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(logs, "_logging_client", initialize)
    monkeypatch.setattr(logs, "read_logs", tracked)
    if blocking_phase == "rpc":
        sdk.list_log_entries.side_effect = fetch
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies=client("reader").cookies,
    ) as browser:
        task = asyncio.create_task(browser.post(PATH, json={}, headers=HEADERS))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not completed.is_set()
            assert (
                real_pipeline.records[log_budget.document_id()]["attempts"][0][
                    "completed_at"
                ]
                is None
            )
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 3)
    assert sdk.list_log_entries.call_count == (1 if blocking_phase == "rpc" else 0)
    # The sole owner returned within the total deadline. It can now safely
    # charge 60 more seconds, even though HTTP is gone. This is never a refund.
    assert (
        real_pipeline.records[log_budget.document_id()]["attempts"][0]["completed_at"]
        == real_pipeline.now
    )


@pytest.mark.asyncio
async def test_late_browser_disconnect_may_complete_one_already_accepted_sample(
    monkeypatch, real_pipeline
):
    entered, release = Event(), Event()
    disconnected = asyncio.Event()
    sdk = Mock()
    sdk.list_log_entries.return_value = type(
        "Pager",
        (),
        {"pages": [type("Page", (), {"entries": [], "next_page_token": ""})()]},
    )()

    def initialize(scope):
        entered.set()
        assert release.wait(3)
        return sdk

    monkeypatch.setattr(logs, "_logging_client", initialize)
    delivered = False
    messages = []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "root_path": "",
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 123),
        "headers": [
            (b"content-type", b"application/json"),
            (b"origin", HEADERS["Origin"].encode()),
            (b"x-requested-with", b"EngineeringPlatform"),
            (b"cookie", f"session={client('reader').cookies.get('session')}".encode()),
        ],
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        disconnected.set()
        await asyncio.sleep(0)
        # A browser disconnect is not necessarily an ASGI task cancellation.
        # While SDK setup is blocked, the reserved permit remains pending.
        assert sdk.list_log_entries.call_count == 0
        assert (
            real_pipeline.records[log_budget.document_id()]["attempts"][0][
                "completed_at"
            ]
            is None
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert sdk.list_log_entries.call_count == 1
    assert len(real_pipeline.records[log_budget.document_id()]["attempts"]) == 1
    assert (
        real_pipeline.records[log_budget.document_id()]["attempts"][0]["completed_at"]
        == real_pipeline.now
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"allowed_logins": ()},
        {"allowed_logins": ("another-reader",)},
    ],
)
def test_per_resource_policy_denies_before_cache_or_quota(monkeypatch, changes):
    from dataclasses import replace

    denied = replace(RESOURCE, **changes)
    monkeypatch.setattr(logs, "resources", lambda: [denied, JOB])
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == 403
    assert response.json() == {"detail": "You are not allowed to view these logs"}
    logs.read_logs.assert_not_called()


def test_unknown_resource_never_consults_runtime_cache():
    response = client("reader").post(
        "/api/catalog/services/not-registered/logs", json={}, headers=HEADERS
    )
    assert response.status_code == 404
    logs.read_logs.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"allowed_logins": ()},
        {"allowed_logins": ("reader", "additional-reader")},
        {"region": "europe-west1"},
        {"project": "another-project"},
        {"kind": "cloud_run_job"},
        None,
    ],
)
def test_policy_or_identity_change_during_io_discards_built_response(
    monkeypatch, changes
):
    from dataclasses import replace

    before = [RESOURCE, JOB]
    after = [replace(RESOURCE, **changes), JOB] if changes is not None else [JOB]
    monkeypatch.setattr(logs, "resources", Mock(side_effect=[before, after]))
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == 403
    assert "entries" not in response.json()
    logs.read_logs.assert_called_once()


@pytest.mark.parametrize(
    "kind", ["global-reader", "global-flag", "oauth-secret", "mock"]
)
def test_global_authorization_revoked_during_io_discards_result(monkeypatch, kind):
    initial = logs.read_logs.return_value

    def revoke(*args, **kwargs):
        if kind == "global-reader":
            monkeypatch.setattr(config.logs, "allowed_logins", ())
        elif kind == "global-flag":
            monkeypatch.setattr(config.logs, "enabled", False)
        elif kind == "oauth-secret":
            monkeypatch.setattr(config.auth, "session_secret", "")
        else:
            monkeypatch.setattr(config, "mock_mode", True)
        return initial

    logs.read_logs.side_effect = revoke
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == (401 if kind == "mock" else 403)
    assert "entries" not in response.json()


def test_global_revoke_during_resolution_denies_before_cache(monkeypatch):
    def revoke():
        monkeypatch.setattr(config.logs, "allowed_logins", ())
        return [RESOURCE, JOB]

    monkeypatch.setattr(logs, "resources", revoke)
    assert client("reader").post(PATH, json={}, headers=HEADERS).status_code == 403
    logs.read_logs.assert_not_called()


def test_corrupt_catalog_during_io_discards_result_without_details(monkeypatch):
    monkeypatch.setattr(
        logs,
        "resources",
        Mock(side_effect=[[RESOURCE, JOB], ValueError("secret-path and reader")]),
    )
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "Runtime log catalog is unavailable"}
    assert response.headers["cache-control"] == "no-store"


def test_real_local_authority_requires_acl_and_never_discovers_cloud(
    monkeypatch, tmp_path
):
    from eng_platform_api.services import catalog, log_catalog
    from tests.test_log_catalog import install_catalog, pin_private_catalog, record

    path = install_catalog(monkeypatch, tmp_path, [record("example-api")])
    pin_private_catalog(monkeypatch, path)
    monkeypatch.setattr(logs, "resources", log_catalog.resources)
    forbidden = Mock(side_effect=AssertionError("Cloud discovery before authorization"))
    monkeypatch.setattr(catalog, "_run_client", forbidden)
    monkeypatch.setattr(catalog, "_job_client", forbidden)
    monkeypatch.setattr(catalog, "deployment_blockers", forbidden)
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == 403
    forbidden.assert_not_called()
    logs.read_logs.assert_not_called()


@pytest.mark.parametrize("phase", ["reserve", "client"])
@pytest.mark.parametrize(
    "revocation",
    [
        "global_reader",
        "enabled",
        "mock",
        "oauth_config",
        "resource_acl",
        "coordinates",
        "session",
    ],
)
def test_revocation_before_dispatch_makes_no_logging_rpc(
    monkeypatch, real_pipeline, phase, revocation
):
    from dataclasses import replace
    from eng_platform_api.routers import logs as route
    from eng_platform_api.services import log_budget

    current = [RESOURCE, JOB]
    monkeypatch.setattr(logs, "resources", lambda: current)
    sdk = Mock()
    sdk.list_log_entries.return_value = type(
        "Pager",
        (),
        {"pages": [type("Page", (), {"entries": [], "next_page_token": ""})()]},
    )()
    revoked = [False]
    original_reader = route.require_log_reader

    def session_guard(request):
        if revocation == "session" and revoked[0]:
            return "different-reader"
        return original_reader(request)

    monkeypatch.setattr(route, "require_log_reader", session_guard)

    def revoke():
        revoked[0] = True
        if revocation == "global_reader":
            monkeypatch.setattr(config.logs, "allowed_logins", ())
        elif revocation == "enabled":
            monkeypatch.setattr(config.logs, "enabled", False)
        elif revocation == "mock":
            monkeypatch.setattr(config, "mock_mode", True)
        elif revocation == "oauth_config":
            monkeypatch.setattr(config.auth, "github_client_secret", "")
        elif revocation == "resource_acl":
            current[0] = replace(RESOURCE, allowed_logins=())
        elif revocation == "coordinates":
            current[0] = replace(RESOURCE, region="europe-west1")

    if phase == "reserve":
        reserve = log_budget.reserve

        def changed_reserve(*args, **kwargs):
            result = reserve(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(log_budget, "reserve", changed_reserve)

    def initialize(scope):
        if phase == "client":
            revoke()
        return sdk

    monkeypatch.setattr(logs, "_logging_client", initialize)
    response = client("reader").post(PATH, json={}, headers=HEADERS)
    assert response.status_code == (401 if revocation in {"mock", "session"} else 403)
    sdk.list_log_entries.assert_not_called()
    attempts = real_pipeline.records[log_budget.document_id()]["attempts"]
    assert len(attempts) == 1 and attempts[0]["completed_at"] is not None
