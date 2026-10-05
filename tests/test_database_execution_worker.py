"""OIDC-only delivery, bounded opaque bodies and generic worker failures."""

from contextlib import contextmanager
import json
from unittest.mock import Mock

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
import pytest

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.routers import database_execution_worker as worker
from eng_platform_api.services.database_registry import DatabaseUnavailable

IDENTITY = "a" * 32
BASE = "/api/internal/database-executions"
ACCOUNT = "database-worker@database-test-project.iam.gserviceaccount.com"
HEADERS = {
    "Authorization": "Bearer opaque-google-token",
    "Content-Type": "application/json",
}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.databases, "worker_service_account", ACCOUNT)
    monkeypatch.setattr(config.databases, "api_origin", "https://database.example")
    verifier = Mock(return_value={"email": ACCOUNT, "email_verified": True})
    monkeypatch.setattr(id_token, "verify_oauth2_token", verifier)
    handlers = {}
    for name in ("run_query", "run_export", "cleanup", "sweep"):
        handlers[name] = Mock()
        monkeypatch.setattr(worker.database_jobs, name, handlers[name])
    handlers["run_sort"] = Mock()
    monkeypatch.setattr(worker.database_views, "run_sort", handlers["run_sort"])
    return verifier, handlers


def browser():
    return TestClient(app, base_url="https://testserver")


def post(operation="query", body=None, *, headers=None):
    return browser().post(
        f"{BASE}/{operation}",
        content=json.dumps({"id": IDENTITY} if body is None else body),
        headers=HEADERS if headers is None else headers,
    )


@pytest.mark.parametrize(
    "operation,handler",
    [
        ("query", "run_query"),
        ("export", "run_export"),
        ("sort", "run_sort"),
        ("cleanup", "cleanup"),
    ],
)
def test_verified_deliveries_dispatch_only_validated_opaque_id(
    configured, operation, handler
):
    verifier, handlers = configured
    response = post(operation)
    assert response.status_code == 204 and response.content == b""
    handlers[handler].assert_called_once_with(IDENTITY)
    assert sum(value.call_count for value in handlers.values()) == 1
    token, transport, audience = verifier.call_args.args
    assert token == "opaque-google-token"
    assert callable(transport)
    assert audience == "https://database.example"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_sweep_requires_empty_json_object_and_has_no_input_argument(configured):
    _, handlers = configured
    response = post("sweep", {})
    assert response.status_code == 204
    handlers["sweep"].assert_called_once_with()


@pytest.mark.parametrize(
    "claim",
    [
        {
            "email": "other@database-test-project.iam.gserviceaccount.com",
            "email_verified": True,
        },
        {"email": ACCOUNT.upper(), "email_verified": True},
        {"email": ACCOUNT, "email_verified": False},
        {"email": ACCOUNT, "email_verified": "true"},
        {"email": ACCOUNT, "email_verified": 1},
        {"email": ACCOUNT},
        {},
    ],
)
def test_exact_service_account_and_verified_boolean_are_required(configured, claim):
    verifier, handlers = configured
    verifier.return_value = claim
    response = post()
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid database worker identity"
    assert all(handler.call_count == 0 for handler in handlers.values())


def test_wrong_oidc_audience_or_provider_error_is_redacted(configured):
    verifier, handlers = configured
    verifier.side_effect = ValueError("wrong-audience SELECT private_value")
    response = post()
    assert response.status_code == 401
    assert "private_value" not in response.text
    assert verifier.call_args.args[2] == "https://database.example"
    assert all(handler.call_count == 0 for handler in handlers.values())


def test_certificate_transport_enforces_four_second_timeout(configured, monkeypatch):
    verifier, _ = configured
    transport = Mock()
    constructor = Mock(return_value=transport)
    monkeypatch.setattr(google_requests, "Request", constructor)

    def verify(token, bounded_transport, audience):
        bounded_transport(
            "https://certs.example",
            method="GET",
            body=None,
            headers={"Accept": "application/json"},
            timeout=999,
        )
        return {"email": ACCOUNT, "email_verified": True}

    verifier.side_effect = verify
    assert post().status_code == 204
    transport.assert_called_once_with(
        "https://certs.example",
        method="GET",
        body=None,
        headers={"Accept": "application/json"},
        timeout=4,
    )


@pytest.mark.parametrize("field", ["worker_service_account", "api_origin"])
def test_missing_worker_configuration_never_verifies_or_dispatches(
    configured, monkeypatch, field
):
    verifier, handlers = configured
    monkeypatch.setattr(config.databases, field, "")
    response = post()
    assert response.status_code == 503
    verifier.assert_not_called()
    assert all(handler.call_count == 0 for handler in handlers.values())


def test_mock_mode_cannot_authorize_internal_delivery(configured, monkeypatch):
    verifier, handlers = configured
    monkeypatch.setattr(config, "mock_mode", True)
    response = post()
    assert response.status_code == 503
    verifier.assert_not_called()
    assert all(handler.call_count == 0 for handler in handlers.values())


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic opaque-google-token", "Bearer", "Bearer ", "Bearer " + "x" * 8193],
)
def test_missing_wrong_scheme_and_oversized_tokens_fail_before_oidc(
    configured, authorization
):
    verifier, _ = configured
    response = post(headers={**HEADERS, "Authorization": authorization})
    assert response.status_code == 401
    verifier.assert_not_called()


def test_bearer_scheme_is_case_insensitive(configured):
    assert (
        post(
            headers={**HEADERS, "Authorization": "bEaReR opaque-google-token"}
        ).status_code
        == 204
    )


def test_maximum_bearer_token_is_accepted_at_the_exact_boundary(configured):
    verifier, _ = configured
    response = post(headers={**HEADERS, "Authorization": "Bearer " + "x" * 8192})
    assert response.status_code == 204
    assert len(verifier.call_args.args[0]) == 8192


@pytest.mark.parametrize(
    "body",
    [
        {"sql": "SELECT private_value"},
        {"id": IDENTITY, "sql": "SELECT private_value"},
        {"id": IDENTITY, "dsn": "private-secret"},
        {"id": "SELECT private_value"},
        {"id": "A" * 32},
        {"id": "a" * 31},
        {"id": 123},
        {"id": None},
        [IDENTITY],
        "private-value",
    ],
)
def test_sql_credentials_extra_fields_and_nonopaque_shapes_never_dispatch(
    configured, body
):
    _, handlers = configured
    response = post(body=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid database worker request"
    assert "private" not in response.text
    assert all(handler.call_count == 0 for handler in handlers.values())


def test_body_limit_accepts_256_bytes_and_rejects_257_without_partial_dispatch(
    configured,
):
    _, handlers = configured
    raw = json.dumps({"id": IDENTITY}).encode()
    accepted = browser().post(
        f"{BASE}/query", content=raw + b" " * (256 - len(raw)), headers=HEADERS
    )
    assert accepted.status_code == 204
    rejected = browser().post(
        f"{BASE}/query", content=raw + b" " * (257 - len(raw)), headers=HEADERS
    )
    assert rejected.status_code == 422
    handlers["run_query"].assert_called_once_with(IDENTITY)


@pytest.mark.parametrize("total,accepted", [(256, True), (257, False)])
async def test_body_limit_is_cumulative_across_streamed_transport_chunks(
    total, accepted
):
    raw = json.dumps({"id": IDENTITY}).encode()
    raw += b" " * (total - len(raw))
    chunks = [raw[:100], raw[100:200], raw[200:]]
    received = []

    async def receive():
        chunk = chunks.pop(0)
        received.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    request = Request(
        {
            "type": "http",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive=receive,
    )
    if accepted:
        assert await worker._body(request) == {"id": IDENTITY}
    else:
        with pytest.raises(HTTPException) as denial:
            await worker._body(request)
        assert denial.value.status_code == 422
    assert received == [100, 100, total - 200]


@pytest.mark.parametrize("raw", [b"not-json-private", b"\xff", b'{"id":'])
def test_malformed_encoding_and_json_are_generic(configured, raw):
    _, handlers = configured
    response = browser().post(f"{BASE}/query", content=raw, headers=HEADERS)
    assert response.status_code == 422
    assert "private" not in response.text
    assert all(handler.call_count == 0 for handler in handlers.values())


@pytest.mark.parametrize(
    "url,content_type",
    [
        (f"{BASE}/query?sql=SELECT-private", "application/json"),
        (f"{BASE}/query", "text/plain"),
        (f"{BASE}/query", ""),
    ],
)
def test_query_parameters_or_non_json_delivery_are_rejected(
    configured, url, content_type
):
    _, handlers = configured
    response = browser().post(
        url,
        content=json.dumps({"id": IDENTITY}),
        headers={**HEADERS, "Content-Type": content_type},
    )
    assert response.status_code == 422
    assert all(handler.call_count == 0 for handler in handlers.values())


def test_json_charset_parameter_is_accepted(configured):
    assert (
        post(
            headers={**HEADERS, "Content-Type": "application/json; charset=utf-8"}
        ).status_code
        == 204
    )


def test_unknown_operation_is_authenticated_before_404(configured):
    verifier, handlers = configured
    assert post("unknown").status_code == 404
    assert verifier.call_count == 1
    assert all(handler.call_count == 0 for handler in handlers.values())
    assert (
        post("unknown", headers={"Content-Type": "application/json"}).status_code == 401
    )


@pytest.mark.parametrize("operation", ["query", "export", "cleanup", "sweep"])
def test_control_store_failure_has_generic_response(configured, operation):
    _, handlers = configured
    name = {
        "query": "run_query",
        "export": "run_export",
        "cleanup": "cleanup",
        "sweep": "sweep",
    }[operation]
    handlers[name].side_effect = DatabaseUnavailable("SELECT private_value")
    response = post(operation, {} if operation == "sweep" else {"id": IDENTITY})
    assert response.status_code == 503
    assert response.json()["detail"] == "Database worker operation is unavailable"
    assert "private_value" not in response.text


def test_unexpected_execution_error_is_redacted_by_database_boundary(configured):
    _, handlers = configured
    handlers["run_query"].side_effect = RuntimeError("private-sql-and-dsn")
    response = post()
    assert response.status_code == 503
    assert response.json()["detail"] == "Database operation is unavailable"
    assert "private-sql" not in response.text


def test_body_timeout_is_bounded_without_waiting_five_seconds(configured, monkeypatch):
    @contextmanager
    def timed_out(seconds):
        assert seconds == 5
        raise TimeoutError("private-body-diagnostic")
        yield

    monkeypatch.setattr(worker.anyio, "fail_after", timed_out)
    response = post()
    assert response.status_code == 422
    assert "private-body" not in response.text


def test_execution_deadline_is_270_seconds_and_timeout_is_generic(
    configured, monkeypatch
):
    _, handlers = configured
    phases = []

    @contextmanager
    def timed_out(seconds):
        phases.append(seconds)
        if seconds == 270:
            raise TimeoutError("private-provider-diagnostic")
        yield

    monkeypatch.setattr(worker.anyio, "fail_after", timed_out)
    response = post()
    assert response.status_code == 503 and phases == [5, 270]
    assert "private-provider" not in response.text
    handlers["run_query"].assert_not_called()


@pytest.mark.parametrize("body", [False, 0, [], "", None])
def test_sweep_rejects_every_nonobject_even_if_json_value_is_falsy(configured, body):
    _, handlers = configured
    response = browser().post(
        f"{BASE}/sweep", content=json.dumps(body), headers=HEADERS
    )
    assert response.status_code == 422
    handlers["sweep"].assert_not_called()


def test_sweep_rejects_nonempty_object_including_sql(configured):
    _, handlers = configured
    response = post("sweep", {"sql": "SELECT private_value"})
    assert response.status_code == 422
    assert "private_value" not in response.text
    handlers["sweep"].assert_not_called()


def test_verify_worker_uses_configured_audience_independent_of_request_host(configured):
    verifier, _ = configured
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer token")],
            "server": ("attacker.example", 443),
            "scheme": "https",
            "path": "/",
        }
    )
    worker.verify_worker(request)
    assert verifier.call_args.args[2] == "https://database.example"
