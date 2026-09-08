import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def measure():
    path = Path(__file__).parents[1] / "scripts/ops/cloud-build-fallback/measure.py"
    spec = importlib.util.spec_from_file_location("fallback_measure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(**overrides):
    return dict(
        id="test",
        status="SUCCESS",
        options={"machineType": "E2_STANDARD_2"},
        createTime="2026-09-08T00:00:00Z",
        startTime="2026-09-08T00:02:00Z",
        finishTime="2026-09-08T00:12:00Z",
        substitutions={"_RELEASE_SHA": "a" * 40},
        **overrides,
    )


def test_missing_costs_cannot_be_treated_as_free(measure):
    row = measure.measurement(build(), {})
    assert row["compute_usd_estimated"] == 0.06
    assert row["queue_seconds"] == 120
    assert row["total_usd_estimated"] is None
    assert not row["meets_build_acceptance"]


def test_fast_but_expensive_or_failed_build_is_rejected(measure):
    costs = dict.fromkeys(measure.AUXILIARY, 0.1)
    assert not measure.measurement(build(), costs)["meets_build_acceptance"]
    costs = dict.fromkeys(measure.AUXILIARY, 0.001)
    failed = build()
    failed["status"] = "FAILURE"
    assert not measure.measurement(failed, costs)["meets_build_acceptance"]
    assert measure.measurement(build(), costs)["meets_build_acceptance"]


def test_experiment_includes_failed_attempts(measure):
    failed = build()
    failed.update(id="failed", status="FAILURE")
    costs = {key: dict.fromkeys(measure.AUXILIARY, 0.001) for key in ("test", "failed")}
    result = measure.compare([build(), failed], costs, 10)
    assert result["selected_build_id"] == "test"
    assert result["experiment_compute_usd_estimated"] == 0.12
    assert result["selected_monthly_cost_usd_estimated"] == pytest.approx(0.64)


def test_slow_cheap_build_is_rejected(measure):
    slow = build()
    slow["finishTime"] = "2026-09-08T00:15:00Z"
    assert not measure.measurement(slow, dict.fromkeys(measure.AUXILIARY, 0))[
        "meets_build_acceptance"
    ]


def test_unknown_comparison_costs_block_selection(measure):
    other = build()
    other["id"] = "unknown"
    costs = {"test": dict.fromkeys(measure.AUXILIARY, 0.001)}
    result = measure.compare([build(), other], costs, 10)
    assert result["selected_build_id"] is None
