"""Atomic quota-project-wide Cloud Logging permits and fair FIFO; metadata only.

Public Firestore compare-and-swap preconditions provide single-document atomic
updates, with explicitly bounded RPCs and no implicit retries. Pending permits
never expire. Completed permits remain charged for 60 seconds AFTER completion
using Firestore time. There is no refund or local coordination fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from threading import Event
from time import monotonic
from typing import Any, Callable
import uuid

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound

from ..config import config
from ..models import LogDeferralReason
from .deployment_store import firestore_client

CADENCE_SECONDS = 5
MAX_ATTEMPTS = 12
MAX_CAS_ATTEMPTS = 3
COORDINATION_TIMEOUT_SECONDS = 2
MAX_WAITERS = 4096
MAX_QUEUE_POLL_SECONDS = 10
WAITER_GRACE_SECONDS = 25
# Keep schema 3 readable by existing replicas: liveness changes only waiting
# membership, never the ownership or lifetime of a reserved permit. An old
# replica advising 60s may have to rejoin the FIFO during a rolling deployment.
WAITER_TTL_SECONDS = MAX_QUEUE_POLL_SECONDS + WAITER_GRACE_SECONDS
SCHEMA_VERSION = 3
_PARTICIPANT = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")


@dataclass(frozen=True)
class Deadline:
    """One monotonic budget shared by the request and its bounded suboperations.

    Cancellation is cooperative: only the worker which owns a synchronous call
    knows when it has returned. Cancelling an HTTP wait never finalizes a permit.
    """

    expires_at: float
    clock: Callable[[], float]
    cancelled: Event = field(default_factory=Event)

    @classmethod
    def after(cls, seconds: float, *, clock: Callable[[], float] | None = None):
        clock = clock or monotonic
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("Runtime log deadline expired")
        return remaining

    def timeout(self, maximum: float) -> float:
        return min(maximum, self.remaining())

    def phase(self, seconds: float, *, reserve_seconds: float = 0) -> Deadline:
        self.remaining()
        return Deadline(
            min(self.expires_at - reserve_seconds, self.clock() + seconds),
            self.clock,
            self.cancelled,
        )

    def completion(self, seconds: float) -> Deadline:
        # Called only by the synchronous owner after the RPC has returned (or
        # before it was started). Ignore wait cancellation, never the time limit.
        return Deadline(min(self.expires_at, self.clock() + seconds), self.clock)


@dataclass(frozen=True)
class Reservation:
    allowed: bool
    server_time: datetime
    next_poll_seconds: int = CADENCE_SECONDS
    token: str = ""
    queue_position: int | None = None
    queue_wait_seconds: int | None = None
    overloaded: bool = False
    deferral_reason: LogDeferralReason | None = None


def _quota_project() -> str:
    project = config.logs.quota_project_id
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        raise RuntimeError("Log quota project is not configured")
    return project


def document_id(quota_project_id: str | None = None) -> str:
    """Stable, global document key; source resource projects never select a key."""
    project = _quota_project() if quota_project_id is None else quota_project_id
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        raise ValueError("Invalid log quota project")
    return "quota-" + hashlib.sha256(project.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoordinationScope:
    quota_project_id: str
    budget_project_id: str
    budget_collection: str


def coordination_scope() -> CoordinationScope:
    """Capture immutable attribution and document coordinates for one operation."""
    settings = config.logs
    scope = CoordinationScope(
        _quota_project(), settings.budget_project_id, settings.budget_collection
    )
    _validate_scope(scope)
    return scope


def _validate_scope(scope: CoordinationScope) -> None:
    if (
        not _PROJECT.fullmatch(scope.quota_project_id)
        or not _PROJECT.fullmatch(scope.budget_project_id)
        or not re.fullmatch(
            r"eng_platform_log_budget(?:_[a-z0-9_]{1,32})?", scope.budget_collection
        )
    ):
        raise RuntimeError("Log coordination is not configured")


def _document(scope: CoordinationScope):
    _validate_scope(scope)
    client = firestore_client(scope.budget_project_id)
    return client, client.collection(scope.budget_collection).document(
        document_id(scope.quota_project_id)
    )


def _participant(value: str) -> None:
    if not isinstance(value, str) or not _PARTICIPANT.fullmatch(value):
        raise ValueError("Invalid log budget participant")


def _timestamp(value: Any, now: datetime | None = None) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RuntimeError("Invalid log budget timestamp")
    if now is not None and value > now:
        raise RuntimeError("Invalid log budget timestamp")
    return value.astimezone(timezone.utc)


def _record(snapshot, quota_project: str):
    # A missing document is the ONLY initialization path. Existing legacy,
    # partial or incompatible metadata must never reset outstanding permits.
    now = _timestamp(snapshot.read_time)
    record = snapshot.to_dict()
    if not snapshot.exists:
        return (
            now,
            {
                "schema_version": SCHEMA_VERSION,
                "quota_project_id": quota_project,
                "attempts": [],
                "waiters": [],
                "next_due": now,
                "updated_at": now,
            },
            [],
            [],
        )
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("quota_project_id") != quota_project
        or set(record)
        != {
            "schema_version",
            "quota_project_id",
            "attempts",
            "waiters",
            "next_due",
            "updated_at",
        }
    ):
        raise RuntimeError("Incompatible log budget record")
    _timestamp(record["next_due"])
    _timestamp(record["updated_at"], now)
    previous = record["attempts"]
    queued = record["waiters"]
    if (
        not isinstance(previous, list)
        or len(previous) > MAX_ATTEMPTS
        or not isinstance(queued, list)
        or len(queued) > MAX_WAITERS
    ):
        raise RuntimeError("Invalid log budget record")
    attempts = []
    seen_tokens = set()
    pending = set()
    for value in previous:
        if not isinstance(value, dict) or set(value) != {
            "token",
            "participant",
            "reserved_at",
            "completed_at",
        }:
            raise RuntimeError("Invalid log budget permit")
        token = value["token"]
        participant = value["participant"]
        if (
            not isinstance(token, str)
            or not _TOKEN.fullmatch(token)
            or token in seen_tokens
            or not isinstance(participant, str)
            or not _PARTICIPANT.fullmatch(participant)
        ):
            raise RuntimeError("Invalid log budget identity")
        seen_tokens.add(token)
        reserved = _timestamp(value["reserved_at"], now)
        completed = value["completed_at"]
        if completed is None:
            if participant in pending:
                raise RuntimeError("Duplicate pending log budget participant")
            pending.add(participant)
        else:
            completed = _timestamp(completed, now)
            if completed < reserved:
                raise RuntimeError("Invalid log budget completion")
            if completed <= now - timedelta(seconds=60):
                continue
        attempts.append(dict(value))
    waiters = []
    seen_waiters = set()
    previous_enqueued = None
    for value in queued:
        if not isinstance(value, dict) or set(value) != {
            "participant",
            "enqueued_at",
            "last_seen_at",
        }:
            raise RuntimeError("Invalid log budget waiter")
        participant = value["participant"]
        if (
            not isinstance(participant, str)
            or not _PARTICIPANT.fullmatch(participant)
            or participant in seen_waiters
            or participant in pending
        ):
            raise RuntimeError("Invalid log budget waiter identity")
        seen_waiters.add(participant)
        enqueued = _timestamp(value["enqueued_at"], now)
        last_seen = _timestamp(value["last_seen_at"], now)
        if enqueued > last_seen or (
            previous_enqueued is not None and enqueued < previous_enqueued
        ):
            raise RuntimeError("Invalid log budget waiter order")
        previous_enqueued = enqueued
        if last_seen >= now - timedelta(seconds=WAITER_TTL_SECONDS):
            waiters.append(dict(value))
    return now, record, attempts, waiters


def _compare_and_swap(
    transform: Callable, deadline: Deadline, scope: CoordinationScope | None = None
) -> Any:
    deadline.remaining()
    scope = scope or coordination_scope()
    quota_project = scope.quota_project_id
    client, document = _document(scope)
    for _ in range(MAX_CAS_ATTEMPTS):
        snapshot = document.get(
            retry=None, timeout=deadline.timeout(COORDINATION_TIMEOUT_SECONDS)
        )
        deadline.remaining()
        now, record, attempts, waiters = _record(snapshot, quota_project)
        result, replacement = transform(now, record, attempts, waiters)
        deadline.remaining()
        if replacement is None:
            return result
        try:
            if snapshot.exists:
                # A concurrent winner invalidates this exact snapshot version.
                # Missing metadata is not permission for an unconditional write.
                if snapshot.update_time is None:
                    raise RuntimeError("Log budget version is unavailable")
                document.update(
                    replacement,
                    option=client.write_option(last_update_time=snapshot.update_time),
                    retry=None,
                    timeout=deadline.timeout(COORDINATION_TIMEOUT_SECONDS),
                )
            else:
                document.create(
                    replacement,
                    retry=None,
                    timeout=deadline.timeout(COORDINATION_TIMEOUT_SECONDS),
                )
            deadline.remaining()
            return result
        except (AlreadyExists, FailedPrecondition, NotFound):
            # Only definite conflicts retry. An unknown commit result may have
            # persisted a permit: never repeat an ambiguous write or refund it.
            continue
    raise RuntimeError("Log budget is contended")


def _wait_seconds(now, record, attempts, position: int) -> int:
    due = record["next_due"]
    if len(attempts) >= MAX_ATTEMPTS:
        # Pending slots cannot become free sooner than completion (at least
        # now) plus 60s. They may NEVER complete, so this is only a lower bound.
        available = min(
            (item["completed_at"] or now) + timedelta(seconds=60) for item in attempts
        )
        due = max(due, available)
    return (
        max(0, math.ceil((due - now).total_seconds()))
        + (position - 1) * CADENCE_SECONDS
    )


def reserve(
    participant: str,
    *,
    deadline: Deadline | None = None,
    scope: CoordinationScope | None = None,
) -> Reservation:
    """Join/refresh FIFO or grant this caller's turn; never dispatch for others.

    A participant is an opaque SHA-256 identity of process incarnation + exact
    resource. Polling coalesces that identity, without moving its queue position.
    Queue waiting is finite and expires; pending permits do not expire, ever.
    """
    _participant(participant)
    deadline = deadline or Deadline.after(config.logs.reserve_timeout_seconds)
    token = uuid.uuid4().hex

    def acquire(now, record, attempts, waiters):
        if any(
            item["participant"] == participant and item["completed_at"] is None
            for item in attempts
        ):
            # Replaying a successful/ambiguous reservation cannot authorize a
            # second outbound call or mint a second pending slot for this owner.
            return Reservation(False, now, 60, deferral_reason="pending"), None
        index = next(
            (i for i, item in enumerate(waiters) if item["participant"] == participant),
            None,
        )
        if index is None:
            if len(waiters) >= MAX_WAITERS:
                return Reservation(
                    False, now, 60, overloaded=True, deferral_reason="overload"
                ), None
            index = len(waiters)
            waiters.append(
                {
                    "participant": participant,
                    "enqueued_at": now,
                    "last_seen_at": now,
                }
            )
        else:
            waiters[index]["last_seen_at"] = now
        replacement = {
            **record,
            "attempts": attempts,
            "waiters": waiters,
            "updated_at": now,
        }
        if index == 0 and now >= record["next_due"] and len(attempts) < MAX_ATTEMPTS:
            replacement["waiters"] = waiters[1:]
            replacement["attempts"] = [
                *attempts,
                {
                    "participant": participant,
                    "token": token,
                    "reserved_at": now,
                    "completed_at": None,
                },
            ]
            replacement["next_due"] = now + timedelta(seconds=CADENCE_SECONDS)
            return Reservation(True, now, token=token), replacement
        wait = _wait_seconds(now, record, attempts, index + 1)
        result = Reservation(
            False,
            now,
            max(CADENCE_SECONDS, min(MAX_QUEUE_POLL_SECONDS, wait)),
            queue_position=index + 1,
            queue_wait_seconds=wait,
            deferral_reason=(
                "budget"
                if len(attempts) >= MAX_ATTEMPTS
                else "queue"
                if index > 0
                else "cadence"
            ),
        )
        return result, replacement if replacement != record else None

    return _compare_and_swap(acquire, deadline, scope)


def finish(
    participant: str,
    token: str,
    *,
    deadline: Deadline | None = None,
    scope: CoordinationScope | None = None,
) -> None:
    """Charge this owner's permit until 60s after known completion; never refund."""
    _participant(participant)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("Invalid log budget token")
    deadline = deadline or Deadline.after(config.logs.finish_timeout_seconds)

    def complete(now, record, attempts, waiters):
        permit = next((item for item in attempts if item["token"] == token), None)
        if permit is None or permit["participant"] != participant:
            raise RuntimeError("Log reservation is missing")
        if permit["completed_at"] is not None:
            return None, None
        permit["completed_at"] = now
        return None, {
            **record,
            "attempts": attempts,
            "waiters": waiters,
            "updated_at": now,
        }

    _compare_and_swap(complete, deadline, scope)
