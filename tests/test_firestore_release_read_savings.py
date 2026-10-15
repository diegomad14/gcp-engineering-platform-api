"""Reconciliation completeness and transferred-document accounting, offline."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eng_platform_api.services import firestore_queries, release_executions as store
from tests.test_firestore_history_read_savings import Collection, Snapshot


class ReleaseCollection(Collection):
    def document(self, identifier):
        def get(*, read_time=None):
            records = self.versions[read_time] if read_time else self.records
            row = records.get(identifier)
            return SimpleNamespace(
                exists=row is not None,
                to_dict=lambda: dict(row.record) if row else None,
            )

        return SimpleNamespace(get=get)


def execution(
    identifier, *, status="waiting_github", operation="main_release", **fields
):
    return {
        "execution_id": str(identifier),
        "repository": "example/repo",
        "head_sha": "a" * 40,
        "operation": operation,
        "status": status,
        "created_at": f"2026-01-01T00:{identifier:06d}",
        "updated_at": f"2026-01-01T00:{identifier:06d}",
        **fields,
    }


def planner(identifier, retry_count=0, **fields):
    return execution(
        identifier,
        status="failed",
        provider="cloud_build",
        release_engine_failed=True,
        engine_event_status="quality_passed",
        evidence_committed=True,
        report_hash="a" * 64,
        build_id="existing-attempt",
        planner_retry_count=retry_count,
        **fields,
    )


def test_due_exhausts_all_states_and_pages_without_reading_terminal_history(
    monkeypatch,
):
    history = [execution(i, status="released") for i in range(5000)]
    passed_pr = [
        execution(6000 + i, status="quality_passed", operation="pr_quality")
        for i in range(1200)
    ]
    statuses = [
        "received",
        "waiting_github",
        "submission_pending",
        "submitting",
        "running_quality",
        "release_planned",
        "publish_pending",
        "unknown",
        "submission_failed",  # Legacy/nonterminal, not a closed allowlist.
    ]
    active = [
        execution(8000 + i, status=statuses[i % len(statuses)]) for i in range(253)
    ]
    active.append(execution(9000, status="quality_passed"))
    retries = [
        planner(9001),
        planner(9002, 1, planner_retry_checked=True),
        planner(
            9003,
            2,
            planner_remediation_retry_checked=True,
            planner_remediation_retry_image="registry.example/planner@sha256:"
            + "b" * 64,
            planner_remediation_retry_hash="c" * 64,
        ),
    ]
    exhausted = planner(9004, 3)
    bootstrap = planner(9005, bootstrap_ticket_id=store.QUALITY_BOOTSTRAP_TICKET_ID)
    collection = ReleaseCollection(
        [*history, *passed_pr, *active[::-1], *retries, exhausted, bootstrap]
    )
    monkeypatch.setattr(store, "_collection", lambda: collection)

    rows = list(store.iter_due(page_size=17))

    expected = sorted(
        [*active, *retries], key=lambda row: (row["updated_at"], row["execution_id"])
    )
    assert rows == expected
    assert len(rows) == 257  # More than the scheduler's old 100-item work cap.
    assert collection.delivered == 259  # 6,459 before; two filtered retry candidates.
    assert collection.aggregations == 3
    assert len(set(collection.read_times)) == 1


@pytest.mark.parametrize("size", [0, 1, 100, 200, 201])
def test_due_page_boundaries_and_equal_update_timestamps(monkeypatch, size):
    records = [execution(i, updated_at="same") for i in range(size)]
    collection = ReleaseCollection(records[::-1])
    monkeypatch.setattr(store, "_collection", lambda: collection)
    rows = list(store.iter_due(page_size=20))
    assert [row["execution_id"] for row in rows] == sorted(str(i) for i in range(size))
    assert collection.delivered == size


def test_due_legacy_missing_status_is_recovered_explicitly(monkeypatch, caplog):
    legacy = execution(1)
    del legacy["status"]
    null_status = execution(2, status=None)
    collection = ReleaseCollection(
        [execution(0, status="released"), legacy, null_status]
    )
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert list(store.iter_due(page_size=1)) == [legacy, null_status]
    assert "legacy status compatibility scan" in caplog.text
    assert collection.delivered == 3
    assert len(set(collection.read_times)) == 1


@pytest.mark.parametrize("operation", [None, "legacy", "missing"])
def test_passed_legacy_operation_is_due_unless_it_is_pr_quality(
    monkeypatch, caplog, operation
):
    legacy = execution(1, status="quality_passed", operation=operation)
    if operation == "missing":
        del legacy["operation"]
    collection = ReleaseCollection(
        [legacy, execution(2, status="quality_passed", operation="pr_quality")]
    )
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert list(store.iter_due()) == [legacy]
    assert "legacy operation compatibility query" in caplog.text


def test_known_bootstrap_fence_does_not_trigger_archive_scan(monkeypatch, caplog):
    collection = ReleaseCollection(
        [execution(i, status="released") for i in range(5000)]
    )
    collection.records[store._BOOTSTRAP_DOCUMENT_ID] = Snapshot(
        store._BOOTSTRAP_DOCUMENT_ID,
        {"ticket_id": store.QUALITY_BOOTSTRAP_TICKET_ID, "execution_id": "consumed"},
    )
    collection.records["active"] = Snapshot("active", execution(8000))
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert [row["execution_id"] for row in store.iter_due()] == ["8000"]
    assert collection.delivered == 1
    assert "compatibility scan" not in caplog.text


def test_due_snapshot_is_shared_across_queries_and_concurrent_pages(monkeypatch):
    original = [execution(i) for i in range(205)]
    collection = ReleaseCollection(original)

    def mutate():
        collection.records.clear()
        collection.records["new"] = Snapshot(
            "new", execution(999, status="quality_passed")
        )

    collection.after_stream = mutate
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert list(store.iter_due(page_size=7)) == original
    assert len(set(collection.read_times)) == 1
    assert [row["execution_id"] for row in store.iter_due()] == ["999"]


def test_repository_history_merges_aliases_before_global_limit(monkeypatch):
    old, new = "diegomad14/cgm-sanplat-api", "diegomad14/cgm-artemis-api"
    records = [execution(i, repository=old if i % 2 else new) for i in range(3000)]
    collection = ReleaseCollection(records)
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert store.list_for_repository(new, limit=3) == records[-3:][::-1]
    assert collection.delivered == 6


def test_repository_history_legacy_timestamp_retains_global_order(monkeypatch, caplog):
    records = [execution(i) for i in range(300)]
    del records[0]["created_at"]
    collection = ReleaseCollection(records[::-1])
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert store.list_for_repository("example/repo", limit=2) == records[-2:][::-1]
    assert collection.delivered == 300
    assert "legacy timestamp compatibility scan" in caplog.text


def test_repository_bootstrap_fence_does_not_consume_top_k(monkeypatch, caplog):
    records = [execution(i) for i in range(500)]
    collection = ReleaseCollection(records)
    collection.records[store._BOOTSTRAP_DOCUMENT_ID] = Snapshot(
        store._BOOTSTRAP_DOCUMENT_ID,
        {"ticket_id": store.QUALITY_BOOTSTRAP_TICKET_ID, "repository": "example/repo"},
    )
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert store.list_for_repository("example/repo", limit=1) == [records[-1]]
    assert collection.delivered == 1
    assert "compatibility scan" not in caplog.text


def test_find_exact_identity_is_not_hidden_behind_100_newer_executions(monkeypatch):
    records = [execution(i, head_sha="b" * 40) for i in range(1000)]
    wanted = execution(0, head_sha="a" * 40)
    collection = ReleaseCollection([wanted, *records[1:]])
    monkeypatch.setattr(store, "_collection", lambda: collection)
    assert store.find("example/repo", "A" * 40, "main_release") == wanted
    assert collection.delivered == 1


def test_query_and_index_errors_never_start_a_scan_fallback(monkeypatch):
    collection = ReleaseCollection([execution(1)])
    collection.fail_stream = True
    monkeypatch.setattr(store, "_collection", lambda: collection)
    with pytest.raises(RuntimeError, match="index unavailable"):
        list(store.iter_due())
    assert collection.delivered == 0


def test_snapshot_count_requires_server_time_and_never_invents_clock():
    query = SimpleNamespace(
        count=lambda **_: SimpleNamespace(get=lambda: [[SimpleNamespace(value=1)]])
    )
    assert firestore_queries.count_query(query) == 1
    with pytest.raises(RuntimeError, match="consistent read timestamp"):
        firestore_queries.count_query_at(query)


def test_paging_rejects_invalid_width_before_any_provider_call():
    with pytest.raises(ValueError, match="positive"):
        list(firestore_queries.iter_query_pages(None, page_size=0))


def test_document_cursor_includes_inequality_and_identity_in_real_sdk():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore

    client = firestore.Client(
        project="offline-test", credentials=AnonymousCredentials()
    )
    reference = client.collection("offline").document("tie")
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = firestore.DocumentSnapshot(
        reference, {"status": "unknown"}, True, at, at, at
    )
    query = client.collection("offline").where("status", "not-in", ["released"])
    request = query.start_after(snapshot)._to_protobuf()
    assert [order.field.field_path for order in request.order_by] == [
        "status",
        "__name__",
    ]
    assert request.start_at.before is False  # Strictly after, not inclusive.


def test_scheduler_processes_every_release_after_old_cap(monkeypatch):
    from eng_platform_api.routers import release_operations as router

    records = [execution(i) for i in range(257)]
    observed = []
    monkeypatch.setattr(router, "_verify_google", lambda *_, **__: {})
    monkeypatch.setattr(
        router.cloud_build_usage, "reconcile", lambda: {"skipped": True}
    )
    monkeypatch.setattr(
        router.release_executions, "iter_due", lambda: iter(deepcopy(records))
    )
    monkeypatch.setattr(
        router.deployment_commands, "reconcile_stalled_dispatches", lambda: {}
    )

    def reconcile(identifier):
        observed.append(identifier)
        return {"status": "unknown"}

    monkeypatch.setattr(router.release_reconciler, "reconcile", reconcile)
    result = router.reconcile_due("Bearer offline")
    assert result["reconciled"] == 257
    assert observed == [record["execution_id"] for record in records]
