"""Offline quota-project FIFO, conservative rolling-window and real-process CAS tests.

The process tests use independent interpreters and shared SQLite CAS records;
no copied-by-fork mock is treated as distributed coordination evidence.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys

import pytest
from google.api_core.exceptions import DeadlineExceeded

from eng_platform_api.config import config
from eng_platform_api.services import log_budget as budget
from tests.log_test_support import FakeClock, FirestoreDouble, SharedFirestoreDouble


QUOTA_PROJECT = "quota-project"


def participant(name="reader"):
    return hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def store(monkeypatch):
    instance = FirestoreDouble()
    monkeypatch.setattr(config.logs, "quota_project_id", QUOTA_PROJECT)
    monkeypatch.setattr(config.logs, "budget_project_id", "example-project")
    monkeypatch.setattr(config.logs, "budget_collection", "eng_platform_log_budget")
    monkeypatch.setattr(budget, "firestore_client", lambda _: instance)
    return instance


def record(store):
    return store.records[budget.document_id()]


def take(name="reader", **kwargs):
    return budget.reserve(participant(name), **kwargs)


def finish(permit, name="reader", **kwargs):
    budget.finish(participant(name), permit.token, **kwargs)


def test_parallel_replicas_share_one_atomic_quota_project(store):
    with ThreadPoolExecutor(max_workers=20) as pool:
        permits = list(pool.map(lambda _: take(), range(50)))
    assert sum(p.allowed for p in permits) == 1
    assert all(5 <= p.next_poll_seconds <= 60 for p in permits)
    assert len(record(store)["attempts"]) == 1
    assert len(record(store)["waiters"]) == 0
    assert not take("another-source-project").allowed
    assert list(store.records) == [budget.document_id(QUOTA_PROJECT)]


def test_five_hundred_source_projects_cannot_multiply_budget(store):
    initial = take("initial")
    finish(initial, "initial")
    for source in range(500):
        queued = take(f"replica/source-project-{source}/service")
        assert not queued.allowed
        assert queued.queue_position == source + 1
    assert len(store.records) == 1
    assert len(record(store)["waiters"]) == 500
    assert record(store)["quota_project_id"] == QUOTA_PROJECT
    assert "source-project" not in repr(record(store))
    for source in range(11):
        store.now += timedelta(seconds=5)
        name = f"replica/source-project-{source}/service"
        permit = take(name)
        assert permit.allowed
        finish(permit, name)
    store.now += timedelta(seconds=4)
    denied = take("replica/source-project-11/service")
    assert not denied.allowed
    assert len(record(store)["attempts"]) == 12


@pytest.mark.parametrize("readers", [1, 10, 100])
def test_readers_coalesce_one_fifo_participant(store, readers):
    first = take("holder")
    finish(first, "holder")
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: take(), range(readers)))
    assert all(not result.allowed and result.queue_position == 1 for result in results)
    assert len(record(store)["waiters"]) == 1
    store.now += timedelta(seconds=5)
    assert take().allowed
    assert not take().allowed  # No replay of an existing token.
    assert len(record(store)["attempts"]) == 2


def test_fifo_repeated_polling_cannot_cut_ahead_or_run_another_owner(store):
    first = take("holder")
    finish(first, "holder")
    for i, name in enumerate(("a", "b", "c"), 1):
        queued = take(name)
        assert queued.queue_position == i
        assert queued.queue_wait_seconds == i * 5
    before = deepcopy(record(store)["waiters"])
    store.now += timedelta(seconds=5)
    assert take("c").queue_position == 3
    assert take("b").queue_position == 2
    assert [w["participant"] for w in record(store)["waiters"]] == [
        w["participant"] for w in before
    ]
    assert len(record(store)["attempts"]) == 1  # No borrowed work for queue head.
    for name in ("a", "b", "c"):
        permit = take(name)
        assert permit.allowed
        finish(permit, name)
        store.now += timedelta(seconds=5)
    assert record(store)["waiters"] == []


def test_waiters_expire_on_inactivity_and_polls_refresh_without_reordering(store):
    holder = take("holder")
    take("abandoned")
    take("alive")
    store.now += timedelta(seconds=budget.WAITER_TTL_SECONDS)
    alive = take("alive")
    assert not alive.allowed and alive.queue_position == 2
    store.now += timedelta(microseconds=1)
    assert take("alive").allowed  # Full advice + grace is inclusive.
    assert any(item["token"] == holder.token for item in record(store)["attempts"])
    assert not take("holder").allowed  # Pending slot never expires with waiter TTL.


def test_expired_waiter_rejoins_at_tail(store):
    holder = take("holder")
    finish(holder, "holder")
    take("abandoned")
    take("alive")
    store.now += timedelta(seconds=budget.WAITER_TTL_SECONDS)
    take("alive")
    store.now += timedelta(microseconds=1)
    assert take("abandoned").queue_position == 2
    assert take("alive").allowed


def test_abandoned_navigation_unblocks_empty_budget_within_liveness_bound(store):
    start = store.now
    permits = []
    for i in range(budget.MAX_ATTEMPTS):
        store.now = start + timedelta(seconds=i * budget.CADENCE_SECONDS)
        permits.append(take(f"old-{i}"))
    store.now = start + timedelta(seconds=60)
    for i, permit in enumerate(permits):
        finish(permit, f"old-{i}")
    store.now = start + timedelta(seconds=119)
    for name in ("closed-tab", "previous-navigation"):
        assert take(name).deferral_reason == "budget"

    # All completed charges disappear at t=120, but abandoned FIFO owners must
    # still get their bounded grace rather than allowing callers to jump ahead.
    for second in range(120, 155, 5):
        store.now = start + timedelta(seconds=second)
        result = take("current-navigation")
        assert result.deferral_reason == "queue"
        assert not result.allowed and result.queue_position == 3
        assert 5 <= result.next_poll_seconds <= 10
        assert record(store)["attempts"] == []
    store.now = start + timedelta(seconds=155)
    result = take("current-navigation")
    assert result.allowed and result.deferral_reason is None
    assert record(store)["waiters"] == []
    assert len(record(store)["attempts"]) == 1


def test_live_slow_waiter_keeps_turn_through_full_advice_and_transport_grace(store):
    held = take("holder")
    take("ahead")
    slow = take("slow-tab")
    assert slow.next_poll_seconds == budget.MAX_QUEUE_POLL_SECONDS == 10
    assert take("other-replica").queue_position == 3
    start = store.now
    store.now = start + timedelta(seconds=5)
    ahead = take("ahead")
    assert ahead.allowed
    finish(ahead, "ahead")
    # Later participants poll aggressively, including exactly at the last
    # instant covered by 10s advice + 25s request/transport allowance.
    for second in range(10, 36, 5):
        store.now = start + timedelta(seconds=second)
        result = take("other-replica")
        assert not result.allowed and result.queue_position == 2
    resumed = take("slow-tab")
    assert resumed.allowed
    assert record(store)["attempts"][0]["token"] == held.token
    assert record(store)["attempts"][0]["completed_at"] is None
    store.now += timedelta(seconds=5)
    assert take("other-replica").allowed


def test_schema_three_mixed_poll_policies_requeue_only_waiting_membership(
    store, monkeypatch
):
    permits = []
    for i in range(budget.MAX_ATTEMPTS):
        permits.append(take(f"owner-{i}"))
        store.now += timedelta(seconds=5)
    start = store.now
    original_attempts = deepcopy(record(store)["attempts"])
    # Previous schema-3 replicas used the same document/permit shape, a 180s
    # waiter TTL and 60s maximum advice. Emulate their polling policy at rollout.
    with monkeypatch.context() as old:
        old.setattr(budget, "MAX_QUEUE_POLL_SECONDS", 60)
        old.setattr(budget, "WAITER_TTL_SECONDS", 180)
        legacy = take("old-replica-waiter")
        assert legacy.next_poll_seconds == 60
    store.now = start + timedelta(seconds=36)
    assert take("new-replica-waiter").queue_position == 1
    store.now = start + timedelta(seconds=60)
    with monkeypatch.context() as old:
        old.setattr(budget, "MAX_QUEUE_POLL_SECONDS", 60)
        old.setattr(budget, "WAITER_TTL_SECONDS", 180)
        rejoined = take("old-replica-waiter")
        assert rejoined.queue_position == 2 and not rejoined.allowed
        assert record(store)["attempts"] == original_attempts
        finish(permits[0], "owner-0")
    current = record(store)
    assert current["schema_version"] == 3
    assert set(current) == {
        "schema_version",
        "quota_project_id",
        "attempts",
        "waiters",
        "next_due",
        "updated_at",
    }
    assert all(
        set(waiter) == {"participant", "enqueued_at", "last_seen_at"}
        for waiter in current["waiters"]
    )
    assert current["attempts"][0]["completed_at"] == store.now
    assert current["attempts"][1:] == original_attempts[1:]
    assert take("new-replica-waiter").deferral_reason == "budget"
    # No changed waiter policy creates capacity or authorizes a pending owner.
    assert take("owner-1").deferral_reason == "pending"
    assert len(record(store)["attempts"]) == budget.MAX_ATTEMPTS


def test_deferral_reasons_separate_cadence_queue_and_pending_from_budget(store):
    held = take("holder")
    assert held.allowed and held.deferral_reason is None
    assert take("holder").deferral_reason == "pending"
    assert take("head").deferral_reason == "cadence"
    assert take("tail").deferral_reason == "queue"
    assert len(record(store)["attempts"]) == 1


def test_finite_fifo_overload_does_not_evict_waiters_or_pending_slots(store):
    held = take("holder")
    now = store.now
    valid = record(store)
    valid["waiters"] = [
        {"participant": participant(str(i)), "enqueued_at": now, "last_seen_at": now}
        for i in range(budget.MAX_WAITERS)
    ]
    original = deepcopy(valid)
    rejected = take("overflow")
    assert rejected.overloaded and not rejected.allowed
    assert rejected.deferral_reason == "overload"
    assert rejected.queue_position is None and rejected.queue_wait_seconds is None
    assert rejected.next_poll_seconds == 60
    assert record(store) == original
    assert take("0").queue_position == 1
    assert record(store)["attempts"][0]["token"] == held.token
    store.now += timedelta(seconds=180)
    assert take("overflow").allowed
    assert len(record(store)["attempts"]) == 2


def test_pending_attempts_never_expire_on_restart(store):
    for i in range(12):
        assert take(f"replica-{i}").allowed
        store.now += timedelta(seconds=5)
    for delta in (1, 60, 3600, 86400):
        store.now += timedelta(seconds=delta)
        result = take("replacement-replica")
        assert not result.allowed and result.queue_position == 1
        assert result.deferral_reason == "budget"
        assert result.next_poll_seconds == 10
        assert result.queue_wait_seconds >= 60  # Lower bound, no recovery promise.
    assert len(record(store)["attempts"]) == 12
    assert all(item["completed_at"] is None for item in record(store)["attempts"])


def test_delayed_requests_count_until_completion_plus_sixty_seconds(store):
    calls = []
    start = store.now
    first = take("delayed")
    for i in range(1, 12):
        store.now = start + timedelta(seconds=i * 5)
        name = f"replica-{i}"
        permit = take(name)
        assert permit.allowed
        calls.append(store.now)
        finish(permit, name)
    store.now = start + timedelta(seconds=59)
    calls.append(store.now)
    finish(first, "delayed")
    store.now = start + timedelta(seconds=60)
    assert not take("next").allowed
    store.now = start + timedelta(seconds=65)
    permit = take("next")
    assert permit.allowed
    calls.append(store.now)
    finish(permit, "next")
    for call in calls:
        assert (
            sum(call <= value < call + timedelta(seconds=60) for value in calls) <= 12
        )
    old = deepcopy(store.records)
    finish(permit, "next")
    assert store.records == old  # Repeated completion cannot refund or shift time.


def test_rolling_budget_boundary_and_failed_finish_retains_charge(store):
    start = store.now
    for i in range(12):
        store.now = start + timedelta(seconds=i * 5)
        name = f"replica-{i}"
        permit = take(name)
        assert permit.allowed
        store.now += timedelta(seconds=1)
        finish(permit, name)
    store.now = start + timedelta(seconds=60)
    assert not take("next").allowed
    store.now = start + timedelta(seconds=61)
    accepted = take("next")
    assert accepted.allowed
    store.fail = True
    with pytest.raises(RuntimeError):
        finish(accepted, "next")
    assert any(
        item["token"] == accepted.token and item["completed_at"] is None
        for item in record(store)["attempts"]
    )


@pytest.mark.parametrize(
    "field", ["quota_project_id", "budget_collection", "budget_project_id"]
)
def test_missing_configuration_fails_closed_without_firestore(
    store, monkeypatch, field
):
    monkeypatch.setattr(config.logs, field, "")
    with pytest.raises(RuntimeError):
        take()
    assert store.reads == store.writes == 0


@pytest.mark.parametrize(
    "identity", ["", "logs-project", "a" * 63, "a" * 65, "A" * 64, "/" * 64, None]
)
def test_participant_must_be_opaque_sha256(store, identity):
    with pytest.raises(ValueError):
        budget.reserve(identity)
    with pytest.raises(ValueError):
        budget.finish(identity, "a" * 32)
    assert store.reads == store.writes == 0


def test_document_key_is_stable_and_quota_project_specific(store):
    assert (
        budget.document_id()
        == "quota-" + hashlib.sha256(QUOTA_PROJECT.encode()).hexdigest()
    )
    assert budget.document_id("another-quota") != budget.document_id()
    with pytest.raises(ValueError):
        budget.document_id("a/b")


@pytest.mark.parametrize(
    "legacy",
    [
        {},
        {"schema_version": 2, "attempts": []},
        {"attempts": [{"token": "a" * 32, "completed_at": None}]},
    ],
)
def test_existing_legacy_metadata_is_never_silently_initialized(store, legacy):
    store.records[budget.document_id()] = deepcopy(legacy)
    store.versions[budget.document_id()] = 1
    with pytest.raises(RuntimeError, match="Incompatible"):
        take()
    assert record(store) == legacy
    assert store.writes == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(schema_version=4),
        lambda r: r.update(schema_version=3.0),
        lambda r: r.update(quota_project_id="other-quota"),
        lambda r: r.update(unknown="metadata"),
        lambda r: r.update(attempts="bad"),
        lambda r: r.update(attempts=r["attempts"] * 13),
        lambda r: r.update(attempts=[42]),
        lambda r: r["attempts"][0].update(token="short"),
        lambda r: r["attempts"][0].update(participant="source-project"),
        lambda r: r["attempts"][0].update(completed_at="bad"),
        lambda r: r.update(attempts=r["attempts"] * 2),
        lambda r: r["attempts"].append({**r["attempts"][0], "token": "b" * 32}),
        lambda r: r["attempts"][0].update(
            completed_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
        ),
        lambda r: r["attempts"][0].update(
            completed_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
        ),
        lambda r: r["attempts"][0].update(reserved_at=datetime(2020, 1, 1)),
        lambda r: r.update(next_due="bad"),
        lambda r: r.update(updated_at=None),
        lambda r: r.update(waiters="bad"),
        lambda r: r.update(waiters=[{}] * (budget.MAX_WAITERS + 1)),
        lambda r: r.update(waiters=[42]),
        lambda r: r.update(
            waiters=[
                {
                    "participant": "bad",
                    "enqueued_at": r["updated_at"],
                    "last_seen_at": r["updated_at"],
                }
            ]
        ),
        lambda r: r.update(
            waiters=[
                {
                    "participant": r["attempts"][0]["participant"],
                    "enqueued_at": r["updated_at"],
                    "last_seen_at": r["updated_at"],
                }
            ]
        ),
    ],
)
def test_corrupt_metadata_fails_closed_without_discarding_permits(store, mutation):
    take()
    mutation(record(store))
    original = deepcopy(record(store))
    writes = store.writes
    with pytest.raises(RuntimeError):
        take("other")
    assert store.writes == writes
    assert record(store) == original


def test_missing_authoritative_time_and_wrong_completion_owner(store):
    store.now = None
    with pytest.raises(RuntimeError):
        take()
    store.now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    held = take()
    with pytest.raises(RuntimeError, match="missing"):
        finish(held, "wrong-owner")
    with pytest.raises(RuntimeError, match="missing"):
        budget.finish(participant(), "a" * 32)
    with pytest.raises(ValueError):
        budget.finish(participant(), "invalid")
    assert record(store)["attempts"][0]["completed_at"] is None


def test_cas_conflicts_are_bounded_and_ambiguous_commit_is_not_retried(store):
    held = take()
    store.conflicts = 3
    reads = store.reads
    with pytest.raises(RuntimeError, match="contended"):
        finish(held)
    assert store.reads - reads == 3
    assert record(store)["attempts"][0]["completed_at"] is None
    store.ambiguous = True
    reads = store.reads
    with pytest.raises(DeadlineExceeded):
        finish(held)
    assert store.reads - reads == 1


def test_ambiguous_reservation_ack_is_never_retried_or_replayed(store):
    store.ambiguous = True
    with pytest.raises(DeadlineExceeded):
        take()
    assert store.reads == 1 and store.writes == 1
    store.ambiguous = False
    original = deepcopy(record(store))
    assert not take().allowed
    assert record(store) == original
    assert record(store)["attempts"][0]["completed_at"] is None


def test_cas_retries_definite_conflict_and_requires_version(store):
    held = take()
    store.conflicts = 1
    finish(held)
    store.now += timedelta(seconds=5)
    store.versions[budget.document_id()] = None
    with pytest.raises(RuntimeError, match="version"):
        take()


def test_deadline_phases_share_total_and_cancellation_but_completion_never_extends():
    clock = FakeClock()
    total = budget.Deadline.after(20, clock=clock)
    clock.advance(3)
    phase = total.phase(10, reserve_seconds=4)
    assert phase.remaining() == 10
    clock.advance(9)
    assert phase.timeout(2) == 1
    total.cancelled.set()
    with pytest.raises(TimeoutError):
        phase.remaining()
    finish_deadline = total.completion(4)
    assert finish_deadline.remaining() == 4
    clock.advance(7)
    assert total.completion(4).remaining() == 1
    clock.advance(1)
    with pytest.raises(TimeoutError):
        total.completion(4).remaining()


@pytest.mark.parametrize("operation", ["reserve", "finish"])
def test_conflict_rpc_timeouts_shrink_within_single_phase(store, operation):
    held = take()
    store.now += timedelta(seconds=5)
    clock = FakeClock()
    store.clock = clock
    store.latency = 1.1
    store.conflicts = 3
    store.timeouts.clear()
    deadline = budget.Deadline.after(4, clock=clock)
    with pytest.raises(DeadlineExceeded):
        if operation == "reserve":
            take("other", deadline=deadline)
        else:
            finish(held, deadline=deadline)
    assert clock.now == pytest.approx(104)
    assert store.timeouts == pytest.approx([2, 2, 1.8, 0.7])
    assert len(record(store)["attempts"]) == 1
    assert record(store)["attempts"][0]["completed_at"] is None


def test_cancelled_deadline_does_not_touch_firestore(store):
    clock = FakeClock()
    deadline = budget.Deadline.after(1, clock=clock)
    deadline.cancelled.set()
    with pytest.raises(TimeoutError):
        take(deadline=deadline)
    with pytest.raises(TimeoutError):
        budget.finish(participant(), "a" * 32, deadline=deadline)
    assert store.reads == store.writes == 0


def test_late_confirmed_reservation_is_not_dispatched_or_reclaimed(store):
    clock = FakeClock()
    store.clock = clock
    store.latency = 2
    with pytest.raises(TimeoutError):
        take(deadline=budget.Deadline.after(4, clock=clock))
    assert len(record(store)["attempts"]) == 1
    assert record(store)["attempts"][0]["completed_at"] is None
    assert clock.now == 104


def test_slow_coordination_setup_prevents_late_firestore_rpc(store, monkeypatch):
    clock = FakeClock()

    def initialize(_):
        clock.advance(5)
        return store

    monkeypatch.setattr(budget, "firestore_client", initialize)
    with pytest.raises(TimeoutError):
        take(deadline=budget.Deadline.after(4, clock=clock))
    assert store.reads == store.writes == 0


def _process_reserve(path, process_id, readers, processes):
    # Fresh interpreters share only the local SQLite store, never Python dicts.
    shared_store = SharedFirestoreDouble(path, processes=processes)
    config.logs.quota_project_id = QUOTA_PROJECT
    config.logs.budget_project_id = "example-project"
    config.logs.budget_collection = "eng_platform_log_budget"
    budget.firestore_client = lambda _: shared_store
    outcomes = []
    for _ in range(readers):
        try:
            result = take(
                f"process-{process_id}/resource", deadline=budget.Deadline.after(30)
            )
            outcomes.append((result.allowed, result.token, result.queue_position))
        except RuntimeError as exc:
            assert str(exc) == "Log budget is contended"
            outcomes.append((False, "", None))
    return outcomes


@pytest.mark.parametrize("processes", [1, 8])
@pytest.mark.parametrize("readers", [1, 10, 100])
def test_real_processes_coordinate_one_shared_cas_document(
    tmp_path, processes, readers
):
    path = tmp_path / "shared-cas.sqlite"
    shared = SharedFirestoreDouble(path, initialize=True, processes=processes)
    command = (
        "import json,sys; from tests.test_log_budget import _process_reserve; "
        "print(json.dumps(_process_reserve(sys.argv[1], int(sys.argv[2]), "
        "int(sys.argv[3]), int(sys.argv[4]))))"
    )
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                command,
                str(path),
                str(i),
                str(readers),
                str(processes),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(processes)
    ]
    results = []
    try:
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=90)
            assert worker.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    granted = [result for group in results for result in group if result[0]]
    assert len(granted) == 1
    key = budget.document_id(QUOTA_PROJECT)
    assert list(shared.records) == [key]
    current = shared.records[key]
    assert len(current["attempts"]) == 1
    assert current["attempts"][0]["token"] == granted[0][1]
    owners = [waiter["participant"] for waiter in current["waiters"]]
    assert len(set(owners)) == len(owners) <= processes - 1
    assert shared.metric("writes") >= 1
    if processes == 8:
        assert shared.metric("conflicts") >= 7  # Forced shared-state create race.
    if readers > 1:
        assert len(owners) == processes - 1


def runtime_pipeline(monkeypatch):
    from eng_platform_api.services import runtime_logs as logs

    monkeypatch.setattr(logs, "_caches", {})
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.logs, "enabled", True)
    resource = logs.Resource(
        "example-api",
        "logs-project",
        "us-central1",
        "cloud_run_service",
        enabled=True,
        allowed_logins=("reader",),
    )
    monkeypatch.setattr(logs, "resources", lambda: [resource])
    return logs, resource


def test_runtime_pipeline_reserve_rpc_finish_share_one_clock(store, monkeypatch):
    store.now = datetime.now(timezone.utc)
    logs, resource = runtime_pipeline(monkeypatch)
    clock = FakeClock()
    store.clock = clock
    store.latency = 1.5
    received = []

    def fetch(*args, deadline, scope, authorize):
        received.append(deadline.remaining())
        clock.advance(9)
        return [], False

    monkeypatch.setattr(logs, "fetch_page", fetch)
    result = logs.read_logs(
        resource, [resource], deadline=budget.Deadline.after(20, clock=clock)
    )
    assert result.status == "fresh"
    assert received == [10]
    assert clock.now == 115  # 3 reserve + 9 RPC + 3 finalize, not per-attempt budgets.
    assert store.timeouts == [2, 2, 2, 2]
    assert (
        store.records[budget.document_id()]["attempts"][0]["completed_at"] == store.now
    )


def test_queue_delay_reduces_rpc_budget_and_preserves_finish_time(store, monkeypatch):
    logs, resource = runtime_pipeline(monkeypatch)
    clock = FakeClock()
    store.clock = clock
    store.latency = 1.5
    deadline = budget.Deadline.after(20, clock=clock)
    clock.advance(5)  # Waiting for a worker is part of the same request budget.
    received = []

    def fetch(*args, deadline, scope, authorize):
        received.append(deadline.remaining())
        clock.advance(deadline.remaining())
        raise DeadlineExceeded("synthetic RPC timeout")

    monkeypatch.setattr(logs, "fetch_page", fetch)
    result = logs.read_logs(resource, [resource], deadline=deadline)
    assert result.status == "unavailable"
    assert received == [8]  # 20 - 5 queue - 3 reserve - 4 protected finalization.
    assert clock.now == 119
    assert (
        store.records[budget.document_id()]["attempts"][0]["completed_at"] == store.now
    )


def test_twelve_overrun_calls_keep_pending_slots_across_replica_restarts(
    store, monkeypatch
):
    logs, resource = runtime_pipeline(monkeypatch)
    clock = FakeClock()
    calls = []

    def blocked_rpc(*args, deadline, scope, authorize):
        calls.append(store.now)
        # Model an SDK callback ignoring its deadline. HTTP can stop waiting,
        # but the worker must not turn this into an expiring/free permit.
        clock.advance(21)
        return [], False

    monkeypatch.setattr(logs, "fetch_page", blocked_rpc)
    for _ in range(12):
        logs._caches.clear()  # Another replica or process restart.
        monkeypatch.setattr(logs, "_REPLICA_ID", f"replica-{len(calls)}")
        result = logs.read_logs(
            resource, [resource], deadline=budget.Deadline.after(20, clock=clock)
        )
        assert result.status == "unavailable"
        store.now += timedelta(seconds=5)
    for elapsed in (60, 3600, 86400):
        store.now += timedelta(seconds=elapsed)
        logs._caches.clear()
        result = logs.read_logs(
            resource, [resource], deadline=budget.Deadline.after(20, clock=clock)
        )
        assert result.status == "throttled"
    assert len(calls) == 12
    assert len(store.records[budget.document_id()]["attempts"]) == 12
    assert all(
        item["completed_at"] is None
        for item in store.records[budget.document_id()]["attempts"]
    )


def test_exceptional_interruption_never_finalizes_an_unknown_rpc(store, monkeypatch):
    logs, resource = runtime_pipeline(monkeypatch)

    class Interrupted(BaseException):
        pass

    def interrupted(*args, **kwargs):
        raise Interrupted()

    monkeypatch.setattr(logs, "fetch_page", interrupted)
    with pytest.raises(Interrupted):
        logs.read_logs(resource, [resource])
    assert store.records[budget.document_id()]["attempts"][0]["completed_at"] is None
    assert all(not cache.lock.locked() for cache in logs._caches.values())


def test_local_processing_stops_at_work_deadline_and_keeps_finish_allowance(
    store, monkeypatch
):
    logs, resource = runtime_pipeline(monkeypatch)
    clock = FakeClock()
    processed = []
    monkeypatch.setattr(
        logs, "fetch_page", lambda *args, **kwargs: ([{}, {}, {}], False)
    )

    def normalize(*args):
        processed.append(1)
        clock.advance(8)
        return None

    monkeypatch.setattr(logs, "normalize", normalize)
    result = logs.read_logs(
        resource, [resource], deadline=budget.Deadline.after(20, clock=clock)
    )
    assert result.status == "unavailable"
    assert processed == [1, 1]
    assert clock.now == 116
    assert (
        store.records[budget.document_id()]["attempts"][0]["completed_at"] == store.now
    )


@pytest.mark.parametrize("kind", ["duplicate", "reversed", "seen_before_join"])
def test_invalid_waiter_order_never_clears_permits(store, kind):
    held = take("holder")
    take("a")
    store.now += timedelta(seconds=1)
    take("b")
    queued = record(store)["waiters"]
    if kind == "duplicate":
        queued.append(deepcopy(queued[0]))
    elif kind == "reversed":
        queued.reverse()
    else:
        queued[0]["last_seen_at"] -= timedelta(seconds=1)
    original = deepcopy(record(store))
    with pytest.raises(RuntimeError):
        take("other")
    assert record(store) == original
    assert record(store)["attempts"][0]["token"] == held.token


def test_active_hundred_resource_fifo_advances_without_starvation(store):
    start = store.now
    due = {f"resource-{i}": 0 for i in range(100)}
    granted = []
    calls = []
    # Every active caller honors its own bounded polling advice. Long queues
    # therefore refresh within 10s and remain active beyond the waiter TTL.
    for second in range(0, 6001, 5):
        store.now = start + timedelta(seconds=second)
        for name, next_poll in list(due.items()):
            if next_poll > second:
                continue
            result = take(name)
            if result.allowed:
                granted.append(name)
                calls.append(store.now)
                finish(result, name)
                del due[name]
            else:
                assert result.queue_position is not None
                due[name] = second + result.next_poll_seconds
        if not due:
            break
    assert not due
    assert granted == [f"resource-{i}" for i in range(100)]
    for call in calls:
        assert (
            sum(call <= other < call + timedelta(seconds=60) for other in calls) <= 12
        )


def test_rolling_actual_dispatches_with_delays_failures_and_permanent_pending(store):
    start = store.now
    outstanding = []
    calls = []
    due = {f"resource-{i}": 0 for i in range(60)}
    granted = 0
    for second in range(0, 901):
        store.now = start + timedelta(seconds=second)
        for work in outstanding:
            if work["dispatch"] == second:
                calls.append(store.now)
            if work["finish"] == second:
                finish(work["permit"], work["owner"])
        for name, next_poll in list(due.items()):
            if next_poll > second:
                continue
            permit = take(name)
            if not permit.allowed:
                due[name] = second + permit.next_poll_seconds
                continue
            delay = (0, 1, 23, 59, 75)[granted % 5]
            dispatch = second + delay
            # A failure returned to the caller still completes/charges a slot;
            # every 13th request simulates an unknown outcome retained forever.
            completed = dispatch + 2 if granted % 13 else None
            outstanding.append(
                {
                    "permit": permit,
                    "owner": name,
                    "dispatch": dispatch,
                    "finish": completed,
                }
            )
            if dispatch == second:
                calls.append(store.now)
            del due[name]
            granted += 1
    assert granted >= 30
    assert any(item["completed_at"] is None for item in record(store)["attempts"])
    assert len(calls) >= 30
    for call in calls:
        assert (
            sum(call <= other < call + timedelta(seconds=60) for other in calls) <= 12
        )


def test_finish_uses_original_immutable_scope_after_configuration_changes(
    store, monkeypatch
):
    scope = budget.coordination_scope()
    first = budget.reserve(participant("owner"), scope=scope)
    old_document = budget.document_id(scope.quota_project_id)
    monkeypatch.setattr(config.logs, "quota_project_id", "changed-quota")
    monkeypatch.setattr(config.logs, "budget_project_id", "changed-database")
    monkeypatch.setattr(
        config.logs, "budget_collection", "eng_platform_log_budget_changed"
    )
    projects = []

    def original_only(project):
        projects.append(project)
        assert project == scope.budget_project_id
        return store

    monkeypatch.setattr(budget, "firestore_client", original_only)
    budget.finish(participant("owner"), first.token, scope=scope)
    assert projects == [scope.budget_project_id]
    assert list(store.records) == [old_document]
    assert store.records[old_document]["attempts"][0]["completed_at"] == store.now
    assert store.records[old_document]["quota_project_id"] == scope.quota_project_id
