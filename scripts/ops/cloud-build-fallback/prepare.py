#!/usr/bin/env python3
"""Prepare a reviewable, exact-commit Python fallback build; never submit it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
PLATFORM = HERE.parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DIGEST_IMAGE = re.compile(r"^[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}$")
RUNTIME_IMAGE = re.compile(
    r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9./_-]+:[a-zA-Z0-9_.-]+$"
)
DOCKER = "gcr.io/cloud-builders/docker"
SDK = "gcr.io/google.com/cloudsdktool/google-cloud-cli:slim"
XDIST_VERSION = "3.8.0"
BUILD_PROFILES = {"performance": ("E2_HIGHCPU_8", 4), "economy": ("E2_STANDARD_2", 2)}
CATALOG_PATH = PLATFORM / "src/eng_platform_api/static_examples/mock_catalog.json"
TIMEOUT_SECONDS = 1256


def service_config(service: str) -> dict:
    services = json.loads(CATALOG_PATH.read_text())["services"]
    matches = [item for item in services if item["service_name"] == service]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous catalog service")
    config = matches[0]
    quality = config.get("quality", {})
    if (
        not quality.get("enabled")
        or quality.get("profile") != "python"
        or quality.get("policy_version") != "oss-v2"
    ):
        raise ValueError(
            "Fallback v1 requires a Python service with enabled oss-v2 quality"
        )
    if not config.get("deployment", {}).get("enabled", True):
        raise ValueError("Deployment is disabled for this service")
    return config


def git(source: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603: argument list, no shell
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def validate(args: argparse.Namespace) -> None:
    config = service_config(args.service)
    args.repository = config["repository"]
    args.coverage_threshold = config["quality"]["coverage_threshold"]
    args.machine_type, args.workers = BUILD_PROFILES[args.profile]
    args.distribution = "worksteal"
    args.tooling_image = ""
    args.catalog_service = config
    args.attempt_id = uuid.uuid4().hex
    deployment = config["deployment"]
    args.image = (
        f"{config['region']}-docker.pkg.dev/{config['project_id']}/"
        f"{deployment['artifact_repository']}/{deployment['image_name']}:fallback-{args.sha}-{args.attempt_id}"
    )
    for key in ("sha", "base_sha"):
        if not SHA.fullmatch(getattr(args, key)):
            raise ValueError(
                f"--{key.replace('_', '-')} requires a full lowercase commit SHA"
            )
    if args.sha == args.base_sha:
        raise ValueError("The comparison base must differ from the release SHA")
    if not REPOSITORY.fullmatch(args.repository):
        raise ValueError("--repository requires OWNER/REPOSITORY")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.service):
        raise ValueError("Invalid service name")
    if (
        not RUNTIME_IMAGE.fullmatch(args.image)
        or args.sha not in args.image.rsplit(":", 1)[1]
    ):
        raise ValueError(
            "--image must be an Artifact Registry tag containing the full SHA"
        )
    if not re.fullmatch(
        r"gs://[a-z0-9][a-z0-9._-]+/[A-Za-z0-9/_.-]+", args.evidence_uri
    ):
        raise ValueError("--evidence-uri requires a GCS bucket and safe object prefix")
    if not 0 <= args.coverage_threshold <= 100:
        raise ValueError("--coverage-threshold must be between 0 and 100")
    context = deployment.get("build_context", ".")
    if Path(context).is_absolute() or ".." in Path(context).parts:
        raise ValueError("Catalog build context must stay within the source tree")
    args.evidence_prefix = (
        f"{args.evidence_uri.rstrip('/')}/{args.sha}/{args.attempt_id}"
    )
    args.report_uri = f"{args.evidence_prefix}/quality-report.json"
    args.summary_uri = f"{args.evidence_prefix}/summary.json"
    source = Path(args.source).resolve()
    for commit in (args.sha, args.base_sha):
        if git(source, "rev-parse", f"{commit}^{{commit}}") != commit:
            raise ValueError("Source commit identity mismatch")
    git(source, "merge-base", "--is-ancestor", args.base_sha, args.sha)
    origin = git(source, "remote", "get-url", "origin").removesuffix(".git")
    if origin not in {
        f"https://github.com/{args.repository}",
        f"git@github.com:{args.repository}",
    }:
        raise ValueError("Source origin does not match --repository")
    if Path(args.output).exists():
        raise ValueError("Output already exists; prepare a fresh directory")


def capture(step_id: str, image: str, wait_for: list[str], command: list[str]) -> dict:
    return {
        "id": step_id,
        "name": image,
        "waitFor": wait_for,
        "entrypoint": "bash",
        "args": ["/workspace/capture.sh", step_id, *command],
    }


def build_config(args: argparse.Namespace) -> dict:
    tooling = args.tooling_image or "cgm-fallback-quality"
    steps = [
        capture(
            "source",
            "gcr.io/cloud-builders/git",
            ["-"],
            ["bash", "/workspace/source.sh"],
        ),
        capture(
            "tooling",
            DOCKER,
            ["-"],
            ["docker", "pull", tooling]
            if args.tooling_image
            else ["docker", "build", "-f", "Dockerfile.quality", "-t", tooling, "."],
        ),
        capture("postgres", DOCKER, ["-"], ["bash", "/workspace/postgres.sh"]),
        capture(
            "runtime",
            DOCKER,
            ["source"],
            [
                "docker",
                "build",
                "--label",
                f"org.opencontainers.image.revision={args.sha}",
                "--label",
                f"org.opencontainers.image.source=https://github.com/{args.repository}",
                "-t",
                args.image,
                f"/workspace/release-source/{args.catalog_service['deployment']['build_context']}",
            ],
        ),
        capture(
            "quality",
            DOCKER,
            ["source", "tooling", "postgres"],
            [
                "docker",
                "run",
                "--rm",
                "-v",
                "/workspace:/workspace",
                "--network",
                "container:cgm-fallback-pg",
                "-e",
                "WM_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:5432/wm_test",
                "-e",
                "FND_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:5432/wm_test",
                tooling,
                "bash",
                "/workspace/gate.sh",
            ],
        ),
        capture(
            "verify",
            DOCKER,
            ["quality", "runtime"],
            [
                "docker",
                "run",
                "--rm",
                "-v",
                "/workspace:/workspace",
                "-e",
                "PYTHONPATH=/workspace/policy",
                "-e",
                "FALLBACK_REQUEST_FINGERPRINT=$_REQUEST_FINGERPRINT",
                tooling,
                "python",
                "/workspace/result.py",
                "verify",
            ],
        ),
        capture("publish", DOCKER, ["verify"], ["bash", "/workspace/publish.sh"]),
        {
            "id": "evidence-and-result",
            "name": SDK,
            "waitFor": ["publish"],
            "entrypoint": "bash",
            "args": ["/workspace/finalize.sh"],
            "env": [
                "FALLBACK_BUILD_ID=$BUILD_ID",
                "FALLBACK_PROJECT_ID=$PROJECT_ID",
                "FALLBACK_REQUEST_FINGERPRINT=$_REQUEST_FINGERPRINT",
                "FALLBACK_RELEASE_SHA=$_RELEASE_SHA",
                "FALLBACK_QUALITY_URI=$_QUALITY_URI",
                "FALLBACK_SUMMARY_URI=$_FALLBACK_SUMMARY_URI",
                "FALLBACK_PLATFORM_SHA=$_PLATFORM_SHA",
            ],
        },
    ]
    # Publication is explicit and gate-protected; images[] would push a second time.
    return {
        "timeout": f"{TIMEOUT_SECONDS}s",
        "options": {"machineType": args.machine_type, "logging": "CLOUD_LOGGING_ONLY"},
        "substitutions": {
            "_RELEASE_SHA": args.sha,
            "_QUALITY_URI": args.report_uri,
            "_FALLBACK_SUMMARY_URI": args.summary_uri,
            "_REQUEST_FINGERPRINT": args.request_fingerprint,
            "_PLATFORM_SHA": args.platform_sha,
        },
        "steps": steps,
    }


def scripts(args: argparse.Namespace) -> dict[str, str]:
    install = (
        args.install_command or 'python -m pip install --no-build-isolation -e ".[dev]"'
    )
    install += f"\npython -m pip install pytest-xdist=={XDIST_VERSION}"
    schedule_environment = {
        "PYTHONPATH": "/workspace",
        "FALLBACK_SOURCE_SHA": args.sha,
        "FALLBACK_REPOSITORY": args.repository,
        "FALLBACK_SCHEDULE_EVIDENCE_DIR": "/workspace/evidence",
    }
    if (HERE / "duration_profiles" / f"{args.service}.json").is_file():
        schedule_environment["FALLBACK_DURATION_PROFILE"] = (
            "/workspace/duration-profile.json"
        )
    test = (
        " ".join(
            f"{key}={shlex.quote(value)}" for key, value in schedule_environment.items()
        )
        + f" python -m pytest -p postgres_workers -p pytest_schedule -q -n {args.workers} --dist {args.distribution} "
        "--durations=25 --cov=. --cov-report=json:quality-reports/coverage.json"
    )
    command = shlex.join(
        [
            "python",
            "/workspace/quality/quality_gate.py",
            "--service-name",
            args.service,
            "--repository",
            args.repository,
            "--commit-sha",
            args.sha,
            "--base-sha",
            args.base_sha,
            "--branch",
            args.branch,
            "--profile",
            "python",
            "--coverage-threshold",
            str(args.coverage_threshold),
            "--output",
            "/workspace/quality-report.json",
            "--install-command",
            install,
            "--test-command",
            test,
        ]
    )
    return {
        "source.sh": (
            "set -euo pipefail\nsha256sum --check input-manifest.sha256\n"
            "git clone --no-checkout /workspace/source.bundle /workspace/repo\n"
            f"git -C /workspace/repo checkout --detach {args.sha}\n"
            f'test "$(git -C /workspace/repo rev-parse HEAD)" = {args.sha}\n'
            f"git -C /workspace/repo cat-file -e {args.base_sha}^{{commit}}\n"
            f"git -C /workspace/repo remote set-url origin https://github.com/{args.repository}.git\n"
            "mkdir /workspace/release-source\n"
            f"git -C /workspace/repo archive {args.sha} | tar -x -C /workspace/release-source\n"
            "chmod -R a+rwX /workspace/repo\n"
        ),
        "gate.sh": f'set -euo pipefail\ncd /workspace/repo\ntest "$(git rev-parse HEAD)" = {args.sha}\n{command}\n',
        "postgres.sh": (
            "set -euo pipefail\n"
            "docker run -d --name cgm-fallback-pg -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=wm_test postgres:16\n"
            "for i in {1..30}; do docker exec cgm-fallback-pg pg_isready -U postgres && exit 0; sleep 2; done\nexit 1\n"
        ),
        "publish.sh": (
            'set -euo pipefail\ntest "$(cat /workspace/evidence/approved)" = yes\n'
            'test "$(sha256sum /workspace/quality-report.json | cut -d \' \' -f1)" = "$(cat /workspace/evidence/verified-quality-report.sha256)"\n'
            f'test "$(docker image inspect --format \'{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}\' {shlex.quote(args.image)})" = {shlex.quote(args.sha)}\n'
            f'test "$(docker image inspect --format \'{{{{index .Config.Labels "org.opencontainers.image.source"}}}}\' {shlex.quote(args.image)})" = {shlex.quote("https://github.com/" + args.repository)}\n'
            f"docker push {shlex.quote(args.image)}\n"
            f"docker image inspect --format '{{{{index .RepoDigests 0}}}}' {shlex.quote(args.image)} > /workspace/evidence/image-digest.txt\n"
        ),
        "finalize.sh": (
            "set -euo pipefail\npython3 /workspace/result.py finalize\n"
            "cp /workspace/request.json /workspace/evidence/\n"
            "if [ -f /workspace/quality-report.json ]; then cp /workspace/quality-report.json /workspace/evidence/; fi\n"
            "if [ -d /workspace/repo/quality-reports ]; then cp -R /workspace/repo/quality-reports /workspace/evidence/; fi\n"
            f"gcloud storage rsync --recursive /workspace/evidence {shlex.quote(args.evidence_prefix)}\n"
            'test "$(cat /workspace/evidence/result)" = passed\n'
        ),
    }


def prepare(args: argparse.Namespace) -> Path:
    validate(args)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cgm-fallback-", dir=output.parent
    ) as temporary:
        stage = Path(temporary) / "build"
        stage.mkdir()
        (stage / ".gcloudignore").write_text(
            "# Every prepared input belongs in the uploaded source.\n"
        )
        bare = Path(temporary) / "source.git"
        git(Path(temporary), "init", "--bare", str(bare))
        git(
            bare,
            "fetch",
            "--no-tags",
            str(Path(args.source).resolve()),
            args.sha,
            args.base_sha,
        )
        git(bare, "update-ref", "refs/heads/release-target", args.sha)
        git(bare, "update-ref", "refs/heads/release-base", args.base_sha)
        git(bare, "symbolic-ref", "HEAD", "refs/heads/release-target")
        git(
            bare,
            "bundle",
            "create",
            str(stage / "source.bundle"),
            "release-target",
            "release-base",
        )
        (stage / "quality").mkdir()
        for filename in ("quality_gate.py", "differential_coverage.py"):
            shutil.copy2(
                PLATFORM / "scripts" / "quality" / filename,
                stage / "quality" / filename,
            )
        for filename in ("capture.sh", "result.py", "Dockerfile.quality"):
            shutil.copy2(HERE / filename, stage / filename)
        shutil.copy2(HERE / "postgres_workers.py", stage / "postgres_workers.py")
        shutil.copy2(HERE / "pytest_schedule.py", stage / "pytest_schedule.py")
        duration_profile = HERE / "duration_profiles" / f"{args.service}.json"
        if duration_profile.is_file():
            shutil.copy2(duration_profile, stage / "duration-profile.json")
        policy_package = stage / "policy/eng_platform_api"
        (policy_package / "services").mkdir(parents=True)
        (policy_package / "__init__.py").touch()
        (policy_package / "services/__init__.py").touch()
        shutil.copy2(
            PLATFORM / "src/eng_platform_api/models.py", policy_package / "models.py"
        )
        shutil.copy2(
            PLATFORM / "src/eng_platform_api/services/quality_policy.py",
            policy_package / "services/quality_policy.py",
        )
        (stage / "catalog-service.json").write_text(
            json.dumps(args.catalog_service, sort_keys=True) + "\n"
        )
        for filename, body in scripts(args).items():
            (stage / filename).write_text(body, encoding="utf-8")
        tool_hashes = {
            str(path.relative_to(stage)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(stage.rglob("*"))
            if path.is_file()
        }
        args.platform_sha = git(PLATFORM, "rev-parse", "HEAD")
        request = {
            "repository": args.repository,
            "service_name": args.service,
            "commit_sha": args.sha,
            "base_sha": args.base_sha,
            "coverage_threshold": args.coverage_threshold,
            "policy_version": "oss-v2",
            "image": args.image,
            "workers": args.workers,
            "distribution": args.distribution,
            "profile": "python",
            "build_profile": args.profile,
            "machine_type": args.machine_type,
            "attempt_id": args.attempt_id,
            "timeout_seconds": TIMEOUT_SECONDS,
            "report_uri": args.report_uri,
            "summary_uri": args.summary_uri,
            "evidence_prefix": args.evidence_prefix,
            "project_id": args.catalog_service["project_id"],
            "region": args.catalog_service["region"],
            "input_hashes": tool_hashes,
            "platform_sha": args.platform_sha,
        }
        args.request_fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request["request_fingerprint"] = args.request_fingerprint
        (stage / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        (stage / "cloudbuild.json").write_text(
            json.dumps(build_config(args), indent=2) + "\n"
        )
        entries = [
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(stage)}"
            for path in sorted(stage.rglob("*"))
            if path.is_file()
        ]
        (stage / "input-manifest.sha256").write_text("\n".join(entries) + "\n")
        stage.rename(output)
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for option in ("source", "service", "sha", "base-sha", "evidence-uri", "output"):
        result.add_argument(f"--{option}", required=True)
    result.add_argument(
        "--profile", choices=tuple(BUILD_PROFILES), default="performance"
    )
    result.add_argument("--install-command", default="")
    result.add_argument("--branch", default="")
    return result


if __name__ == "__main__":
    try:
        print(prepare(parser().parse_args()))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Preparation failed: {exc}") from exc
