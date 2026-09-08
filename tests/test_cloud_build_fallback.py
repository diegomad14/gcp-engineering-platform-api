"""Exercise local preparation and the publication boundary without cloud mutations."""

import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts/ops/cloud-build-fallback"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load_module("prepare")
result = load_module("result")


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def arguments(tmp_path, monkeypatch):
    source = tmp_path / "source with spaces"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Fallback test")
    git(source, "config", "user.email", "fallback@example.invalid")
    git(
        source,
        "remote",
        "add",
        "origin",
        "https://github.com/diegomad14/cgm-sanplat-api.git",
    )
    (source / "app.py").write_text("print('base')\n")
    (source / "entrypoint.sh").write_text("#!/bin/sh\necho ready\n")
    (source / "entrypoint.sh").chmod(0o755)
    git(source, "add", "app.py")
    git(source, "add", "entrypoint.sh")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    (source / "app.py").write_text("print('release')\n")
    git(source, "commit", "-am", "release")
    sha = git(source, "rev-parse", "HEAD")
    (source / ".env").write_text("UNTRACKED_DO_NOT_EXPORT=yes\n")
    (source / "app.py").write_text("uncommitted modification\n")
    return prepare.parser().parse_args(
        [
            "--source",
            str(source),
            "--service",
            "cgm-sanplat-api",
            "--sha",
            sha,
            "--base-sha",
            base,
            "--evidence-uri",
            "gs://test-bucket/fallback",
            "--output",
            str(tmp_path / "prepared build"),
            "--install-command",
            'python -m pip install -e ".[dev,sqlserver,postgresql]"',
        ]
    )


@pytest.fixture
def prepared(arguments):
    return prepare.prepare(arguments)


def test_bundle_contains_exact_commits_and_excludes_working_tree(
    prepared, arguments, tmp_path
):
    checkout = tmp_path / "extracted"
    git(tmp_path, "clone", str(prepared / "source.bundle"), str(checkout))
    git(checkout, "checkout", "--detach", arguments.sha)
    assert git(checkout, "rev-parse", "HEAD") == arguments.sha
    assert git(checkout, "show", f"{arguments.base_sha}:app.py") == "print('base')"
    assert (checkout / "app.py").read_text() == "print('release')\n"
    assert not (checkout / ".env").exists()
    # Original dirty checkout was neither staged nor changed by preparation.
    assert (
        Path(arguments.source) / "app.py"
    ).read_text() == "uncommitted modification\n"


def test_gate_dependencies_allow_independent_work_but_never_early_push(prepared):
    config = json.loads((prepared / "cloudbuild.json").read_text())
    steps = {step["id"]: step for step in config["steps"]}
    assert steps["tooling"]["waitFor"] == ["-"]
    assert steps["postgres"]["waitFor"] == ["-"]
    assert steps["runtime"]["waitFor"] == ["source"]
    assert set(steps["quality"]["waitFor"]) == {"source", "tooling", "postgres"}
    assert set(steps["verify"]["waitFor"]) == {"quality", "runtime"}
    assert steps["publish"]["waitFor"] == ["verify"]
    assert steps["evidence-and-result"]["waitFor"] == ["publish"]
    assert "images" not in config
    assert (prepared / "publish.sh").read_text().count("docker push") == 1
    assert config["timeout"] == "1256s"
    step_config = json.dumps(config["steps"])
    assert all(f"${key}" in step_config for key in config["substitutions"])


def test_source_permissions_allow_quality_writes_without_changing_runtime_modes(
    prepared,
):
    # Run the generated setup in a local workspace, preserving its input manifest.
    script = (prepared / "source.sh").read_text().replace("/workspace", ".")
    completed = subprocess.run(
        ["bash", "-c", script], cwd=prepared, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert (prepared / "repo/app.py").stat().st_mode & 0o777 == 0o666
    assert (prepared / "repo/entrypoint.sh").stat().st_mode & 0o777 == 0o777
    assert (prepared / "release-source/app.py").stat().st_mode & 0o777 == 0o644
    assert (prepared / "release-source/entrypoint.sh").stat().st_mode & 0o777 == 0o755
    assert git(prepared / "repo", "diff", "--name-only", "HEAD") == ""


def test_tooling_runs_as_nonroot_with_installable_private_environment():
    dockerfile = (SCRIPTS / "Dockerfile.quality").read_text()
    assert dockerfile.splitlines()[-2] == "USER quality"
    assert "python -m venv /opt/quality" in dockerfile
    assert "chown -R quality:quality /opt/quality" in dockerfile
    assert 'PATH="/opt/quality/bin:${PATH}"' in dockerfile
    assert "safe.directory /workspace/repo" in dockerfile
    assert "safe.directory '*'" not in dockerfile


@pytest.mark.parametrize(
    "profile,machine,workers",
    [("performance", "E2_HIGHCPU_8", 4), ("economy", "E2_STANDARD_2", 2)],
)
def test_profiles_keep_catalog_owned_quality_and_bound_resources(
    arguments, profile, machine, workers
):
    arguments.profile = profile
    directory = prepare.prepare(arguments)
    request = json.loads((directory / "request.json").read_text())
    config = json.loads((directory / "cloudbuild.json").read_text())
    assert request["profile"] == "python"
    assert request["build_profile"] == profile
    assert request["workers"] == workers
    assert request["coverage_threshold"] == 70
    assert config["options"]["machineType"] == machine
    assert request["repository"] == "diegomad14/cgm-sanplat-api"
    assert request["image"].startswith(
        "us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/cgm-sanplat-api:"
    )
    assert arguments.sha in request["image"]
    assert config["substitutions"]["_QUALITY_URI"] == request["report_uri"]
    assert config["substitutions"]["_FALLBACK_SUMMARY_URI"] == request["summary_uri"]


@pytest.mark.parametrize(
    "option",
    ["--coverage-threshold", "--workers", "--repository", "--image", "--tooling-image"],
)
def test_callers_cannot_override_catalog_or_resource_policy(option):
    with pytest.raises(SystemExit):
        prepare.parser().parse_args([option, "attacker"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha", "main"),
        ("sha", "a" * 39),
        ("base_sha", "b" * 7),
        ("service", "unknown"),
        ("service", "cgm-sanplat-web"),
        ("evidence_uri", "gs://bucket/prefix;touch /tmp/injected"),
    ],
)
def test_invalid_preparation_fails_before_output(arguments, field, value):
    setattr(arguments, field, value)
    with pytest.raises(ValueError):
        prepare.prepare(arguments)
    assert not Path(arguments.output).exists()


def test_same_base_and_wrong_repository_are_rejected(arguments):
    arguments.base_sha = arguments.sha
    with pytest.raises(ValueError, match="base must differ"):
        prepare.prepare(arguments)
    arguments.base_sha = git(Path(arguments.source), "rev-parse", "HEAD^")
    git(
        Path(arguments.source),
        "remote",
        "set-url",
        "origin",
        "https://github.com/other/repository.git",
    )
    with pytest.raises(ValueError, match="origin"):
        prepare.prepare(arguments)


def test_shell_values_remain_single_arguments_and_install_is_not_executed(
    arguments, tmp_path
):
    sentinel = tmp_path / "unexpected"
    arguments.branch = f"branch'; touch {sentinel}; echo '"
    arguments.install_command = f"printf '%s' '$(touch {sentinel})'"
    directory = prepare.prepare(arguments)
    command = (directory / "gate.sh").read_text().splitlines()[3:]
    tokens = shlex.split("\n".join(command))
    assert tokens[tokens.index("--branch") + 1] == arguments.branch
    assert tokens[tokens.index("--install-command") + 1].startswith(
        arguments.install_command
    )
    tests = tokens[tokens.index("--test-command") + 1]
    assert "-p postgres_workers" in tests
    assert "-p pytest_schedule" in tests
    assert f"FALLBACK_SOURCE_SHA={arguments.sha}" in tests
    assert "FALLBACK_REPOSITORY=diegomad14/cgm-sanplat-api" in tests
    assert "FALLBACK_DURATION_PROFILE=/workspace/duration-profile.json" in tests
    assert "-n 4 --dist worksteal" in tests
    assert "--cov=. --cov-report=json:quality-reports/coverage.json" in tests
    assert not sentinel.exists()


def test_duration_hints_and_plugin_are_frozen_prepared_inputs(prepared):
    request = json.loads((prepared / "request.json").read_text())
    for filename in ("pytest_schedule.py", "duration-profile.json"):
        assert (
            request["input_hashes"][filename]
            == hashlib.sha256((prepared / filename).read_bytes()).hexdigest()
        )
        assert filename in (prepared / "input-manifest.sha256").read_text()


@pytest.fixture
def passing_evidence(prepared, monkeypatch):
    request = json.loads((prepared / "request.json").read_text())
    monkeypatch.setenv("FALLBACK_REQUEST_FINGERPRINT", request["request_fingerprint"])
    for field, variable in (
        ("commit_sha", "FALLBACK_RELEASE_SHA"),
        ("report_uri", "FALLBACK_QUALITY_URI"),
        ("summary_uri", "FALLBACK_SUMMARY_URI"),
        ("platform_sha", "FALLBACK_PLATFORM_SHA"),
        ("project_id", "FALLBACK_PROJECT_ID"),
    ):
        monkeypatch.setenv(variable, request[field])
    monkeypatch.setenv("FALLBACK_BUILD_ID", "build-test")
    evidence = prepared / "evidence"
    evidence.mkdir()
    for step in result.REQUIRED_STEPS:
        (evidence / f"{step}.json").write_text(
            json.dumps({"step": step, "exit_code": 0, "duration_seconds": 1})
        )
    quality = {
        **{
            key: request[key]
            for key in (
                "repository",
                "service_name",
                "commit_sha",
                "base_sha",
                "profile",
                "policy_version",
            )
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": 78,
        "coverage_threshold": 70,
        "changed_lines": 10,
        "covered_changed_lines": 9,
        "differential_coverage": 90,
        "differential_threshold": 80,
        "checks": [
            {"name": name, "category": name, "status": "PASSED"}
            for name in (
                "setup",
                "tests",
                "lint",
                "format",
                "typecheck",
                "sast",
                "dependencies",
                "secrets",
                "misconfiguration",
                "differential_coverage",
            )
        ],
    }
    (prepared / "quality-report.json").write_text(json.dumps(quality))
    return prepared, quality


def test_publication_accepts_only_canonical_complete_quality(passing_evidence):
    workspace, _ = passing_evidence
    assert result.errors_for(workspace) == []
    assert result.main(workspace, "verify") == 0
    assert (workspace / "evidence/approved").read_text() == "yes"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(commit_sha="f" * 40),
        lambda report: report.update(base_sha=report["commit_sha"]),
        lambda report: report.update(coverage_threshold=1),
        lambda report: report.update(differential_threshold=1),
        lambda report: report.update(covered_changed_lines=8),
        lambda report: report["checks"].append(report["checks"][0]),
        lambda report: report["checks"][1].update(status="SKIPPED"),
        lambda report: report["checks"][0].update(blocking_findings=1),
        lambda report: report.update(changed_lines=0, differential_coverage=None),
        lambda report: report.update(checks=[]),
    ],
)
def test_weak_or_forged_quality_fails_closed(passing_evidence, mutation):
    workspace, quality = passing_evidence
    mutation(quality)
    (workspace / "quality-report.json").write_text(json.dumps(quality))
    assert result.main(workspace, "verify") == 1
    assert (workspace / "evidence/approved").read_text() == "no"


def test_no_applicable_lines_requires_exact_zero_counts_and_skipped_check(
    passing_evidence,
):
    workspace, quality = passing_evidence
    quality.update(changed_lines=0, covered_changed_lines=0, differential_coverage=None)
    quality["checks"][-1]["status"] = "SKIPPED"
    (workspace / "quality-report.json").write_text(json.dumps(quality))
    assert result.errors_for(workspace) == []


def test_missing_step_and_modified_input_block_publication(passing_evidence):
    workspace, _ = passing_evidence
    (workspace / "evidence/runtime.json").unlink()
    (workspace / "gate.sh").write_text("exit 0\n")
    errors = result.errors_for(workspace)
    assert any("runtime" in error for error in errors)
    assert any("gate.sh" in error for error in errors)


def test_publish_never_calls_docker_without_approval(prepared, tmp_path):
    (prepared / "evidence").mkdir()
    (prepared / "evidence/approved").write_text("no")
    sentinel = tmp_path / "pushed"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n")
    fake_docker.chmod(0o755)
    command = (prepared / "publish.sh").read_text().replace("/workspace", str(prepared))
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        env={**os.environ, "PATH": str(fake_bin) + ":" + os.environ["PATH"]},
    )
    assert completed.returncode != 0
    assert not sentinel.exists()


@pytest.mark.parametrize("wrong_label", ["revision", "source", ""])
def test_publish_inspects_actual_image_identity_before_pushing(
    arguments, tmp_path, wrong_label
):
    prepare.validate(arguments)
    workspace = tmp_path / "publish"
    (workspace / "evidence").mkdir(parents=True)
    report = b"quality bytes"
    (workspace / "quality-report.json").write_bytes(report)
    (workspace / "evidence/approved").write_text("yes")
    (workspace / "evidence/verified-quality-report.sha256").write_text(
        hashlib.sha256(report).hexdigest()
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinel = tmp_path / "pushed"
    revision = "wrong" if wrong_label == "revision" else arguments.sha
    source = (
        "wrong"
        if wrong_label == "source"
        else "https://github.com/" + arguments.repository
    )
    executable = fake_bin / "docker"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = push ]; then touch {shlex.quote(str(sentinel))}; exit 0; fi\n'
        f'if [[ "$*" == *image.revision* ]]; then echo {shlex.quote(revision)};\n'
        f'elif [[ "$*" == *image.source* ]]; then echo {shlex.quote(source)};\n'
        "else echo 'test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; fi\n"
    )
    executable.chmod(0o755)
    script = prepare.scripts(arguments)["publish.sh"].replace(
        "/workspace", str(workspace)
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        env={**os.environ, "PATH": str(fake_bin) + ":" + os.environ["PATH"]},
    )
    assert (completed.returncode == 0) is (not wrong_label)
    assert sentinel.exists() is (not wrong_label)


def test_failed_command_captures_logs_timing_and_original_exit_code(tmp_path):
    wrapper = tmp_path / "capture.sh"
    wrapper.write_text(
        (SCRIPTS / "capture.sh").read_text().replace("/workspace", str(tmp_path))
    )
    completed = subprocess.run(
        ["bash", str(wrapper), "quality", "bash", "-c", "echo failed-check; exit 9"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    captured = json.loads((tmp_path / "evidence/quality.json").read_text())
    assert captured["exit_code"] == 9
    assert captured["duration_seconds"] >= 0
    assert completed.stdout.count("failed-check") == 1
    assert (tmp_path / "evidence").stat().st_mode & 0o777 == 0o777


def test_final_summary_binds_report_digest_and_attempt(passing_evidence, monkeypatch):
    workspace, _ = passing_evidence
    assert result.main(workspace, "verify") == 0
    request = json.loads((workspace / "request.json").read_text())
    for step in ("verify", "publish"):
        (workspace / f"evidence/{step}.json").write_text(
            json.dumps({"step": step, "exit_code": 0})
        )
    image_digest = request["image"].rsplit(":", 1)[0] + "@sha256:" + "a" * 64
    (workspace / "evidence/image-digest.txt").write_text(image_digest)
    monkeypatch.setenv("FALLBACK_BUILD_ID", "accepted-build")
    assert result.main(workspace, "finalize") == 0
    summary = json.loads((workspace / "evidence/summary.json").read_text())
    assert summary["passed"] is True
    assert summary["build_id"] == "accepted-build"
    assert summary["digest"] == "sha256:" + "a" * 64
    assert summary["request_fingerprint"] == request["request_fingerprint"]
    assert (
        summary["quality_report_sha256"]
        == hashlib.sha256((workspace / "quality-report.json").read_bytes()).hexdigest()
    )


def test_report_cannot_change_after_canonical_verification(passing_evidence):
    workspace, quality = passing_evidence
    assert result.main(workspace, "verify") == 0
    quality["coverage"] = 100
    (workspace / "quality-report.json").write_text(json.dumps(quality))
    assert "Quality report changed after canonical verification" in result.errors_for(
        workspace, published=True
    )


def test_finalizer_keeps_failure_evidence_for_upload_before_final_exit(
    passing_evidence,
):
    workspace, _ = passing_evidence
    (workspace / "evidence/quality.json").write_text('{"step":"quality","exit_code":1}')
    assert result.main(workspace, "finalize") == 0
    assert (workspace / "evidence/result").read_text() == "failed"
    assert (
        json.loads((workspace / "evidence/summary.json").read_text())["passed"] is False
    )
