from pathlib import Path

import pytest

from eng_platform_api.services.cloud_build_policy import (
    economy_options,
    validate_submission,
)


@pytest.mark.parametrize(
    "key,value",
    [
        ("machineType", "E2_HIGHCPU_8"),
        ("machineType", "E2_STANDARD_2"),
        ("machineType", "UNSPECIFIED"),
        ("pool", {}),
        ("workerPool", "private"),
        ("diskSizeGb", 100),
        ("logging", "GCS_ONLY"),
    ],
)
def test_no_override_can_bypass_economy_policy(key, value):
    with pytest.raises(ValueError):
        validate_submission(
            {
                "timeout": "1800s",
                "options": {"logging": "CLOUD_LOGGING_ONLY", key: value},
            }
        )


@pytest.mark.parametrize("timeout", ["", "3601s", "0s", "-1s", "30m", "oops"])
def test_timeout_must_be_bounded(timeout):
    with pytest.raises(ValueError):
        validate_submission({"timeout": timeout, "options": economy_options()})


def test_ci_guards_active_generators_and_workflows():
    root = Path(__file__).resolve().parents[1]
    historical_rates = {
        root / "src/eng_platform_api/services/cloud_build_usage.py",
        root / "scripts/ops/cloud-build-fallback/measure.py",
    }
    paths = [
        path
        for directory in ("src", "scripts", ".github", "templates", "examples")
        for path in (root / directory).rglob("*")
        if path.suffix in {".py", ".sh", ".yml", ".yaml"}
        and path not in historical_rates
    ]
    for path in paths:
        text = path.read_text()
        assert "E2_HIGHCPU" not in text, path
        assert "e2-highcpu" not in text, path
    assert economy_options() == {"logging": "CLOUD_LOGGING_ONLY"}
