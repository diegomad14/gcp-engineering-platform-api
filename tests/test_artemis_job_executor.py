"""Offline contract for the Artemis Job deploy path (no Cloud Build calls)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eng_platform_api.services.catalog import get_service
from eng_platform_api.services.release_profiles import profile_for
from eng_platform_api.services import cloud_build
from eng_platform_api.models import DeploymentItem


_ENGINE = (
    Path(__file__).resolve().parents[1] / "docker/release-executor/release_executor.py"
)
spec = importlib.util.spec_from_file_location("artemis_release_executor", _ENGINE)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def test_job_deploy_snapshots_before_mutation_and_does_not_execute(monkeypatch):
    events = []
    state = {"definition": "before", "image": "repo/old@sha256:old"}
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-fnd-observation-worker")
    monkeypatch.setenv("CGM_PROFILE", "cgm-artemis-fnd-observation-worker")
    monkeypatch.setenv("CGM_REGION", "us-central1")
    monkeypatch.setenv("CGM_PROJECT_ID", "cgm-assistant-prod")
    monkeypatch.setenv("CGM_EVIDENCE_BUCKET", "evidence-bucket")
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    monkeypatch.setattr(engine, "_job_definition", lambda: state["definition"])
    monkeypatch.setattr(engine, "_job_operational_spec", lambda definition: definition)
    monkeypatch.setattr(engine, "_job_image", lambda: state["image"])
    monkeypatch.setattr(engine, "emit", lambda *args, **kwargs: events.append(args))
    monkeypatch.setattr(
        engine,
        "_save_job_definition",
        lambda value: events.append(("snapshot", value)) or "job-" + "a" * 64,
    )

    def fake_run(*args, **kwargs):
        events.append(args)
        if args[:4] == ("gcloud", "run", "jobs", "update"):
            state.update(definition="after", image="repo/new@sha256:new")
        return ""

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine.deploy_job("repo/new@sha256:new")
    assert result["production_revision"] == "job-" + "a" * 64
    assert events.index(("snapshot", "before")) < next(
        index
        for index, event in enumerate(events)
        if event[:4] == ("gcloud", "run", "jobs", "update")
    )
    assert not any("execute" in event for event in events)


def test_job_deploy_restores_definition_on_update_failure(monkeypatch):
    monkeypatch.setenv("CGM_PROFILE", "cgm-artemis-fnd-observation-worker")
    restored = []
    monkeypatch.setattr(engine, "_job_definition", lambda: "before")
    monkeypatch.setattr(engine, "_job_operational_spec", lambda definition: definition)
    monkeypatch.setattr(engine, "_job_image", lambda: "repo/old@sha256:old")
    monkeypatch.setattr(engine, "_save_job_definition", lambda value: "job-" + "a" * 64)
    monkeypatch.setattr(engine, "_replace_job", restored.append)
    monkeypatch.setattr(engine, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        engine,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("update failed")),
    )
    with pytest.raises(engine.AutomaticRollback):
        engine.deploy_job("repo/new@sha256:new")
    assert restored == ["before"]


def test_job_snapshot_identity_is_content_addressed(monkeypatch):
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-wm-sweep-worker")
    monkeypatch.setenv("CGM_EVIDENCE_BUCKET", "evidence-bucket")
    with pytest.raises(RuntimeError, match="fingerprint"):
        engine._job_snapshot_uri("../another-service")
    assert engine._job_snapshot_uri("job-" + "a" * 64).endswith(
        "/cgm-artemis-wm-sweep-worker/job-" + "a" * 64 + ".yaml"
    )


def test_job_definition_validation_rejects_other_resource(monkeypatch):
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-wm-sweep-worker")
    with pytest.raises(RuntimeError, match="authorized resource"):
        engine._job_operational_spec(
            "kind: Job\nmetadata:\n  name: cgm-sanplat-wm-sweep-worker\nspec: {}\n"
        )


def test_all_artemis_profile_hashes_match_the_pinned_executor():
    for name in engine.PROFILE_SPECS:
        if not name.startswith("cgm-artemis-"):
            continue
        service = get_service(name)
        assert service is not None
        assert profile_for(service).fingerprint() == engine.profile_fingerprint(name)
        assert service.deployment.build_context == "."
        assert service.deployment.dockerfile_path == "Dockerfile"


def test_job_reconciliation_requires_summary_and_live_image(monkeypatch):
    from google.cloud import run_v2, storage

    service = get_service("cgm-artemis-wm-sweep-worker")
    item = DeploymentItem(
        id="artemis-job-test",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
    )
    image = "us-central1-docker.pkg.dev/p/r/i@sha256:" + "b" * 64
    summary = {
        "deployment_id": item.id,
        "fingerprint": cloud_build.fingerprint(item, service),
        "service": item.service_name,
        "sha": item.sha,
        "tag": item.tag,
        "production_revision": "job-" + "c" * 64,
        "image_digest": image,
    }
    blob = SimpleNamespace(download_as_text=lambda: json.dumps(summary))
    bucket = SimpleNamespace(blob=lambda name: blob)
    monkeypatch.setattr(
        storage, "Client", lambda **kwargs: SimpleNamespace(bucket=lambda name: bucket)
    )
    monkeypatch.setattr(
        run_v2,
        "JobsClient",
        lambda: SimpleNamespace(
            get_job=lambda **kwargs: SimpleNamespace(
                template=SimpleNamespace(
                    template=SimpleNamespace(containers=[SimpleNamespace(image=image)])
                )
            )
        ),
    )
    cloud_build._reconcile_job(item, service, {"status": "SUCCESS"})
    assert item.status == "SUCCEEDED"
    assert item.production_revision == summary["production_revision"]
    summary["fingerprint"] = "wrong"
    with pytest.raises(cloud_build.CloudBuildError, match="summary identity"):
        cloud_build._reconcile_job(item, service, {"status": "SUCCESS"})


def test_private_worker_smoke_uses_base_service_audience(monkeypatch):
    monkeypatch.setenv("CGM_PRIVATE_RUNTIME", "true")
    monkeypatch.setenv("CGM_HEALTH_PATH", "/ready")
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-sync-worker")
    monkeypatch.setenv("CGM_REGION", "us-central1")
    monkeypatch.setenv("CGM_PROJECT_ID", "cgm-assistant-prod")
    monkeypatch.setattr(engine, "_service_url", lambda *args: "https://base.example")
    called = []
    monkeypatch.setattr(
        engine, "run", lambda *args: called.append(args) or "identity-token"
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def open_request(request, **kwargs):
        assert request.full_url == "https://candidate.example/ready"
        assert request.get_header("Authorization") == "Bearer identity-token"
        return Response()

    monkeypatch.setattr(engine.urllib.request, "urlopen", open_request)
    engine._smoke("https://candidate.example")
    assert called == [
        ("gcloud", "auth", "print-identity-token", "--audiences=https://base.example")
    ]


@pytest.mark.parametrize(
    ("service", "worker_type"),
    [
        ("cgm-artemis-sync-worker", "sync"),
        ("cgm-artemis-clock-sync-worker", "clock-sync"),
        ("cgm-artemis-data-recovery-worker", "data-recovery"),
        ("cgm-artemis-fnd-ip-sync-worker", "fnd-ip-sync"),
    ],
)
def test_artemis_worker_profiles_pin_one_task_type(monkeypatch, service, worker_type):
    monkeypatch.setenv("CGM_SERVICE", service)
    monkeypatch.setenv("CGM_PROFILE", service)
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    assert engine.candidate_env_args() == [
        "--update-env-vars",
        f"APP_RELEASE_SHA={'a' * 40},ARTEMIS_WORKER_TYPE={worker_type}",
    ]


def test_artemis_web_uses_the_tag_as_app_version():
    service = get_service("cgm-artemis-web")
    assert service is not None
    assert profile_for(service).build_args == (("APP_VERSION", "{tag}"),)
    assert engine.PROFILE_SPECS["cgm-artemis-web"]["build_args"] == [
        ["APP_VERSION", "{tag}"]
    ]


def test_artemis_api_candidate_disables_embedded_background_workers(monkeypatch):
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-api")
    monkeypatch.setenv("CGM_PROFILE", "cgm-artemis-api")
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    assert engine.candidate_env_args() == [
        "--update-env-vars",
        f"APP_RELEASE_SHA={'a' * 40},APP_BACKGROUND_TASKS_ENABLED=false",
    ]


def test_artemis_dispatcher_candidate_routes_only_to_separate_workers(monkeypatch):
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-job-dispatcher")
    monkeypatch.setenv("CGM_PROFILE", "cgm-artemis-job-dispatcher")
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("CGM_REGION", "us-central1")
    monkeypatch.setenv("CGM_PROJECT_ID", "cgm-assistant-prod")
    resolved = []

    def service_url(name, region, project):
        resolved.append((name, region, project))
        return f"https://{name}-{'a' * 10}-uc.a.run.app"

    monkeypatch.setattr(engine, "_service_url", service_url)
    actual = engine.candidate_env_args()
    assert actual[0] == "--update-env-vars"
    values = dict(item.split("=", 1) for item in actual[1].split(","))
    assert values["APP_RELEASE_SHA"] == "a" * 40
    assert values["JOB_WORKER_PREFIX"] == "cgm-artemis"
    assert values["JOB_TASK_OIDC_SERVICE_ACCOUNT"] == (
        "artemis-tasks-invoker@cgm-assistant-prod.iam.gserviceaccount.com"
    )
    assert values["ARTEMIS_SYNC_QUEUE"] == "cgm-artemis-sync"
    assert values["ARTEMIS_CLOCK_SYNC_QUEUE"] == "cgm-artemis-clock-sync"
    assert values["ARTEMIS_DATA_RECOVERY_QUEUE"] == "cgm-artemis-data-recovery"
    assert values["ARTEMIS_FND_IP_SYNC_QUEUE"] == "cgm-artemis-fnd-ip-sync"
    assert resolved == [
        ("cgm-artemis-sync-worker", "us-central1", "cgm-assistant-prod"),
        ("cgm-artemis-clock-sync-worker", "us-central1", "cgm-assistant-prod"),
        ("cgm-artemis-data-recovery-worker", "us-central1", "cgm-assistant-prod"),
        ("cgm-artemis-fnd-ip-sync-worker", "us-central1", "cgm-assistant-prod"),
    ]


def test_artemis_dispatcher_rejects_an_unallowlisted_dynamic_route(monkeypatch):
    service = "cgm-artemis-job-dispatcher"
    monkeypatch.setenv("CGM_SERVICE", service)
    monkeypatch.setenv("CGM_PROFILE", service)
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("CGM_REGION", "us-central1")
    monkeypatch.setenv("CGM_PROJECT_ID", "cgm-assistant-prod")
    monkeypatch.setattr(
        engine,
        "_service_url",
        lambda name, _region, _project: f"https://{name}-{'a' * 10}-uc.a.run.app",
    )
    monkeypatch.setitem(
        engine.PROFILE_SPECS[service],
        "candidate_env_vars",
        engine.PROFILE_SPECS[service]["candidate_env_vars"]
        + [["ARTEMIS_UNLISTED_WORKER_URL", "{service_uri:cgm-artemis-unknown-worker}"]],
    )
    with pytest.raises(RuntimeError, match="service URL is not allowed"):
        engine.candidate_env_args()


def test_artemis_cloud_build_contract_is_economical(monkeypatch):
    from eng_platform_api.config import config

    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(config.cloud_build, "project_id", "cgm-assistant-prod")
    monkeypatch.setattr(
        config.cloud_build,
        "service_account",
        "projects/p/serviceAccounts/deploy@p.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        config.cloud_build, "executor_image", "repo/executor@sha256:" + "f" * 64
    )
    for name in engine.PROFILE_SPECS:
        if not name.startswith("cgm-artemis-"):
            continue
        service = get_service(name)
        monkeypatch.setattr(
            config.cloud_build,
            "repositories",
            {name: "projects/p/locations/us-central1/connections/c/repositories/r"},
        )
        item = DeploymentItem(
            id="economy-test",
            service_name=name,
            repository=service.repository,
            tag="v1.0.0",
            sha="a" * 40,
        )
        request = cloud_build.build_request(item, service)
        assert request["options"]["machineType"] == "E2_STANDARD_2"
        assert request["options"]["logging"] == "CLOUD_LOGGING_ONLY"
        assert len(request["steps"]) == 1
        assert request["steps"][0]["name"] == config.cloud_build.executor_image
        assert not any(
            word in json.dumps(request).lower()
            for word in ("pytest", "semantic-release", "postgres", "scanner")
        )
        assert service.deployment.image_name in {"cgm-artemis-api", "cgm-artemis-web"}
