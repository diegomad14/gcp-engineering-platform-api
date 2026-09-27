"""Fault injection for independent runtime activation and resource recovery."""

import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest

from eng_platform_api.config import config
from eng_platform_api.services import catalog, github_actions_quota


@pytest.fixture
def engine(monkeypatch):
    path = Path(__file__).parents[1] / "docker/release-executor/release_executor.py"
    spec = importlib.util.spec_from_file_location("independent_executor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in {
        "CGM_SERVICE": "cgm-artemis-mcp-worker",
        "CGM_REGION": "us-central1",
        "CGM_PROJECT_ID": "p",
        "CGM_RELEASE_SHA": "b" * 40,
        "CGM_DEPLOYMENT_ID": "deployment",
        "CGM_EVIDENCE_BUCKET": "evidence",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "emit", lambda *a, **kw: None)
    return module


@pytest.fixture
def lifecycle(engine, monkeypatch):
    state = {"traffic": {"old": 100}, "grants": []}
    monkeypatch.setattr(engine, "_traffic", lambda service, *a: dict(state["traffic"]))

    def traffic(service, region, project, target):
        assert service == "cgm-artemis-mcp-worker"
        state["traffic"] = dict(target)

    monkeypatch.setattr(engine, "_set_traffic", traffic)
    monkeypatch.setattr(
        engine,
        "revision_runtime",
        lambda revision: {
            "APP_RELEASE_SHA": "a" * 40,
            "APP_RELEASE_SCOPE": "runtime-v1",
        },
    )
    monkeypatch.setattr(
        engine, "_deploy_candidate", lambda *a: ("new", "https://candidate")
    )
    monkeypatch.setattr(engine, "_service_url", lambda *a: "https://production")
    monkeypatch.setattr(
        engine,
        "runtime_grant",
        lambda action, sha: state["grants"].append((action, sha)),
    )
    return engine, state


def test_candidate_failure_leaves_all_authorizations_and_traffic(
    lifecycle, monkeypatch
):
    engine, state = lifecycle
    monkeypatch.setattr(
        engine,
        "runtime_ready",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("wrong identity")),
    )
    with pytest.raises(RuntimeError, match="wrong identity"):
        engine.deploy_runtime("image")
    assert state == {"traffic": {"old": 100}, "grants": []}


def test_success_preserves_prior_grant_during_drain(lifecycle, monkeypatch):
    engine, state = lifecycle
    monkeypatch.setattr(engine, "runtime_ready", lambda *a, **kw: None)
    assert engine.deploy_runtime("image")["production_revision"] == "new"
    assert state["traffic"] == {"new": 100}
    assert state["grants"] == [("authorize", "b" * 40), ("retire", "a" * 40)]


def test_production_failure_restores_only_affected_runtime(lifecycle, monkeypatch):
    engine, state = lifecycle

    def ready(url, **kwargs):
        if url == "https://production":
            raise RuntimeError("readiness failed")

    monkeypatch.setattr(engine, "runtime_ready", ready)
    with pytest.raises(engine.AutomaticRollback, match="readiness failed"):
        engine.deploy_runtime("image")
    assert state["traffic"] == {"old": 100}
    assert state["grants"] == [
        ("authorize", "b" * 40),
        ("authorize", "a" * 40),
        ("revoke", "b" * 40),
    ]


def test_rollback_rejects_pre_migration_revision(engine, monkeypatch):
    monkeypatch.setenv("CGM_TARGET_REVISION", "old-legacy")
    monkeypatch.setattr(
        engine, "revision_runtime", lambda *a: {"APP_RELEASE_SHA": "a" * 40}
    )
    with pytest.raises(RuntimeError, match="compatible"):
        engine.rollback_runtime()


@pytest.mark.parametrize(
    "code,reason,accepted",
    [
        (503, "release_not_activated", True),
        (500, "release_not_activated", False),
        (503, "activation_store_unavailable", False),
    ],
)
def test_candidate_readiness_requires_structured_identity(
    engine, monkeypatch, code, reason, accepted
):
    payload = {
        "detail": {
            "resource": "cgm-artemis-mcp-worker",
            "release_sha": "b" * 40,
            "scope": "runtime-v1",
            "status": "unready",
            "reason": reason,
        }
    }

    def request(*a, **kw):
        raise urllib.error.HTTPError(
            "https://candidate",
            code,
            "error",
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )

    monkeypatch.setattr(engine.urllib.request, "urlopen", request)
    if accepted:
        engine.runtime_ready("https://candidate", candidate=True)
    else:
        with pytest.raises(RuntimeError):
            engine.runtime_ready("https://candidate", candidate=True)


def test_paused_schedulers_remain_paused(engine, monkeypatch):
    monkeypatch.setenv("CGM_SERVICE", "cgm-artemis-wm-sweep-worker")
    calls = []

    def command(action, name, *args):
        calls.append((action, name))
        return json.dumps({"state": "ENABLED" if name.endswith("hour") else "PAUSED"})

    monkeypatch.setattr(engine, "scheduler_command", command)
    active = engine.pause_job_schedules()
    engine.resume_job_schedules(active)
    assert active == ["cgm-artemis-wm-sweep-hour"]
    assert all(name.endswith("hour") for action, name in calls if action != "describe")


def test_twelve_resources_require_complete_cloud_build_configuration(monkeypatch):
    services = [
        row
        for row in catalog.get_services().services
        if row.repository == "diegomad14/cgm-artemis-api"
    ]
    assert len(services) == 12
    build = config.cloud_build
    monkeypatch.setattr(build, "enabled", True)
    monkeypatch.setattr(
        build, "enabled_services", tuple(row.service_name for row in services)
    )
    monkeypatch.setattr(
        build,
        "repositories",
        {
            row.service_name: "projects/p/locations/r/connections/c/repositories/artemis"
            for row in services
        },
    )
    for field in (
        "project_id",
        "service_account",
        "callback_service_account",
        "evidence_bucket",
    ):
        monkeypatch.setattr(build, field, "configured")
    monkeypatch.setattr(build, "executor_image", "image@sha256:" + "a" * 64)
    for service in services:
        assert not catalog.deployment_blockers(service)
        assert github_actions_quota.should_use_cloud_build(
            service.service_name, service.repository
        )
    monkeypatch.setattr(build, "repositories", {})
    assert "connected repository" in " ".join(catalog.deployment_blockers(services[0]))
    with pytest.raises(ValueError, match="connected repository"):
        github_actions_quota.should_use_cloud_build(
            services[0].service_name, services[0].repository
        )


def test_scheduler_destinations_preserve_literal_colon():
    path = (
        Path(__file__).parents[1] / "scripts/configure_artemis_independent_releases.py"
    )
    spec = importlib.util.spec_from_file_location("configure_artemis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for job in module.SCHEDULES.values():
        assert (
            module.job_uri(job)
            == f"https://run.googleapis.com/v2/projects/cgm-assistant-prod/locations/us-central1/jobs/{job}:run"
        )
    current = {
        "ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES": "existing",
        "ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON": '{"existing":"preserved"}',
    }
    result = module.routing_variables(current, ["cgm-artemis-mcp-worker"])
    assert "existing" in result["ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES"]
    assert (
        json.loads(result["ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON"])["existing"]
        == "preserved"
    )


def test_hook_error_identifies_hook_without_secrets(engine, monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(engine, "HOOKS_ROOT", tmp_path)
    (tmp_path / "corporate_window_artemis.sh").write_text("exit 1")
    monkeypatch.setenv("SOME_SECRET", "sensitive-value")

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1,
            args,
            stderr="pair mismatch sensitive-value Bearer abc123 password=private",
        )

    monkeypatch.setattr(engine, "run", fail)
    with pytest.raises(RuntimeError) as exc:
        engine.hook("corporate_window_artemis")
    assert "corporate_window_artemis" in str(exc.value) and "pair mismatch" in str(
        exc.value
    )
    assert not any(
        value in str(exc.value) for value in ("sensitive-value", "abc123", "private")
    )
