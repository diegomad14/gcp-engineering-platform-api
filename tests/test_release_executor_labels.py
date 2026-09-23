"""The pinned deploy engine labels every new runtime with its exact release SHA."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def engine():
    path = (
        Path(__file__).resolve().parents[1]
        / "docker/release-executor/release_executor.py"
    )
    spec = importlib.util.spec_from_file_location("release_executor_labels_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _environment(monkeypatch, service):
    values = {
        "CGM_SERVICE": service,
        "CGM_PROFILE": service,
        "CGM_REGION": "us-central1",
        "CGM_PROJECT_ID": "example-project",
        "CGM_REQUEST_FINGERPRINT": "f" * 64,
        "CGM_RELEASE_SHA": "a" * 40,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_cloud_run_service_candidate_gets_release_sha_label(engine, monkeypatch):
    _environment(monkeypatch, "eng-platform-web")
    commands = []
    candidate = "eng-platform-web-ep-ffffffffff"

    def run(*args, **_kwargs):
        commands.append(args)
        if "--format=value(status.latestCreatedRevisionName)" in args:
            return candidate
        if "--format=json(status.traffic)" in args:
            return json.dumps(
                {
                    "status": {
                        "traffic": [
                            {
                                "tag": "candidate-ffffffffffff",
                                "url": "https://candidate.example.test",
                            }
                        ]
                    }
                }
            )
        return ""

    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(engine, "_traffic", lambda *_: {"old-revision": 100})
    monkeypatch.setattr(engine, "_set_traffic", lambda *_: None)
    monkeypatch.setattr(engine, "_smoke", lambda *_: None)
    monkeypatch.setattr(engine, "_service_url", lambda *_: "https://prod.example.test")
    monkeypatch.setattr(engine, "emit", lambda *_args, **_kwargs: None)

    result = engine.deploy("image@sha256:" + "b" * 64)

    assert result["production_revision"] == candidate
    updates = [
        cmd for cmd in commands if cmd[:4] == ("gcloud", "run", "services", "update")
    ]
    assert len(updates) == 1
    assert (
        "--update-labels",
        "commit-sha=" + "a" * 40,
    ) == updates[0][updates[0].index("--update-labels") :][:2]
    assert "--no-traffic" in updates[0]


def test_cloud_run_job_definition_gets_release_sha_label(engine, monkeypatch):
    _environment(monkeypatch, "cgm-artemis-wm-sweep-worker")
    commands = []
    image = "image@sha256:" + "b" * 64
    monkeypatch.setenv("CGM_EVIDENCE_BUCKET", "test-evidence")
    monkeypatch.setattr(engine, "_job_definition", lambda: "job-definition")
    monkeypatch.setattr(engine, "_job_operational_spec", lambda *_: {"ok": True})
    images = iter(["prior-image", image])
    monkeypatch.setattr(engine, "_job_image", lambda: next(images))
    monkeypatch.setattr(engine, "_save_job_definition", lambda *_: "job-" + "c" * 64)
    monkeypatch.setattr(engine, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        engine, "run", lambda *args, **_kwargs: commands.append(args) or ""
    )

    result = engine.deploy_job(image)

    assert result["image_digest"] == image
    updates = [
        cmd for cmd in commands if cmd[:4] == ("gcloud", "run", "jobs", "update")
    ]
    assert len(updates) == 1
    assert (
        updates[0][updates[0].index("--update-labels") + 1] == "commit-sha=" + "a" * 40
    )


def test_executor_dockerfile_uses_python_with_apk_yaml():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker/release-executor/Dockerfile"
    ).read_text(encoding="utf-8")
    assert "apk add --no-cache docker-cli git python3 py3-yaml" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/bin/python3", "/opt/eng-platform/release_executor.py"]'
        in dockerfile
    )
    assert "\nUSER root\n" in dockerfile
