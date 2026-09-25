"""Safety contracts for the one-time Artemis runtime bootstrap."""

import importlib.util
import sys
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parents[1] / "scripts/bootstrap_artemis_runtimes.py"
_SPEC = importlib.util.spec_from_file_location("artemis_bootstrap", _PATH)
bootstrap = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bootstrap
_SPEC.loader.exec_module(bootstrap)


def _source(kind: str, name: str) -> dict:
    container = {
        "image": "registry.example/source@sha256:" + "a" * 64,
        "env": [
            {
                "name": "DATABASE_URL",
                "valueFrom": {
                    "secretKeyRef": {"name": "database-url", "key": "latest"}
                },
            },
            {"name": "APP_BACKGROUND_TASKS_ENABLED", "value": "true"},
        ],
    }
    template = {
        "metadata": {
            "name": "old-revision",
            "annotations": {
                "run.googleapis.com/client-name": "gcloud",
                "run.googleapis.com/cloudsql-instances": "project:region:db",
            },
            "labels": {"commit-sha": "wrong"},
        },
        "spec": {
            "serviceAccountName": "old@example.com",
            "containers": [container],
        },
    }
    if kind == "Job":
        template["spec"] = {"template": {"spec": template["spec"]}}
    result = {
        "kind": kind,
        "metadata": {
            "name": name,
            "annotations": {"run.googleapis.com/urls": "old-url"},
            "labels": {"commit-sha": "wrong"},
        },
        "spec": {"template": template},
    }
    if kind == "Service":
        result["spec"]["traffic"] = [{"revisionName": "old-revision", "percent": 100}]
    return result


def test_api_bootstrap_is_passive_and_drops_stale_provenance():
    source = _source("Service", "cgm-sanplat-api")
    manifest = bootstrap.make_manifest(
        source, "cgm-artemis-api", {"cgm-artemis-database-url"}
    )
    spec = manifest["spec"]["template"]["spec"]
    env = {row["name"]: row for row in spec["containers"][0]["env"]}
    assert source["metadata"]["name"] == "cgm-sanplat-api"
    assert manifest["metadata"]["name"] == "cgm-artemis-api"
    assert manifest["spec"]["traffic"] == [{"latestRevision": True, "percent": 100}]
    assert "commit-sha" not in manifest["spec"]["template"]["metadata"]["labels"]
    assert (
        env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
        == "cgm-artemis-database-url"
    )
    assert env["APP_BACKGROUND_TASKS_ENABLED"]["value"] == "false"
    assert env["WM_SWEEP_ENABLED"]["value"] == "false"
    assert env["DATA_RECOVERY_CONTINUOUS_ENABLED"]["value"] == "false"


def test_typed_worker_does_not_inherit_a_general_worker_identity():
    source = _source("Service", "cgm-sanplat-job-worker")
    manifest = bootstrap.make_manifest(
        source, "cgm-artemis-sync-worker", {"cgm-artemis-database-url"}
    )
    spec = manifest["spec"]["template"]["spec"]
    env = {row["name"]: row for row in spec["containers"][0]["env"]}
    assert spec["serviceAccountName"].startswith("artemis-sync-runtime@")
    assert env["ARTEMIS_WORKER_TYPE"]["value"] == "sync"
    assert env["JOB_WORKER_PREFIX"]["value"] == "cgm-artemis"


def test_job_bootstrap_changes_identity_without_scheduling_work():
    source = _source("Job", "cgm-sanplat-fnd-observation-worker")
    manifest = bootstrap.make_manifest(
        source, "cgm-artemis-fnd-observation-worker", {"cgm-artemis-database-url"}
    )
    spec = manifest["spec"]["template"]["spec"]["template"]["spec"]
    assert manifest["kind"] == "Job"
    assert "traffic" not in manifest["spec"]
    assert spec["serviceAccountName"].startswith("artemis-fndobs-runtime@")


def test_bootstrap_rejects_missing_secret_alias_and_mutable_image():
    source = _source("Service", "cgm-sanplat-api")
    with pytest.raises(ValueError, match="secret alias is missing"):
        bootstrap.make_manifest(source, "cgm-artemis-api", set())
    source["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "registry.example/source:latest"
    )
    with pytest.raises(ValueError, match="immutable source image"):
        bootstrap.make_manifest(source, "cgm-artemis-api", {"cgm-artemis-database-url"})
