"""Cloud commands are mocked: prove candidate failures never touch auxiliaries/traffic."""

from copy import deepcopy
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest


@pytest.fixture
def executor(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "central_executor", Path(__file__).parents[1] / "scripts/central_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("EXECUTION_ID", "execution")
    monkeypatch.setattr(module, "api", MagicMock())
    monkeypatch.setattr(module, "cloud", Mock())
    monkeypatch.setattr(module.time, "sleep", Mock())
    return module


def plan():
    return {
        "project_id": "project",
        "region": "region",
        "service_name": "service",
        "image_name": "service",
        "artifact_repository": "images",
        "auxiliary_services": ["worker"],
        "auxiliary_jobs": ["job"],
        "kind": "deploy",
        "configuration": {
            "secrets": {
                "WM_PASSWORD": "projects/project/secrets/wm-password/versions/2"
            }
        },
    }


def snapshot():
    image = "region-docker.pkg.dev/project/images/service@sha256:" + "a" * 64
    return {
        "services": {
            name: {
                "image": image,
                "secrets": {"WM_PASSWORD": "wm-password:1"},
                "traffic": {name + "-old": 100},
            }
            for name in ("service", "worker")
        },
        "jobs": {"job": {"image": image, "secrets": {}, "traffic": {}}},
    }


def setup_release(executor, monkeypatch):
    image = "region-docker.pkg.dev/project/images/service@sha256:" + "b" * 64
    monkeypatch.setenv("IMAGE_DIGEST", image)
    applied = deepcopy(snapshot())
    for runtimes in applied.values():
        for runtime in runtimes.values():
            runtime["image"] = image
    monkeypatch.setattr(
        executor, "capture", Mock(side_effect=[deepcopy(snapshot()), applied])
    )
    monkeypatch.setattr(executor, "numeric_secret", Mock(return_value="wm-password:2"))
    monkeypatch.setattr(executor, "update_runtime", Mock())
    monkeypatch.setattr(
        executor, "candidate_url", Mock(return_value="https://candidate.example")
    )
    monkeypatch.setattr(executor, "traffic", Mock())
    monkeypatch.setattr(executor, "restore", Mock())
    monkeypatch.setattr(executor, "smoke", Mock())
    monkeypatch.setattr(
        executor,
        "describe",
        Mock(
            return_value={
                "status": {"url": "https://prod.example", "imageDigest": image}
            }
        ),
    )


def test_candidate_smoke_failure_keeps_production_untouched(executor, monkeypatch):
    setup_release(executor, monkeypatch)
    executor.smoke.side_effect = RuntimeError("private provider payload")
    with pytest.raises(RuntimeError, match="Release failed"):
        executor.release(plan())
    executor.update_runtime.assert_called_once()
    assert executor.update_runtime.call_args.args[2] == "service"
    assert executor.update_runtime.call_args.kwargs["suffix"] == "r123-1"
    executor.traffic.assert_not_called()
    executor.restore.assert_not_called()
    assert executor.api.call_args.args[1] == {"status": "FAILED"}


def test_deploy_uses_same_digest_and_numeric_configuration_in_all_runtimes(
    executor, monkeypatch
):
    setup_release(executor, monkeypatch)
    executor.release(plan())
    assert executor.update_runtime.call_count == 3
    for call in executor.update_runtime.call_args_list:
        assert call.args[3].endswith("b" * 64)
        assert call.args[4]["WM_PASSWORD"] == "wm-password:2"
    assert executor.traffic.call_args.args[2] == {"service-r123-1": 100}
    assert executor.api.call_args.args[1]["status"] == "SUCCEEDED"


def test_failed_production_smoke_restores_auxiliaries_and_primary(
    executor, monkeypatch
):
    setup_release(executor, monkeypatch)
    executor.smoke.side_effect = [None, RuntimeError("unavailable")]
    with pytest.raises(RuntimeError, match="Release failed"):
        executor.release(plan())
    executor.restore.assert_called_once_with(plan(), snapshot())
    assert executor.api.call_args.args[1]["status"] == "ROLLED_BACK"


def test_restore_failure_is_reported_without_claiming_prod_recovered(
    executor, monkeypatch
):
    setup_release(executor, monkeypatch)
    executor.smoke.side_effect = [None, RuntimeError("unavailable")]
    executor.restore.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="Release failed"):
        executor.release(plan())
    assert executor.api.call_args.args[1]["status"] == "ROLLBACK_FAILED"


def test_recovery_snapshot_failure_cannot_mutate_runtimes(executor, monkeypatch):
    setup_release(executor, monkeypatch)
    executor.capture.side_effect = RuntimeError("metadata unavailable")
    with pytest.raises(RuntimeError, match="Release failed"):
        executor.release(plan())
    executor.update_runtime.assert_not_called()
    assert executor.api.call_args.args[1] == {"status": "FAILED"}


def test_pinning_replaces_secret_references_instead_of_merging_latest(executor):
    executor.update_runtime(
        plan(),
        "services",
        "service",
        "digest",
        {"WM_PASSWORD": "wm-password:2"},
        suffix="candidate",
    )
    args = executor.cloud.call_args.args[0]
    assert "--set-secrets=WM_PASSWORD=wm-password:2" in args
    assert "--no-traffic" in args
    executor.update_runtime(plan(), "jobs", "job", "digest", {})
    assert "--clear-secrets" in executor.cloud.call_args.args[0]


def test_restoration_creates_pinned_revisions_and_promotes_primary_last(
    executor, monkeypatch
):
    operations = []
    monkeypatch.setattr(
        executor,
        "update_runtime",
        lambda *args, **kwargs: operations.append(("update", args[2], args[4])),
    )
    monkeypatch.setattr(
        executor,
        "traffic",
        lambda _, name, refs: operations.append(("traffic", name, refs)),
    )
    revision = executor.restore(plan(), snapshot())
    assert operations[-1] == ("traffic", "service", {revision: 100})
    assert operations[0][1] == "job"
    assert operations[1][1] == "worker"
    assert operations[-2][2] == {"WM_PASSWORD": "wm-password:1"}
