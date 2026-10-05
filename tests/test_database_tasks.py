"""Cloud Tasks authority, opaque payload and uncertain delivery boundaries."""

from datetime import timedelta
import json
from unittest.mock import Mock

from google.api_core.exceptions import AlreadyExists, PermissionDenied
from google.cloud import tasks_v2
import pytest

from eng_platform_api.config import config
from eng_platform_api.services import database_tasks as tasks
from eng_platform_api.services.database_registry import DatabaseUnavailable

IDENTITY = "a" * 32


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setattr(tasks, "_test_dispatcher", None)
    for field, value in {
        "project_id": "database-test-project",
        "queue_location": "us-central1",
        "query_queue": "readonly-query",
        "export_queue": "readonly-export",
        "cleanup_queue": "readonly-cleanup",
        "worker_service_account": "database-worker@database-test-project.iam.gserviceaccount.com",
        "api_origin": "https://database.example",
    }.items():
        monkeypatch.setattr(config.databases, field, value)
    client = Mock()
    client.queue_path.side_effect = lambda project, region, queue: (
        f"projects/{project}/locations/{region}/queues/{queue}"
    )
    constructor = Mock(return_value=client)
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", constructor)
    return client, constructor


@pytest.mark.parametrize("kind", ["query", "export", "sort", "cleanup"])
def test_sdk_payload_has_only_opaque_id_fixed_oidc_and_bounded_deadline(sdk, kind):
    client, _ = sdk
    tasks.enqueue(kind, IDENTITY)
    call = client.create_task.call_args.kwargs
    assert call["timeout"] == 4 and call["retry"] is None
    request = call["request"]
    task = request["task"]
    assert request["parent"].endswith(
        f"/queues/readonly-{'export' if kind == 'sort' else kind}"
    )
    assert task["name"] == f"{request['parent']}/tasks/{kind}-{IDENTITY}"
    http = task["http_request"]
    assert http["http_method"] == tasks_v2.HttpMethod.POST
    assert (
        http["url"]
        == f"https://database.example/api/internal/database-executions/{kind}"
    )
    assert http["headers"] == {"Content-Type": "application/json"}
    assert json.loads(http["body"]) == {"id": IDENTITY}
    assert http["oidc_token"] == {
        "service_account_email": config.databases.worker_service_account,
        "audience": "https://database.example",
    }
    assert task["dispatch_deadline"] == {"seconds": 275}
    converted = tasks_v2.CreateTaskRequest(request)
    assert converted.task.dispatch_deadline == timedelta(seconds=275)
    assert json.loads(converted.task.http_request.body) == {"id": IDENTITY}
    assert "schedule_time" not in task
    assert not any(
        value in http["body"] for value in (b"sql", b"login", b"cookie", b"dsn")
    )


def test_cleanup_schedule_and_name_bind_to_expiry_without_sharing_immediate_task(sdk):
    client, _ = sdk
    tasks.enqueue("cleanup", IDENTITY, schedule_at=1_700_000_000.25)
    scheduled = client.create_task.call_args.kwargs["request"]["task"]
    assert scheduled["schedule_time"] == {"seconds": 1_700_000_001}
    assert scheduled["name"].endswith(f"cleanup-{IDENTITY}-expiry-1700000001")
    tasks.enqueue("cleanup", IDENTITY)
    immediate = client.create_task.call_args.kwargs["request"]["task"]
    assert immediate["name"].endswith(f"cleanup-{IDENTITY}")
    assert immediate["name"] != scheduled["name"]
    assert "schedule_time" not in immediate


def test_retries_reuse_same_name_and_confirmed_duplicate_is_harmless(sdk):
    client, _ = sdk
    client.create_task.side_effect = [
        None,
        AlreadyExists("private-provider-diagnostic"),
    ]
    tasks.enqueue("query", IDENTITY)
    tasks.enqueue("query", IDENTITY)
    names = [
        call.kwargs["request"]["task"]["name"]
        for call in client.create_task.call_args_list
    ]
    assert names[0] == names[1]
    assert client.create_task.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("SELECT private_value"),
        PermissionDenied("private-provider-diagnostic"),
    ],
)
def test_unknown_provider_failure_is_redacted_and_not_retried(sdk, error):
    client, _ = sdk
    client.create_task.side_effect = error
    with pytest.raises(DatabaseUnavailable) as denied:
        tasks.enqueue("query", IDENTITY)
    assert str(denied.value) == "Database task dispatch is unavailable"
    assert client.create_task.call_count == 1


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "queue_location",
        "query_queue",
        "worker_service_account",
        "api_origin",
    ],
)
def test_missing_production_setting_cannot_construct_client(sdk, monkeypatch, field):
    _, constructor = sdk
    monkeypatch.setattr(config.databases, field, "")
    with pytest.raises(DatabaseUnavailable):
        tasks.enqueue("query", IDENTITY)
    constructor.assert_not_called()


def test_http_worker_origin_is_rejected_before_sdk_call(sdk, monkeypatch):
    _, constructor = sdk
    monkeypatch.setattr(config.databases, "api_origin", "http://database.example")
    with pytest.raises(DatabaseUnavailable):
        tasks.enqueue("query", IDENTITY)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "kind,identity",
    [
        ("sql", IDENTITY),
        ("query", "a" * 31),
        ("query", "A" * 32),
        ("query", "../" + "a" * 32),
    ],
)
def test_invalid_identity_never_reaches_sdk(sdk, kind, identity):
    _, constructor = sdk
    with pytest.raises(DatabaseUnavailable):
        tasks.enqueue(kind, identity)
    constructor.assert_not_called()


def test_explicit_dispatcher_is_only_injected_and_errors_still_fail_closed(
    sdk, monkeypatch
):
    _, constructor = sdk
    injected = Mock()
    monkeypatch.setattr(tasks, "_test_dispatcher", injected)
    tasks.enqueue("export", IDENTITY, schedule_at=123.5)
    injected.assert_called_once_with("export", IDENTITY, 123.5)
    constructor.assert_not_called()
    injected.side_effect = RuntimeError("private-query")
    with pytest.raises(DatabaseUnavailable) as denied:
        tasks.enqueue("export", IDENTITY)
    assert "private-query" not in str(denied.value)
