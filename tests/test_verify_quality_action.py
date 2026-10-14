"""Authorization boundaries of the SHA-pinned release evidence verifier."""

import importlib.util
import io
import json
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.pin_artemis_release_catalog import REPOSITORY, SERVICES


SHA = "159851bcb9cfdc81d5e6b0b1612bcc14f461d253"
WORKER = "cgm-artemis-mcp-worker"


@pytest.fixture
def verifier():
    path = Path(__file__).parents[1] / ".github/actions/verify-quality/verify.py"
    spec = importlib.util.spec_from_file_location("verify_quality_action", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report():
    return {
        "service_name": "cgm-artemis-api",
        "repository": REPOSITORY,
        "commit_sha": SHA,
        "policy_version": "oss-v2",
        "quality_gate_status": "PASSED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [{"status": "PASSED"}],
    }


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_api_evidence_authorizes_only_reviewed_artemis_services(
    verifier, report, service
):
    verifier.validate(report, service, REPOSITORY, SHA)


def test_registry_contains_exact_reviewed_derivatives(verifier):
    assert verifier.EVIDENCE_OWNERS == {
        (REPOSITORY, service): "cgm-artemis-api"
        for service in SERVICES - {"cgm-artemis-api"}
    }


@pytest.mark.parametrize(
    "service,repository,overrides",
    [
        ("cgm-artemis-unlisted-worker", REPOSITORY, {}),
        ("cgm-artemis-web", REPOSITORY, {}),
        (WORKER, "other/cgm-artemis-api", {}),
        (WORKER, "diegomad14/cgm-sanplat-api", {}),
        (WORKER, REPOSITORY, {"service_name": "cgm-sanplat-api"}),
        (WORKER, REPOSITORY, {"service_name": "cgm-artemis-sync-worker"}),
        (WORKER, REPOSITORY, {"repository": "other/cgm-artemis-api"}),
        (WORKER, REPOSITORY, {"repository": "diegomad14/cgm-sanplat-api"}),
        (WORKER, REPOSITORY, {"commit_sha": "b" * 40}),
        (WORKER, REPOSITORY, {"policy_version": "oss-v1"}),
        (WORKER, REPOSITORY, {"quality_gate_status": "FAILED"}),
        (WORKER, REPOSITORY, {"generated_at": "2025-01-01T00:00:00Z"}),
        (WORKER, REPOSITORY, {"generated_at": "2099-01-01T00:00:00Z"}),
        (WORKER, REPOSITORY, {"checks": []}),
        (WORKER, REPOSITORY, {"checks": [{"status": "FAILED"}]}),
    ],
)
def test_wrong_identity_and_quality_fail_closed(
    verifier, report, service, repository, overrides
):
    with pytest.raises(ValueError):
        verifier.validate(report | overrides, service, repository, SHA)


def test_missing_mapping_rejects_api_report(verifier, report, monkeypatch):
    monkeypatch.setattr(verifier, "EVIDENCE_OWNERS", {})
    with pytest.raises(ValueError, match="identity"):
        verifier.validate(report, WORKER, REPOSITORY, SHA)


def test_caller_cannot_supply_or_expand_mapping(
    verifier, report, monkeypatch, tmp_path
):
    monkeypatch.setenv("CGM_QUALITY_EVIDENCE_SERVICES", "attacker-api,cgm-artemis-api")
    monkeypatch.setenv("CGM_REPOSITORY_ALIASES", "attacker/repo")
    monkeypatch.setenv("QUALITY_EVIDENCE_OWNER", "attacker-api")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evidence-owners.json").write_text(json.dumps({WORKER: "attacker-api"}))
    verifier.validate(report, WORKER, REPOSITORY, SHA)
    with pytest.raises(ValueError, match="identity"):
        verifier.validate(
            report | {"service_name": "attacker-api"}, WORKER, REPOSITORY, SHA
        )
    with pytest.raises(ValueError, match="identity"):
        verifier.validate(report, "cgm-artemis-unlisted-worker", REPOSITORY, SHA)


@pytest.fixture
def tagged_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "release",
        ],
        check=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "tag", "v1.51.15"], check=True)
    return sha


def test_release_tag_must_resolve_to_exact_evidence_sha(verifier, tagged_checkout):
    verifier.verify_release_tag("v1.51.15", tagged_checkout)
    with pytest.raises(ValueError, match="does not match"):
        verifier.verify_release_tag("v1.51.15", "b" * 40)
    with pytest.raises(subprocess.CalledProcessError):
        verifier.verify_release_tag("v1.51.16", tagged_checkout)


@pytest.mark.parametrize("tag", ["main", "--help", "v1.51.15^{tree}", "v1.51.15\n"])
def test_invalid_release_tag_is_rejected(verifier, tag):
    with pytest.raises(ValueError, match="semantic"):
        verifier.verify_release_tag(tag, SHA)


@pytest.fixture
def action_env(monkeypatch):
    for key, value in {
        "QUALITY_URL": "https://platform.example",
        "QUALITY_SERVICE": WORKER,
        "QUALITY_SHA": SHA,
        "QUALITY_WAIT": "0",
        "QUALITY_TAG": "",
        "GITHUB_REPOSITORY": REPOSITORY,
    }.items():
        monkeypatch.setenv(key, value)


def test_action_fetches_target_endpoint_and_validates_real_tag(
    verifier, report, action_env, tagged_checkout, monkeypatch, capsys
):
    monkeypatch.setenv("QUALITY_SHA", tagged_checkout)
    monkeypatch.setenv("QUALITY_TAG", "v1.51.15")
    requests = []

    def fetch(endpoint, timeout):
        requests.append((endpoint, timeout))
        return io.StringIO(json.dumps(report | {"commit_sha": tagged_checkout}))

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fetch)
    verifier.main()
    assert requests == [
        (
            f"https://platform.example/api/quality/services/{WORKER}/commits/{tagged_checkout}?for_release=true",
            20,
        )
    ]
    assert (
        f"Verified oss-v2 evidence: {WORKER} {tagged_checkout}"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("QUALITY_URL", "http://platform.example"),
        ("QUALITY_SERVICE", "bad/name"),
        ("QUALITY_SHA", "123"),
    ],
)
def test_action_rejects_invalid_inputs_before_fetch(
    verifier, action_env, monkeypatch, key, value
):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="HTTPS"):
        verifier.main()


@pytest.mark.parametrize("status", [404, 409, 500])
def test_action_does_not_retry_rejected_or_expired_evidence(
    verifier, action_env, monkeypatch, status
):
    def fetch(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://platform.example", status, "rejected", {}, None
        )

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fetch)
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        verifier.main()


def test_action_waits_only_for_missing_evidence(
    verifier, report, action_env, monkeypatch
):
    monkeypatch.setenv("QUALITY_WAIT", "30")
    clock = iter([0, 0, 1])
    monkeypatch.setattr(verifier.time, "monotonic", lambda: next(clock))
    sleeps = []
    monkeypatch.setattr(verifier.time, "sleep", sleeps.append)
    calls = []

    def fetch(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "https://platform.example", 404, "missing", {}, None
            )
        return io.StringIO(json.dumps(report))

    monkeypatch.setattr(verifier.urllib.request, "urlopen", fetch)
    verifier.main()
    assert len(calls) == 2
    assert sleeps == [15]
