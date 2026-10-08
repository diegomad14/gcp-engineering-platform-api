"""GitHub deployment configuration fails before credentials or remote effects."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _steps(filename):
    workflow = yaml.safe_load((WORKFLOWS / filename).read_text())
    return next(iter(workflow["jobs"].values()))["steps"]


def _environment():
    return {
        "PATH": os.environ["PATH"],
        "RELEASE_EXECUTOR_IMAGE": "registry.example/release@sha256:" + "a" * 64,
        "PLATFORM_API_URL": "https://platform.example",
        "RELEASE_SIGNING_PUBLIC_KEY": "test-public-verification-key",
        "RELEASE_WIF_PROVIDER": "test-wif-provider",
        "RELEASE_SERVICE_ACCOUNT": "release@example.iam.gserviceaccount.com",
    }


@pytest.mark.parametrize("filename", ["platform-deploy.yml", "platform-rollback.yml"])
def test_deployment_preflight_precedes_checkout_authorization_and_cloud_auth(filename):
    steps = _steps(filename)
    assert steps[0]["name"] == "Validate release executor configuration"
    assert "google-github-actions/auth@" in str(steps[1:])
    assert "verify-release-authorization" in str(steps[1:])
    assert '"$RELEASE_EXECUTOR_IMAGE" --service' in str(steps)
    for step in steps:
        assert "${{ vars.ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE }}" not in step.get(
            "run", ""
        )


@pytest.mark.parametrize("filename", ["platform-deploy.yml", "platform-rollback.yml"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("RELEASE_EXECUTOR_IMAGE", ""),
        ("RELEASE_EXECUTOR_IMAGE", "registry.example/release:latest"),
        ("RELEASE_EXECUTOR_IMAGE", "registry.example/release@sha256:short"),
        ("RELEASE_EXECUTOR_IMAGE", "registry.example/release @sha256:" + "a" * 64),
        ("RELEASE_EXECUTOR_IMAGE", "registry.example/release@sha256:" + "z" * 64),
        ("PLATFORM_API_URL", "http://platform.example"),
        ("PLATFORM_API_URL", ""),
        ("RELEASE_SIGNING_PUBLIC_KEY", ""),
        ("RELEASE_WIF_PROVIDER", ""),
        ("RELEASE_SERVICE_ACCOUNT", ""),
    ],
)
def test_deployment_preflight_rejects_incomplete_configuration(filename, key, value):
    environment = _environment() | {key: value}
    result = subprocess.run(
        ["bash", "-c", _steps(filename)[0]["run"]],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("filename", ["platform-deploy.yml", "platform-rollback.yml"])
def test_deployment_preflight_accepts_digest_and_required_configuration(filename):
    result = subprocess.run(
        ["bash", "-c", _steps(filename)[0]["run"]],
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
