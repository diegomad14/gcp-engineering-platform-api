import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.release import local_release


ROOT = Path(__file__).parents[1]


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
