"""The pinned deploy engine labels every new runtime with its exact release SHA."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from eng_platform_api.services.catalog import get_service


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
                                "tag": "c-ffffffffffff",
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


@pytest.mark.parametrize(
    "service",
    [
        "cgm-artemis-api",
        "cgm-artemis-job-dispatcher",
        "cgm-artemis-job-worker",
        "cgm-artemis-sync-worker",
        "cgm-artemis-clock-sync-worker",
        "cgm-artemis-data-recovery-worker",
        "cgm-artemis-fnd-ip-sync-worker",
        "cgm-artemis-mcp-worker",
    ],
)
def test_artemis_candidate_tag_fits_cloud_run_limit(engine, monkeypatch, service):
    _environment(monkeypatch, service)
    tags = []
    revision = service + "-ep-ffffffffff"

    def run(*args, **kwargs):
        if "--update-tags" in args:
            tag, target = args[args.index("--update-tags") + 1].split("=")
            assert len(tag) + len(service) <= 46
            assert target == revision
            tags.append(tag)
        if "--format=value(status.latestCreatedRevisionName)" in args:
            return revision
        if "--format=json(status.traffic)" in args:
            return json.dumps(
                {"status": {"traffic": [{"tag": tags[0], "url": "https://ready"}]}}
            )
        return ""

    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(engine, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "candidate_env_args", lambda: [])
    assert engine._deploy_candidate("image", service, "r", "p") == (
        revision,
        "https://ready",
    )


def test_cloud_run_job_definition_gets_release_sha_label(engine, monkeypatch):
    _environment(monkeypatch, "cgm-artemis-wm-sweep-worker")
    commands = []
    image = "image@sha256:" + "b" * 64
    monkeypatch.setenv("CGM_EVIDENCE_BUCKET", "test-evidence")
    import yaml

    definition = yaml.safe_dump(
        {
            "spec": {
                "template": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "args": [
                                            "-m",
                                            "cgm_sanplat_param.worker",
                                            "--type",
                                            "wm-sweep",
                                        ],
                                        "env": [],
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
    )
    monkeypatch.setattr(engine, "_job_definition", lambda: definition)
    monkeypatch.setattr(engine, "pause_job_schedules", lambda: [])
    monkeypatch.setattr(engine, "resume_job_schedules", lambda names: None)
    monkeypatch.setattr(engine, "runtime_grant", lambda *a: None)
    monkeypatch.setattr(engine, "check_job_runtime", lambda: None)
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
    assert len(updates) == 2
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


def test_bot_catalog_builds_with_repository_relative_dockerfile(
    engine, monkeypatch, tmp_path
):
    service = get_service("cgm-bot-api")
    assert service is not None
    _environment(monkeypatch, "cgm-bot-api")
    monkeypatch.setenv("CGM_IMAGE", "registry.example/bot:v1.17.4")
    monkeypatch.setenv("CGM_REPOSITORY", service.repository)
    monkeypatch.setenv("CGM_BUILD_CONTEXT", service.deployment.build_context)
    monkeypatch.setenv("CGM_DOCKERFILE_PATH", service.deployment.dockerfile_path)
    monkeypatch.delenv("CGM_CACHE_IMAGE", raising=False)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    dockerfile = tmp_path / "services/cgm-bot-api/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n")
    commands = []

    def run(*args, **_kwargs):
        commands.append(args)
        if args[:2] == ("docker", "pull"):
            raise subprocess.CalledProcessError(1, args)
        if args[:4] == ("gcloud", "artifacts", "docker", "images"):
            return "sha256:" + "b" * 64
        return ""

    monkeypatch.setattr(engine, "run", run)
    assert engine.image_for_tag().endswith("@sha256:" + "b" * 64)
    builds = [cmd for cmd in commands if cmd[:2] == ("docker", "build")]
    assert len(builds) == 1
    assert builds[0][builds[0].index("--file") + 1] == str(dockerfile)
    assert builds[0][-1] == str(dockerfile.parent)
