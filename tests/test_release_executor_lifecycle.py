"""Release lifecycle contracts are exercised without submitting Cloud Builds."""

import importlib.util
from pathlib import Path

import pytest

from eng_platform_api.services.catalog import get_service
from eng_platform_api.services.release_profiles import profile_for


@pytest.fixture
def engine():
    path = (
        Path(__file__).resolve().parents[1]
        / "docker/release-executor/release_executor.py"
    )
    spec = importlib.util.spec_from_file_location("release_executor_lifecycle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _environment(monkeypatch, profile):
    for name, value in {
        "CGM_PROFILE": profile,
        "CGM_SERVICE": profile,
        "CGM_REGION": "us-central1",
        "CGM_PROJECT_ID": "test-project",
        "CGM_REQUEST_FINGERPRINT": "f" * 64,
    }.items():
        monkeypatch.setenv(name, value)


def _setup_deploy(engine, monkeypatch, *, profile, failing_phase=""):
    _environment(monkeypatch, profile)
    events = []
    traffic = {"old-revision": 100}
    monkeypatch.setattr(engine, "verify_hooks", lambda: events.append("verify_hooks"))
    monkeypatch.setattr(engine, "_traffic", lambda *_: dict(traffic))
    monkeypatch.setattr(
        engine,
        "_deploy_candidate",
        lambda *_: ("new-revision", "https://candidate.example"),
    )
    monkeypatch.setattr(engine, "_smoke", lambda *_: events.append("smoke"))
    monkeypatch.setattr(engine, "_service_url", lambda *_: "https://production.example")
    monkeypatch.setattr(engine, "emit", lambda *_args, **_kwargs: None)

    def set_traffic(_service, _region, _project, value):
        events.append(("traffic", value))
        traffic.clear()
        traffic.update(value)

    def run_hooks(phase):
        events.append(phase)
        if phase == failing_phase:
            raise RuntimeError("hook failed")

    monkeypatch.setattr(engine, "_set_traffic", set_traffic)
    monkeypatch.setattr(engine, "run_hooks", run_hooks)
    return events, traffic


def test_corporate_activation_waits_until_after_promotion(engine, monkeypatch):
    events, _ = _setup_deploy(engine, monkeypatch, profile="cgm-sanplat-web")
    result = engine.deploy("repo/image@sha256:" + "a" * 64)
    assert result["production_revision"] == "new-revision"
    assert events.index("pre_promote_hooks") < events.index(
        ("traffic", {"new-revision": 100})
    )
    assert events.index("post_promote_hooks") > events.index(
        ("traffic", {"new-revision": 100})
    )


def test_bot_hooks_receive_previous_api_traffic_snapshot(engine, monkeypatch):
    _setup_deploy(engine, monkeypatch, profile="cgm-bot-api")
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    engine.deploy("repo/image@sha256:" + "b" * 64)
    assert engine.os.environ["CGM_PREVIOUS_TRAFFIC"] == '{"old-revision": 100}'


def test_corporate_failure_uses_paired_recovery_not_traffic_only(engine, monkeypatch):
    events, traffic = _setup_deploy(
        engine,
        monkeypatch,
        profile="cgm-sanplat-web",
        failing_phase="post_promote_hooks",
    )

    def recover(name):
        events.append(("recover", name))
        traffic.clear()
        traffic.update({"maintenance-revision": 100})

    monkeypatch.setattr(engine, "hook", recover)
    with pytest.raises(engine.AutomaticRollback, match="prior resources"):
        engine.deploy("repo/image@sha256:" + "a" * 64)
    assert ("recover", "recover_corporate_web") in events
    assert ("traffic", {"old-revision": 100}) not in events
    assert traffic == {"maintenance-revision": 100}


def test_failed_paired_recovery_is_not_reported_as_rolled_back(engine, monkeypatch):
    _setup_deploy(
        engine,
        monkeypatch,
        profile="cgm-sanplat-api",
        failing_phase="pre_promote_hooks",
    )
    monkeypatch.setattr(
        engine,
        "hook",
        lambda *_: (_ for _ in ()).throw(RuntimeError("recovery failed")),
    )
    with pytest.raises(RuntimeError, match="could not be verified"):
        engine.deploy("repo/image@sha256:" + "a" * 64)


def test_corporate_user_rollback_invokes_paired_hook(engine, monkeypatch):
    _environment(monkeypatch, "cgm-sanplat-api")
    monkeypatch.setenv("CGM_TARGET_REVISION", "old-revision")
    events = []
    monkeypatch.setattr(engine, "verify_hooks", lambda: None)
    monkeypatch.setattr(engine, "hook", lambda name: events.append(name))
    monkeypatch.setattr(engine, "_traffic", lambda *_: {"maintenance-revision": 100})
    monkeypatch.setattr(engine, "_service_url", lambda *_: "https://production.example")
    monkeypatch.setattr(engine, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        engine,
        "run",
        lambda *_args, **_kwargs: pytest.fail("traffic-only rollback is unsafe"),
    )
    result = engine.rollback()
    assert events == ["rollback_corporate_api"]
    assert result["production_revision"] == "maintenance-revision"


def test_missing_allowlisted_hook_fails_before_build(engine, monkeypatch, tmp_path):
    _environment(monkeypatch, "cgm-bot-api")
    monkeypatch.setattr(engine, "HOOKS_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="absent from exact commit"):
        engine.verify_hooks()


@pytest.mark.parametrize(
    "profile",
    ["cgm-sanplat-api", "cgm-artemis-api", "cgm-artemis-wm-sweep-worker"],
)
def test_api_derived_candidates_bind_release_sha_in_runtime(
    engine, monkeypatch, profile
):
    _environment(monkeypatch, profile)
    monkeypatch.setenv("CGM_RELEASE_SHA", "a" * 40)
    assert engine.candidate_env_args() == [
        "--update-env-vars",
        "APP_RELEASE_SHA=" + "a" * 40,
    ]


def test_web_candidate_does_not_inherit_api_release_env(engine, monkeypatch):
    _environment(monkeypatch, "cgm-artemis-web")
    assert engine.candidate_env_args() == []


def test_platform_profile_hashes_stay_compatible_with_the_live_executor(engine):
    legacy_hashes = {
        "eng-platform-api": (
            "b29b8c450169374047dcddbb019eb27264ceeabf41272306199eb0d70632dbf0"
        ),
        "eng-platform-web": (
            "38743606c97948b57645c990b15fe4d841770a0d1b033dc82115d0ae9eccd961"
        ),
        "communications-ms": (
            "161ca2094fd1f9cd663e49def35a4ac755122bfafe42aa8bac41862a74d1213e"
        ),
    }
    for name, digest in legacy_hashes.items():
        service = get_service(name)
        assert service is not None
        assert profile_for(service).fingerprint() == digest
        assert engine.profile_fingerprint(name) == digest
