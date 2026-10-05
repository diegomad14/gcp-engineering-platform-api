"""Demand-driven single-resource samples with bounded replica-local retention.

No logs are persisted. A shared FIFO grants only the requesting replica/resource
one first-page RPC; this viewer is intentionally not a complete log stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import hmac
import json
import logging
import math
import os
import re
from threading import Lock
from time import monotonic
from typing import Any, Callable, cast
from urllib.parse import urlencode
import uuid

from ..config import config
from ..models import LogDeferralReason, LogEntry, LogSeverity, ServiceLogsResponse
from . import log_budget
from .log_catalog import REGION_PATTERN, Resource, resources
from .log_redaction import sanitize_payload, sanitize_text

SEVERITIES = (
    "DEFAULT",
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "ALERT",
    "EMERGENCY",
)
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
_REGION = re.compile(REGION_PATTERN)
_RESOURCE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_MAX_ENTRY_BYTES = 16_384
_MAX_CACHE_RESOURCES = 4096
_MAX_FILTER_CHARS = 20_000
_REPLICA_ID = uuid.uuid4().hex
_LIMITATIONS = [
    "best_effort_replica_local_cache",
    "late_entries_may_be_missing",
    "resource_sampling_not_complete",
    "queue_wait_is_lower_bound",
]


def _valid_resource(resource: Resource) -> bool:
    return bool(
        len(resource.project) <= 30
        and len(resource.region) <= 63
        and len(resource.service_id) <= 63
        and _PROJECT.fullmatch(resource.project)
        and _REGION.fullmatch(resource.region)
        and _RESOURCE.fullmatch(resource.service_id)
        and resource.kind in {"cloud_run_service", "cloud_run_job"}
    )


def iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _resource_filter(resource: Resource) -> str:
    # JSON quoting is also valid Cloud Logging string-literal escaping. All
    # values have already been checked against narrow catalog coordinate rules.
    if not _valid_resource(resource):
        raise ValueError("Invalid runtime log catalog coordinates")
    labels = {
        "project_id": resource.project,
        "location": resource.region,
        resource.name_label: resource.service_id,
    }
    return " AND ".join(
        [
            f"resource.type={json.dumps(resource.resource_type)}",
            *[
                f"resource.labels.{key}={json.dumps(value)}"
                for key, value in labels.items()
            ],
        ]
    )


def query_filter(
    project: str, items: list[Resource], since: datetime, until: datetime
) -> str:
    selected = [item for item in items if item.project == project]
    if len(selected) != 1:
        raise ValueError("Exactly one demanded catalog resource is required")
    query = (
        f"timestamp>={json.dumps(iso(since))} AND timestamp<={json.dumps(iso(until))} AND "
        f"({_resource_filter(selected[0])})"
    )
    if len(query) > _MAX_FILTER_CHARS:
        raise ValueError("Runtime log filter exceeds provider limit")
    return query


def explorer_url(resource: Resource) -> str:
    return "https://console.cloud.google.com/logs/query?" + urlencode(
        {"project": resource.project, "query": _resource_filter(resource)}
    )


class _NoRawLogPayloads(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


def _disable_sdk_payload_logging() -> None:
    # The generated SDK interceptor can log complete unsanitized responses at
    # DEBUG. Disable only this transport's logger, before any client/RPC exists.
    logger = logging.getLogger(
        "google.cloud.logging_v2.services.logging_service_v2.transports.grpc"
    )
    logger.disabled = True
    if not any(isinstance(item, _NoRawLogPayloads) for item in logger.filters):
        logger.addFilter(_NoRawLogPayloads())


@lru_cache(maxsize=1)
def _logging_client(quota_project: str):
    from google.cloud.logging_v2.services.logging_service_v2 import (
        LoggingServiceV2Client,
    )

    from google.cloud.logging_v2.services.logging_service_v2.transports import (
        LoggingServiceV2GrpcTransport,
    )

    import google.auth

    if not _PROJECT.fullmatch(quota_project):
        raise RuntimeError("Log quota project is not configured")
    _disable_sdk_payload_logging()
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id=quota_project,
    )
    # Fail closed if this credential type cannot attribute requests to the same
    # explicit project used by the shared budget. Do not silently use ADC's own.
    if getattr(credentials, "quota_project_id", None) != quota_project:
        raise RuntimeError("Log quota attribution is unavailable")
    # Disable gRPC transparent retries as well as GAPIC retries on every call.
    channel = LoggingServiceV2GrpcTransport.create_channel(
        "logging.googleapis.com",
        credentials=credentials,
        quota_project_id=quota_project,
        options=[("grpc.enable_retries", 0)],
    )
    return LoggingServiceV2Client(
        transport=LoggingServiceV2GrpcTransport(channel=channel)
    )


def fetch_page(
    project: str,
    filter_: str,
    *,
    deadline: log_budget.Deadline | None = None,
    scope: log_budget.CoordinationScope | None = None,
    authorize: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    from google.cloud.logging_v2.types import LogEntry as GoogleLogEntry

    deadline = deadline or log_budget.Deadline.after(config.logs.rpc_timeout_seconds)
    deadline.remaining()
    if len(filter_) > _MAX_FILTER_CHARS or not _PROJECT.fullmatch(project):
        raise ValueError("Invalid runtime log query")
    scope = scope or log_budget.coordination_scope()
    if scope != log_budget.coordination_scope():
        raise RuntimeError("Runtime log scope changed")
    client = _logging_client(scope.quota_project_id)
    if scope != log_budget.coordination_scope():
        raise RuntimeError("Runtime log scope changed")
    if authorize is not None:
        authorize()
    # Client/ADC initialization can block independently of the RPC timeout.
    # Recheck after construction; the router also bounds its asynchronous wait.
    pager = client.list_log_entries(
        request={
            "resource_names": [f"projects/{project}"],
            "filter": filter_,
            "order_by": "timestamp desc",
            "page_size": config.logs.page_size,
        },
        retry=None,
        timeout=deadline.timeout(config.logs.rpc_timeout_seconds),
    )
    # The first response is already fetched. Never iterate pager entries/pages
    # beyond this first page: every page would be a separately billed RPC.
    page = next(iter(pager.pages))
    deadline.remaining()
    return [
        GoogleLogEntry.to_dict(entry, use_integers_for_enums=False)
        for entry in page.entries
    ], bool(page.next_page_token)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _identifier(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,127}", value):
        return value
    return None


def normalize(
    raw: dict[str, Any], allowed: list[Resource], since: datetime, until: datetime
) -> tuple[str, LogEntry, bool] | None:
    monitored = raw.get("resource", {})
    if not isinstance(monitored, dict):
        return None
    labels = monitored.get("labels", {})
    if not isinstance(labels, dict):
        return None
    resource = next(
        (
            r
            for r in allowed
            if monitored.get("type") == r.resource_type
            and labels.get("project_id") == r.project
            and labels.get("location") == r.region
            and labels.get(r.name_label) == r.service_id
        ),
        None,
    )
    timestamp = _timestamp(raw.get("timestamp"))
    if resource is None or timestamp is None or timestamp < since or timestamp > until:
        return None
    source = raw.get("json_payload", raw.get("proto_payload"))
    payload, clipped = sanitize_payload(source)
    message_source = raw.get("text_payload")
    if not isinstance(message_source, str) or not message_source:
        message_source = payload.get("message", "") if isinstance(payload, dict) else ""
    if not isinstance(message_source, str):
        message_source = ""
    message, text_clipped = sanitize_text(message_source)
    clipped = clipped or text_clipped
    severity = raw.get("severity", "DEFAULT")
    severity = severity if severity in SEVERITIES else "DEFAULT"
    extra = raw.get("labels", {})
    extra = extra if isinstance(extra, dict) else {}
    task = extra.get("run.googleapis.com/task_index")
    task_index = (
        int(task)
        if isinstance(task, (str, int)) and re.fullmatch(r"[0-9]{1,6}", str(task))
        else None
    )
    fingerprint = {
        "resource": resource.key,
        "timestamp": iso(timestamp),
        "insert_id": str(raw.get("insert_id", ""))[:512],
        "log_name": str(raw.get("log_name", ""))[:512],
        "message": message,
        "payload": payload,
    }
    trace = raw.get("trace")
    trace = (
        trace
        if isinstance(trace, str)
        and re.fullmatch(
            rf"projects/{re.escape(resource.project)}/traces/[0-9a-f]{{32}}", trace
        )
        else None
    )
    span = raw.get("span_id")
    span = (
        span
        if trace and isinstance(span, str) and re.fullmatch(r"[0-9a-f]{16}", span)
        else None
    )
    entry = LogEntry(
        id=hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest(),
        timestamp=iso(timestamp),
        severity=cast(LogSeverity, severity),
        message=message,
        payload=payload,
        revision=_identifier(labels.get("revision_name"))
        if resource.kind == "cloud_run_service"
        else None,
        execution=_identifier(extra.get("run.googleapis.com/execution_name"))
        if resource.kind == "cloud_run_job"
        else None,
        task_index=task_index if resource.kind == "cloud_run_job" else None,
        trace=trace,
        span_id=span,
    )
    if len(entry.model_dump_json().encode()) > _MAX_ENTRY_BYTES:
        entry.payload = {"notice": "Payload omitted: entry size limit"}
        # Escaping quotes, backslashes or newlines can expand a bounded source
        # string in JSON. The final serialized entry bound still applies.
        if len(entry.model_dump_json().encode()) > _MAX_ENTRY_BYTES:
            entry.message = "Message omitted: entry size limit"
        clipped = True
    return resource.service_id, entry, clipped


@dataclass
class Cache:
    resource: Resource
    lock: Any = field(default_factory=Lock)
    entries: dict[str, tuple[str, LogEntry]] = field(default_factory=dict)
    next_due: float = 0
    status: str = "unavailable"
    observed_at: datetime | None = None
    last_success: datetime | None = None
    truncated: bool = False
    cache_evicted: bool = False
    errors: int = 0
    touched: float = 0
    queue_position: int | None = None
    queue_wait_seconds: int | None = None
    overloaded: bool = False
    deferral_reason: LogDeferralReason | None = None
    invalidated: bool = False


CacheKey = tuple[str, str, str, str, str]
_caches: dict[CacheKey, Cache] = {}
_caches_lock = Lock()


def _key(resource: Resource) -> CacheKey:
    return (*resource.key, resource.policy_fingerprint)


def _generation(resource: Resource) -> str:
    # The public cache token must not expose a dictionary-testable ACL digest.
    # Real routes already require a strong session secret before reading logs.
    material = json.dumps(
        ["runtime-log-redaction-v1", *_key(resource)], separators=(",", ":")
    ).encode()
    return hmac.new(
        config.auth.session_secret.encode(), material, hashlib.sha256
    ).hexdigest()


def _participant(resource: Resource) -> str:
    # No user, source name, project or log content is stored in the FIFO.
    return hashlib.sha256(
        json.dumps(
            [_REPLICA_ID, os.getpid(), *_key(resource)], separators=(",", ":")
        ).encode()
    ).hexdigest()


def _discard(cache: Cache) -> None:
    cache.entries = {}
    cache.last_success = None
    cache.observed_at = None
    cache.invalidated = True
    cache.status = "unavailable"


def _reconcile_locked(items: list[Resource]) -> None:
    current = {_key(item) for item in items}
    for key in list(_caches):
        if key not in current:
            _discard(_caches.pop(key))


def _cache(resource: Resource, items: list[Resource]) -> Cache | None:
    with _caches_lock:
        _reconcile_locked(items)
        key = _key(resource)
        if key not in {_key(item) for item in items}:
            return None
        if key not in _caches:
            if len(_caches) >= _MAX_CACHE_RESOURCES:
                candidates = [
                    row for row in _caches.items() if not row[1].lock.locked()
                ]
                if not candidates:
                    return None
                oldest, evicted = min(candidates, key=lambda row: row[1].touched)
                del _caches[oldest]
                _discard(evicted)
            _caches[key] = Cache(resource)
        result = _caches[key]
        result.touched = monotonic()
        return result


def _is_current(cache: Cache) -> bool:
    # Reload before publishing AND before returning, including throttle/error
    # paths. An old in-flight RPC must never repopulate revoked coordinates/ACL.
    try:
        current = resources()
    except (ValueError, TypeError, KeyError):
        with _caches_lock:
            _discard(cache)
            _caches.pop(_key(cache.resource), None)
        return False
    with _caches_lock:
        _reconcile_locked(current)
        return not cache.invalidated and _caches.get(_key(cache.resource)) is cache


def _bound_locked(
    target: Cache,
    updated: dict[str, tuple[str, LogEntry]],
    deadline: log_budget.Deadline,
) -> None:
    # Stage retention changes, then publish atomically. A work deadline during
    # eviction must not leave an oversized retained cache or a partial sample.
    retained = {key: dict(cache.entries) for key, cache in _caches.items()}
    retained[_key(target.resource)] = updated
    sizes: dict[CacheKey, int] = {}
    row_sizes: dict[str, int] = {}
    for key, entries in retained.items():
        sizes[key] = 0
        for identity, (_, entry) in entries.items():
            deadline.remaining()
            row_sizes[identity] = len(entry.model_dump_json().encode())
            sizes[key] += row_sizes[identity]
    count = sum(len(entries) for entries in retained.values())
    size = sum(sizes.values())
    evicted: set[CacheKey] = set()
    # Global water-filling: remove from the largest resource first; ties use
    # LRU. A noisy source's newer timestamps cannot crowd out a sparse source.
    while count > config.logs.buffer_entries or size > config.logs.buffer_bytes:
        deadline.remaining()
        candidates = [(key, entries) for key, entries in retained.items() if entries]
        key, entries = max(
            candidates,
            key=lambda row: (
                len(row[1]) if count > config.logs.buffer_entries else sizes[row[0]],
                -_caches[row[0]].touched,
            ),
        )
        oldest = min(entries, key=lambda item: (entries[item][1].timestamp, item))
        del entries[oldest]
        removed = row_sizes[oldest]
        sizes[key] -= removed
        size -= removed
        count -= 1
        evicted.add(key)
    deadline.remaining()
    for key, entries in retained.items():
        cache = _caches[key]
        cache.entries = entries
        if key in evicted:
            cache.truncated = True
            cache.cache_evicted = True
            if cache.status == "fresh":
                cache.status = "stale"


def _refresh(
    cache: Cache,
    deadline: log_budget.Deadline,
    authorize: Callable[[], None] | None = None,
) -> None:
    now = monotonic()
    if now < cache.next_due or not cache.lock.acquire(blocking=False):
        return
    reservation = None
    scope = None
    safe_to_finish = True
    resource = cache.resource
    participant = _participant(resource)

    def dispatch_guard() -> None:
        if not _is_current(cache):
            raise RuntimeError("Runtime log policy changed")
        if authorize is not None:
            authorize()

    try:
        if now < cache.next_due or not _is_current(cache):
            return
        cache.next_due = now + 5
        scope = log_budget.coordination_scope()
        reservation = log_budget.reserve(
            participant,
            deadline=deadline.phase(config.logs.reserve_timeout_seconds),
            scope=scope,
        )
        cache.observed_at = reservation.server_time
        cache.queue_position = reservation.queue_position
        cache.queue_wait_seconds = reservation.queue_wait_seconds
        cache.overloaded = reservation.overloaded
        cache.deferral_reason = reservation.deferral_reason
        if not reservation.allowed:
            cache.status = "throttled"
            cache.next_due = monotonic() + reservation.next_poll_seconds
            return
        # Firestore admission may take seconds. Start the local cooldown after
        # the acknowledgement so our next call cannot race its own cadence.
        cache.next_due = monotonic() + log_budget.CADENCE_SECONDS
        window = reservation.server_time - timedelta(
            minutes=config.logs.lookback_minutes
        )
        since = (
            max(window, cache.last_success - timedelta(minutes=2))
            if cache.last_success
            else window
        )
        work = deadline.phase(
            deadline.remaining(), reserve_seconds=config.logs.finish_timeout_seconds
        )
        rpc = work.phase(config.logs.rpc_timeout_seconds)
        # Recheck catalog after reservation as coordination may consume seconds.
        dispatch_guard()
        if scope != log_budget.coordination_scope():
            raise RuntimeError("Runtime log scope changed")
        filter_ = query_filter(
            resource.project, [resource], since, reservation.server_time
        )
        safe_to_finish = False
        try:
            page, has_more = fetch_page(
                resource.project,
                filter_,
                deadline=rpc,
                scope=scope,
                authorize=dispatch_guard,
            )
        except Exception:
            safe_to_finish = True
            raise
        safe_to_finish = True
        work.remaining()
        with _caches_lock:
            updated = {
                key: row
                for key, row in cache.entries.items()
                if row[1].timestamp >= iso(window)
            }
        clipped_page = has_more or len(page) >= config.logs.page_size
        for raw in page[: config.logs.page_size]:
            work.remaining()
            result = normalize(raw, [resource], window, reservation.server_time)
            if result:
                service_id, entry, clipped = result
                updated[entry.id] = (service_id, entry)
                clipped_page = clipped_page or clipped
        work.remaining()
        if not _is_current(cache):
            return
        with _caches_lock:
            if cache.invalidated or _caches.get(_key(resource)) is not cache:
                return
            _bound_locked(cache, updated, work)
            cache.truncated = cache.truncated or clipped_page
            cache.last_success = reservation.server_time
            cache.status = "stale" if cache.cache_evicted else "fresh"
            cache.errors = 0
    except Exception:
        # Provider errors can contain queries, payloads, identities or secrets.
        cache.errors = min(cache.errors + 1, 4)
        cache.status = "stale" if cache.last_success else "unavailable"
        cache.queue_position = None
        cache.queue_wait_seconds = None
        cache.overloaded = False
        cache.deferral_reason = None
        cache.next_due = monotonic() + min(60, 5 * 2**cache.errors)
    finally:
        if reservation is not None and reservation.allowed:
            try:
                if not safe_to_finish:
                    raise RuntimeError("Log call completion is unknown")
                log_budget.finish(
                    participant,
                    reservation.token,
                    deadline=deadline.completion(config.logs.finish_timeout_seconds),
                    scope=scope,
                )
            except Exception:
                cache.status = "stale" if cache.last_success else "unavailable"
                cache.queue_position = None
                cache.queue_wait_seconds = None
                cache.overloaded = False
                cache.deferral_reason = None
                cache.next_due = monotonic() + 60
        cache.lock.release()


def read_logs(
    resource: Resource,
    items: list[Resource],
    *,
    limit: int = 200,
    lookback_minutes: int = 15,
    severity: str = "DEFAULT",
    text: str = "",
    revision: str | None = None,
    execution: str | None = None,
    task_index: int | None = None,
    deadline: log_budget.Deadline | None = None,
    authorize: Callable[[], None] | None = None,
) -> ServiceLogsResponse:
    deadline = deadline or log_budget.Deadline.after(
        config.logs.request_timeout_seconds
    )
    now = datetime.now(timezone.utc)
    window = now - timedelta(
        minutes=min(lookback_minutes, config.logs.lookback_minutes)
    )
    response = ServiceLogsResponse(
        status="disabled",
        resource_generation=_generation(resource),
        explorer_url=explorer_url(resource),
        window_start=iso(window),
        limitations=_LIMITATIONS,
    )
    if (
        not config.logs.enabled
        or config.mock_mode
        or not resource.enabled
        or not resource.allowed_logins
    ):
        return response
    cache = _cache(resource, items)
    if cache is None:
        response.status = "unavailable"
        response.overloaded = True
        response.deferral_reason = "overload"
        response.next_poll_seconds = 60
        return response
    _refresh(cache, deadline, authorize)
    if not _is_current(cache):
        response.status = "unavailable"
        return response
    now = datetime.now(timezone.utc)
    window = now - timedelta(
        minutes=min(lookback_minutes, config.logs.lookback_minutes)
    )
    response.window_start = iso(window)
    with _caches_lock:
        if cache.invalidated:
            response.status = "unavailable"
            return response
        age = (
            max(0.0, (now - cache.last_success).total_seconds())
            if cache.last_success
            else None
        )
        response.status = cast(
            Any,
            "stale"
            if cache.status == "fresh" and age is not None and age > 15
            else cache.status,
        )
        response.observed_at = iso(cache.observed_at) if cache.observed_at else None
        response.last_success_at = (
            iso(cache.last_success) if cache.last_success else None
        )
        response.cache_age_seconds = round(age, 1) if age is not None else None
        response.next_poll_seconds = max(
            5, min(60, math.ceil(cache.next_due - monotonic()))
        )
        response.truncated = cache.truncated
        response.cache_evicted = cache.cache_evicted
        response.queue_position = cache.queue_position
        response.queue_wait_seconds = cache.queue_wait_seconds
        response.overloaded = cache.overloaded
        response.deferral_reason = cache.deferral_reason
        entries = list(cache.entries.values())
    matching = []
    for _, entry in entries:
        deadline.remaining()
        if entry.timestamp < iso(window):
            continue
        if SEVERITIES.index(entry.severity) < SEVERITIES.index(severity):
            continue
        if revision is not None and entry.revision != revision:
            continue
        if execution is not None and entry.execution != execution:
            continue
        if task_index is not None and entry.task_index != task_index:
            continue
        if (
            text
            and text.casefold()
            not in (
                entry.message + json.dumps(entry.payload, ensure_ascii=False)
            ).casefold()
        ):
            continue
        matching.append(entry)
    matching.sort(key=lambda entry: (entry.timestamp, entry.id), reverse=True)
    response.entries = matching[:limit]
    response.truncated = response.truncated or len(matching) > limit
    return response
