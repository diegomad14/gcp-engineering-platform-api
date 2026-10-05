"""Overview queries stay within Firestore limits as the catalog grows."""

from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from eng_platform_api.main import app
from eng_platform_api.models import DeploymentItem
from eng_platform_api.routers import deployments
from eng_platform_api.services import catalog, deployment_store


def _deployment(service_name, *, newer=False):
    return DeploymentItem(
        id=f"{service_name}-{'new' if newer else 'old'}",
        service_name=service_name,
        repository="example/deployable-service",
        tag="v1.0.0",
        created_at=f"2026-01-0{2 if newer else 1}T00:00:00+00:00",
    )


def _collection(records):
    """Enforce the provider's actual bound instead of accepting any mocked IN."""
    collection = Mock()

    def where(field, operator, names):
        assert (field, operator) == ("service_name", "in")
        assert 0 < len(names) <= 30
        snapshots = [
            SimpleNamespace(to_dict=lambda record=record: record.model_dump())
            for record in records
            if record.service_name in names
        ]
        return SimpleNamespace(stream=lambda: iter(snapshots))

    collection.where.side_effect = where
    return collection


@pytest.mark.parametrize("size", [1, 30, 31, 51, 60, 61])
def test_latest_batches_firestore_queries_without_losing_services(monkeypatch, size):
    names = [f"service-{index}" for index in range(size)]
    # Unsorted records exercise latest selection in every batch.
    records = [
        _deployment(name, newer=newer) for newer in (False, True) for name in names
    ]
    collection = _collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    latest = deployment_store.latest_for_services(names)

    assert set(latest) == set(names)
    assert all(item.id == f"{name}-new" for name, item in latest.items())
    assert [call.args[2] for call in collection.where.call_args_list] == [
        names[offset : offset + 30] for offset in range(0, size, 30)
    ]
    collection.stream.assert_not_called()  # Never broaden to the full collection.


def test_latest_deduplicates_names_without_mutating_input(monkeypatch):
    names = [f"service-{index}" for index in range(31)]
    requested = names + names[::-1]
    original = requested.copy()
    collection = _collection([_deployment(names[-1])])
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    latest = deployment_store.latest_for_services(requested)

    assert requested == original
    assert set(latest) == {names[-1]}
    assert [call.args[2] for call in collection.where.call_args_list] == [
        names[:30],
        names[30:],
    ]


def test_latest_empty_input_never_initializes_firestore(monkeypatch):
    collection = Mock(side_effect=AssertionError("No query for an empty catalog"))
    monkeypatch.setattr(deployment_store, "_firestore_collection", collection)
    assert deployment_store.latest_for_services([]) == {}
    collection.assert_not_called()


def test_later_batch_failure_is_not_hidden_as_partial_history(monkeypatch):
    names = [f"service-{index}" for index in range(31)]
    collection = _collection([_deployment(names[0])])
    first_batch = collection.where.side_effect

    def where(field, operator, batch):
        if batch == names[30:]:
            raise RuntimeError("Provider unavailable")
        return first_batch(field, operator, batch)

    collection.where.side_effect = where
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)

    with pytest.raises(RuntimeError, match="Provider unavailable"):
        deployment_store.latest_for_services(names)
    assert collection.where.call_count == 2


def test_local_latest_retains_filtering_and_order_for_large_catalog(monkeypatch):
    names = [f"service-{index}" for index in range(51)]
    records = [
        _deployment(name, newer=newer).model_dump()
        for newer in (False, True)
        for name in [*names, "not-requested"]
    ]
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: None)
    monkeypatch.setattr(deployment_store, "_local_load", lambda: records)

    latest = deployment_store.latest_for_services(names)

    assert set(latest) == set(names)
    assert all(item.id == f"{name}-new" for name, item in latest.items())


def test_overview_returns_all_51_resources_with_batched_firestore(monkeypatch):
    services = catalog.get_services().services
    assert len(services) == 51
    managed = [service for service in services if service.management_mode == "managed"]
    observed = [
        service
        for service in services
        if service.management_mode == "observability_only"
    ]
    records = [_deployment(service.service_name, newer=True) for service in managed]
    collection = _collection(records)
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    monkeypatch.setattr(deployments, "_overview_cache", None)
    client = TestClient(app)

    response = client.get("/api/deployments/overview")

    assert response.status_code == 200
    items = {item["service_name"]: item for item in response.json()["items"]}
    assert set(items) == {service.service_name for service in services}
    for service in managed:
        assert items[service.service_name]["last_deployment"]["id"] == (
            f"{service.service_name}-new"
        )
    for service in observed:
        item = items[service.service_name]
        assert item["last_deployment"] is None
        assert item["deployment_ready"] is False
        assert item["deployment_blockers"] == service.deployment_blockers
    assert [len(call.args[2]) for call in collection.where.call_args_list] == [30, 21]
    assert client.get("/api/deployments/overview").json() == response.json()
    assert collection.where.call_count == 2  # Cached overview does not re-query.
