"""GitHub-native tag discovery and deployment state projection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from github import Github, GithubIntegration

from ..config import config
from ..models import (
    DeploymentItem,
    DeploymentStage,
    DeploymentStageStatus,
    CatalogService,
    ReleaseTag,
    ReleaseTagPage,
)
from . import deployment_store

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_STAGES = [
    ("verify-release", "Verify release"),
    ("build", "Build image"),
    ("deploy-candidate", "Deploy candidate"),
    ("validate-candidate", "Validate candidate"),
    ("promote", "Promote production"),
    ("validate-production", "Validate production"),
]
_STAGE_TO_STATUS = {
    "verify-release": "VERIFYING_RELEASE",
    "build": "BUILDING",
    "deploy-candidate": "DEPLOYING_CANDIDATE",
    "validate-candidate": "VALIDATING_CANDIDATE",
    "promote": "PROMOTING",
    "validate-production": "VALIDATING_PRODUCTION",
    "rollback": "ROLLING_BACK",
}
_ROLLBACK_STAGES = [("rollback", "Roll back production")]
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"})
_LIVE_STATUSES = frozenset({"SUCCEEDED", "ROLLED_BACK"})


def default_stages(kind: str = "deploy") -> list[DeploymentStage]:
    stages = _ROLLBACK_STAGES if kind == "rollback" else _STAGES
    return [DeploymentStage(key=key, label=label) for key, label in stages]


def github_client() -> Github:
    github_config = config.github
    if github_config.token:
        return Github(github_config.token)
    if (
        github_config.app_id
        and github_config.installation_id
        and github_config.private_key
    ):
        integration = GithubIntegration(
            int(github_config.app_id), github_config.private_key
        )
        return integration.get_github_for_installation(
            int(github_config.installation_id)
        )
    raise RuntimeError("GitHub authentication is not configured")


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def list_tags(
    repository: str,
    service_name: str,
    cursor: str | None = None,
    limit: int = 10,
) -> ReleaseTagPage:
    offset = int(cursor or "0")
    if offset < 0:
        raise ValueError("Tag cursor must be non-negative")
    if config.mock_mode:
        mock_tags = [
            ReleaseTag(
                name=f"v0.{minor}.{patch}",
                sha=(f"{minor:x}{patch:x}" * 20)[:40],
                created_at=f"2026-07-{16 - index:02d}T12:00:00+00:00",
                url=f"https://github.com/{repository}/releases/tag/v0.{minor}.{patch}",
                eligible=index != 2,
                reason="This tag is already live in production"
                if index == 2
                else "",
            )
            for index, (minor, patch) in enumerate(
                [(5, 1), (5, 0), (4, 1), (4, 0), (3, 0), (2, 2), (2, 1), (2, 0)]
            )
        ]
        items = mock_tags[offset : offset + limit]
        next_cursor = (
            str(offset + len(items)) if offset + len(items) < len(mock_tags) else None
        )
        return ReleaseTagPage(items=items, next_cursor=next_cursor)
    repo = github_client().get_repo(repository)
    tags = repo.get_tags()
    page: list[ReleaseTag] = []
    previous = deployment_store.list_for_service(service_name, limit=100)
    active = next(
        (item for item in previous if item.status not in TERMINAL_STATUSES), None
    )
    current_live_tag = next(
        (item.tag for item in previous if item.status in _LIVE_STATUSES), None
    )
    for index, tag in enumerate(tags):
        if index < offset:
            continue
        if len(page) >= limit:
            break
        eligible = bool(_SEMVER.fullmatch(tag.name))
        reason = "" if eligible else "Tag does not follow semantic versioning"
        created_at = ""
        try:
            commit = repo.get_commit(tag.commit.sha)
            created_at = _iso(commit.commit.committer.date)
        except Exception:
            pass
        if eligible and active is not None:
            eligible = False
            reason = f"Deployment {active.tag} is already active for this service"
        elif eligible and tag.name == current_live_tag:
            eligible = False
            reason = "This tag is already live in production"
        page.append(
            ReleaseTag(
                name=tag.name,
                sha=tag.commit.sha,
                created_at=created_at,
                url=f"https://github.com/{repository}/releases/tag/{tag.name}",
                eligible=eligible,
                reason=reason,
            )
        )
    next_cursor = str(offset + len(page)) if len(page) == limit else None
    return ReleaseTagPage(items=page, next_cursor=next_cursor)


def get_tag(repository: str, service_name: str, name: str) -> ReleaseTag | None:
    cursor: str | None = None
    for _ in range(20):
        page = list_tags(repository, service_name, cursor=cursor, limit=100)
        match = next((item for item in page.items if item.name == name), None)
        if match or not page.next_cursor:
            return match
        cursor = page.next_cursor
    return None


def start_deployment(
    *, service: CatalogService, tag: ReleaseTag, requested_by: str
) -> DeploymentItem:
    repository = service.repository
    service_name = service.service_name
    now = datetime.now(timezone.utc).isoformat()
    if config.mock_mode:
        deployment_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        return DeploymentItem(
            id=deployment_id,
            service_name=service_name,
            repository=repository,
            tag=tag.name,
            sha=tag.sha,
            status="QUEUED",
            current_stage="queued",
            stages=default_stages(),
            requested_by=requested_by,
            created_at=now,
            updated_at=now,
        )
    repo = github_client().get_repo(repository)
    github_deployment = repo.create_deployment(
        ref=tag.name,
        task="deploy",
        auto_merge=False,
        required_contexts=[],
        environment=f"{service_name}-production",
        description=f"Deploy {service_name} {tag.name}",
        payload={"service_name": service_name, "tag": tag.name},
    )
    github_deployment.create_status(
        state="queued",
        description="Queued by Engineering Platform",
    )
    workflow = repo.get_workflow(
        service.deployment.workflow_file or config.github.deployment_workflow
    )
    workflow.create_dispatch(
        ref=tag.name,
        inputs={
            "service_name": service_name,
            "tag": tag.name,
            "github_deployment_id": str(github_deployment.id),
            "project_id": service.project_id,
            "region": service.region,
            "image_name": service.deployment.image_name or service_name,
            "artifact_repository": service.deployment.artifact_repository,
            "build_context": service.deployment.build_context,
            "health_path": service.deployment.health_path,
        },
    )
    return DeploymentItem(
        id=str(github_deployment.id),
        service_name=service_name,
        repository=repository,
        tag=tag.name,
        sha=tag.sha,
        status="QUEUED",
        current_stage="queued",
        stages=default_stages(),
        requested_by=requested_by,
        created_at=now,
        updated_at=now,
        github_deployment_id=github_deployment.id,
    )


def start_rollback(
    *, service: CatalogService, target: DeploymentItem, requested_by: str
) -> DeploymentItem:
    """Dispatch a traffic-only rollback to a previously succeeded revision."""
    repository = service.repository
    service_name = service.service_name
    now = datetime.now(timezone.utc).isoformat()
    if config.mock_mode:
        deployment_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        return DeploymentItem(
            id=deployment_id,
            service_name=service_name,
            repository=repository,
            tag=target.tag,
            sha=target.sha,
            kind="rollback",
            status="QUEUED",
            current_stage="queued",
            stages=default_stages("rollback"),
            requested_by=requested_by,
            created_at=now,
            updated_at=now,
        )
    repo = github_client().get_repo(repository)
    github_deployment = repo.create_deployment(
        ref=target.tag,
        task="rollback",
        auto_merge=False,
        required_contexts=[],
        environment=f"{service_name}-production",
        description=(
            f"Rollback {service_name} to {target.tag} "
            f"({target.production_revision})"
        ),
        payload={"service_name": service_name, "tag": target.tag},
    )
    github_deployment.create_status(
        state="queued",
        description="Rollback queued by Engineering Platform",
    )
    workflow = repo.get_workflow(config.github.rollback_workflow)
    workflow.create_dispatch(
        ref=target.tag,
        inputs={
            "service_name": service_name,
            "target_tag": target.tag,
            "target_revision": target.production_revision,
            "github_deployment_id": str(github_deployment.id),
            "project_id": service.project_id,
            "region": service.region,
            "health_path": service.deployment.health_path,
        },
    )
    return DeploymentItem(
        id=str(github_deployment.id),
        service_name=service_name,
        repository=repository,
        tag=target.tag,
        sha=target.sha,
        kind="rollback",
        status="QUEUED",
        current_stage="queued",
        stages=default_stages("rollback"),
        requested_by=requested_by,
        created_at=now,
        updated_at=now,
        github_deployment_id=github_deployment.id,
    )


def _job_stage(name: str) -> str | None:
    normalized = name.lower().replace("_", "-")
    aliases = {
        "verify": "verify-release",
        "build": "build",
        "candidate deploy": "deploy-candidate",
        "deploy candidate": "deploy-candidate",
        "candidate validation": "validate-candidate",
        "validate candidate": "validate-candidate",
        "promote": "promote",
        "production validation": "validate-production",
        "validate production": "validate-production",
        "rollback": "rollback",
    }
    return next(
        (stage for token, stage in aliases.items() if token in normalized), None
    )


def _stage_status(job: Any) -> DeploymentStageStatus:
    if getattr(job, "status", "") != "completed":
        return "running" if getattr(job, "status", "") == "in_progress" else "pending"
    conclusion = getattr(job, "conclusion", "")
    if conclusion == "success":
        return "succeeded"
    if conclusion in {"skipped", "neutral"}:
        return "skipped"
    return "failed"


def _run_id(url: str) -> int | None:
    match = re.search(r"/actions/runs/(\d+)", url)
    return int(match.group(1)) if match else None


def _metadata_from_statuses(repo: Any, item: DeploymentItem) -> int | None:
    """Read workflow and revision evidence from GitHub Deployment statuses."""
    if not item.github_deployment_id:
        return None
    deployment = repo.get_deployment(item.github_deployment_id)
    discovered_run_id: int | None = None
    for status in deployment.get_statuses():
        log_url = str(
            getattr(status, "log_url", "") or getattr(status, "target_url", "") or ""
        )
        discovered_run_id = discovered_run_id or _run_id(log_url)
        description = str(getattr(status, "description", "") or "")
        environment_url = str(getattr(status, "environment_url", "") or "")
        if description.startswith("candidate_revision="):
            item.candidate_revision = description.removeprefix("candidate_revision=")
            item.candidate_url = environment_url or item.candidate_url
        elif description.startswith("production_revision="):
            item.production_revision = description.removeprefix("production_revision=")
            item.production_url = environment_url or item.production_url
    return discovered_run_id


def refresh(item: DeploymentItem) -> DeploymentItem:
    """Project GitHub workflow jobs into the platform's friendly stage model."""
    if config.mock_mode:
        return item
    repo = github_client().get_repo(item.repository)
    try:
        run_id = item.github_run_id or _metadata_from_statuses(repo, item)
        run = repo.get_workflow_run(run_id) if run_id else None
    except Exception:
        item.error = "Unable to read GitHub workflow"
        return item
    if run is None:
        return item

    item.github_run_id = run.id
    item.github_run_url = run.html_url
    item.logs_url = run.html_url
    item.updated_at = _iso(run.updated_at)
    stages = {stage.key: stage for stage in default_stages(item.kind)}
    rollback_status = None
    for job in run.jobs():
        key = _job_stage(job.name)
        if key == "rollback":
            rollback_status = _stage_status(job)
            if key not in stages:
                continue
        elif key not in stages:
            continue
        stage = stages[key]
        stage.status = _stage_status(job)
        stage.started_at = _iso(getattr(job, "started_at", None))
        stage.completed_at = _iso(getattr(job, "completed_at", None))
        if getattr(job, "html_url", ""):
            stage.details = job.html_url
        if getattr(job, "started_at", None) and getattr(job, "completed_at", None):
            stage.duration_seconds = (job.completed_at - job.started_at).total_seconds()
    item.stages = list(stages.values())

    active = next((stage for stage in item.stages if stage.status == "running"), None)
    failed = next((stage for stage in item.stages if stage.status == "failed"), None)
    if rollback_status == "running":
        item.status = "ROLLING_BACK"
        item.current_stage = "rollback"
    elif rollback_status == "succeeded":
        item.status = "ROLLED_BACK"
        item.current_stage = "rollback"
    elif rollback_status == "failed":
        item.status = "ROLLBACK_FAILED"
        item.current_stage = "rollback"
    elif active:
        item.status = _STAGE_TO_STATUS[active.key]  # type: ignore[assignment]
        item.current_stage = active.key
    elif failed or run.conclusion in {"failure", "cancelled", "timed_out"}:
        item.status = "FAILED"
        item.current_stage = failed.key if failed else "failed"
        item.error = f"{failed.label if failed else 'Workflow'} failed"
    elif run.conclusion == "success":
        item.status = "SUCCEEDED"
        item.current_stage = "complete"
    return item
