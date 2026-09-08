"""Historical timings may reorder the entire suite, but cannot replace evidence."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts/ops/cloud-build-fallback"
SPEC = importlib.util.spec_from_file_location(
    "pytest_schedule", SCRIPTS / "pytest_schedule.py"
)
schedule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedule)
REPOSITORY = "diegomad14/cgm-sanplat-api"


def profile(durations):
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit_sha": "a" * 40,
        "test_count": len(durations),
        "collection_sha256": schedule.collection_hash(sorted(durations)),
        "durations": durations,
    }


def test_long_tests_start_in_distinct_worksteal_blocks():
    durations = {f"test_{i:03}": float(200 - i) for i in range(20)}
    nodeids = list(reversed(durations))
    ordered, details = schedule.schedule(
        nodeids, 4, "a" * 40, REPOSITORY, profile(durations)
    )
    assert len(ordered) == len(nodeids) and set(ordered) == set(nodeids)
    assert [ordered[i] for i in (0, 5, 10, 15)] == list(durations)[:4]
    assert details["known_tests"] == 20
    assert details["unknown_tests"] == 0
    assert (
        schedule.schedule(
            list(reversed(nodeids)), 4, "a" * 40, REPOSITORY, profile(durations)
        )[0]
        == ordered
    )


def test_next_release_additions_and_removals_never_omit_collected_items():
    history = profile({"slow": 10, "fast": 2, "removed": 6})
    nodeids = ["new", "slow", "fast"]
    ordered, details = schedule.schedule(nodeids, 2, "b" * 40, REPOSITORY, history)
    assert sorted(ordered) == sorted(nodeids)
    assert details["known_tests"] == 2
    assert details["unknown_tests"] == 1
    assert details["removed_profile_tests"] == 1
    assert details["unknown_duration_seconds"] == 6
    assert details["profile_commit_sha"] == "a" * 40
    assert details["source_commit_sha"] == "b" * 40
    assert details["mode"] == "historical_lpt"


def test_other_repository_keeps_the_original_order():
    original = ["fast", "slow"]
    ordered, details = schedule.schedule(
        original, 4, "a" * 40, "other/repository", profile({"slow": 10})
    )
    assert ordered == original
    assert details["mode"] == "repository_not_profiled"


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_uneven_or_short_collections_preserve_every_item(workers):
    nodeids = ["third", "first", "second"]
    order, totals = schedule.balanced_order(
        nodeids, {"first": 9, "second": 4, "third": 1}, workers
    )
    assert sorted(order) == sorted(nodeids)
    assert sum(totals) == 14


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema_version=2),
        lambda value: value.update(commit_sha="main"),
        lambda value: value.update(test_count=2),
        lambda value: value.update(collection_sha256="wrong"),
        lambda value: value["durations"].update(test=-1),
        lambda value: value["durations"].update(test=float("nan")),
        lambda value: value["durations"].update(test=float("inf")),
        lambda value: value["durations"].update(test=True),
        lambda value: value["durations"].update(test="3"),
    ],
)
def test_invalid_duration_profiles_cannot_affect_scheduling(mutation):
    history = profile({"test": 1})
    mutation(history)
    with pytest.raises(ValueError, match="Invalid historical"):
        schedule.schedule(["test"], 4, "a" * 40, REPOSITORY, history)


@pytest.mark.parametrize("nodes,workers", [(["duplicate", "duplicate"], 4), ([], 0)])
def test_invalid_collection_or_worker_count_is_rejected(nodes, workers):
    with pytest.raises(ValueError, match="positive workers and unique"):
        schedule.balanced_order(nodes, {"duplicate": 1}, workers)


def test_controller_transfers_distribution_before_xdist_resets_worker_option():
    node = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda option: "worksteal"), workerinput={}
    )
    schedule.pytest_configure_node(node)
    assert node.workerinput == {"fallback_distribution": "worksteal"}


def worker_config():
    return SimpleNamespace(
        workerinput={
            "workerid": "gw0",
            "workercount": 2,
            "fallback_distribution": "worksteal",
        },
        # xdist remote.py deliberately sets this to "no" inside each worker.
        getoption=lambda option, default=None: "no",
    )


def test_worker_hook_preserves_item_objects_and_records_profile_provenance(
    tmp_path, monkeypatch
):
    history = tmp_path / "history.json"
    history.write_text(json.dumps(profile({"slow": 10, "fast": 2})))
    monkeypatch.setenv("FALLBACK_DURATION_PROFILE", str(history))
    monkeypatch.setenv("FALLBACK_SOURCE_SHA", "b" * 40)
    monkeypatch.setenv("FALLBACK_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("FALLBACK_SCHEDULE_EVIDENCE_DIR", str(tmp_path / "evidence"))
    items = [SimpleNamespace(nodeid=name) for name in ("fast", "slow", "new")]
    identities = {id(item) for item in items}
    schedule.pytest_collection_modifyitems(worker_config(), items)
    assert {id(item) for item in items} == identities
    report = json.loads((tmp_path / "evidence/schedule-gw0.json").read_text())
    assert report["test_count"] == 3
    assert report["known_tests"] == 2 and report["unknown_tests"] == 1
    assert report["source_sha"] == "b" * 40
    assert report["profile_commit_sha"] == "a" * 40
    assert report["timings_are_quality_evidence"] is False
    assert (
        report["duration_profile_sha256"]
        == hashlib.sha256(history.read_bytes()).hexdigest()
    )
    assert report["scheduled_order_sha256"] == schedule.collection_hash(
        [item.nodeid for item in items]
    )


def test_no_profile_and_non_worksteal_keep_original_items(monkeypatch):
    monkeypatch.delenv("FALLBACK_DURATION_PROFILE", raising=False)
    monkeypatch.delenv("FALLBACK_SCHEDULE_EVIDENCE_DIR", raising=False)
    items = [SimpleNamespace(nodeid=name) for name in ("b", "a")]
    original = list(items)
    schedule.pytest_collection_modifyitems(SimpleNamespace(), items)
    schedule.pytest_collection_modifyitems(worker_config(), items)
    config = worker_config()
    config.workerinput["fallback_distribution"] = "load"
    schedule.pytest_collection_modifyitems(config, items)
    assert items == original


def test_invalid_profile_is_a_clear_collection_error(tmp_path, monkeypatch):
    history = tmp_path / "invalid.json"
    history.write_text("[]")
    monkeypatch.setenv("FALLBACK_DURATION_PROFILE", str(history))
    with pytest.raises(pytest.UsageError, match="JSON object"):
        schedule.pytest_collection_modifyitems(worker_config(), [])


def test_versioned_golden_profile_keeps_all_1053_tests_and_separates_the_four_slowest():
    history = json.loads(
        (SCRIPTS / "duration_profiles/cgm-sanplat-api.json").read_text()
    )
    nodeids = list(history["durations"])
    ordered, details = schedule.schedule(
        nodeids, 4, history["commit_sha"], REPOSITORY, history
    )
    assert len(ordered) == len(set(ordered)) == 1053
    slowest = sorted(nodeids, key=history["durations"].get, reverse=True)[:4]
    assert [ordered[i] for i in (0, 263, 526, 789)] == slowest
    assert max(details["estimated_initial_worker_seconds"]) < 236
