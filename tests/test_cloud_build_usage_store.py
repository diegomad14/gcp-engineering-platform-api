from unittest.mock import Mock

from eng_platform_api.config import config
from eng_platform_api.services import cloud_build_usage_store as store


def test_firestore_collection_and_transactional_upsert(monkeypatch):
    from google.cloud import firestore

    store._client.cache_clear()

    monkeypatch.setattr(config, "mock_mode", False)
    client = Mock()
    monkeypatch.setattr(firestore, "Client", lambda **_: client)
    monkeypatch.setattr(firestore, "transactional", lambda fn: fn)
    collection = client.collection.return_value
    document = collection.document.return_value
    document.get.return_value.exists = True
    document.get.return_value.to_dict.return_value = {"old": 1}
    assert store.get("key") == {"old": 1}
    assert store.update("key", lambda old: {**old, "new": 2}) == {"old": 1, "new": 2}
    document._client.transaction.return_value.set.assert_called_once_with(
        document, {"old": 1, "new": 2}
    )
    collection.where.return_value.stream.return_value = [document.get.return_value]
    assert store.rows() == [{"old": 1}]
    document.get.return_value.exists = False
    assert store.get("missing") is None
    client.collection.assert_called_with("cloud_build_usage")
    for category in ("deployment", "release_quality"):
        document.get.return_value.exists = True
        assert store.execution(category, "exec") == {"old": 1}
        document.get.return_value.exists = False
        assert store.execution(category, "missing") is None
    store._client.cache_clear()


def test_mock_collection_and_utc_clock(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", True)
    assert store._collection() is None
    assert store.execution("deployment", "ignored") is None
    assert store.utc_now().utcoffset().total_seconds() == 0
