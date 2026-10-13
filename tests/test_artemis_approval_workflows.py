"""Artemis approvals must separate authorization, candidate work and promotion."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
DEPLOY = "platform-deploy.yml"
ROLLBACK = "platform-rollback.yml"


def _jobs(filename):
    return yaml.safe_load((WORKFLOWS / filename).read_text())["jobs"]


def _needs(job):
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else value


def _ancestors(jobs, job_name):
    result = set()
    for dependency in _needs(jobs[job_name]):
        result.add(dependency)
        result.update(_ancestors(jobs, dependency))
    return result


def _expression_tree(expression, service, results):
    """Parse the small GitHub condition vocabulary used by these job graphs."""
    source = expression.removeprefix("${{").removesuffix("}}").strip()
    source = source.replace("inputs.service_name", repr(service))
    source = re.sub(
        r"needs\.([a-z-]+)\.result",
        lambda match: repr(results.get(match[1], "skipped")),
        source,
    )
    source = source.replace("&&", " and ").replace("||", " or ")
    source = re.sub(r"!(?!=)", "not ", source).strip()
    return ast.parse(source, mode="eval").body


def _value(node):
    # No eval, imports or attribute lookup: conditions cannot execute Python.
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _value(node.operand)
    if isinstance(node, ast.BoolOp):
        result = None
        for operand in node.values:
            result = _value(operand)
            if isinstance(node.op, ast.And) and not result:
                return result
            if isinstance(node.op, ast.Or) and result:
                return result
        return result
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _value(node.left), _value(node.comparators[0])
        if isinstance(node.ops[0], ast.Eq):
            return left == right
        if isinstance(node.ops[0], ast.NotEq):
            return left != right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "always" and not node.args:
            return True
        if node.func.id == "startsWith" and len(node.args) == 2:
            return _value(node.args[0]).startswith(_value(node.args[1]))
    raise AssertionError(f"Unsupported workflow condition: {ast.dump(node)}")


def _condition(expression, service, results):
    return _value(_expression_tree(expression, service, results))


def _admitted(job, service, results):
    expression = job.get("if", "${{ True }}")
    tree = _expression_tree(expression, service, results)
    explicit_always = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "always"
        for node in ast.walk(tree)
    )
    # GitHub implicitly requires successful dependencies unless a status
    # function overrides that rule. This matters for workers' skipped candidate.
    if not explicit_always and any(
        results.get(dependency) != "success" for dependency in _needs(job)
    ):
        return False
    return bool(_value(tree))


def _step(job, name):
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _authorization(job):
    matches = [
        step
        for step in job["steps"]
        if "verify-release-authorization" in step.get("uses", "")
    ]
    assert len(matches) == 1
    return matches[0]


def _engine(job):
    matches = [step for step in job["steps"] if "docker run " in step.get("run", "")]
    assert len(matches) == 1
    return matches[0]


def _run_pure(script, cwd, environment):
    # Execute only the preflight/checksum snippets, never a cloud or engine step.
    for command in ("docker ", "gcloud ", "curl ", "gh ", "fetch("):
        assert command not in script
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={"PATH": os.environ["PATH"]} | environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("filename", [DEPLOY, ROLLBACK])
def test_ticket_is_consumed_before_any_artemis_environment_approval(filename):
    jobs = _jobs(filename)
    verify = jobs["verify-release"]
    assert "environment" not in verify
    assert not _needs(verify)
    authorization = _authorization(verify)
    assert authorization["with"].get("mode", "consume") == "consume"
    assert not authorization.get("if")
    assert authorization["with"]["token"] == "${{ inputs.platform_authorization }}"
    assert authorization["with"]["repository"] == "${{ github.repository }}"
    assert (
        authorization["with"]["github_deployment_id"]
        == "${{ inputs.github_deployment_id }}"
    )
    binding = _step(verify, "Bind verified runtime execution configuration")
    assert verify["steps"].index(authorization) < verify["steps"].index(binding)
    assert not any("docker run " in step.get("run", "") for step in verify["steps"])
    assert not any(
        "google-github-actions/auth@" in step.get("uses", "")
        for step in verify["steps"]
    )
    environment_jobs = {
        name: job for name, job in jobs.items() if job.get("environment")
    }
    assert environment_jobs
    for name, job in environment_jobs.items():
        assert "verify-release" in _ancestors(jobs, name)
        assert not _admitted(
            job,
            "cgm-artemis-web",
            {"verify-release": "failure", "candidate": "success"},
        )


@pytest.mark.parametrize(
    "filename,job_name,environment,stage",
    [
        (DEPLOY, "candidate", "candidate", "candidate"),
        (DEPLOY, "promote", "production", "promote"),
        (ROLLBACK, "approved-rollback", "production", "rollback"),
    ],
)
def test_approved_jobs_revalidate_ticket_and_configuration_before_wif(
    filename, job_name, environment, stage
):
    job = _jobs(filename)[job_name]
    assert job["environment"] == environment
    authorization = _authorization(job)
    assert authorization["with"]["mode"] == "continue"
    assert authorization["with"]["stage"] == stage
    assert not authorization.get("if")
    binding = _step(job, "Bind verified runtime execution configuration")
    assert not binding.get("if")
    cloud_auth = [
        step
        for step in job["steps"]
        if "google-github-actions/auth@" in step.get("uses", "")
    ]
    assert len(cloud_auth) == 1
    engine = _engine(job)
    assert not engine.get("if")
    assert job["steps"].index(authorization) < job["steps"].index(binding)
    assert job["steps"].index(binding) < job["steps"].index(cloud_auth[0])
    assert job["steps"].index(cloud_auth[0]) < job["steps"].index(engine)
    assert (
        engine["env"]["RELEASE_EXECUTOR_IMAGE"]
        == "${{ needs.verify-release.outputs.executor_image }}"
    )
    if stage == "rollback":
        assert (
            authorization["with"]["target_revision"] == "${{ inputs.target_revision }}"
        )
        assert engine["env"]["CGM_OPERATION"] == "rollback"


@pytest.mark.parametrize("filename", [DEPLOY, ROLLBACK])
def test_initial_preflight_freezes_the_image_for_all_approval_jobs(filename, tmp_path):
    jobs = _jobs(filename)
    verify = jobs["verify-release"]
    preflight = _step(verify, "Validate release executor configuration")
    output = tmp_path / "outputs"
    frozen = "registry.example/release@sha256:" + "a" * 64
    result = _run_pure(
        preflight["run"],
        tmp_path,
        {
            "RELEASE_EXECUTOR_IMAGE": frozen,
            "PLATFORM_API_URL": "https://platform.example",
            "RELEASE_SIGNING_PUBLIC_KEY": "local-test-key",
            "RELEASE_WIF_PROVIDER": "local-test-provider",
            "RELEASE_SERVICE_ACCOUNT": "release@example.iam.gserviceaccount.com",
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [f"image={frozen}"]
    assert (
        verify["outputs"]["executor_image"]
        == f"${{{{ steps.{preflight['id']}.outputs.image }}}}"
    )
    for job in jobs.values():
        if job.get("environment"):
            assert (
                _step(job, "Validate release executor configuration")["env"][
                    "RELEASE_EXECUTOR_IMAGE"
                ]
                == "${{ needs.verify-release.outputs.executor_image }}"
            )
            assert (
                _engine(job)["env"]["RELEASE_EXECUTOR_IMAGE"]
                == "${{ needs.verify-release.outputs.executor_image }}"
            )


@pytest.mark.parametrize(
    "service,verification,candidate_result,allowed,phase",
    [
        ("cgm-artemis-api", "success", "success", True, "promote"),
        ("cgm-artemis-web", "success", "success", True, "promote"),
        ("cgm-artemis-wm-sweep-worker", "success", "skipped", True, "full"),
        ("cgm-artemis-api", "success", "skipped", False, None),
        ("cgm-artemis-web", "success", "failure", False, None),
        ("cgm-artemis-wm-sweep-worker", "failure", "skipped", False, None),
    ],
)
def test_promotion_requires_candidate_success_but_workers_can_skip_candidate(
    service, verification, candidate_result, allowed, phase
):
    jobs = _jobs(DEPLOY)
    results = {"verify-release": verification, "candidate": candidate_result}
    assert set(_needs(jobs["promote"])) == {"verify-release", "candidate"}
    assert _admitted(jobs["promote"], service, results) is allowed
    paired_service = service in {"cgm-artemis-api", "cgm-artemis-web"}
    assert _admitted(jobs["candidate"], service, results) is (
        paired_service and verification == "success"
    )
    assert not _admitted(jobs["release"], service, results)
    if allowed:
        engine = _engine(jobs["promote"])
        assert _condition(engine["env"]["CGM_RELEASE_PHASE"], service, results) == phase
        download = next(
            step
            for step in jobs["promote"]["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        )
        assert bool(_condition(download["if"], service, results)) is paired_service


def test_candidate_build_and_promotion_share_exact_same_run_evidence():
    jobs = _jobs(DEPLOY)
    candidate, promote = jobs["candidate"], jobs["promote"]
    assert _needs(candidate) == ["verify-release"]
    candidate_engine = _engine(candidate)
    assert candidate_engine["env"]["CGM_RELEASE_PHASE"] == "candidate"
    assert "--env CGM_RELEASE_PHASE" in candidate_engine["run"]
    assert "--env CGM_RELEASE_PHASE" in _engine(promote)["run"]
    assert not any("docker build " in step.get("run", "") for step in promote["steps"])
    upload = next(
        step
        for step in candidate["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    receipt = _step(candidate, "Publish validated candidate evidence")
    assert candidate["steps"].index(candidate_engine) < candidate["steps"].index(
        receipt
    )
    assert candidate["steps"].index(receipt) < candidate["steps"].index(upload)
    assert upload["with"]["path"] == ".eng-platform-candidate.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["include-hidden-files"] is True
    assert "${{ github.run_id }}" in upload["with"]["name"]
    assert "${{ github.run_attempt }}" in upload["with"]["name"]
    assert (
        candidate["outputs"]["artifact_id"]
        == f"${{{{ steps.{upload['id']}.outputs.artifact-id }}}}"
    )
    assert (
        candidate["outputs"]["candidate_sha256"]
        == f"${{{{ steps.{receipt['id']}.outputs.sha256 }}}}"
    )
    download = next(
        step
        for step in promote["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert (
        download["with"]["artifact-ids"] == "${{ needs.candidate.outputs.artifact_id }}"
    )
    assert "name" not in download["with"]
    # Without name, artifact-ids otherwise adds a named subdirectory even for
    # one artifact. The checksum and promote engine consume the root file.
    assert download["with"]["path"] == "."
    assert download["with"]["merge-multiple"] is True
    assert not {"github-token", "repository", "run-id"} & download["with"].keys()
    checksum = _step(promote, "Verify candidate evidence checksum")
    assert (
        checksum["env"]["EXPECTED_CANDIDATE_SHA256"]
        == "${{ needs.candidate.outputs.candidate_sha256 }}"
    )
    assert promote["steps"].index(download) < promote["steps"].index(checksum)
    assert promote["steps"].index(checksum) < promote["steps"].index(_engine(promote))
    for service in [
        "cgm-artemis-api",
        "cgm-artemis-web",
        "cgm-artemis-wm-sweep-worker",
    ]:
        assert _condition(download["if"], service, {}) == _condition(
            checksum["if"], service, {}
        )


@pytest.mark.parametrize("mutation", ["none", "tampered", "missing"])
def test_actual_candidate_checksum_blocks_tampered_or_missing_evidence(
    mutation, tmp_path
):
    jobs = _jobs(DEPLOY)
    evidence = tmp_path / ".eng-platform-candidate.json"
    original = b'{"candidate_revision":"local-only","candidate_sha":"exact"}\n'
    evidence.write_bytes(original)
    output = tmp_path / "outputs"
    receipt = _run_pure(
        _step(jobs["candidate"], "Publish validated candidate evidence")["run"],
        tmp_path,
        {"GITHUB_OUTPUT": str(output)},
    )
    assert receipt.returncode == 0, receipt.stderr
    expected = hashlib.sha256(original).hexdigest()
    assert output.read_text().splitlines() == [f"sha256={expected}"]
    if mutation == "tampered":
        evidence.write_bytes(b'{"candidate_revision":"another-revision"}\n')
    elif mutation == "missing":
        evidence.unlink()
    verified = _run_pure(
        _step(jobs["promote"], "Verify candidate evidence checksum")["run"],
        tmp_path,
        {"EXPECTED_CANDIDATE_SHA256": expected},
    )
    assert (verified.returncode == 0) is (mutation == "none")


@pytest.mark.parametrize(
    "filename,legacy_job", [(DEPLOY, "release"), (ROLLBACK, "rollback")]
)
def test_non_artemis_keeps_the_existing_single_engine_without_new_approvals(
    filename, legacy_job
):
    jobs = _jobs(filename)
    legacy = jobs[legacy_job]
    assert not _needs(legacy)
    assert "environment" not in legacy
    assert _authorization(legacy)["with"].get("mode", "consume") == "consume"
    assert "CGM_RELEASE_PHASE" not in _engine(legacy)["env"]
    for service in ["eng-platform-api", "eng-platform-web", "cgm-sanplat-api"]:
        assert _admitted(legacy, service, {})
        assert not _admitted(jobs["verify-release"], service, {})
        assert not any(
            _admitted(
                job, service, {"verify-release": "skipped", "candidate": "skipped"}
            )
            for job in jobs.values()
            if job.get("environment")
        )


@pytest.mark.parametrize("reported_code", ["approved", "legacy"])
def test_staged_engine_probe_rejects_old_code_before_credentials(
    reported_code, tmp_path
):
    engine_path = (
        WORKFLOWS.parents[1] / "docker" / "release-executor" / "release_executor.py"
    )
    approved_code_hash = hashlib.sha256(engine_path.read_bytes()).hexdigest()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['MOCK_DOCKER_CALLS'], 'a', encoding='utf-8') as calls:\n"
        "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(os.environ['MOCK_ENGINE_CODE_SHA256'] + '  /opt/eng-platform/release_executor.py')\n"
    )
    docker.chmod(0o755)
    for job_name in ["candidate", "promote"]:
        script = _engine(_jobs(DEPLOY)[job_name])["run"]
        start = script.index('if [ "$CGM_RELEASE_PHASE" != full ]; then')
        credential_position = script.index('credential_file="$(basename')
        guard = script[start:credential_position]
        fixed_hash = re.search(
            r'test "\$actual_engine_sha256" = ["\']?([0-9a-f]{64})["\']? \|\|',
            guard,
        )
        assert fixed_hash is not None, (
            "Staged engine code hash must be finalized, never a placeholder"
        )
        assert fixed_hash[1] == approved_code_hash
        assert script.index("--entrypoint /usr/bin/sha256sum") < credential_position
        assert credential_position < script.index("docker run --rm --network host")
        calls_path = tmp_path / f"{job_name}-calls.jsonl"
        image = "registry.example/release@sha256:" + "b" * 64
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + guard + "printf 'next-marker\\n'\n"],
            cwd=tmp_path,
            env={
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "CGM_RELEASE_PHASE": job_name,
                "RELEASE_EXECUTOR_IMAGE": image,
                "MOCK_DOCKER_CALLS": str(calls_path),
                "MOCK_ENGINE_CODE_SHA256": approved_code_hash
                if reported_code == "approved"
                else "0" * 64,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
        assert len(calls) == 1
        args = calls[0]
        assert args[args.index("--network") + 1] == "none"
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--security-opt") + 1] == "no-new-privileges"
        assert "--read-only" in args
        assert args[args.index("--entrypoint") + 1] == "/usr/bin/sha256sum"
        assert args[-2:] == [image, "/opt/eng-platform/release_executor.py"]
        assert not {"--volume", "-v", "--env", "--mount"} & set(args)
        assert (result.returncode == 0) is (reported_code == "approved")
        assert ("next-marker" in result.stdout) is (reported_code == "approved")
