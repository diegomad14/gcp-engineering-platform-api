"""GitHub-hosted Actions quota inspection and strict fallback classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any

import httpx

from ..config import config
from . import catalog
from .github_deployments import github_client

_CACHE_SECONDS = 300
_cache: tuple[float, int, int, "Usage | None"] | None = None
_cache_lock = Lock()
_QUOTA_MARKERS = (
    "billing",
    "spending limit",
    "included minutes",
    "minute quota",
    "quota exceeded",
    "payment required",
    "included usage",
)


@dataclass(frozen=True)
class Usage:
    private_linux_minutes: float
    observed_at: datetime

    @property
    def exhausted(self) -> bool:
        return self.private_linux_minutes >= config.github.included_private_minutes


def _private_repositories() -> set[str]:
    """Read visibility once per lookup; public repositories never consume quota."""
    client = github_client()
    private: set[str] = set()
    for service in catalog.get_services().services:
        repo = client.get_repo(service.repository)
        if bool(getattr(repo, "private", False)):
            private.add(service.repository.rsplit("/", 1)[-1])
    return private


def current_usage(*, force: bool = False) -> Usage | None:
    """Return private Linux minutes for the current UTC month or ``None``.

    A missing billing credential is intentionally non-fatal: normal GitHub
    dispatch remains the conservative choice and the strict reactive path can
    still switch after a real quota rejection.
    """
    global _cache
    with _cache_lock:
        now = datetime.now(timezone.utc)
        if (
            not force
            and _cache
            and _cache[1:3] == (now.year, now.month)
            and monotonic() - _cache[0] < _CACHE_SECONDS
        ):
            return _cache[3]
        owner, token = config.github.billing_owner, config.github.token
        if not owner or not token:
            _cache = (monotonic(), now.year, now.month, None)
            return None
        endpoint = (
            f"https://api.github.com/users/{owner}/settings/billing/usage"
            f"?year={now.year}&month={now.month}"
        )
        try:
            response = httpx.get(
                endpoint,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10,
            )
            response.raise_for_status()
            private = _private_repositories()
            minutes = sum(
                float(item.get("quantity", 0))
                for item in response.json().get("usageItems", [])
                if str(item.get("product", "")).lower() == "actions"
                and str(item.get("sku", "")).lower() == "actions linux"
                and str(item.get("unitType", "")).lower() == "minutes"
                and item.get("repositoryName") in private
            )
            usage = Usage(minutes, now)
        except Exception:  # Billing data is an optimization, never an outage.
            usage = None
        _cache = (monotonic(), now.year, now.month, usage)
        return usage


def should_use_cloud_build(service_name: str, repository: str) -> bool:
    """Choose the managed fallback only when quota evidence is conclusive."""
    if not config.cloud_build.enabled:
        return False
    if service_name not in config.cloud_build.enabled_services:
        return False
    if service_name in config.cloud_build.cloud_build_only_services:
        return True
    if config.cloud_build.mode == "cloud_build":
        return True
    if config.cloud_build.mode == "github_actions":
        return False
    try:
        if not bool(getattr(github_client().get_repo(repository), "private", False)):
            return False
    except Exception:
        return False
    from . import executor_circuits

    owner = config.github.billing_owner or repository.split("/", 1)[0]
    if executor_circuits.is_open(owner):
        return True
    usage = current_usage()
    if usage and usage.exhausted:
        executor_circuits.open_circuit(
            owner,
            reason="included_private_minutes_exhausted",
            repository=repository,
            evidence=f"private_linux_minutes={usage.private_linux_minutes}",
        )
        return True
    return False


def is_quota_error(error: BaseException | str) -> bool:
    # A GitHubDispatchError deliberately keeps its public message generic.
    # Classify only the original ``workflow_dispatch`` response retained on
    # the exception; failures before dispatch (status creation, auth, etc.)
    # must never switch providers even when their text happens to mention
    # billing.
    dispatch_attempted = getattr(error, "dispatch_attempted", None)
    if dispatch_attempted is False:
        return False
    dispatch_detail = str(getattr(error, "dispatch_error_detail", "") or "")
    text = dispatch_detail or str(error)
    if not dispatch_detail and isinstance(error, BaseException):
        cause = error.__cause__ or error.__context__
        if cause is not None:
            data = getattr(cause, "data", None)
            response = getattr(cause, "response", None)
            response_text = (
                getattr(response, "text", "") if response is not None else ""
            )
            text = "\n".join(
                part
                for part in (str(cause), str(data or ""), str(response_text or ""))
                if part
            )
    text = text.lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def _job_annotations_are_quota_failure(repository: str, jobs: list[Any]) -> bool:
    """Recognize GitHub's zero-step billing rejection from check annotations."""
    if not jobs or any(getattr(job, "steps", []) for job in jobs):
        return False
    try:
        requester = github_client().get_repo(repository)._requester
        for job in jobs:
            _, annotations = requester.requestJsonAndCheck(
                "GET", f"/repos/{repository}/check-runs/{job.id}/annotations"
            )
            if any(is_quota_error(row.get("message", "")) for row in annotations):
                return True
    except Exception:
        return False
    return False


def is_reactive_quota_failure(run: Any, item: Any) -> bool:
    """Only accept the GitHub no-job startup failure caused by exhaustion."""
    if not config.cloud_build.enabled:
        return False
    if item.service_name not in config.cloud_build.enabled_services:
        return False
    if item.kind != "rollback" and getattr(run, "head_sha", "") != item.sha:
        return False
    if getattr(run, "event", "") != "workflow_dispatch":
        return False
    try:
        jobs = list(run.jobs())
    except Exception:
        return False
    if item.kind != "rollback" and (
        item.candidate_revision or item.production_revision
    ):
        return False
    if _job_annotations_are_quota_failure(item.repository, jobs):
        return True
    if getattr(run, "conclusion", "") != "startup_failure" or jobs:
        return False
    usage = current_usage(force=True)
    return bool(usage and usage.exhausted)


def is_release_quota_failure(run: Any, *, repository: str, expected_sha: str) -> bool:
    """Classify PR/push startup rejection without treating code failures as quota."""
    event = getattr(run, "event", "")
    if event == "pull_request_target":
        if getattr(run, "display_title", "") != f"eng-platform-quality-{expected_sha}":
            return False
    elif getattr(run, "head_sha", "") != expected_sha:
        return False
    if event not in {"push", "pull_request_target"}:
        return False
    try:
        jobs = list(run.jobs())
    except Exception:
        return False
    if _job_annotations_are_quota_failure(repository, jobs):
        return True
    if getattr(run, "conclusion", "") != "startup_failure" or jobs:
        return False
    usage = current_usage(force=True)
    return bool(usage and usage.exhausted)
