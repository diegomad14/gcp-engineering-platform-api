"""Runtime log safety: provider calls, cache bounds and per-resource isolation."""

from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.cloud.logging_v2.types import LogEntry as GoogleLogEntry
from google.api_core.exceptions import ResourceExhausted

from eng_platform_api.config import config, load_config
from eng_platform_api.services import runtime_logs as logs
from eng_platform_api.services.log_budget import Reservation

NOW = datetime.now(timezone.utc)
SERVICE = logs.Resource(
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
OTHER = logs.Resource(
    "other-api",
    "another-project",
    "europe-west1",
    "cloud_run_service",
    enabled=True,
    allowed_logins=("reader",),
)


def raw(
    resource=SERVICE, *, seconds=1, message="hello", insert_id="one", severity="INFO"
):
    return {
        "resource": {
            "type": resource.resource_type,
            "labels": {
                "project_id": resource.project,
                "location": resource.region,
                resource.name_label: resource.service_id,
                "revision_name": "example-api-00001-abc",
            },
        },
        "timestamp": logs.iso(NOW - timedelta(seconds=seconds)),
        "text_payload": message,
        "insert_id": insert_id,
        "severity": severity,
        "labels": {
            "run.googleapis.com/execution_name": "example-job-12345",
            "run.googleapis.com/task_index": "2",
        },
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    global NOW
    NOW = datetime.now(timezone.utc)
    logs._caches.clear()
    monkeypatch.setattr(logs, "resources", lambda: [SERVICE, JOB, OTHER])
    monkeypatch.setattr(config.logs, "quota_project_id", "quota-project")
    monkeypatch.setattr(config.logs, "budget_project_id", "budget-project")
    monkeypatch.setattr(config.logs, "budget_collection", "eng_platform_log_budget")
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.logs, "enabled", True)
    monkeypatch.setattr(config.logs, "page_size", 1000)
    monkeypatch.setattr(config.logs, "buffer_entries", 2000)
    monkeypatch.setattr(config.logs, "buffer_bytes", 2097152)
    monkeypatch.setattr(config.logs, "lookback_minutes", 15)
    monkeypatch.setattr(
        logs.log_budget, "reserve", Mock(return_value=Reservation(True, NOW))
    )
    monkeypatch.setattr(logs.log_budget, "finish", Mock())
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([raw()], False)))
    yield
    logs._caches.clear()


def read(resource=SERVICE, **kwargs):
    return logs.read_logs(resource, [SERVICE, JOB, OTHER], **kwargs)


def refresh_again():
    logs._caches[logs._key(SERVICE)].next_due = 0


def test_filters_only_use_validated_catalog_coordinates():
    query = logs.query_filter(
        SERVICE.project, [SERVICE], NOW - timedelta(minutes=15), NOW
    )
    assert 'resource.type="cloud_run_revision"' in query
    assert 'resource.type="cloud_run_job"' not in query
    assert " OR " not in query
    with pytest.raises(ValueError, match="Exactly one"):
        logs.query_filter(SERVICE.project, [SERVICE, JOB], NOW, NOW)
    assert 'resource.labels.location="us-central1"' in query
    assert OTHER.service_id not in query
    assert OTHER.project not in query
    with pytest.raises(ValueError):
        logs.query_filter("missing", [SERVICE], NOW, NOW)
    for field in ("service_id", "project", "region", "kind"):
        values = {**SERVICE.__dict__, field: '" OR severity>=DEFAULT'}
        corrupted = logs.Resource(**values)
        with pytest.raises(ValueError):
            logs.query_filter(corrupted.project, [corrupted], NOW, NOW)


def test_resources_are_the_validated_authority_alias():
    from eng_platform_api.services import log_catalog

    assert logs.Resource is log_catalog.Resource


def test_real_sdk_serialization_and_no_retry_or_second_page(monkeypatch):
    # Restore real adapter rather than testing a provider-shaped dict only.
    from importlib import reload

    real = reload(logs)
    entry = GoogleLogEntry(text_payload="hello", severity="ERROR", timestamp=NOW)
    page = SimpleNamespace(entries=[entry], next_page_token="DO_NOT_FETCH")

    class Pager:
        @property
        def pages(self):
            yield page
            pytest.fail("A second page must never be requested")

    client = Mock()
    client.list_log_entries.return_value = Pager()
    monkeypatch.setattr(real, "_logging_client", lambda scope: client)
    deadline = logs.log_budget.Deadline.after(10, clock=lambda: 100)
    rows, truncated = real.fetch_page("example-project", "safe", deadline=deadline)
    assert rows[0]["severity"] == "ERROR"
    assert truncated
    assert client.list_log_entries.call_args.kwargs == {
        "request": {
            "resource_names": ["projects/example-project"],
            "filter": "safe",
            "order_by": "timestamp desc",
            "page_size": 1000,
        },
        "retry": None,
        "timeout": 10,
    }


def test_first_history_and_filters_query_only_demanded_resource(monkeypatch):
    provider = Mock(
        return_value=(
            [raw(), raw(JOB, insert_id="job"), raw(OTHER, insert_id="foreign")],
            False,
        )
    )
    monkeypatch.setattr(logs, "fetch_page", provider)
    result = read()
    assert result.status == "fresh"
    assert len(result.entries) == 1
    assert result.entries[0].message == "hello"
    assert result.entries[0].revision == "example-api-00001-abc"
    assert result.entries[0].execution is None
    assert read(severity="ERROR").entries == []
    assert read(text="absent").entries == []
    assert len(read(text="HELLO").entries) == 1
    assert read(revision="missing").entries == []
    job = read(JOB, execution="example-job-12345", task_index=2)
    assert len(job.entries) == 1
    assert job.entries[0].task_index == 2
    assert job.entries[0].revision is None
    assert read(JOB, task_index=1).entries == []
    assert read(JOB, execution="missing").entries == []
    assert provider.call_count == 2
    query = provider.call_args.args[1]
    assert "HELLO" not in query
    assert SERVICE.service_id not in query and JOB.service_id in query
    assert OTHER.project not in query


def test_overlap_dedupe_reconnect_and_late_entries(monkeypatch):
    read()
    refresh_again()
    provider = Mock(return_value=([raw(), raw(seconds=90, insert_id="late")], False))
    monkeypatch.setattr(logs, "fetch_page", provider)
    result = read()
    assert len(result.entries) == 2
    assert len({entry.id for entry in result.entries}) == 2
    assert logs.iso(NOW - timedelta(minutes=2)) in provider.call_args.args[1]
    assert len(read().entries) == 2
    # A restarted replica starts with a fresh cache and a full bounded history.
    logs._caches.clear()
    read()
    assert logs.iso(NOW - timedelta(minutes=15)) in provider.call_args.args[1]


def test_provider_is_unreachable_when_disabled_mock_or_budget_fails(monkeypatch):
    provider = logs.fetch_page
    monkeypatch.setattr(config.logs, "enabled", False)
    assert read().status == "disabled"
    monkeypatch.setattr(config.logs, "enabled", True)
    monkeypatch.setattr(config, "mock_mode", True)
    assert read().status == "disabled"
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(
        logs.log_budget, "reserve", Mock(side_effect=RuntimeError("secret"))
    )
    result = read()
    assert result.status == "unavailable"
    assert "secret" not in result.model_dump_json()
    assert result.next_poll_seconds == 10
    provider.assert_not_called()


def test_budget_denial_is_throttled_and_keeps_cache(monkeypatch):
    read()
    refresh_again()
    provider = logs.fetch_page
    monkeypatch.setattr(
        logs.log_budget, "reserve", Mock(return_value=Reservation(False, NOW, 20))
    )
    result = read()
    assert result.status == "throttled"
    assert len(result.entries) == 1
    assert result.next_poll_seconds == 20
    assert provider.call_count == 1


def test_rate_limit_errors_back_off_and_do_not_expose_provider_details(monkeypatch):
    read()
    provider = Mock(side_effect=ResourceExhausted("Bearer do-not-return"))
    monkeypatch.setattr(logs, "fetch_page", provider)
    for delay in (10, 20, 40, 60, 60):
        refresh_again()
        result = read()
        assert result.status == "stale"
        assert result.next_poll_seconds == delay
        assert "do-not-return" not in result.model_dump_json()
    assert provider.call_count == 5


def test_buffer_count_byte_and_response_limit_are_bounded(monkeypatch):
    monkeypatch.setattr(config.logs, "buffer_entries", 3)
    monkeypatch.setattr(
        logs,
        "fetch_page",
        Mock(
            return_value=([raw(insert_id=str(i), seconds=i) for i in range(10)], True)
        ),
    )
    result = read(limit=2)
    assert len(result.entries) == 2 and result.truncated
    assert len(logs._caches[logs._key(SERVICE)].entries) == 3
    logs._caches.clear()
    monkeypatch.setattr(config.logs, "buffer_entries", 2000)
    monkeypatch.setattr(config.logs, "buffer_bytes", 1000)
    monkeypatch.setattr(
        logs,
        "fetch_page",
        Mock(
            return_value=(
                [raw(insert_id=str(i), message="x" * 700) for i in range(10)],
                False,
            )
        ),
    )
    result = read()
    cache = logs._caches[logs._key(SERVICE)]
    assert (
        sum(len(row[1].model_dump_json().encode()) for row in cache.entries.values())
        <= 1000
    )
    assert result.truncated


def test_sanitized_payload_search_and_large_entry(monkeypatch):
    record = raw(message="")
    record["json_payload"] = {
        "message": "safe",
        "password": "SECRETNEVER",
        "context": ["x" * 8192] * 10,
    }
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([record], False)))
    result = read()
    assert "SECRETNEVER" not in result.model_dump_json()
    assert result.truncated
    assert len(result.entries[0].model_dump_json().encode()) <= 16384
    assert not read(text="SECRETNEVER").entries
    assert read(text="safe").entries


def test_bad_provider_entries_are_ignored_and_controlled_fields_cannot_leak():
    for record in (
        {},
        {"resource": []},
        {**raw(), "resource": {"labels": []}},
        {**raw(), "timestamp": "bad"},
        {**raw(), "timestamp": "2026-01-01T00:00:00"},
        raw(seconds=-10),
        raw(seconds=1000),
        raw(OTHER),
    ):
        assert (
            logs.normalize(record, [SERVICE], NOW - timedelta(minutes=15), NOW) is None
        )
    record = raw(severity="CUSTOM")
    record["resource"]["labels"]["revision_name"] = "Bearer secret"
    record["labels"] = []
    row = logs.normalize(record, [SERVICE], NOW - timedelta(minutes=15), NOW)
    assert row[1].severity == "DEFAULT"
    assert row[1].revision is None
    assert logs._timestamp(42) is None
    assert logs._identifier(42) is None


def test_concurrent_requests_coalesce(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return [raw()], False

    provider = Mock(side_effect=fetch)
    monkeypatch.setattr(logs, "fetch_page", provider)
    worker = threading.Thread(target=read)
    worker.start()
    assert entered.wait(5)
    for _ in range(30):
        assert read().entries == []
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert provider.call_count == 1
    assert read().entries


def test_timestamps_sort_fractional_seconds_and_retention(monkeypatch):
    older = NOW.replace(microsecond=0)
    assert logs.iso(older) < logs.iso(older + timedelta(microseconds=1))
    read()
    cache = logs._caches[logs._key(SERVICE)]
    cache.last_success = NOW - timedelta(minutes=16)
    cache.next_due = logs.monotonic() + 60
    assert read().status == "stale"
    cache.next_due = 0
    stale = raw(seconds=1000, insert_id="old")
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([stale], False)))
    result = read()
    assert all(entry.id != "old" for entry in result.entries)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ENG_PLATFORM_LOGS_LOOKBACK_MINUTES", "16"),
        ("ENG_PLATFORM_LOGS_PAGE_SIZE", "1001"),
        ("ENG_PLATFORM_LOGS_BUFFER_ENTRIES", "2001"),
        ("ENG_PLATFORM_LOGS_BUFFER_BYTES", "2097153"),
        ("ENG_PLATFORM_LOGS_BUDGET_COLLECTION", "deployment_executions"),
        ("ENG_PLATFORM_LOGS_BUDGET_PROJECT_ID", 'evil"project'),
        ("ENG_PLATFORM_LOGS_REQUEST_TIMEOUT_SECONDS", "21"),
        ("ENG_PLATFORM_LOGS_REQUEST_TIMEOUT_SECONDS", "3"),
        ("ENG_PLATFORM_LOGS_REQUEST_TIMEOUT_SECONDS", "nan"),
        ("ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS", "inf"),
        ("ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS", "13"),
        ("ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS", "0"),
        ("ENG_PLATFORM_LOGS_RESERVE_TIMEOUT_SECONDS", "7"),
        ("ENG_PLATFORM_LOGS_RESERVE_TIMEOUT_SECONDS", "0"),
        ("ENG_PLATFORM_LOGS_FINISH_TIMEOUT_SECONDS", "7"),
        ("ENG_PLATFORM_LOGS_FINISH_TIMEOUT_SECONDS", "0"),
        # Individually valid ceilings must also fit within the total plus margin.
        ("ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS", "12"),
    ],
)
def test_config_rejects_unsafe_limits(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_config()


def test_config_logs_explicit_and_separate(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_LOGS_ALLOWED_GITHUB_LOGINS", "Reader, ANOTHER")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_BUDGET_COLLECTION", "eng_platform_log_budget")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_BUDGET_PROJECT_ID", "example-project")
    result = load_config()
    assert result.logs.allowed_logins == ("reader", "another")
    assert not result.logs.enabled
    assert result.logs.buffer_entries == 2000
    assert result.logs.request_timeout_seconds == 20
    assert result.logs.rpc_timeout_seconds == 10
    assert (
        result.logs.reserve_timeout_seconds == result.logs.finish_timeout_seconds == 4
    )


def test_official_client_constructor_disables_transport_retries(monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.logging_v2.services.logging_service_v2.transports import (
        LoggingServiceV2GrpcTransport,
    )
    import google.auth

    logs._logging_client.cache_clear()
    credentials = AnonymousCredentials()
    credentials._quota_project_id = "quota-project"
    auth = Mock(return_value=(credentials, None))
    monkeypatch.setattr(google.auth, "default", auth)
    original = LoggingServiceV2GrpcTransport.create_channel
    called = Mock(side_effect=original)
    monkeypatch.setattr(LoggingServiceV2GrpcTransport, "create_channel", called)
    instance = logs._logging_client(config.logs.quota_project_id)
    assert ("grpc.enable_retries", 0) in called.call_args.kwargs["options"]
    assert called.call_args.kwargs["quota_project_id"] == "quota-project"
    assert called.call_args.kwargs["credentials"] is credentials
    assert auth.call_args.kwargs["quota_project_id"] == "quota-project"
    assert isinstance(instance.transport, LoggingServiceV2GrpcTransport)
    instance.transport.close()
    logs._logging_client.cache_clear()


def test_finalization_failure_keeps_budget_charge_and_reports_stale(monkeypatch):
    monkeypatch.setattr(
        logs.log_budget, "finish", Mock(side_effect=RuntimeError("private"))
    )
    result = read()
    assert result.status == "stale"
    assert result.next_poll_seconds == 60
    assert result.entries
    assert "private" not in result.model_dump_json()


def test_sdk_interceptor_never_records_raw_payloads_even_at_debug(caplog):
    import logging
    from google.cloud.logging_v2.services.logging_service_v2.transports import grpc
    from google.cloud.logging_v2.types import (
        ListLogEntriesRequest,
        ListLogEntriesResponse,
    )

    logs._disable_sdk_payload_logging()
    logger = logging.getLogger(grpc.__name__)
    details = SimpleNamespace(metadata=[], method="ListLogEntries")
    result = ListLogEntriesResponse(
        entries=[GoogleLogEntry(text_payload="SYNTHETIC-RAW-PASSWORD")]
    )
    future = SimpleNamespace(trailing_metadata=lambda: [], result=lambda: result)
    with caplog.at_level(logging.DEBUG, logger=grpc.__name__):
        grpc._LoggingClientInterceptor().intercept_unary_unary(
            lambda *_: future, details, ListLogEntriesRequest()
        )
        # A late change of disabled cannot defeat the logger's drop filter.
        logger.disabled = False
        logger.debug("SYNTHETIC-RAW-PASSWORD")
        logger.disabled = True
    assert "SYNTHETIC-RAW-PASSWORD" not in caplog.text
    assert not any(hasattr(record, "response") for record in caplog.records)


def test_freshness_uses_clock_after_fetch_and_coordination(monkeypatch):
    from unittest.mock import patch

    later = NOW + timedelta(seconds=30)
    with patch.object(logs, "datetime") as clock:
        clock.now.side_effect = [NOW, later]
        clock.fromisoformat.side_effect = datetime.fromisoformat
        result = read()
    assert result.status == "stale"
    assert result.cache_age_seconds == 30
    assert result.window_start == logs.iso(later - timedelta(minutes=15))


def test_configured_lower_lookback_clamps_instead_of_breaking_viewer(monkeypatch):
    monkeypatch.setattr(config.logs, "lookback_minutes", 5)
    monkeypatch.setattr(
        logs,
        "fetch_page",
        Mock(
            return_value=([raw(seconds=30), raw(seconds=400, insert_id="older")], False)
        ),
    )
    result = read(lookback_minutes=15)
    assert len(result.entries) == 1
    assert logs._timestamp(result.window_start) > NOW - timedelta(minutes=6)
    assert logs.iso(NOW - timedelta(minutes=5)) in logs.fetch_page.call_args.args[1]


def test_config_allows_smaller_coherent_deadlines(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_LOGS_REQUEST_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_RESERVE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_FINISH_TIMEOUT_SECONDS", "2")
    result = load_config().logs
    assert result.request_timeout_seconds == 8
    assert result.rpc_timeout_seconds == 2.5
    assert not result.enabled


def test_sdk_construction_consumes_rpc_budget_and_cancellation_prevents_dispatch(
    monkeypatch,
):
    from importlib import reload

    real = reload(logs)
    elapsed = [100.0]
    deadline = logs.log_budget.Deadline.after(10, clock=lambda: elapsed[0])
    client = Mock()
    client.list_log_entries.return_value = SimpleNamespace(
        pages=[SimpleNamespace(entries=[], next_page_token="")]
    )

    def initialize(scope):
        elapsed[0] += 3
        return client

    monkeypatch.setattr(real, "_logging_client", initialize)
    real.fetch_page("example-project", "safe", deadline=deadline)
    assert client.list_log_entries.call_args.kwargs["timeout"] == 7
    deadline.cancelled.set()
    with pytest.raises(TimeoutError):
        real.fetch_page("example-project", "safe", deadline=deadline)
    assert client.list_log_entries.call_count == 1


def test_expired_sdk_initialization_never_dispatches_a_late_rpc(monkeypatch):
    from importlib import reload

    real = reload(logs)
    elapsed = [0.0]
    deadline = logs.log_budget.Deadline.after(10, clock=lambda: elapsed[0])
    client = Mock()

    def initialize(scope):
        elapsed[0] += 11
        return client

    monkeypatch.setattr(real, "_logging_client", initialize)
    with pytest.raises(TimeoutError):
        real.fetch_page("example-project", "safe", deadline=deadline)
    client.list_log_entries.assert_not_called()


def test_500_resources_and_projects_get_individual_bounded_filters(monkeypatch):
    # Model a slow runner deterministically: each independent request has a new
    # wall-clock observation and a matching budget timestamp. A fixed timestamp
    # across all 500 requests incorrectly makes later successful samples stale.
    observed = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed[0]

    monkeypatch.setattr(logs, "datetime", Clock)
    monkeypatch.setattr(
        logs.log_budget,
        "reserve",
        Mock(side_effect=lambda *args, **kwargs: Reservation(True, observed[0])),
    )
    resources = [
        logs.Resource(
            f"service-{index:04d}",
            f"project-{index:04d}",
            "us-central1",
            "cloud_run_service",
            True,
            ("reader",),
        )
        for index in range(500)
    ]
    monkeypatch.setattr(logs, "resources", lambda: resources)
    provider = Mock(return_value=([], False))
    monkeypatch.setattr(logs, "fetch_page", provider)
    for index, resource in enumerate(resources):
        observed[0] = NOW + timedelta(seconds=index * 2)
        result = logs.read_logs(resource, resources)
        assert result.status == "fresh" and result.entries == []
        assert result.last_success_at == logs.iso(observed[0])
        assert result.cache_age_seconds == 0
        query = provider.call_args.args[1]
        assert len(query) <= 20_000 and " OR " not in query
        assert f'"{resource.service_id}"' in query
        assert provider.call_args.args[0] == resource.project
    assert provider.call_count == 500
    participants = [call.args[0] for call in logs.log_budget.reserve.call_args_list]
    assert len(set(participants)) == 500
    assert all(len(item) == 64 for item in participants)
    assert len(logs._caches) == 500


def test_noisy_resource_cannot_mark_quiet_resource_observed(monkeypatch):
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([raw()], True)))
    first = read()
    assert first.truncated and first.status == "fresh"
    monkeypatch.setattr(
        logs.log_budget,
        "reserve",
        Mock(
            return_value=Reservation(
                False, NOW, 5, queue_position=1, queue_wait_seconds=5
            )
        ),
    )
    quiet = read(JOB)
    assert quiet.status == "throttled"
    assert quiet.last_success_at is None and quiet.cache_age_seconds is None
    assert quiet.entries == [] and quiet.queue_position == 1
    assert logs.fetch_page.call_count == 1
    logs._caches[logs._key(JOB)].next_due = 0
    monkeypatch.setattr(
        logs.log_budget, "reserve", Mock(return_value=Reservation(True, NOW))
    )
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([raw(JOB)], False)))
    quiet = read(JOB)
    assert quiet.status == "fresh" and len(quiet.entries) == 1
    assert logs.iso(NOW - timedelta(minutes=15)) in logs.fetch_page.call_args.args[1]
    assert SERVICE.service_id not in logs.fetch_page.call_args.args[1]


@pytest.mark.parametrize(
    "change",
    [
        {"region": "europe-west1"},
        {"kind": "cloud_run_job"},
        {"project": "moved-project"},
        {"allowed_logins": ("another",)},
    ],
)
@pytest.mark.parametrize("failure", ["throttle", "provider"])
def test_coordinates_and_policy_invalidate_cache_even_when_refresh_fails(
    monkeypatch, change, failure
):
    from dataclasses import replace

    assert read().entries
    old_cache = logs._caches[logs._key(SERVICE)]
    moved = replace(SERVICE, **change)
    current = [moved, JOB, OTHER]
    monkeypatch.setattr(logs, "resources", lambda: current)
    if failure == "throttle":
        monkeypatch.setattr(
            logs.log_budget, "reserve", Mock(return_value=Reservation(False, NOW, 60))
        )
    else:
        monkeypatch.setattr(
            logs, "fetch_page", Mock(side_effect=RuntimeError("private"))
        )
    result = logs.read_logs(moved, current)
    assert result.entries == [] and result.last_success_at is None
    assert old_cache.entries == {} and old_cache.invalidated
    assert logs._key(SERVICE) not in logs._caches
    assert result.status in {"throttled", "unavailable"}


def test_revoked_policy_during_rpc_does_not_publish_old_result(monkeypatch):
    from dataclasses import replace

    current = [SERVICE, JOB, OTHER]
    monkeypatch.setattr(logs, "resources", lambda: current)

    def changed_during_rpc(*args, **kwargs):
        current[0] = replace(SERVICE, enabled=False)
        return [raw(message="old policy content")], False

    monkeypatch.setattr(logs, "fetch_page", changed_during_rpc)
    result = logs.read_logs(SERVICE, list(current))
    assert result.status == "unavailable" and result.entries == []
    assert result.last_success_at is None and not logs._caches
    logs.log_budget.finish.assert_called_once()


def test_catalog_failure_clears_cached_data_before_return(monkeypatch):
    assert read().entries
    monkeypatch.setattr(logs, "resources", Mock(side_effect=ValueError("private path")))
    result = read()
    assert result.entries == [] and result.last_success_at is None
    assert result.status == "unavailable" and not logs._caches


def test_retention_is_global_and_noisy_source_does_not_evict_quiet_source(monkeypatch):
    monkeypatch.setattr(config.logs, "buffer_entries", 4)
    monkeypatch.setattr(
        logs, "fetch_page", Mock(return_value=([raw(JOB, seconds=10)], False))
    )
    assert read(JOB).entries
    monkeypatch.setattr(
        logs,
        "fetch_page",
        Mock(
            return_value=([raw(seconds=1, insert_id=str(i)) for i in range(100)], True)
        ),
    )
    noisy = read()
    assert noisy.cache_evicted and noisy.truncated and noisy.status == "stale"
    assert len(read(JOB).entries) == 1
    assert not read(JOB).cache_evicted
    assert sum(len(cache.entries) for cache in logs._caches.values()) == 4


def test_global_byte_bound_reports_eviction_of_last_entry(monkeypatch):
    monkeypatch.setattr(config.logs, "buffer_bytes", 1000)
    monkeypatch.setattr(
        logs, "fetch_page", Mock(return_value=([raw(JOB, message="q" * 600)], False))
    )
    read(JOB)
    monkeypatch.setattr(
        logs, "fetch_page", Mock(return_value=([raw(message="n" * 600)], False))
    )
    read()
    retained = sum(
        len(entry.model_dump_json().encode())
        for cache in logs._caches.values()
        for _, entry in cache.entries.values()
    )
    assert retained <= 1000
    evicted = [cache for cache in logs._caches.values() if cache.cache_evicted]
    assert evicted and all(cache.truncated for cache in evicted)
    result = read(evicted[0].resource)
    assert result.cache_evicted and result.status == "stale"
    assert result.entries == [] and result.last_success_at is not None


def test_historical_project_churn_releases_retired_caches(monkeypatch):
    for index in range(100):
        resource = logs.Resource(
            f"service-{index}",
            f"project-{index}",
            "us-central1",
            "cloud_run_service",
            True,
            ("reader",),
        )
        monkeypatch.setattr(logs, "resources", lambda: [resource])
        monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([], False)))
        assert logs.read_logs(resource, [resource]).status == "fresh"
        assert len(logs._caches) == 1


def test_cache_state_bound_uses_lru_and_never_overflows(monkeypatch):
    monkeypatch.setattr(logs, "_MAX_CACHE_RESOURCES", 2)
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([], False)))
    read(SERVICE)
    read(JOB)
    read(SERVICE)  # Most recently used; job is evicted by the third resource.
    read(OTHER)
    assert set(logs._caches) == {logs._key(SERVICE), logs._key(OTHER)}
    # Evicted sample metadata is forgotten, never inherited from another source.
    monkeypatch.setattr(
        logs.log_budget, "reserve", Mock(return_value=Reservation(False, NOW))
    )
    result = read(JOB)
    assert result.last_success_at is None and result.status == "throttled"
    for cache in logs._caches.values():
        cache.lock.acquire()
    try:
        result = read(SERVICE)
        assert result.overloaded and result.status == "unavailable"
        assert len(logs._caches) == 2
    finally:
        for cache in logs._caches.values():
            cache.lock.release()


def test_filter_guard_runs_before_sdk_and_rejects_oversized_builder(monkeypatch):
    from importlib import reload

    real = reload(logs)
    client = Mock()
    monkeypatch.setattr(real, "_logging_client", lambda scope: client)
    with pytest.raises(ValueError, match="Invalid runtime log query"):
        real.fetch_page(SERVICE.project, "x" * 20_001)
    client.list_log_entries.assert_not_called()
    monkeypatch.setattr(real, "_resource_filter", lambda _: "x" * 20_000)
    with pytest.raises(ValueError, match="filter exceeds"):
        real.query_filter(SERVICE.project, [SERVICE], NOW, NOW)


def test_empty_page_with_token_is_truncated_but_empty_without_token_is_observation(
    monkeypatch,
):
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([], True)))
    result = read()
    assert result.entries == [] and result.truncated and result.last_success_at
    logs._caches.clear()
    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([], False)))
    result = read()
    assert result.entries == [] and result.status == "fresh" and not result.truncated


@pytest.mark.parametrize(
    "trace,span,expected",
    [
        ("projects/example-project/traces/" + "a" * 32, "b" * 16, True),
        ("projects/another-project/traces/" + "a" * 32, "b" * 16, False),
        ("https://evil.invalid/" + "a" * 32, "b" * 16, False),
        ("projects/example-project/traces/" + "A" * 32, "b" * 16, False),
        (None, "b" * 16, False),
    ],
)
def test_trace_metadata_is_strict_and_bound_to_authorized_project(
    trace, span, expected
):
    record = {**raw(), "trace": trace, "span_id": span}
    _, entry, _ = logs.normalize(record, [SERVICE], NOW - timedelta(minutes=15), NOW)
    assert (entry.trace is not None) is expected
    assert (entry.span_id is not None) is expected
    record["trace"] = "projects/example-project/traces/" + "a" * 32
    record["span_id"] = "unsafe<script>"
    _, entry, _ = logs.normalize(record, [SERVICE], NOW - timedelta(minutes=15), NOW)
    assert entry.trace and entry.span_id is None


def test_enabled_configuration_requires_explicit_quota_project(monkeypatch):
    # Isolate quota validation from production-only private-catalog prerequisites.
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "true")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_ENABLED", "true")
    monkeypatch.delenv("ENG_PLATFORM_LOGS_QUOTA_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="quota project"):
        load_config()
    monkeypatch.setenv("ENG_PLATFORM_LOGS_QUOTA_PROJECT_ID", "quota-project")
    assert load_config().logs.quota_project_id == "quota-project"
    monkeypatch.setenv("ENG_PLATFORM_LOGS_QUOTA_PROJECT_ID", "unsafe/project")
    with pytest.raises(ValueError, match="quota project"):
        load_config()


def test_sdk_rejects_credentials_without_matching_quota_attribution(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials

    logs._logging_client.cache_clear()
    monkeypatch.setattr(
        google.auth, "default", lambda **kwargs: (AnonymousCredentials(), None)
    )
    with pytest.raises(RuntimeError, match="quota attribution"):
        logs._logging_client(config.logs.quota_project_id)
    logs._logging_client.cache_clear()


def test_public_generation_is_opaque_stable_and_changes_on_identity_or_policy(
    monkeypatch,
):
    from dataclasses import replace

    monkeypatch.setattr(
        config.auth, "session_secret", "synthetic-secret-unique-32-characters"
    )
    generation = read().resource_generation
    assert generation == read().resource_generation and len(generation) == 64
    assert generation != SERVICE.policy_fingerprint
    for resource in (
        replace(SERVICE, region="europe-west1"),
        replace(SERVICE, allowed_logins=("another",)),
    ):
        assert logs._generation(resource) != generation
    monkeypatch.setattr(
        config.auth, "session_secret", "rotated-secret-unique-32-characters"
    )
    assert logs._generation(SERVICE) != generation
    monkeypatch.setattr(config.logs, "enabled", False)
    assert read().resource_generation == logs._generation(SERVICE)


def test_expired_retention_staging_never_publishes_oversized_data(monkeypatch):
    from tests.log_test_support import FakeClock

    monkeypatch.setattr(logs, "fetch_page", Mock(return_value=([raw(JOB)], False)))
    read(JOB)
    old_cache = logs._caches[logs._key(JOB)]
    previous = dict(old_cache.entries)
    monkeypatch.setattr(config.logs, "buffer_entries", 1)
    cache = logs._cache(SERVICE, [SERVICE, JOB, OTHER])
    record = logs.normalize(raw(), [SERVICE], NOW - timedelta(minutes=15), NOW)
    updated = {record[1].id: (record[0], record[1])}
    clock = FakeClock()
    deadline = logs.log_budget.Deadline.after(1, clock=clock)
    clock.advance(1)
    with logs._caches_lock, pytest.raises(TimeoutError):
        logs._bound_locked(cache, updated, deadline)
    assert cache.entries == {} and old_cache.entries == previous
    assert sum(len(item.entries) for item in logs._caches.values()) == 1


def test_disabled_resource_never_reserves_or_dispatches(monkeypatch):
    from dataclasses import replace

    resource = replace(SERVICE, enabled=False)
    assert logs.read_logs(resource, [resource]).status == "disabled"
    logs.log_budget.reserve.assert_not_called()
    logs.fetch_page.assert_not_called()


def test_process_forks_get_distinct_participants_even_with_inherited_uuid(monkeypatch):
    monkeypatch.setattr(logs.os, "getpid", lambda: 100)
    first = logs._participant(SERVICE)
    assert first == logs._participant(SERVICE)
    monkeypatch.setattr(logs.os, "getpid", lambda: 101)
    assert first != logs._participant(SERVICE)


def test_entry_bound_accounts_for_json_escaping():
    record = raw(message='"' * 8192)
    _, entry, clipped = logs.normalize(
        record, [SERVICE], NOW - timedelta(minutes=15), NOW
    )
    assert clipped and len(entry.model_dump_json().encode()) <= 16384
    assert entry.message == "Message omitted: entry size limit"


def test_sdk_missing_quota_fails_before_credential_discovery(monkeypatch):
    import google.auth

    logs._logging_client.cache_clear()
    monkeypatch.setattr(config.logs, "quota_project_id", "")
    discover = Mock()
    monkeypatch.setattr(google.auth, "default", discover)
    with pytest.raises(RuntimeError, match="quota project"):
        logs._logging_client(config.logs.quota_project_id)
    discover.assert_not_called()
    logs._logging_client.cache_clear()


def test_catalog_revoked_during_coordination_spends_permit_without_dispatch(
    monkeypatch,
):
    from dataclasses import replace

    current = [SERVICE, JOB, OTHER]
    monkeypatch.setattr(logs, "resources", lambda: current)

    def reserve(*args, **kwargs):
        current[0] = replace(SERVICE, allowed_logins=("another",))
        return Reservation(True, NOW)

    monkeypatch.setattr(logs.log_budget, "reserve", reserve)
    result = logs.read_logs(SERVICE, list(current))
    assert result.entries == [] and result.status == "unavailable"
    logs.fetch_page.assert_not_called()
    logs.log_budget.finish.assert_called_once()


def test_direct_snapshot_missing_target_cannot_read_or_reserve():
    result = logs.read_logs(SERVICE, [JOB])
    assert result.entries == [] and result.status == "unavailable"
    logs.fetch_page.assert_not_called()
    logs.log_budget.reserve.assert_not_called()


def test_payload_message_nonstring_and_response_window_filter(monkeypatch):
    record = {**raw(message=""), "json_payload": {"message": 42}}
    _, entry, _ = logs.normalize(record, [SERVICE], NOW - timedelta(minutes=15), NOW)
    assert entry.message == ""
    monkeypatch.setattr(
        logs, "fetch_page", Mock(return_value=([raw(seconds=300)], False))
    )
    assert read(lookback_minutes=15).entries
    assert not read(lookback_minutes=1).entries


def test_sdk_clients_and_rpc_attribution_follow_quota_changes(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.logging_v2.services import logging_service_v2
    from google.cloud.logging_v2.services.logging_service_v2 import transports
    from importlib import reload

    real = reload(logs)
    clients = {}
    discovered = []

    def credentials(**kwargs):
        quota = kwargs["quota_project_id"]
        discovered.append(quota)
        value = AnonymousCredentials()
        value._quota_project_id = quota
        return value, None

    class Transport:
        @staticmethod
        def create_channel(host, *, credentials, quota_project_id, options):
            assert credentials.quota_project_id == quota_project_id
            assert ("grpc.enable_retries", 0) in options
            return quota_project_id

        def __init__(self, *, channel):
            self.quota = channel

    def client(*, transport):
        instance = Mock()
        instance.list_log_entries.return_value = SimpleNamespace(
            pages=[SimpleNamespace(entries=[], next_page_token="")]
        )
        clients[transport.quota] = instance
        return instance

    monkeypatch.setattr(google.auth, "default", credentials)
    monkeypatch.setattr(transports, "LoggingServiceV2GrpcTransport", Transport)
    monkeypatch.setattr(logging_service_v2, "LoggingServiceV2Client", client)
    monkeypatch.setattr(config.logs, "quota_project_id", "first-quota")
    real.fetch_page(SERVICE.project, "safe")
    monkeypatch.setattr(config.logs, "quota_project_id", "second-quota")
    real.fetch_page(SERVICE.project, "safe")
    assert discovered == ["first-quota", "second-quota"]
    assert clients["first-quota"].list_log_entries.call_count == 1
    assert clients["second-quota"].list_log_entries.call_count == 1
    assert real._logging_client("second-quota") is clients["second-quota"]
    real._logging_client.cache_clear()


@pytest.mark.parametrize(
    "field,value",
    [
        ("quota_project_id", "changed-quota"),
        ("budget_project_id", "changed-budget"),
        ("budget_collection", "eng_platform_log_budget_changed"),
    ],
)
def test_scope_change_during_cold_sdk_prevents_dispatch(monkeypatch, field, value):
    from importlib import reload

    real = reload(logs)
    scope = real.log_budget.coordination_scope()
    client = Mock()

    def initialize(quota):
        assert quota == scope.quota_project_id
        monkeypatch.setattr(config.logs, field, value)
        return client

    monkeypatch.setattr(real, "_logging_client", initialize)
    with pytest.raises(RuntimeError, match="scope changed"):
        real.fetch_page(SERVICE.project, "safe", scope=scope)
    client.list_log_entries.assert_not_called()
    # A caller holding the obsolete snapshot must fail even before SDK lookup.
    builder = Mock()
    monkeypatch.setattr(real, "_logging_client", builder)
    with pytest.raises(RuntimeError, match="scope changed"):
        real.fetch_page(SERVICE.project, "safe", scope=scope)
    builder.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("quota_project_id", "changed-quota"),
        ("budget_project_id", "changed-budget"),
        ("budget_collection", "eng_platform_log_budget_changed"),
    ],
)
def test_scope_change_during_reservation_cannot_dispatch_and_finishes_original_scope(
    monkeypatch, field, value
):
    original = logs.log_budget.coordination_scope()

    def reserve(*args, scope, **kwargs):
        assert scope == original
        monkeypatch.setattr(config.logs, field, value)
        return Reservation(True, NOW, token="a" * 32)

    monkeypatch.setattr(logs.log_budget, "reserve", reserve)
    result = read()
    assert result.status == "unavailable" and result.entries == []
    logs.fetch_page.assert_not_called()
    assert logs.log_budget.finish.call_args.kwargs["scope"] == original
    assert logs.log_budget.finish.call_args.args[1] == "a" * 32


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_multidigit_regions_share_catalog_pattern_for_filter_and_normalization(kind):
    from eng_platform_api.services.log_catalog import REGION_PATTERN

    resource = logs.Resource(
        "future-runtime", "example-project", "europe-west12", kind, True, ("reader",)
    )
    query = logs.query_filter(
        resource.project, [resource], NOW - timedelta(minutes=15), NOW
    )
    assert logs._REGION.pattern == REGION_PATTERN
    assert 'resource.labels.location="europe-west12"' in query
    assert f'resource.type="{resource.resource_type}"' in query
    assert len(query) < 20_000
    result = logs.normalize(raw(resource), [resource], NOW - timedelta(minutes=15), NOW)
    assert result is not None and result[0] == resource.service_id
    assert result[1].message == "hello"
