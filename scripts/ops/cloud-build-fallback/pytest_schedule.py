"""Balance worksteal's initial contiguous blocks using historical timings.

This external plugin only permutes collected items. It never changes tests,
fixtures, timeouts, assertions, coverage or the number of xdist workers. A
new source commit can reuse timings for matching node IDs from the same
repository; historical timings are hints, never reusable quality evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics

import pytest


def collection_hash(nodeids: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(nodeids, separators=(",", ":")).encode()
    ).hexdigest()


def balanced_order(
    nodeids: list[str], durations: dict[str, float], workers: int
) -> tuple[list[str], list[float]]:
    """LPT assignment with the exact item-count caps used by xdist worksteal."""
    if workers < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("Scheduling requires positive workers and unique node IDs")
    capacities = []
    remaining = len(nodeids)
    for index in range(workers):
        capacity = remaining // (workers - index)
        capacities.append(capacity)
        remaining -= capacity
    queues: list[list[str]] = [[] for _ in range(workers)]
    totals = [0.0] * workers
    for nodeid in sorted(nodeids, key=lambda item: (-durations[item], item)):
        worker = min(
            (
                index
                for index in range(workers)
                if len(queues[index]) < capacities[index]
            ),
            key=lambda index: (totals[index], index),
        )
        queues[worker].append(nodeid)
        totals[worker] += durations[nodeid]
    return [item for queue in queues for item in queue], totals


def schedule(
    nodeids: list[str], workers: int, source_sha: str, repository: str, profile: dict
) -> tuple[list[str], dict]:
    details = {"mode": "repository_not_profiled"}
    if profile.get("repository") != repository:
        return list(nodeids), details
    durations = profile.get("durations")
    if (
        profile.get("schema_version") != 1
        or not re.fullmatch(r"[0-9a-f]{40}", profile.get("commit_sha", ""))
        or not isinstance(durations, dict)
        or not all(isinstance(nodeid, str) for nodeid in durations)
        or profile.get("test_count") != len(durations)
        or profile.get("collection_sha256") != collection_hash(sorted(durations))
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
            for value in durations.values()
        )
    ):
        raise ValueError("Invalid historical duration profile")
    default_duration = statistics.median(durations.values()) if durations else 0.1
    weights = {nodeid: durations.get(nodeid, default_duration) for nodeid in nodeids}
    order, totals = balanced_order(nodeids, weights, workers)
    if len(order) != len(nodeids) or set(order) != set(nodeids):
        raise ValueError("Scheduling must preserve every collected test")
    return order, {
        "mode": "historical_lpt",
        "known_tests": len(set(nodeids) & set(durations)),
        "unknown_tests": len(set(nodeids) - set(durations)),
        "removed_profile_tests": len(set(durations) - set(nodeids)),
        "unknown_duration_seconds": default_duration,
        "estimated_initial_worker_seconds": [round(value, 3) for value in totals],
        "profile_commit_sha": profile["commit_sha"],
        "source_commit_sha": source_sha,
    }


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    # xdist resets config.option.dist to "no" inside each remote worker.
    node.workerinput["fallback_distribution"] = node.config.getoption("dist")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    worker = getattr(config, "workerinput", None)
    if worker is None or worker.get("fallback_distribution") != "worksteal":
        return
    nodeids = [item.nodeid for item in items]
    order = list(nodeids)
    source_sha = os.environ.get("FALLBACK_SOURCE_SHA", "")
    repository = os.environ.get("FALLBACK_REPOSITORY", "")
    details = {"mode": "no_duration_profile"}
    profile_path = os.environ.get("FALLBACK_DURATION_PROFILE")
    workers = int(worker["workercount"])
    try:
        if profile_path:
            profile_bytes = Path(profile_path).read_bytes()
            profile = json.loads(profile_bytes)
            if not isinstance(profile, dict):
                raise ValueError("Duration profile must be a JSON object")
            order, details = schedule(nodeids, workers, source_sha, repository, profile)
            details["duration_profile_sha256"] = hashlib.sha256(
                profile_bytes
            ).hexdigest()
        if details["mode"] == "historical_lpt":
            by_nodeid = {item.nodeid: item for item in items}
            items[:] = [by_nodeid[nodeid] for nodeid in order]
        evidence = os.environ.get("FALLBACK_SCHEDULE_EVIDENCE_DIR")
        if evidence:
            worker_id = worker["workerid"]
            if not re.fullmatch(r"gw[0-9]+", worker_id):
                raise ValueError("Invalid scheduling evidence worker ID")
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"schedule-{worker_id}.json").write_text(
                json.dumps(
                    {
                        **details,
                        "source_sha": source_sha,
                        "repository": repository,
                        "workers": workers,
                        "worker_id": worker_id,
                        "test_count": len(items),
                        "original_order_sha256": collection_hash(nodeids),
                        "scheduled_order_sha256": collection_hash(order),
                        "collection_sha256": collection_hash(sorted(nodeids)),
                        "timings_are_quality_evidence": False,
                    },
                    indent=2,
                )
                + "\n"
            )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise pytest.UsageError(f"Fallback test scheduling: {exc}") from exc
