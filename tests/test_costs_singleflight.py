"""Billing cache concurrency uses controlled events, never live providers."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from unittest.mock import Mock

import pytest

from eng_platform_api.routers import costs


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(costs.config, "mock_mode", False)
    monkeypatch.setattr(costs, "_cache", {})
    monkeypatch.setattr(costs, "_inflight", {})
    monkeypatch.setattr(costs, "catalog_source_identity", lambda: ("path", "digest"))
    monkeypatch.setattr(
        costs.billing,
        "utc_now",
        lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
    )


def test_slow_key_does_not_block_other_key_or_cached_hit():
    started, release = Event(), Event()
    cached_loader = Mock(return_value="cached")
    assert costs._cached(("cached",), cached_loader) == "cached"

    def slow():
        started.set()
        assert release.wait(5), "test did not release the fake billing call"
        return "slow"

    with ThreadPoolExecutor(max_workers=3) as executor:
        blocked = executor.submit(costs._cached, ("daily",), slow)
        try:
            assert started.wait(5)
            other = executor.submit(costs._cached, ("summary",), lambda: "other")
            hit = executor.submit(costs._cached, ("cached",), cached_loader)
            assert other.result(timeout=2) == "other"
            assert hit.result(timeout=2) == "cached"
            assert not blocked.done()
        finally:
            release.set()
        assert blocked.result(timeout=2) == "slow"
    assert cached_loader.call_count == 1
    assert not costs._inflight


@pytest.mark.parametrize("fails", [False, True])
def test_same_key_singleflight_wakes_waiters_and_recovers(monkeypatch, fails):
    started, release, waiting = Event(), Event(), Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    monkeypatch.setattr(costs, "Future", ObservedFuture)
    error = RuntimeError("offline test failure")

    def load():
        started.set()
        assert release.wait(5)
        if fails:
            raise error
        return "shared"

    loader = Mock(side_effect=load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(costs._cached, ("summary",), loader)
        try:
            assert started.wait(5)
            waiter = executor.submit(costs._cached, ("summary",), loader)
            assert waiting.wait(5), "second caller did not join the shared load"
            assert loader.call_count == 1
        finally:
            release.set()
        for result in (owner, waiter):
            if fails:
                with pytest.raises(RuntimeError) as caught:
                    result.result(timeout=2)
                assert caught.value is error
            else:
                assert result.result(timeout=2) == "shared"
    assert not costs._inflight
    if fails:
        assert not costs._cache
        assert costs._cached(("summary",), lambda: "retry") == "retry"
    else:
        assert costs._cached(("summary",), loader) == "shared"
        assert loader.call_count == 1


def test_cache_expiry_and_catalog_identity_are_preserved(monkeypatch):
    clock, source = [10.0], [("path", "first")]
    monkeypatch.setattr(costs, "monotonic", lambda: clock[0])
    monkeypatch.setattr(costs, "catalog_source_identity", lambda: source[0])
    loader = Mock(side_effect=["one", "two", "three"])
    assert costs._cached(("summary",), loader) == "one"
    clock[0] += costs._CACHE_TTL_SECONDS - 1
    assert costs._cached(("summary",), loader) == "one"
    clock[0] += 1
    assert costs._cached(("summary",), loader) == "two"
    source[0] = ("path", "second")
    assert costs._cached(("summary",), loader) == "three"


def test_mock_mode_does_not_populate_cache(monkeypatch):
    monkeypatch.setattr(costs.config, "mock_mode", True)
    loader = Mock(side_effect=[1, 2])
    assert costs._cached(("summary",), loader) == 1
    assert costs._cached(("summary",), loader) == 2
    assert not costs._cache and not costs._inflight
