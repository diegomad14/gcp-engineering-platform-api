#!/usr/bin/env python3
"""Compare execution and complete estimated costs, never treating unknowns as free."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

RATES_USD_MINUTE = {"E2_HIGHCPU_8": 0.0156, "E2_STANDARD_2": 0.006}
BASELINE_SECONDS = 1255.71
BASELINE_COMPUTE_USD = BASELINE_SECONDS / 60 * RATES_USD_MINUTE["E2_HIGHCPU_8"]
AUXILIARY = ("storage_usd", "logging_usd", "transfer_usd", "operations_usd")


def elapsed(start: str, end: str) -> float:
    seconds = (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()
    if seconds < 0:
        raise ValueError("Invalid build timestamps")
    return seconds


def finite_cost(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def measurement(build: dict, costs: dict) -> dict:
    machine = build.get("options", {}).get("machineType", "E2_STANDARD_2")
    if machine not in RATES_USD_MINUTE:
        raise ValueError("Unsupported machine profile")
    seconds = elapsed(build["startTime"], build["finishTime"])
    queue = elapsed(build["createTime"], build["startTime"])
    compute = seconds / 60 * RATES_USD_MINUTE[machine]
    complete = all(finite_cost(costs.get(key)) for key in AUXILIARY)
    total = compute + sum(costs[key] for key in AUXILIARY) if complete else None
    # Baseline compute alone is a conservative ceiling for the new complete cost:
    # baseline auxiliary costs can only make the old total higher.
    passed = (
        build["status"] == "SUCCESS"
        and seconds <= 628
        and total is not None
        and total <= BASELINE_COMPUTE_USD
    )
    return {
        "build_id": build["id"],
        "status": build["status"],
        "machine_type": machine,
        "execution_seconds": seconds,
        "queue_seconds": queue,
        "compute_usd_estimated": compute,
        "total_usd_estimated": total,
        "auxiliary_costs_complete": complete,
        "cost_inputs": costs,
        "meets_build_acceptance": passed,
        "source_sha": build.get("substitutions", {}).get("_RELEASE_SHA"),
        "quality_uri": build.get("substitutions", {}).get("_QUALITY_URI"),
    }


def compare(builds: list[dict], costs: dict, monthly_releases: int) -> dict:
    if monthly_releases < 1 or not builds:
        raise ValueError("Positive release volume and at least one build required")
    if len({build["id"] for build in builds}) != len(builds):
        raise ValueError("Duplicate build IDs would double count costs")
    shas = {build.get("substitutions", {}).get("_RELEASE_SHA") for build in builds}
    if len(shas) != 1 or None in shas:
        raise ValueError("Comparison requires the same exact release SHA")
    rows = [measurement(build, costs.get(build["id"], {})) for build in builds]
    all_costs_known = all(row["auxiliary_costs_complete"] for row in rows)
    eligible = (
        [row for row in rows if row["meets_build_acceptance"]]
        if all_costs_known
        else []
    )
    winner = (
        min(
            eligible,
            key=lambda row: (row["total_usd_estimated"], row["execution_seconds"]),
        )
        if eligible
        else None
    )
    all_costs_known = all(row["auxiliary_costs_complete"] for row in rows)
    return {
        "tariff_date": "2026-09-08",
        "tariff_source": "https://cloud.google.com/build/pricing",
        "baseline_execution_seconds": BASELINE_SECONDS,
        "conservative_cost_ceiling_usd": BASELINE_COMPUTE_USD,
        "builds": rows,
        "selected_build_id": winner["build_id"] if winner else None,
        "experiment_compute_usd_estimated": sum(
            row["compute_usd_estimated"] for row in rows
        ),
        "experiment_total_usd_estimated": sum(
            row["total_usd_estimated"] for row in rows
        )
        if all_costs_known
        else None,
        "monthly_release_volume": monthly_releases,
        "selected_monthly_cost_usd_estimated": winner["total_usd_estimated"]
        * monthly_releases
        if winner
        else None,
        "limitations": "Estimates exclude no listed cost category. Unknown auxiliary costs block selection. Actual billing, full oss-v2 evidence and production maintenance acceptance must be verified separately.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="append", type=Path, required=True)
    parser.add_argument("--costs", type=Path, required=True)
    parser.add_argument("--monthly-releases", type=int, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            compare(
                [json.loads(path.read_text()) for path in args.build],
                json.loads(args.costs.read_text()),
                args.monthly_releases,
            ),
            indent=2,
        )
    )
