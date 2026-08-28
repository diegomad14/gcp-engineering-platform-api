"""GitHub-native tag discovery and deployment state projection."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
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
_TAG_CACHE_TTL_SECONDS = 30
_tag_metadata_cache: dict[tuple[str, int, int], tuple[float, ReleaseTagPage]] = {}
_tag_metadata_cache_lock = Lock()
GITHUB_WORKFLOW_DISPATCH_FAILED = "GitHub workflow dispatch failed"
GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED = "GitHub rollback workflow dispatch failed"


class GitHubDispatchError(RuntimeError):
    """A GitHub Deployment was created but its workflow could not be dispatched."""

    def __init__(self, item: DeploymentItem):
        self.item = item
        super().__init__(item.error or GITHUB_WORKFLOW_DISPATCH_FAILED)


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
                reason="This tag is already live in production" if index == 2 else "",
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
    metadata = _tag_metadata_page(repository, offset, limit)
    previous = deployment_store.list_for_service(service_name, limit=100)
    active = next(
        (item for item in previous if item.status not in TERMINAL_STATUSES), None
    )
    current_live_tag = next(
        (item.tag for item in previous if item.status in _LIVE_STATUSES), None
    )
    page: list[ReleaseTag] = []
    for tag in metadata.items:
        eligible = bool(_SEMVER.fullmatch(tag.name))
        reason = "" if eligible else "Tag does not follow semantic versioning"
        if eligible and active is not None:
            eligible = False
            reason = f"Deployment {active.tag} is already active for this service"
        elif eligible and tag.name == current_live_tag:
            eligible = False
            reason = "This tag is already live in production"
        page.append(tag.model_copy(update={"eligible": eligible, "reason": reason}))
    return ReleaseTagPage(items=page, next_cursor=metadata.next_cursor)


def _tag_metadata_page(repository: str, offset: int, limit: int) -> ReleaseTagPage:
    key = (repository, offset, limit)
    with _tag_metadata_cache_lock:
        cached = _tag_metadata_cache.get(key)
        now = monotonic()
        if cached and now - cached[0] < _TAG_CACHE_TTL_SECONDS:
            return cached[1]

        repo = github_client().get_repo(repository)
        selected: list[Any] = []
        for index, tag in enumerate(repo.get_tags()):
            if index < offset:
                continue
            if len(selected) >= limit:
                break
            selected.append(tag)

        def metadata(tag: Any) -> ReleaseTag:
            created_at = ""
            try:
                commit = repo.get_commit(tag.commit.sha)
                created_at = _iso(commit.commit.committer.date)
            # Commit metadata is optional evidence; the tag remains usable.
            except Exception:  # nosec B110
                pass
            return ReleaseTag(
                name=tag.name,
                sha=tag.commit.sha,
                created_at=created_at,
                url=f"https://github.com/{repository}/releases/tag/{tag.name}",
            )

        workers = min(5, max(1, len(selected)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            items = list(executor.map(metadata, selected))
        result = ReleaseTagPage(
            items=items,
            next_cursor=str(offset + len(items)) if len(items) == limit else None,
        )
        _tag_metadata_cache[key] = (monotonic(), result)
        return result


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
    item = DeploymentItem(
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
    try:
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
    except Exception as exc:
        item.status = "FAILED"
        item.current_stage = "dispatch"
        item.error = GITHUB_WORKFLOW_DISPATCH_FAILED
        try:
            github_deployment.create_status(
                state="failure",
                description=item.error,
            )
        except Exception:
            item.error = f"{GITHUB_WORKFLOW_DISPATCH_FAILED}; status update failed"
        raise GitHubDispatchError(item) from exc
    return item


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
            production_revision=target.production_revision,
        )
    repo = github_client().get_repo(repository)
    github_deployment = repo.create_deployment(
        ref=target.tag,
        task="rollback",
        auto_merge=False,
        required_contexts=[],
        environment=f"{service_name}-production",
        description=(
            f"Rollback {service_name} to {target.tag} ({target.production_revision})"
        ),
        payload={"service_name": service_name, "tag": target.tag},
    )
    item = DeploymentItem(
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
        production_revision=target.production_revision,
    )
    try:
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
    except Exception as exc:
        item.status = "FAILED"
        item.current_stage = "dispatch"
        item.error = GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED
        try:
            github_deployment.create_status(
                state="failure",
                description=item.error,
            )
        except Exception:
            item.error = (
                f"{GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED}; status update failed"
            )
        raise GitHubDispatchError(item) from exc
    return item


def _retry_workflow_and_inputs(
    repo: Any, service: CatalogService, item: DeploymentItem, target_revision: str
) -> tuple[Any, dict[str, str]]:
    if item.kind == "rollback":
        revision = item.production_revision or target_revision
        if not revision:
            raise ValueError("Rollback retry has no target revision")
        workflow = repo.get_workflow(config.github.rollback_workflow)
        inputs = {
            "service_name": service.service_name,
            "target_tag": item.tag,
            "target_revision": revision,
            "github_deployment_id": str(item.github_deployment_id),
            "project_id": service.project_id,
            "region": service.region,
            "health_path": service.deployment.health_path,
        }
        item.production_revision = revision
        return workflow, inputs
    workflow = repo.get_workflow(
        service.deployment.workflow_file or config.github.deployment_workflow
    )
    inputs = {
        "service_name": service.service_name,
        "tag": item.tag,
        "github_deployment_id": str(item.github_deployment_id),
        "project_id": service.project_id,
        "region": service.region,
        "image_name": service.deployment.image_name or service.service_name,
        "artifact_repository": service.deployment.artifact_repository,
        "build_context": service.deployment.build_context,
        "health_path": service.deployment.health_path,
    }
    return workflow, inputs


def retry_dispatch(
    *,
    service: CatalogService,
    item: DeploymentItem,
    target_revision: str = "",
) -> DeploymentItem:
    """Retry a dispatch that failed before GitHub created a workflow run.

    The existing GitHub Deployment is reused so the retry preserves the
    platform id, release SHA/tag and idempotency correlation.
    """
    if item.status != "FAILED" or item.current_stage != "dispatch":
        raise ValueError("Only failed dispatches can be retried")
    if not item.github_deployment_id:
        raise ValueError("Failed dispatch has no GitHub Deployment id")
    if (
        item.service_name != service.service_name
        or item.repository != service.repository
    ):
        raise ValueError("Failed dispatch does not match the selected service")

    repo = github_client().get_repo(item.repository)
    github_deployment = repo.get_deployment(item.github_deployment_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        github_deployment.create_status(
            state="queued",
            description="Dispatch retry queued by Engineering Platform",
        )
        workflow, inputs = _retry_workflow_and_inputs(
            repo, service, item, target_revision
        )
        workflow.create_dispatch(ref=item.tag, inputs=inputs)
    except Exception as exc:
        item.status = "FAILED"
        item.current_stage = "dispatch"
        item.updated_at = now
        item.error = (
            GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED
            if item.kind == "rollback"
            else GITHUB_WORKFLOW_DISPATCH_FAILED
        )
        try:
            github_deployment.create_status(state="failure", description=item.error)
        except Exception:
            item.error = f"{item.error}; status update failed"
        raise GitHubDispatchError(item) from exc

    item.status = "QUEUED"
    item.current_stage = "queued"
    item.updated_at = now
    item.error = ""
    return item


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
        discovered_run_id = None
        if not item.github_run_id or not item.production_revision:
            discovered_run_id = _metadata_from_statuses(repo, item)
        run_id = item.github_run_id or discovered_run_id
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
