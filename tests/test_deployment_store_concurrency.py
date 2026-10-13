"""Local snapshot writes retain admissions made by independent API processes."""

import fcntl
import multiprocessing
from pathlib import Path
import traceback

import pytest

from eng_platform_api.models import DeploymentItem, ProviderSelection
from eng_platform_api.services import deployment_store


def _save_in_process(path, item_payload, key, role, release_first, events):
    """Pause one reader and observe the real flock contention in its peer."""
    deployment_store._DEFAULT_STORE_PATH = Path(path)
    deployment_store._COLLECTION = ""
    original_load = deployment_store._local_load
    original_flock = fcntl.flock

    def coordinated_load():
        records = original_load()
        events.put((role, "loaded"))
        if role == "first" and not release_first.wait(10):
            raise TimeoutError("First writer was not released")
        return records

    def observed_flock(descriptor, operation):
        if role == "second" and operation == fcntl.LOCK_EX:
            try:
                original_flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                events.put((role, "blocked"))
                original_flock(descriptor, operation)
            return
        original_flock(descriptor, operation)

    try:
        item = DeploymentItem(**item_payload)
        # Different service command fences cannot protect the shared JSON file.
        with deployment_store.command_guard(item.service_name):
            deployment_store._local_load = coordinated_load
            fcntl.flock = observed_flock
            deployment_store.save(item, key)
        events.put((role, "saved"))
    except Exception:
        events.put((role, "error", traceback.format_exc()))
        raise
    finally:
        deployment_store._local_load = original_load
        fcntl.flock = original_flock


def _intent(service_name, provider):
    return DeploymentItem(
        id=f"intent-{service_name}",
        service_name=service_name,
        repository=f"example/{service_name}",
        tag="v1.0.0",
        provider_selection=ProviderSelection(
            requested=provider,
            service_default="cloud_build",
            effective=provider,
            source="request",
        ),
    )


def test_local_snapshot_retains_different_service_intents_across_processes(
    monkeypatch, tmp_path
):
    path = tmp_path / "deployments.json"
    monkeypatch.setattr(deployment_store, "_DEFAULT_STORE_PATH", path)
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    first = _intent("service-a", "github_actions")
    second = _intent("service-b", "cloud_build")
    context = multiprocessing.get_context("spawn")
    release_first = context.Event()
    events = context.Queue()
    processes = [
        context.Process(
            target=_save_in_process,
            args=(
                str(path),
                item.model_dump(),
                f"key-{item.service_name}",
                role,
                release_first,
                events,
            ),
        )
        for item, role in ((first, "first"), (second, "second"))
    ]
    try:
        processes[0].start()
        assert events.get(timeout=10) == ("first", "loaded")
        processes[1].start()
        second_observation = events.get(timeout=10)
        if second_observation == ("second", "loaded"):
            # Without the global lock this guarantees the stale first snapshot
            # overwrites the second writer, without depending on a timed sleep.
            assert events.get(timeout=10) == ("second", "saved")
        release_first.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        saved = {
            item.service_name: deployment_store.find_by_idempotency_key(
                f"key-{item.service_name}"
            )
            for item in (first, second)
        }
        assert saved == {first.service_name: first, second.service_name: second}
        assert deployment_store.idempotency_key_for(first.id) == "key-service-a"
        assert deployment_store.idempotency_key_for(second.id) == "key-service-b"
        assert second_observation == ("second", "blocked")
    finally:
        release_first.set()
        for process in processes:
            if process.pid is not None:
                process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
        events.close()
        events.join_thread()


def test_local_snapshot_guard_releases_after_write_failure(monkeypatch, tmp_path):
    path = tmp_path / "deployments.json"
    monkeypatch.setattr(deployment_store, "_DEFAULT_STORE_PATH", path)
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    original_save = deployment_store._local_save

    def fail_write(records):
        raise OSError("Snapshot storage unavailable")

    monkeypatch.setattr(deployment_store, "_local_save", fail_write)
    with pytest.raises(OSError, match="Snapshot storage unavailable"):
        deployment_store.save(_intent("failed-service", "github_actions"), "failed")
    monkeypatch.setattr(deployment_store, "_local_save", original_save)
    context = multiprocessing.get_context("spawn")
    release_first = context.Event()
    events = context.Queue()
    item = _intent("recovered-service", "github_actions")
    process = context.Process(
        target=_save_in_process,
        args=(
            str(path),
            item.model_dump(),
            "recovered",
            "second",
            release_first,
            events,
        ),
    )
    try:
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert events.get(timeout=1) == ("second", "loaded")
        assert events.get(timeout=1) == ("second", "saved")
        assert deployment_store.find_by_idempotency_key("recovered") == item
        assert deployment_store.find_by_idempotency_key("failed") is None
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        events.close()
        events.join_thread()
