"""GitHub App effects for provider-neutral checks and release publication."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from github import GithubIntegration
from github.GithubException import GithubException

from ..config import config
from .github_deployments import github_client

CHECK_NAMES = {
    "quality": "Engineering Platform / quality",
    "workflows": "Engineering Platform / workflows",
    "title": "Engineering Platform / conventional-title",
    "release": "Engineering Platform / release",
}


class GitHubReleaseConflict(RuntimeError):
    pass


def repository_is_private(repository: str) -> bool:
    return bool(github_client().get_repo(repository).private)


def set_repository_execution_mode(repository: str, mode: str) -> None:
    """Set the server-owned Actions skip variable for a private repository."""
    if mode not in {"github_actions", "cloud_build"}:
        raise ValueError("Invalid repository execution mode")
    repo = github_client().get_repo(repository)
    if not bool(getattr(repo, "private", False)):
        return
    name = config.release_orchestrator.github_mode_variable
    if not name:
        raise RuntimeError("GitHub execution mode variable is not configured")
    requester = repo._requester  # type: ignore[attr-defined]
    path = f"/repos/{repository}/actions/variables/{name}"
    try:
        requester.requestJsonAndCheck(
            "PATCH", path, input={"name": name, "value": mode}
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
        requester.requestJsonAndCheck(
            "POST",
            f"/repos/{repository}/actions/variables",
            input={"name": name, "value": mode},
        )


def upsert_check(
    *,
    repository: str,
    head_sha: str,
    kind: str,
    status: str,
    conclusion: str | None = None,
    details_url: str = "",
    summary: str = "",
    check_run_id: int | None = None,
    external_id: str = "",
) -> int:
    """Publish one canonical App check regardless of the underlying executor."""
    if kind not in CHECK_NAMES:
        raise ValueError("Unknown Engineering Platform check")
    if status not in {"queued", "in_progress", "completed"}:
        raise ValueError("Invalid check status")
    if status == "completed" and conclusion not in {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
    }:
        raise ValueError("Completed check requires a valid conclusion")
    repo = github_client().get_repo(repository)
    output = {"title": CHECK_NAMES[kind], "summary": summary[:65_000] or status}
    if not check_run_id and external_id:
        # GitHub has no idempotency key for Check Run creation. The stable
        # external ID recovers a create whose successful response was lost.
        for existing in repo.get_commit(head_sha).get_check_runs(
            check_name=CHECK_NAMES[kind]
        ):
            if str(getattr(existing, "external_id", "")) == external_id:
                check_run_id = int(existing.id)
                break
    check_fields: dict[str, Any] = {
        "name": CHECK_NAMES[kind],
        "status": status,
        "output": output,
    }
    if conclusion:
        check_fields["conclusion"] = conclusion
    if details_url:
        check_fields["details_url"] = details_url
    if status == "completed":
        check_fields["completed_at"] = datetime.now(timezone.utc)
    if check_run_id:
        check = repo.get_check_run(check_run_id)
        check.edit(**check_fields)  # type: ignore[arg-type]
        return int(check.id)
    check_fields["head_sha"] = head_sha
    if external_id:
        check_fields["external_id"] = external_id
    if status == "in_progress":
        check_fields["started_at"] = datetime.now(timezone.utc)
    check = repo.create_check_run(**check_fields)  # type: ignore[arg-type]
    return int(check.id)


def current_default_sha(repository: str) -> str:
    repo = github_client().get_repo(repository)
    return str(repo.get_branch(repo.default_branch).commit.sha)


def is_fast_forward(repository: str, base_sha: str, head_sha: str) -> bool:
    comparison = github_client().get_repo(repository).compare(base_sha, head_sha)
    return str(getattr(comparison, "status", "")) in {"ahead", "identical"}


def workflow_run(repository: str, run_id: int):
    return github_client().get_repo(repository).get_workflow_run(run_id)


def repository_execution_mode(repository: str) -> str:
    """Read the server-managed mode used to skip private GitHub-hosted jobs."""
    repo = github_client().get_repo(repository)
    requester = repo._requester  # type: ignore[attr-defined]
    path = (
        f"/repos/{repository}/actions/variables/"
        f"{config.release_orchestrator.github_mode_variable}"
    )
    _, payload = requester.requestJsonAndCheck("GET", path)
    return str(payload.get("value", ""))


def dispatch_health_probe(repository: str, *, nonce: str) -> None:
    workflow = (
        github_client()
        .get_repo(repository)
        .get_workflow(config.release_orchestrator.github_health_workflow)
    )
    workflow.create_dispatch(ref="main", inputs={"probe_nonce": nonce})


def publish_release(execution: dict[str, Any]) -> dict[str, Any]:
    """Create a tag and GitHub Release idempotently using the GitHub App."""
    repository = str(execution["repository"])
    expected_sha = str(execution["head_sha"])
    plan = execution.get("release_plan") or {}
    tag = str(plan.get("git_tag", ""))
    if not tag or str(plan.get("release_type", "none")) == "none":
        raise ValueError("Release execution does not contain a publishable plan")
    if current_default_sha(repository) != expected_sha:
        raise GitHubReleaseConflict("Default branch moved after release validation")
    repo = github_client().get_repo(repository)
    try:
        reference = repo.get_git_ref(f"tags/{tag}")
        tag_sha = str(reference.object.sha)
        # Annotated tags point to a tag object; resolve it before comparison.
        if getattr(reference.object, "type", "") == "tag":
            tag_sha = str(repo.get_git_tag(tag_sha).object.sha)
        if tag_sha != expected_sha:
            raise GitHubReleaseConflict("Release tag already points to another commit")
    except GitHubReleaseConflict:
        raise
    except GithubException as exc:
        if exc.status != 404:
            raise
        reference = repo.create_git_ref(f"refs/tags/{tag}", expected_sha)

    try:
        release = repo.get_release(tag)
        if (
            str(getattr(release, "tag_name", tag)) != tag
            or bool(getattr(release, "draft", False))
            or bool(getattr(release, "prerelease", False))
            or str(getattr(release, "body", plan.get("notes", "")))
            != str(plan.get("notes", ""))
        ):
            raise GitHubReleaseConflict(
                "GitHub Release metadata conflicts with the authorized plan"
            )
    except GitHubReleaseConflict:
        raise
    except GithubException as exc:
        if exc.status != 404:
            raise
        release = repo.create_git_release(
            tag=tag,
            name=tag,
            message=str(plan.get("notes", "")),
            draft=False,
            prerelease=False,
            target_commitish=expected_sha,
        )
    return {
        "tag": tag,
        "tag_sha": expected_sha,
        "release_id": int(release.id),
        "release_url": str(release.html_url),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }


def installation_read_token(repository: str) -> tuple[str, str]:
    """Mint a short-lived token restricted to one repository and contents:read."""
    settings = config.github
    if not (settings.app_id and settings.installation_id and settings.private_key):
        raise RuntimeError("GitHub App authentication is not configured")
    repo = github_client().get_repo(repository)
    integration = GithubIntegration(int(settings.app_id), settings.private_key)
    _, payload = integration.requester.requestJsonAndCheck(
        "POST",
        f"/app/installations/{int(settings.installation_id)}/access_tokens",
        input={
            "repository_ids": [int(repo.id)],
            "permissions": {"contents": "read"},
        },
    )
    return str(payload["token"]), str(payload["expires_at"])
