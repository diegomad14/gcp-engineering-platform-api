"""Provider-shaped read accounting for history and reconciliation queries."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from eng_platform_api.models import DeploymentItem
from eng_platform_api.services import deployment_store, releases_store


def deployment(identifier, *, service="service-a", status="SUCCEEDED", created=None):
    return DeploymentItem(
        id=str(identifier),
        service_name=service,
        repository="example/repo",
        tag="v1.0.0",
        status=status,
        created_at=created or f"2026-01-01T00:{identifier:06d}",
    ).model_dump()


@dataclass(frozen=True)
class Snapshot:
    id: str
    record: dict
    read_time: datetime | None = None

    def to_dict(self):
        return dict(self.record)


class Collection:
    def __init__(self, records):
        self.records = {
            str(record.get("execution_id", index)): Snapshot(
                str(record.get("execution_id", index)), record
            )
            for index, record in enumerate(records)
        }
        self.delivered = 0
        self.aggregations = 0
        self.streams = 0
        self.filters = []
        self.limits = []
        self.fail_stream = False
        self.versions = {}
        self.read_times = []
        self.after_aggregation = None
        self.after_stream = None

    def capture(self):
        # A provider timestamp, independent of any wall-clock value in the reader.
        read_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=len(self.versions)
        )
        self.versions[read_time] = deepcopy(self.records)
        return read_time

    def order_by(self, field, direction="ASCENDING"):
        return Query(self).order_by(field, direction=direction)

    def limit(self, bound):
        return Query(self).limit(bound)

    def where(self, field, operator, value):
        return Query(self).where(field, operator, value)

    def count(self, alias="total"):
        return Query(self).count(alias=alias)

    def document(self, identifier):
        def set_record(record):
            self.records[identifier] = Snapshot(identifier, record)

        return SimpleNamespace(
            get=lambda: SimpleNamespace(exists=False), set=set_record
        )

    def stream(self):
        raise AssertionError("No full collection document scan")


@dataclass(frozen=True)
class Query:
    collection: Collection
    filters: tuple = ()
    orders: tuple = ()
    bound: int | None = None
    cursor: Snapshot | None = None

    def where(self, field, operator, value):
        self.collection.filters.append((field, operator, value))
        return replace(self, filters=(*self.filters, (field, operator, value)))

    def order_by(self, field, direction="ASCENDING"):
        return replace(self, orders=(*self.orders, (field, direction)))

    def limit(self, bound):
        self.collection.limits.append(bound)
        return replace(self, bound=bound)

    def start_after(self, snapshot):
        assert isinstance(snapshot, Snapshot)
        return replace(self, cursor=snapshot)

    def _rows(self, read_time=None):
        records = (
            self.collection.records
            if read_time is None
            else self.collection.versions[read_time]
        )
        rows = sorted(records.values(), key=lambda row: row.id)
        for field, operator, value in self.filters:
            if operator == "==":
                rows = [row for row in rows if row.record.get(field) == value]
            elif operator == "!=":
                assert value is None
                rows = [row for row in rows if row.record.get(field) is not None]
            elif operator == "in":
                rows = [row for row in rows if row.record.get(field) in value]
            else:
                assert operator == "not-in"
                rows = [
                    row
                    for row in rows
                    if row.record.get(field) is not None
                    and row.record[field] not in value
                ]
        orders = self.orders
        if not orders and any(op == "not-in" for _, op, _ in self.filters):
            orders = (("status", "ASCENDING"),)
        for field, direction in reversed(orders):
            # Firestore order_by excludes documents missing the ordered field.
            rows = [row for row in rows if field == "__name__" or field in row.record]
            rows.sort(
                key=lambda row: row.id if field == "__name__" else row.record[field],
                reverse=direction == "DESCENDING",
            )
        if self.cursor is not None:
            ids = [row.id for row in rows]
            rows = rows[ids.index(self.cursor.id) + 1 :]
        return rows if self.bound is None else rows[: self.bound]

    def stream(self, *, read_time=None):
        if self.collection.fail_stream:
            raise RuntimeError("index unavailable")
        read_time = read_time or self.collection.capture()
        self.collection.read_times.append(read_time)
        rows = self._rows(read_time)
        self.collection.delivered += len(rows)
        self.collection.streams += 1
        if self.collection.after_stream:
            hook = self.collection.after_stream
            self.collection.after_stream = None
            hook()
        return iter(replace(row, read_time=read_time) for row in rows)

    def count(self, alias="total"):
        def get(*, read_time=None):
            self.collection.aggregations += 1
            read_time = read_time or self.collection.capture()
            self.collection.read_times.append(read_time)
            result = len(self._rows(read_time))
            if self.collection.after_aggregation:
                hook = self.collection.after_aggregation
                self.collection.after_aggregation = None
                hook()
            return [[SimpleNamespace(value=result, read_time=read_time)]]

        return SimpleNamespace(get=get)


def test_unfinished_exhausts_pages_and_restores_global_chronology(monkeypatch):
    terminal = [deployment(index) for index in range(5000)]
    statuses = ["UNKNOWN", "QUEUED", "ROLLING_BACK", "PROMOTING", "BUILDING"]
    pending = [
        deployment(index, status=statuses[index % len(statuses)])
        for index in range(453)
    ]
    collection = Collection([*terminal, *pending[::-1]])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    items = list(deployment_store.iter_unfinished(page_size=37))

    assert [item.id for item in items] == [row["id"] for row in pending]
    assert len(items) == 453  # More than either prior 50/200 reconciliation cap.
    assert {item.status for item in items} == set(statuses)
    assert collection.delivered == 453  # Previously: 5,453 document reads.
    assert collection.streams == 13
    assert set(collection.limits) == {37}
    assert collection.filters == [
        ("status", "!=", None),
        ("status", "not-in", sorted(deployment_store._TERMINAL_STORE_STATUSES)),
    ]


@pytest.mark.parametrize("count", [0, 1, 200, 400, 401])
def test_unfinished_exact_page_boundaries_never_drop_work(monkeypatch, count):
    collection = Collection([deployment(i, status="UNKNOWN") for i in range(count)])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    assert len(list(deployment_store.iter_unfinished())) == count
    assert collection.delivered == count


def test_unfinished_equal_timestamps_use_snapshot_identity_cursor(monkeypatch):
    records = [
        deployment(i, status="UNKNOWN", created="2026-01-01T00:00:00Z")
        for i in range(407)
    ]
    collection = Collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    items = list(deployment_store.iter_unfinished(page_size=19))
    assert len(items) == 407
    assert {item.id for item in items} == {str(i) for i in range(407)}
    assert collection.delivered == 407


def test_history_latest_and_local_readers_share_deterministic_timestamp_ties(
    monkeypatch,
):
    records = [deployment(i, created="same") for i in [2, 1, 3]]
    collection = Collection([])
    # Real writer uses item.id as the Firestore document id.
    collection.records = {row["id"]: Snapshot(row["id"], row) for row in records}
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    history, total = deployment_store.list_for_service_with_total("service-a", limit=3)
    assert [item.id for item in history] == ["3", "2", "1"]
    assert total == 3
    assert deployment_store.latest_for_services(["service-a"])["service-a"].id == "3"
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: None)
    monkeypatch.setattr(deployment_store, "_local_load", lambda: records)
    assert [item.id for item in deployment_store.list_for_service("service-a")] == [
        "3",
        "2",
        "1",
    ]
    assert deployment_store.latest_for_services(["service-a"])["service-a"].id == "3"


def test_unfinished_local_matches_exhaustive_firestore_order(monkeypatch):
    records = [deployment(i, status="ROLLING_BACK") for i in range(501)]
    records.append(deployment(700, status="ROLLED_BACK"))
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: None)
    monkeypatch.setattr(deployment_store, "_local_load", lambda: records[::-1])
    assert [item.id for item in deployment_store.iter_unfinished()] == [
        str(i) for i in range(501)
    ]
    assert len(deployment_store.list_unfinished(limit=7)) == 7


def test_history_reads_only_requested_rows_and_counts_server_side(monkeypatch):
    records = [deployment(index) for index in range(3000)]
    records.extend(deployment(index, service="other") for index in range(500))
    collection = Collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    items, total = deployment_store.list_for_service_with_total("service-a", limit=20)

    assert total == 3000
    assert [item.id for item in items] == [str(i) for i in range(2999, 2979, -1)]
    assert collection.delivered == 20  # Previously 3,000 document reads.
    assert collection.aggregations == 2
    assert deployment_store.count_for_service("service-a") == 3000
    assert collection.delivered == 20
    assert collection.aggregations == 3


def test_latest_independently_bounds_busy_quiet_and_missing_services(monkeypatch):
    records = [deployment(index) for index in range(3000)]
    records.append(deployment(0, service="quiet"))
    collection = Collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    names = ["service-a", "quiet", "absent", "service-a"]

    latest = deployment_store.latest_for_services(names)

    assert latest["service-a"].id == "2999"
    assert latest["quiet"].id == "0"
    assert "absent" not in latest
    assert collection.delivered == 2  # Previously 3,001 document reads.
    assert collection.streams == 3
    assert names == ["service-a", "quiet", "absent", "service-a"]


@pytest.mark.parametrize(
    "service, expected", [(None, 3500), ("service-a", 3000), ("absent", 0)]
)
def test_release_counts_use_aggregations_without_transferring_rows(
    monkeypatch, service, expected
):
    collection = Collection(
        [deployment(index) for index in range(3000)]
        + [deployment(index, service="other") for index in range(500)]
    )
    monkeypatch.setattr(releases_store, "_firestore_collection", lambda: collection)
    assert releases_store.count_releases(service_name=service) == expected
    assert collection.delivered == 0
    assert collection.aggregations == 1


@pytest.mark.parametrize("operation", ["history", "latest", "unfinished"])
def test_index_errors_propagate_without_broadening_query(monkeypatch, operation):
    collection = Collection([deployment(1)])
    collection.fail_stream = True
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    with pytest.raises(RuntimeError, match="index unavailable"):
        if operation == "history":
            deployment_store.list_for_service_with_total("service-a")
        elif operation == "latest":
            deployment_store.latest_for_services(["service-a"])
        else:
            list(deployment_store.iter_unfinished())


def test_writer_always_materializes_indexed_status_and_created_at(monkeypatch):
    collection = Collection([])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    item = DeploymentItem(
        id="defaults", service_name="service-a", repository="example/repo", tag="v1"
    )
    deployment_store.save(item, "")
    record = collection.records["defaults"].to_dict()
    assert record["status"] == "QUEUED"
    assert record["created_at"] == ""


def test_unfinished_legacy_missing_status_is_not_hidden_and_warning_is_explicit(
    monkeypatch, caplog
):
    legacy = deployment(1)
    legacy.pop("status")
    records = [deployment(i) for i in range(1000)] + [
        legacy,
        deployment(1001, status="UNKNOWN"),
    ]
    collection = Collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    items = list(deployment_store.iter_unfinished(page_size=71))

    assert [(item.id, item.status) for item in items] == [
        ("1", "QUEUED"),
        ("1001", "UNKNOWN"),
    ]
    assert collection.delivered == 1002  # Explicit legacy-only compatibility cost.
    assert "legacy compatibility scan: missing/null status" in caplog.text
    assert len(set(collection.read_times)) == 1


def test_legacy_null_status_keeps_prior_validation_failure_instead_of_hiding_row(
    monkeypatch,
):
    from pydantic import ValidationError

    legacy = {**deployment(1), "status": None}
    collection = Collection([legacy])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    with pytest.raises(ValidationError):
        list(deployment_store.iter_unfinished())
    assert collection.delivered == 1


def test_history_keeps_legacy_missing_created_at_at_end_of_global_order(
    monkeypatch, caplog
):
    legacy = deployment(1)
    legacy.pop("created_at")
    collection = Collection([deployment(2), legacy, deployment(3)])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    items, total = deployment_store.list_for_service_with_total("service-a", limit=20)

    assert total == 3
    assert [item.id for item in items] == ["3", "2", "1"]
    assert items[-1].created_at == ""
    assert "legacy compatibility scan: missing created_at" in caplog.text
    assert len(set(collection.read_times)) == 1


def test_latest_legacy_scan_is_confined_to_affected_requested_service(
    monkeypatch, caplog
):
    legacy = deployment(0, service="legacy")
    legacy.pop("created_at")
    unrelated_legacy = {**legacy, "service_name": "unrelated"}
    collection = Collection(
        [deployment(i) for i in range(3000)] + [legacy, unrelated_legacy]
    )
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    latest = deployment_store.latest_for_services(["service-a", "legacy", "absent"])

    assert latest["service-a"].id == "2999"
    assert latest["legacy"].id == "0"
    assert latest["legacy"].created_at == ""
    assert "absent" not in latest
    assert collection.delivered == 2  # Busy, valid history is still bounded.
    assert "latest legacy compatibility scan" in caplog.text
    assert len(set(collection.read_times)) == 1


@pytest.mark.parametrize("initially_missing", [False, True])
def test_unfinished_schema_detection_and_pages_share_snapshot_during_writes(
    monkeypatch, caplog, initially_missing
):
    record = deployment(1, status="UNKNOWN")
    if initially_missing:
        record.pop("status")
    collection = Collection([record])

    def concurrent_write():
        # This happens AFTER the first server COUNT captured its snapshot.
        collection.records["0"] = Snapshot("0", deployment(1, status="SUCCEEDED"))
        new = deployment(2)
        new.pop("status")
        collection.records["new"] = Snapshot("new", new)

    collection.after_aggregation = concurrent_write
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    items = list(deployment_store.iter_unfinished(page_size=1))

    assert [(item.id, item.status) for item in items] == [
        ("1", "QUEUED" if initially_missing else "UNKNOWN")
    ]
    assert len(set(collection.read_times)) == 1
    assert ("legacy compatibility scan" in caplog.text) == initially_missing


@pytest.mark.parametrize("operation", ["history", "latest"])
def test_history_snapshot_prevents_concurrent_schema_change_or_phantom_latest(
    monkeypatch, caplog, operation
):
    collection = Collection([deployment(1), deployment(2)])

    def concurrent_write():
        current = dict(collection.records["1"].record)
        current.pop("created_at")
        collection.records["1"] = Snapshot("1", current)
        collection.records["phantom"] = Snapshot("phantom", deployment(999))

    collection.after_aggregation = concurrent_write
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    if operation == "history":
        items, total = deployment_store.list_for_service_with_total(
            "service-a", limit=1
        )
        assert total == 2
        assert [item.id for item in items] == ["2"]
    else:
        latest = deployment_store.latest_for_services(["service-a"])
        assert latest["service-a"].id == "2"
    assert len(set(collection.read_times)) == 1
    assert "legacy compatibility scan" not in caplog.text


@pytest.mark.parametrize("operation", ["history", "latest", "unfinished"])
def test_absent_server_snapshot_fails_explicitly_without_unpinned_reads(
    monkeypatch, operation
):
    collection = Collection([deployment(1)])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    monkeypatch.setattr(
        deployment_store, "count_query_at", lambda query, **kwargs: (1, None)
    )
    with pytest.raises(RuntimeError, match="snapshot time is unavailable"):
        if operation == "history":
            deployment_store.list_for_service_with_total("service-a")
        elif operation == "latest":
            deployment_store.latest_for_services(["service-a"])
        else:
            list(deployment_store.iter_unfinished())
    assert collection.delivered == 0


def test_unfinished_mid_page_writes_do_not_drop_or_duplicate_snapshot_work(monkeypatch):
    collection = Collection([deployment(i, status="UNKNOWN") for i in range(5)])

    def concurrent_write():
        collection.records["2"] = Snapshot("2", deployment(2, status="SUCCEEDED"))
        collection.records["3"] = Snapshot("3", deployment(3, status="PROMOTING"))
        collection.records["new"] = Snapshot("new", deployment(6, status="QUEUED"))

    collection.after_stream = concurrent_write
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    items = list(deployment_store.iter_unfinished(page_size=2))
    assert [item.id for item in items] == [str(i) for i in range(5)]
    assert {item.status for item in items} == {"UNKNOWN"}
    assert collection.delivered == 5
    assert collection.streams == 3
    assert len(set(collection.read_times)) == 1
