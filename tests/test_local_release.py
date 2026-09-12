import json
import multiprocessing
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.release import local_release, release_lifecycle
from scripts.release.execution_control import (
    ExecutionContext,
    ExecutionControlError,
    PlatformExecutionControlClient,
)


ROOT = Path(__file__).parents[1]


def test_capability_issue_keeps_actor_out_of_issuance_payload():
    requests = []

    def transport(path, payload):
        requests.append((path, payload))
        return {"token": "fixture-capability", "expires_at": 1}

    client = PlatformExecutionControlClient("", transport=transport)
    context = ExecutionContext(
        release_id="release-1",
        repository="example/service",
        service_name="service",
        source_sha="a" * 40,
        tag="v1.0.0",
        operation="candidate",
        actor_id="not-sent-to-issuer",
        target="project/region/service",
        configuration_hash="b" * 64,
    )
    assert client.issue_capability(context) == "fixture-capability"
    assert requests == [
        (
            "/api/internal/release-execution/authorizations/issue",
            {
                "release_id": "release-1",
                "repository": "example/service",
                "service_name": "service",
                "source_sha": "a" * 40,
                "tag": "v1.0.0",
                "artifact_digest": "",
                "target": "project/region/service",
                "operation": "candidate",
                "configuration_hash": "b" * 64,
            },
        )
    ]


def test_control_client_rejects_unprotected_auth_sources(monkeypatch):
    monkeypatch.delenv("ENG_PLATFORM_AUTH_HEADERS_FILE", raising=False)
    monkeypatch.setenv("ENG_PLATFORM_OAUTH_BEARER_TOKEN", "unsupported")
    with pytest.raises(ExecutionControlError, match="protected platform session"):
        PlatformExecutionControlClient("https://control.example")


def test_lifecycle_context_hashes_canonical_deployment_when_manifest_omits_it(tmp_path):
    value = {
        "release_id": "release-1",
        "repository": "example/service",
        "service_name": "service",
        "source": {"sha": "a" * 40},
        "version": {"tag": "v1.0.0"},
        "artifact": {"digest": "sha256:" + "b" * 64},
        "catalog": {
            "project_id": "project",
            "region": "region",
            "deployment": {"image_name": "service", "health_path": "/health"},
        },
    }
    context = release_lifecycle.lifecycle_execution_context(
        value, "candidate", "fixture-actor"
    )
    assert context.configuration_hash == local_release.digest(
        value["catalog"]["deployment"]
    )
    assert len(context.configuration_hash) == 64


def _build_metadata(path, image_id="sha256:" + "1" * 64):
    local_release.write_json(
        Path(path),
        {
            "containerimage.config.digest": image_id,
            "containerimage.digest": "sha256:" + "3" * 64,
            "containerimage.descriptor": {
                "digest": "sha256:" + "3" * 64,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
            },
        },
    )


def _synthetic_build_worker(manifest_path, state_dir, ready, start, builds, results):
    ready.put("ready")
    start.wait(30)

    def synthetic_runner(args, **_kwargs):
        if args[:3] == ["docker", "buildx", "build"]:
            with builds.get_lock():
                builds.value += 1
            _build_metadata(args[args.index("--metadata-file") + 1])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout="sha256:" + "1" * 64 + "\n", stderr=""
        )

    try:
        result = local_release.build_local(
            Path(manifest_path),
            state_dir=Path(state_dir),
            execute=True,
            runner=synthetic_runner,
        )
        results.put(result["action"])
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put(f"ERROR: {exc}")


def _interrupted_build_worker(manifest_path, state_dir):
    def interrupted(args, **_kwargs):
        assert args[:3] == ["docker", "buildx", "build"]
        _build_metadata(args[args.index("--metadata-file") + 1])
        os._exit(17)

    local_release.build_local(
        manifest_path, state_dir=state_dir, execute=True, runner=interrupted
    )


def _reconcile_worker(manifest_path, state_dir, results):
    try:
        result = local_release.reconcile_local_build(
            manifest_path,
            state_dir=state_dir,
            inspector=lambda record: {
                "reuse_key": record["reuse_key"],
                "image_id": "sha256:" + "1" * 64,
            },
        )
        results.put((result["action"], result["artifact"]["digest"]))
    except Exception as exc:
        results.put(("ERROR", str(exc)))


@pytest.fixture()
def digest_build(tmp_path):
    manifest = {
        "schema_version": local_release.SCHEMA_VERSION,
        "repository": "example/service",
        "service_name": "service",
        "source": {"sha": "a" * 40},
        "version": {"tag": "v1.0.0"},
        "quality": {"status": "PASSED"},
        "artifact": {
            "reuse_key": "b" * 64,
            "local_image": "local/service:v1.0.0",
            "context": str(tmp_path),
        },
    }
    path = tmp_path / "manifest.json"
    local_release.write_json(path, manifest)
    return path, tmp_path / "state"


def test_interrupted_build_recovered_by_two_processes(digest_build):
    manifest_path, state_dir = digest_build
    context = multiprocessing.get_context("spawn")
    builder = context.Process(target=_interrupted_build_worker, args=digest_build)
    builder.start()
    builder.join(20)
    assert builder.exitcode == 17
    results = context.Queue()
    processes = [
        context.Process(target=_reconcile_worker, args=(*digest_build, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted([results.get(timeout=5), results.get(timeout=5)]) == [
        ("reconciled", "sha256:" + "3" * 64),
        ("reused", "sha256:" + "3" * 64),
    ]
    assert (
        local_release.read_json(manifest_path)["artifact"]["digest"]
        == "sha256:" + "3" * 64
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "not-object",
        "no-digest",
        "invalid-digest",
        "config",
        "same-id",
        "descriptor",
        "oci-index",
        "docker-list",
    ],
)
def test_build_metadata_failures_never_succeed_and_can_reconcile(digest_build, fault):
    manifest_path, state_dir = digest_build
    metadata_path = None

    def runner(args, **_kwargs):
        nonlocal metadata_path
        if args[:3] == ["docker", "buildx", "build"]:
            metadata_path = Path(args[args.index("--metadata-file") + 1])
            if fault != "missing":
                _build_metadata(metadata_path)
                metadata = local_release.read_json(metadata_path)
                if fault == "malformed":
                    metadata_path.write_text("{", encoding="utf-8")
                else:
                    if fault == "not-object":
                        metadata = []
                    elif fault == "no-digest":
                        del metadata["containerimage.digest"]
                    elif fault == "invalid-digest":
                        metadata["containerimage.digest"] = "sha256:bad"
                    elif fault == "config":
                        metadata["containerimage.config.digest"] = "sha256:" + "2" * 64
                    elif fault == "same-id":
                        metadata["containerimage.digest"] = "sha256:" + "1" * 64
                    elif fault == "descriptor":
                        metadata["containerimage.descriptor"]["digest"] = (
                            "sha256:" + "2" * 64
                        )
                    elif fault in {"oci-index", "docker-list"}:
                        metadata["containerimage.descriptor"]["mediaType"] = (
                            "application/vnd.oci.image.index.v1+json"
                            if fault == "oci-index"
                            else "application/vnd.docker.distribution.manifest.list.v2+json"
                        )
                    local_release.write_json(metadata_path, metadata)
        return subprocess.CompletedProcess(args, 0, stdout="sha256:" + "1" * 64)

    with pytest.raises(local_release.ReleaseError):
        local_release.build_local(
            manifest_path, state_dir=state_dir, execute=True, runner=runner
        )
    assert not local_release.read_json(manifest_path)["artifact"].get("digest")
    record_path = local_release._artifact_record_path(state_dir, "b" * 64)
    assert local_release.read_json(record_path)["status"] == "UNKNOWN"
    with pytest.raises(local_release.ReleaseError, match="reconcile-build"):
        local_release.build_local(
            manifest_path, state_dir=state_dir, execute=True, runner=runner
        )

    def inspector(record):
        return {
            "reuse_key": record["reuse_key"],
            "image_id": "sha256:" + "1" * 64,
        }

    with pytest.raises(local_release.ReleaseError):
        local_release.reconcile_local_build(
            manifest_path, state_dir=state_dir, inspector=inspector
        )
    _build_metadata(metadata_path)
    result = local_release.reconcile_local_build(
        manifest_path, state_dir=state_dir, inspector=inspector
    )
    assert result["artifact"]["digest"] == "sha256:" + "3" * 64


@pytest.mark.parametrize("field", ["build_identity", "reuse_key", "image"])
def test_reconcile_rejects_wrong_identity(digest_build, field):
    manifest_path, state_dir = digest_build
    manifest = local_release.read_json(manifest_path)
    record = {
        "status": "AVAILABLE",
        "reuse_key": "b" * 64,
        "image": manifest["artifact"]["local_image"],
        "build_identity": local_release._build_identity(manifest),
    }
    record[field] = "different"
    local_release.write_json(
        local_release._artifact_record_path(state_dir, "b" * 64), record
    )
    with pytest.raises(local_release.ReleaseError, match="identity"):
        local_release.reconcile_local_build(
            manifest_path,
            state_dir=state_dir,
            inspector=lambda _: pytest.fail("must not inspect"),
        )


@pytest.mark.parametrize(
    "fault", ["missing-digest", "digest", "metadata", "identity", "image"]
)
def test_reuse_rejects_incomplete_or_mismatched_record(digest_build, fault):
    manifest_path, state_dir = digest_build
    manifest = local_release.read_json(manifest_path)
    metadata_path = state_dir / "metadata.json"
    _build_metadata(metadata_path)
    record = {
        "status": "AVAILABLE",
        "reuse_key": "b" * 64,
        "image": manifest["artifact"]["local_image"],
        "build_identity": local_release._build_identity(manifest),
        "image_id": "sha256:" + "1" * 64,
        "digest": "sha256:" + "3" * 64,
        "metadata_path": str(metadata_path),
    }
    if fault == "missing-digest":
        del record["digest"]
    elif fault == "digest":
        record["digest"] = "sha256:" + "4" * 64
    elif fault == "metadata":
        record["metadata_path"] = str(state_dir / "absent.json")
    elif fault == "identity":
        del record["build_identity"]
    else:
        record["image"] = "different"
    local_release.write_json(
        local_release._artifact_record_path(state_dir, "b" * 64), record
    )
    with pytest.raises(local_release.ReleaseError):
        local_release.build_local(manifest_path, state_dir=state_dir, execute=False)
    assert not local_release.read_json(manifest_path)["artifact"].get("digest")


def test_reconcile_available_repairs_manifest_using_metadata(digest_build, monkeypatch):
    manifest_path, state_dir = digest_build
    manifest = local_release.read_json(manifest_path)
    metadata_path = state_dir / "metadata.json"
    _build_metadata(metadata_path)
    record = {
        "status": "AVAILABLE",
        "reuse_key": "b" * 64,
        "image": manifest["artifact"]["local_image"],
        "build_identity": local_release._build_identity(manifest),
        "metadata_path": str(metadata_path),
    }
    local_release.write_json(
        local_release._artifact_record_path(state_dir, "b" * 64), record
    )
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="sha256:" + "1" * 64)

    monkeypatch.setattr(local_release, "run", inspect)
    assert (
        local_release.reconcile_local_build(manifest_path, state_dir=state_dir)[
            "action"
        ]
        == "reconciled"
    )
    local_release.write_json(manifest_path, manifest)
    assert (
        local_release.reconcile_local_build(manifest_path, state_dir=state_dir)[
            "action"
        ]
        == "reused"
    )
    assert (
        local_release.read_json(manifest_path)["artifact"]["digest"]
        == "sha256:" + "3" * 64
    )
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


@pytest.mark.parametrize(
    "change", ["source", "version", "quality", "configuration", "build_args"]
)
def test_same_reuse_key_does_not_allow_different_build_identity(digest_build, change):
    manifest_path, state_dir = digest_build
    manifest = local_release.read_json(manifest_path)
    record = {
        "status": "BUILDING",
        "reuse_key": "b" * 64,
        "image": manifest["artifact"]["local_image"],
        "build_identity": local_release._build_identity(manifest),
    }
    local_release.write_json(
        local_release._artifact_record_path(state_dir, "b" * 64), record
    )
    if change == "source":
        manifest["source"]["sha"] = "c" * 40
    elif change == "version":
        manifest["version"]["tag"] = "v1.0.1"
    elif change == "quality":
        manifest["quality"]["status"] = "FAILED"
    elif change == "configuration":
        manifest["catalog"] = {"deployment": {"region": "different"}}
    else:
        manifest["artifact"]["inputs"] = {"build_args": {"APP_VERSION": "different"}}
    local_release.write_json(manifest_path, manifest)
    with pytest.raises(local_release.ReleaseError, match="identity"):
        local_release.build_local(
            manifest_path,
            state_dir=state_dir,
            execute=True,
            runner=lambda *_a, **_k: pytest.fail("must not build"),
        )
    with pytest.raises(local_release.ReleaseError, match="identity"):
        local_release.reconcile_local_build(
            manifest_path,
            state_dir=state_dir,
            inspector=lambda _: pytest.fail("must not inspect"),
        )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture()
def conventional_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "service"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Release Test")
    (repo / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: initial service")
    git(repo, "tag", "v1.2.3")
    base_sha = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("print('feature')\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "feat(api): add local release planning")
    return repo, base_sha


def service() -> dict:
    return {
        "service_name": "eng-platform-api",
        "repository": "diegomad14/gcp-engineering-platform-api",
        "project_id": "cgm-assistant-prod",
        "region": "us-central1",
        "quality": {"enabled": True, "profile": "python", "coverage_threshold": 70},
        "deployment": {
            "image_name": "eng-platform-api",
            "artifact_repository": "cgm-sanplat-repo",
            "build_context": ".",
        },
    }


def snapshot(repo: Path) -> dict:
    return local_release.repo_snapshot(repo)


def test_conventional_commit_version_and_notes(conventional_repo):
    repo, base_sha = conventional_repo
    result = local_release.version_plan(
        repo,
        service(),
        snapshot(repo),
        base_sha=base_sha,
    )

    assert result["tag"] == "v1.3.0"
    assert result["change_level"] == "minor"
    assert "add local release planning" in result["notes"]
    assert len(result["notes_sha256"]) == 64


def test_breaking_and_non_release_commit_rules():
    breaking = local_release.Commit(
        "a" * 40,
        "feat!: replace release contract",
        "",
        "",
    )
    docs = local_release.Commit("b" * 40, "docs: clarify operator guide", "", "")
    body_breaking = local_release.Commit(
        "c" * 40,
        "refactor: simplify planner",
        "BREAKING CHANGE: manifest shape changed",
        "",
    )

    assert local_release.commit_release_level(breaking) == "major"
    assert local_release.commit_release_level(body_breaking) == "major"
    assert local_release.commit_release_level(docs) is None


def test_existing_conflicting_tag_fails_closed(conventional_repo):
    repo, base_sha = conventional_repo
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "conflict-tag", base_sha)
    (repo / "conflict.txt").write_text("unmerged release tag\n", encoding="utf-8")
    git(repo, "add", "conflict.txt")
    git(repo, "commit", "-m", "chore: create conflicting local tag")
    git(repo, "tag", "v1.3.0")
    conflicting_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")

    with pytest.raises(
        local_release.ReleaseError, match="exists locally on another SHA"
    ):
        local_release.version_plan(repo, service(), snapshot(repo), base_sha=base_sha)

    assert git(repo, "rev-parse", "refs/tags/v1.3.0") == conflicting_sha
    assert head != base_sha


def test_prepare_reuses_release_identity_but_creates_new_execution(
    conventional_repo, tmp_path
):
    repo, base_sha = conventional_repo
    state_dir = tmp_path / "state"

    first = local_release.prepare(
        ROOT,
        "eng-platform-api",
        repo,
        state_dir=state_dir,
        base_sha=base_sha,
    )
    second = local_release.prepare(
        ROOT,
        "eng-platform-api",
        repo,
        state_dir=state_dir,
        base_sha=base_sha,
    )

    assert first["manifest"]["release_id"] == second["manifest"]["release_id"]
    assert first["execution"]["execution_id"] != second["execution"]["execution_id"]
    assert first["execution"]["attempt"] == 1
    assert second["execution"]["attempt"] == 2
    assert second["execution"]["reused_release_identity"] is True
    assert second["execution"]["remote_effects"] == []


def test_prepare_rejects_dirty_tree_without_explicit_non_publishable_override(
    conventional_repo, tmp_path
):
    repo, base_sha = conventional_repo
    (repo / "unreviewed.txt").write_text("not committed\n", encoding="utf-8")

    with pytest.raises(local_release.ReleaseError, match="Working tree is dirty"):
        local_release.prepare(
            ROOT,
            "eng-platform-api",
            repo,
            state_dir=tmp_path / "state",
            base_sha=base_sha,
        )


def test_artifact_key_changes_when_dependency_input_changes(conventional_repo):
    repo, base_sha = conventional_repo
    snap = snapshot(repo)
    version = local_release.version_plan(repo, service(), snap, base_sha=base_sha)
    version["source_sha"] = snap["sha"]
    before = local_release.build_inputs(repo, service(), version)["reuse_key"]

    (repo / "pyproject.toml").write_text(
        "[project]\nname='changed'\n", encoding="utf-8"
    )
    after = local_release.build_inputs(repo, service(), version)["reuse_key"]

    assert before != after


def test_evidence_reuse_requires_exact_sha_base_policy_and_toolchain(
    conventional_repo, tmp_path
):
    repo, base_sha = conventional_repo
    snap = snapshot(repo)
    toolchain = local_release.toolchain_fingerprint(repo)
    report_path = tmp_path / "quality-report.json"
    report = {
        "service_name": service()["service_name"],
        "repository": service()["repository"],
        "commit_sha": snap["sha"],
        "base_sha": base_sha,
        "policy_version": local_release.POLICY_ID,
        "checks": [{"name": "Tests", "status": "PASSED"}],
        "changed_lines": 0,
        "differential_coverage": None,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    evidence = {
        "schema_version": local_release.SCHEMA_VERSION,
        "service_name": service()["service_name"],
        "repository": service()["repository"],
        "commit_sha": snap["sha"],
        "base_sha": base_sha,
        "policy_id": local_release.POLICY_ID,
        "quality_gate_status": "PASSED",
        "policy_status": "PASSED",
        "expires_at": local_release.iso(local_release.utc_now() + timedelta(hours=1)),
        "toolchain": toolchain,
        "report_path": str(report_path),
        "report_sha256": local_release.file_digest(report_path),
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    assert (
        local_release.validate_evidence(
            path,
            service=service(),
            snapshot=snap,
            base_sha=base_sha,
            current_toolchain=toolchain,
        )["policy_status"]
        == "PASSED"
    )

    with pytest.raises(local_release.ReleaseError, match="base SHA mismatch"):
        local_release.validate_evidence(
            path,
            service=service(),
            snapshot=snap,
            base_sha="0" * 40,
            current_toolchain=toolchain,
        )


def test_workflow_inventory_marks_actions_and_configured_limits_as_non_observed(
    tmp_path,
):
    repo = tmp_path / "repo"
    workflow_dir = repo / ".github/workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "platform-deploy.yml").write_text(
        """name: Platform Deploy\n\non:\n  pull_request:\n  workflow_dispatch:\n\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@deadbeef\n      - run: timeout=1800\n""",
        encoding="utf-8",
    )

    inventory = local_release.workflow_inventory(repo)

    assert inventory["actions_dependency"] is True
    assert inventory["pre_merge_checks"][0]["actions_dependency"] is True
    limit = inventory["files"][0]["configured_limits"][0]
    assert limit["configured_seconds"] == 1800
    assert limit["observed_duration_seconds"] is None


def test_local_release_identity_catalog_and_version_rejections(
    conventional_repo, tmp_path
):
    repo, base_sha = conventional_repo
    head = git(repo, "rev-parse", "HEAD")

    assert local_release.require_sha("A" * 40) == "a" * 40
    with pytest.raises(local_release.ReleaseError, match="full 40-character"):
        local_release.require_sha("short")
    assert (
        local_release.parse_repository("git@github.com:owner/repo.git") == "owner/repo"
    )
    assert local_release.parse_repository("https://example.invalid/repo") == ""
    assert (
        local_release.load_catalog(ROOT, "eng-platform-api")["service_name"]
        == "eng-platform-api"
    )

    with pytest.raises(local_release.ReleaseError, match="does not match"):
        local_release.validate_catalog_repository(
            {"repository": "owner/other"}, service()
        )
    assert local_release.latest_release(repo, head)["name"] == "v1.2.3"
    assert local_release.commit_log(repo, None)
    assert local_release.release_notes([], "v0.0.1") == "# v0.0.1\n"

    explicit = local_release.version_plan(
        repo, service(), snapshot(repo), base_sha=base_sha, requested_version="v2.0.0"
    )
    assert explicit["tag"] == "v2.0.0"
    with pytest.raises(local_release.ReleaseError, match="greater than"):
        local_release.version_plan(
            repo,
            service(),
            snapshot(repo),
            base_sha=base_sha,
            requested_version="v1.0.0",
        )
    with pytest.raises(local_release.ReleaseError, match="differ"):
        local_release.resolve_base_sha(repo, head, requested=head)

    no_tag = tmp_path / "no-tag"
    no_tag.mkdir()
    git(no_tag, "init", "-b", "main")
    git(no_tag, "config", "user.email", "test@example.invalid")
    git(no_tag, "config", "user.name", "Release Test")
    (no_tag / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (no_tag / "app.py").write_text("print('initial')\n", encoding="utf-8")
    git(no_tag, "add", ".")
    git(no_tag, "commit", "-m", "fix: initial release")
    no_tag_snapshot = snapshot(no_tag)
    with pytest.raises(local_release.ReleaseError, match="No reachable SemVer"):
        local_release.version_plan(no_tag, service(), no_tag_snapshot)
    initial = local_release.version_plan(
        no_tag, service(), no_tag_snapshot, requested_version="v0.1.0"
    )
    assert initial["previous_tag"] == ""


def test_local_release_workflow_context_and_state_helpers(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert local_release.workflow_inventory(empty)["files"] == []
    workflow_dir = empty / ".github/workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "release.yml").write_text(
        """name: Release
on:
  push:
  pull_request:
  workflow_dispatch:
  workflow_call:
jobs:
  release:
    steps:
      - uses: actions/checkout@deadbeef
      - run: semantic-release && gh release create v1.0.0
      - run: git push origin refs/tags/v1.0.0
      - run: docker push image
      - run: gcloud builds submit --source .
      - run: timeout-minutes: 2 --task-timeout=120
""",
        encoding="utf-8",
    )
    inventory = local_release.workflow_inventory(empty)
    assert inventory["actions_dependency"] is True
    assert inventory["cloud_build_references"]
    assert inventory["pre_merge_checks"][0]["name"] == "Release"
    assert {
        item["configured_seconds"]
        for item in inventory["files"][0]["configured_limits"]
    } == {120, 120}

    (empty / ".dockerignore").write_text("ignored\n", encoding="utf-8")
    (empty / "Dockerfile").write_text("FROM python:3.11\n", encoding="utf-8")
    (empty / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (empty / "ignored").write_text("ignored\n", encoding="utf-8")
    (empty / ".git").mkdir()
    (empty / "__pycache__").mkdir()
    paths = list(local_release.iter_context_files(empty))
    assert empty / "Dockerfile" in paths
    assert empty / "ignored" in paths
    assert all(
        ".git" not in path.parts and "__pycache__" not in path.parts for path in paths
    )
    assert len(local_release.context_fingerprint(empty)) == 64
    with pytest.raises(local_release.ReleaseError, match="does not exist"):
        local_release.context_fingerprint(tmp_path / "missing")

    monkeypatch.setenv("CGM_RELEASE_STATE_DIR", str(tmp_path / "configured"))
    assert local_release.default_state_dir() == (tmp_path / "configured").resolve()
    monkeypatch.delenv("CGM_RELEASE_STATE_DIR")
    assert local_release.default_state_dir().name == "cgm-release"
    with local_release.local_lock(tmp_path / "state"):
        assert (tmp_path / "state" / "release.lock").exists()

    monkeypatch.setattr(local_release.shutil, "which", lambda _name: None)
    assert local_release.tool_info("docker")["available"] is False
    monkeypatch.setattr(local_release.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        local_release,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["tool"], 0, stdout="tool 1.0\n", stderr=""
        ),
    )
    assert local_release.tool_info("docker")["version"] == "tool 1.0"
    assert local_release.toolchain_fingerprint(empty)["sha256"]


def test_local_release_quality_evidence_and_differential_validation(
    conventional_repo, tmp_path, monkeypatch
):
    repo, base_sha = conventional_repo
    report = {"checks": [{"name": "tests", "status": "PASSED"}]}
    assert local_release.quality_status(report) == ("PASSED", [])
    assert local_release.quality_status({})[0] == "FAILED"
    assert local_release.quality_status({"checks": []})[0] == "FAILED"
    assert (
        local_release.quality_status(
            {"checks": [{"name": "build", "status": "SKIPPED"}]}
        )[0]
        == "FAILED"
    )
    failed, errors = local_release.quality_status(
        {"checks": [{"name": "format", "status": "FAILED"}]}
    )
    assert failed == "FAILED" and errors

    valid_diff = {
        "policy_version": "oss-v2",
        "base_sha": "a" * 40,
        "differential_threshold": 80,
        "differential_coverage": 90,
        "changed_lines": 10,
    }
    diff_path = tmp_path / "diff.json"
    diff_path.write_text(json.dumps(valid_diff), encoding="utf-8")
    assert (
        local_release.differential_status(
            diff_path, base_sha="a" * 40, profile="python"
        )[0]
        == "PASSED"
    )
    assert (
        local_release.differential_status(None, base_sha="a" * 40, profile="python")[0]
        == "FAILED"
    )
    assert (
        local_release.differential_status(None, base_sha="a" * 40, profile="static")[0]
        == "PASSED"
    )
    invalid_diff = {
        **valid_diff,
        "policy_version": "other",
        "differential_threshold": 70,
    }
    diff_path.write_text(json.dumps(invalid_diff), encoding="utf-8")
    assert (
        local_release.differential_status(
            diff_path, base_sha="b" * 40, profile="python"
        )[0]
        == "FAILED"
    )

    evidence = {
        "schema_version": local_release.SCHEMA_VERSION,
        "service_name": "eng-platform-api",
        "repository": service()["repository"],
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "policy_id": local_release.POLICY_ID,
        "quality_gate_status": "PASSED",
        "policy_status": "PASSED",
        "expires_at": local_release.iso(local_release.utc_now() + timedelta(hours=1)),
        "toolchain": {"sha256": "tool"},
    }
    report_path = tmp_path / "quality-report.json"
    report = {
        "service_name": service()["service_name"],
        "repository": service()["repository"],
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "policy_version": local_release.POLICY_ID,
        "checks": [{"name": "Tests", "status": "PASSED"}],
        "changed_lines": 0,
        "differential_coverage": None,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    evidence["report_path"] = str(report_path)
    evidence["report_sha256"] = local_release.file_digest(report_path)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(local_release.ReleaseError, match="invalid"):
        bad = {**evidence, "expires_at": "not-a-date"}
        path.write_text(json.dumps(bad), encoding="utf-8")
        local_release.validate_evidence(
            path,
            service=service(),
            snapshot={"sha": "a" * 40},
            base_sha="b" * 40,
        )
    with pytest.raises(local_release.ReleaseError, match="stale"):
        stale = {**evidence, "expires_at": "2000-01-01T00:00:00+00:00"}
        path.write_text(json.dumps(stale), encoding="utf-8")
        local_release.validate_evidence(
            path,
            service=service(),
            snapshot={"sha": "a" * 40},
            base_sha="b" * 40,
        )
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(local_release.ReleaseError, match="toolchain"):
        local_release.validate_evidence(
            path,
            service=service(),
            snapshot={"sha": "a" * 40},
            base_sha="b" * 40,
            current_toolchain={"sha256": "different"},
        )
    assert (
        local_release.find_reusable_evidence(
            tmp_path / "state",
            service=service(),
            snapshot={"sha": "a" * 40},
            base_sha="b" * 40,
            current_toolchain={"sha256": "tool"},
        )
        is None
    )

    monkeypatch.setattr(
        local_release, "toolchain_fingerprint", lambda _repo: {"sha256": "tool"}
    )
    monkeypatch.setattr(
        local_release, "validate_evidence", lambda *_args, **_kwargs: evidence
    )
    manifest = local_release.build_manifest(
        ROOT,
        "eng-platform-api",
        repo,
        state_dir=tmp_path / "state",
        base_sha_arg=base_sha,
        quality_evidence_path=path,
    )
    assert manifest["quality"]["status"] == "PASSED"


def test_local_release_quality_runner_and_local_build_doubles(
    conventional_repo, tmp_path, monkeypatch
):
    repo, base_sha = conventional_repo
    state_dir = tmp_path / "state"
    real_report = {
        "service_name": service()["service_name"],
        "repository": service()["repository"],
        "commit_sha": local_release.repo_snapshot(repo)["sha"],
        "base_sha": base_sha,
        "policy_version": local_release.POLICY_ID,
        "checks": [{"name": "Tests", "status": "PASSED"}],
        "changed_lines": 1,
        "differential_coverage": 100,
    }
    diff_path = tmp_path / "diff.json"
    diff_path.write_text(
        json.dumps(
            {
                "policy_version": "oss-v2",
                "base_sha": base_sha,
                "differential_threshold": 80,
                "differential_coverage": 100,
                "changed_lines": 1,
            }
        ),
        encoding="utf-8",
    )

    def fake_quality_run(args, **_kwargs):
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(real_report), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="gate ok", stderr="")

    real_run = local_release.run

    def dispatch_run(args, **kwargs):
        if args and args[0] == "git":
            return real_run(args, **kwargs)
        return fake_quality_run(args, **kwargs)

    monkeypatch.setattr(local_release, "run", dispatch_run)
    monkeypatch.setattr(
        local_release, "toolchain_fingerprint", lambda _repo: {"sha256": "tool"}
    )
    status, result = local_release.run_quality(
        ROOT,
        "eng-platform-api",
        repo,
        state_dir=state_dir,
        base_sha_arg=base_sha,
        differential_report=diff_path,
        command_overrides={"format": "ruff format --check ."},
    )
    assert status == 0
    assert result["evidence"]["policy_status"] == "PASSED"
    assert Path(result["evidence_path"]).exists()

    prepared = local_release.prepare(
        ROOT, "eng-platform-api", repo, state_dir=state_dir / "build", base_sha=base_sha
    )
    manifest_path = tmp_path / "manifest.json"
    local_release.write_json(manifest_path, prepared["manifest"])
    planned = local_release.build_local(
        manifest_path, state_dir=state_dir / "build", execute=False
    )
    assert planned["action"] == "planned"
    available = {
        "reuse_key": prepared["manifest"]["artifact"]["reuse_key"],
        "status": "AVAILABLE",
        "image": prepared["manifest"]["artifact"]["local_image"],
        "image_id": "sha256:" + "1" * 64,
        "digest": "sha256:" + "3" * 64,
        "metadata_path": str(tmp_path / "reused-metadata.json"),
        "build_identity": local_release._build_identity(prepared["manifest"]),
    }
    _build_metadata(available["metadata_path"])
    local_release.write_json(
        local_release.state_paths(state_dir / "build")["artifacts"] / "available.json",
        available,
    )
    assert (
        local_release.build_local(
            manifest_path, state_dir=state_dir / "build", execute=False
        )["action"]
        == "reused"
    )

    built_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    built_manifest["quality"]["status"] = "PASSED"
    local_release.write_json(manifest_path, built_manifest)
    calls = []

    def fake_docker(args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="sha256:" + "1" * 64 + "\n", stderr=""
            )
        _build_metadata(args[args.index("--metadata-file") + 1])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(local_release, "run", fake_docker)
    built = local_release.build_local(
        manifest_path, state_dir=state_dir / "built", execute=True
    )
    assert built["action"] == "built"
    assert built["artifact"]["digest"] == "sha256:" + "3" * 64
    assert built["artifact"]["remote_published"] is False
    assert any("--load" in call for call in calls)
    assert any("--provenance=false" in call for call in calls)


def test_two_build_processes_share_one_deterministic_artifact(
    conventional_repo, tmp_path
):
    repo, base_sha = conventional_repo
    state_dir = tmp_path / "process-state"
    prepared = local_release.prepare(
        ROOT, "eng-platform-api", repo, state_dir=state_dir, base_sha=base_sha
    )
    manifest_path = tmp_path / "process-manifest.json"
    value = prepared["manifest"]
    value["quality"]["status"] = "PASSED"
    local_release.write_json(manifest_path, value)

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    builds = context.Value("i", 0)

    processes = [
        context.Process(
            target=_synthetic_build_worker,
            args=(manifest_path, state_dir, ready, start, builds, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert [ready.get(timeout=10), ready.get(timeout=10)] == ["ready", "ready"]
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    actions = sorted([results.get(timeout=10), results.get(timeout=10)])
    assert actions == ["built", "reused"]
    assert builds.value == 1
    artifact_path = local_release._artifact_record_path(
        state_dir, value["artifact"]["reuse_key"]
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "AVAILABLE"
    assert artifact["digest"] == "sha256:" + "3" * 64
    assert (
        local_release.read_json(manifest_path)["artifact"]["digest"]
        == artifact["digest"]
    )
    assert artifact["artifact_id"] == f"artifact-{value['artifact']['reuse_key']}"


def test_reconcile_build_promotes_observed_image_without_rebuild(tmp_path):
    value = {
        "schema_version": local_release.SCHEMA_VERSION,
        "release_id": "release-build-reconcile",
        "service_name": "example",
        "repository": "diegomad14/example",
        "source": {"sha": "a" * 40, "base_sha": "b" * 40},
        "version": {"tag": "v1.0.0"},
        "quality": {"status": "PASSED"},
        "artifact": {
            "reuse_key": "c" * 64,
            "local_image": "local/example:v1.0.0",
            "context": str(tmp_path),
        },
    }
    manifest_path = tmp_path / "manifest.json"
    local_release.write_json(manifest_path, value)
    state_dir = tmp_path / "state"
    record_path = local_release._artifact_record_path(
        state_dir, value["artifact"]["reuse_key"]
    )
    metadata_path = tmp_path / "metadata.json"
    _build_metadata(metadata_path, "sha256:" + "2" * 64)
    local_release.write_json(
        record_path,
        {
            "status": "BUILDING",
            "reuse_key": value["artifact"]["reuse_key"],
            "image": value["artifact"]["local_image"],
            "build_identity": local_release._build_identity(value),
            "metadata_path": str(metadata_path),
        },
    )

    result = local_release.reconcile_local_build(
        manifest_path,
        state_dir=state_dir,
        inspector=lambda record: {
            "reuse_key": record["reuse_key"],
            "image_id": "sha256:" + "2" * 64,
        },
    )

    assert result["action"] == "reconciled"
    assert result["artifact"]["status"] == "AVAILABLE"
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["artifact"]["image_id"] == "sha256:" + "2" * 64
    assert updated["artifact"]["digest"] == "sha256:" + "3" * 64


def test_local_release_cli_dispatch_and_resume_paths(
    conventional_repo, tmp_path, monkeypatch, capsys
):
    repo, base_sha = conventional_repo
    common = [
        "--service",
        "eng-platform-api",
        "--repo-path",
        str(repo),
        "--base-sha",
        base_sha,
        "--platform-root",
        str(ROOT),
    ]
    assert local_release.main(["version", *common]) == 0
    assert local_release.main(["notes", *common]) == 0
    assert local_release.main(["plan", *common]) == 0
    assert (
        local_release.main(
            ["doctor", "--repo-path", str(repo), "--platform-root", str(ROOT)]
        )
        == 0
    )
    adopted = local_release.main(
        [
            "adopt",
            "--service",
            "eng-platform-api",
            "--repo-path",
            str(repo),
            "--platform-root",
            str(ROOT),
        ]
    )
    assert adopted == 0
    prepared = local_release.prepare(
        ROOT, "eng-platform-api", repo, state_dir=tmp_path / "state", base_sha=base_sha
    )
    manifest_path = Path(prepared["manifest_path"])
    assert (
        local_release.main(
            [
                "build",
                "--manifest",
                str(manifest_path),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )
    assert (
        local_release.main(
            [
                "resume",
                "--release-id",
                prepared["manifest"]["release_id"],
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )

    monkeypatch.setattr(
        "scripts.release.release_lifecycle.publish",
        lambda *args, **kwargs: {"simulated": True},
    )
    assert (
        local_release.main(
            [
                "publish",
                "--manifest",
                str(manifest_path),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out


def test_register_parser_uses_platform_url_environment(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_API_URL", "https://platform.example")
    args = local_release.parser().parse_args(
        ["register", "--manifest", "manifest.json"]
    )
    assert args.platform_api_url == "https://platform.example"
