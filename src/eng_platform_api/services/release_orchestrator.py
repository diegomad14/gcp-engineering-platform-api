"""Coordinate GitHub events, the billing circuit and release executors."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from ..config import config
from ..models import CatalogService
from . import (
    catalog,
    deployment_commands,
    deployment_executions,
    deployment_store,
    executor_circuits,
    github_actions_quota,
    github_release_control,
    release_cloud_build,
    release_executions,
    release_reconciler,
    release_workflow_identity,
)
from .quality_profiles import executor_image, planner_hash, profile_for

logger = logging.getLogger(__name__)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TERMINAL = {"quality_failed", "no_release", "released", "failed"}


class ReleaseOrchestratorError(RuntimeError):
    pass


def _is_terminal(execution: dict[str, Any]) -> bool:
    return execution.get("status") in _TERMINAL or (
        execution.get("operation") == "pr_quality"
        and execution.get("status") == "quality_passed"
    )


def _owner(repository: str) -> str:
    return config.github.billing_owner or repository.split("/", 1)[0]


def _service_enabled(service: CatalogService) -> bool:
    settings = config.release_orchestrator
    return settings.enabled and service.service_name in {
        *settings.enabled_services,
        *settings.canary_services,
    }


def _services(repository: str) -> list[CatalogService]:
    return [
        service
        for service in catalog.get_services_by_repository(repository)
        if _service_enabled(service)
    ]


def _policy_hash(service: CatalogService) -> str:
    value = {
        "policy_version": service.release_policy,
        "profile": service.quality.profile,
        "coverage_threshold": service.quality.coverage_threshold,
        "differential_threshold": service.quality.differential_threshold,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _open_circuit(*, repository: str, run_id: str, reason: str, evidence: str) -> None:
    owner = _owner(repository)
    executor_circuits.open_circuit(
        owner,
        reason=reason,
        repository=repository,
        run_id=run_id,
        evidence=evidence,
    )
    # Circuit persistence is the safety boundary. Repository variables are an
    # economic optimization and can be retried independently after a partial
    # GitHub API failure.
    repositories = {
        service.repository
        for service in catalog.get_services().services
        if service.repository
    }
    for candidate in repositories:
        try:
            if github_release_control.repository_is_private(candidate):
                github_release_control.set_repository_execution_mode(
                    candidate, "cloud_build"
                )
        except Exception:
            logger.exception("Unable to propagate open circuit to %s", candidate)


def _provider(service: CatalogService) -> str:
    try:
        private = github_release_control.repository_is_private(service.repository)
    except Exception:
        return "github_actions"
    if not private:
        return "github_actions"
    owner = _owner(service.repository)
    if executor_circuits.is_open(owner):
        _open_circuit(
            repository=service.repository,
            run_id="",
            reason="persistent_github_actions_billing_circuit",
            evidence="circuit already open",
        )
        return "cloud_build"
    usage = github_actions_quota.current_usage()
    if usage and usage.exhausted:
        _open_circuit(
            repository=service.repository,
            run_id="",
            reason="included_private_minutes_exhausted",
            evidence=f"private_linux_minutes={usage.private_linux_minutes}",
        )
        return "cloud_build"
    return "github_actions"


def _reserve(
    *,
    service: CatalogService,
    operation: str,
    head_sha: str,
    base_sha: str,
    branch: str,
    delivery_id: str,
    provider: str,
) -> tuple[dict[str, Any], bool]:
    profile = profile_for(service)
    image = executor_image(profile)
    plan_hash = planner_hash() if operation == "main_release" else ""
    policy_hash = _policy_hash(service)
    fingerprint_value = release_executions.fingerprint(
        repository=service.repository,
        service_name=service.service_name,
        operation=operation,  # type: ignore[arg-type]
        head_sha=head_sha,
        base_sha=base_sha,
        profile_hash=profile.fingerprint(),
        executor_digest=image,
        policy_hash=policy_hash,
        planner_hash=plan_hash,
    )
    return release_executions.reserve(
        fingerprint_value=fingerprint_value,
        repository=service.repository,
        service_name=service.service_name,
        operation=operation,  # type: ignore[arg-type]
        head_sha=head_sha,
        base_sha=base_sha,
        branch=branch,
        profile_hash=profile.fingerprint(),
        executor_digest=image,
        policy_hash=policy_hash,
        planner_hash=plan_hash,
        provider=provider,  # type: ignore[arg-type]
        delivery_id=delivery_id,
    )


def _start_checks(execution: dict[str, Any], *, pr_title: str = "") -> None:
    check_ids = dict(execution.get("check_ids") or {})
    if "quality" not in check_ids:
        check_ids["quality"] = github_release_control.upsert_check(
            repository=execution["repository"],
            head_sha=execution["head_sha"],
            kind="quality",
            status="queued",
            summary="Quality execution reserved by Engineering Platform.",
            external_id=f"{execution['execution_id']}:quality",
        )
    if "workflows" not in check_ids:
        check_ids["workflows"] = github_release_control.upsert_check(
            repository=execution["repository"],
            head_sha=execution["head_sha"],
            kind="workflows",
            status="queued",
            summary="Repository-specific contracts are pending.",
            external_id=f"{execution['execution_id']}:workflows",
        )
    if execution["operation"] == "pr_quality" and "title" not in check_ids:
        valid = bool(
            re.match(
                r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: .+",
                pr_title,
            )
        )
        check_ids["title"] = github_release_control.upsert_check(
            repository=execution["repository"],
            head_sha=execution["head_sha"],
            kind="title",
            status="completed",
            conclusion="success" if valid else "failure",
            summary=(
                "Pull request title follows Conventional Commits."
                if valid
                else "Pull request title must follow Conventional Commits."
            ),
            external_id=f"{execution['execution_id']}:title",
        )
    if execution["operation"] == "main_release" and "release" not in check_ids:
        check_ids["release"] = github_release_control.upsert_check(
            repository=execution["repository"],
            head_sha=execution["head_sha"],
            kind="release",
            status="queued",
            summary="Release preparation is pending.",
            external_id=f"{execution['execution_id']}:release",
        )
    release_executions.save(execution["execution_id"], check_ids=check_ids)


def _submit_if_managed(execution: dict[str, Any], service: CatalogService) -> dict:
    if execution.get("provider") != "cloud_build":
        return execution
    result = release_cloud_build.submit(execution["execution_id"], service)
    check_ids = result.get("check_ids") or execution.get("check_ids") or {}
    for kind in ("quality", "workflows"):
        if check_ids.get(kind):
            github_release_control.upsert_check(
                repository=execution["repository"],
                head_sha=execution["head_sha"],
                kind=kind,
                status="in_progress",
                details_url=str(result.get("logs_url", "")),
                summary="Quality is running.",
                check_run_id=int(check_ids[kind]),
            )
    return result


def handle_pull_request(payload: dict[str, Any], *, delivery_id: str) -> list[dict]:
    if payload.get("action") not in {"opened", "synchronize", "reopened"}:
        return []
    repository = str(payload.get("repository", {}).get("full_name", ""))
    pull_request = payload.get("pull_request", {})
    head_sha = str(pull_request.get("head", {}).get("sha", "")).lower()
    base_sha = str(pull_request.get("base", {}).get("sha", "")).lower()
    branch = str(pull_request.get("head", {}).get("ref", ""))
    if (
        not (_SHA.fullmatch(head_sha) and _SHA.fullmatch(base_sha))
        or head_sha == base_sha
    ):
        raise ReleaseOrchestratorError("Pull request SHAs are invalid")
    results = []
    for service in _services(repository):
        provider = _provider(service)
        execution, created = _reserve(
            service=service,
            operation="pr_quality",
            head_sha=head_sha,
            base_sha=base_sha,
            branch=branch,
            delivery_id=delivery_id,
            provider=provider,
        )
        if created:
            release_executions.save(
                execution["execution_id"],
                pull_request_number=int(payload.get("number", 0)),
            )
        # Only PR builds may be cancelled when a new head supersedes them.
        # Repeat this scan on redelivery so a transient cancel API failure does
        # not leave the obsolete build consuming quota.
        for previous in release_executions.list_for_repository(repository):
            if (
                previous.get("execution_id") != execution["execution_id"]
                and previous.get("operation") == "pr_quality"
                and previous.get("pull_request_number") == int(payload.get("number", 0))
                and previous.get("provider") == "cloud_build"
                and not _is_terminal(previous)
            ):
                release_cloud_build.cancel(str(previous["execution_id"]))
        # Resume safe side effects after a partially processed webhook delivery.
        execution = release_executions.get(execution["execution_id"]) or execution
        if not _is_terminal(execution):
            _start_checks(execution, pr_title=str(pull_request.get("title", "")))
            execution = release_executions.get(execution["execution_id"]) or execution
            execution = _submit_if_managed(execution, service)
        results.append(execution)
    return results


def handle_push(payload: dict[str, Any], *, delivery_id: str) -> list[dict]:
    if payload.get("deleted") or payload.get("ref") != "refs/heads/main":
        return []
    repository = str(payload.get("repository", {}).get("full_name", ""))
    head_sha = str(payload.get("after", "")).lower()
    base_sha = str(payload.get("before", "")).lower()
    if (
        not _SHA.fullmatch(head_sha)
        or not _SHA.fullmatch(base_sha)
        or base_sha == "0" * 40
        or base_sha == head_sha
    ):
        raise ReleaseOrchestratorError(
            "Push does not contain a valid differential base"
        )
    if not github_release_control.is_fast_forward(repository, base_sha, head_sha):
        raise ReleaseOrchestratorError("Non-fast-forward main push is not releasable")
    results = []
    for service in _services(repository):
        provider = _provider(service)
        execution, _created = _reserve(
            service=service,
            operation="main_release",
            head_sha=head_sha,
            base_sha=base_sha,
            branch="main",
            delivery_id=delivery_id,
            provider=provider,
        )
        if not _is_terminal(execution):
            _start_checks(execution)
            execution = release_executions.get(execution["execution_id"]) or execution
            execution = _submit_if_managed(execution, service)
        results.append(execution)
    return results


def _close_circuit_from_probe(repository: str, payload_run: dict[str, Any]) -> bool:
    settings = config.release_orchestrator
    workflow_path = str(payload_run.get("path", ""))
    if not workflow_path.endswith(settings.github_health_workflow):
        return False
    owner = _owner(repository)
    circuit = executor_circuits.get(owner)
    expected = circuit.get("probe", {})
    if (
        circuit.get("state") != "open"
        or expected.get("status") != "requested"
        or expected.get("repository") != repository
        or expected.get("workflow") != settings.github_health_workflow
    ):
        return True
    nonce = str(expected.get("nonce", ""))
    if (
        str(payload_run.get("event", "")) != "workflow_dispatch"
        or str(payload_run.get("display_title", "")) != f"eng-platform-health-{nonce}"
    ):
        # Another invocation of the same workflow is not the administrator's
        # bound probe and must not close the account-wide circuit.
        return True
    run_id = int(payload_run.get("id", 0))
    run = github_release_control.workflow_run(repository, run_id)
    jobs = list(run.jobs())
    started = sum(1 for job in jobs if getattr(job, "started_at", None))
    executor_circuits.record_probe(
        owner,
        repository=repository,
        workflow=settings.github_health_workflow,
        run_id=str(run_id),
        conclusion=str(getattr(run, "conclusion", "")),
        jobs_started=started,
    )
    if getattr(run, "conclusion", "") == "success" and started:
        updated: list[str] = []
        repositories = {
            service.repository
            for service in catalog.get_services().services
            if service.repository
        }
        try:
            for candidate in repositories:
                github_release_control.set_repository_execution_mode(
                    candidate, "github_actions"
                )
                updated.append(candidate)
        except Exception:
            logger.exception("Unable to restore all GitHub Actions mode variables")
            # Keep the circuit and repository variables aligned. A new admin
            # probe can retry after the GitHub API/configuration issue clears.
            for candidate in updated:
                try:
                    github_release_control.set_repository_execution_mode(
                        candidate, "cloud_build"
                    )
                except Exception:
                    logger.exception(
                        "Unable to roll back GitHub mode for %s", candidate
                    )
        else:
            executor_circuits.close_after_successful_probe(owner, run_id=str(run_id))
    return True


def _handle_deployment_workflow_run(
    repository: str, payload_run: dict[str, Any], run: Any
) -> list[dict[str, Any]]:
    if str(getattr(run, "event", "")) != "workflow_dispatch":
        return []
    workflow_path = str(payload_run.get("path", ""))
    results = []
    for service in catalog.get_services_by_repository(repository):
        expected = {
            service.deployment.workflow_file or config.github.deployment_workflow,
            config.github.rollback_workflow,
        }
        if workflow_path and not any(
            workflow_path.endswith(candidate) for candidate in expected
        ):
            continue
        title = str(payload_run.get("display_title", ""))
        prefixes = {
            "deploy": "eng-platform-deploy-",
            "rollback": "eng-platform-rollback-",
        }
        correlated = next(
            (
                (kind, title.removeprefix(prefix))
                for kind, prefix in prefixes.items()
                if title.startswith(prefix) and title.removeprefix(prefix).isdigit()
            ),
            None,
        )
        active = deployment_store.get(correlated[1]) if correlated else None
        if active is not None and (
            active.kind != correlated[0]
            or active.service_name != service.service_name
            or active.repository != repository
        ):
            active = None
        if active is None:
            # Legacy deploy workflows did not expose the deployment ID in the
            # run title. Exact SHA correlation remains safe for deploys only;
            # rollback never guesses because its run is dispatched from main.
            active = next(
                (
                    item
                    for item in deployment_store.list_for_service(
                        service.service_name, limit=100
                    )
                    if item.kind == "deploy"
                    and item.status
                    not in {"SUCCEEDED", "ROLLED_BACK", "ROLLBACK_FAILED"}
                    and item.repository == repository
                    and item.sha == str(getattr(run, "head_sha", ""))
                ),
                None,
            )
        if active is None:
            continue
        active.github_run_id = int(payload_run.get("id", 0))
        active.github_run_url = str(payload_run.get("html_url", ""))
        active.logs_url = active.github_run_url
        deployment_store.save(active, "")
        execution = deployment_executions.get(active.id)
        if not execution or execution.get("provider") != "github_actions":
            continue
        if not github_actions_quota.is_reactive_quota_failure(run, active):
            continue
        _open_circuit(
            repository=repository,
            run_id=str(payload_run.get("id", "")),
            reason="github_actions_billing_rejection",
            evidence="deployment workflow rejected before starting",
        )
        submitted = deployment_commands.start_cloud_build(
            service, active, reason="github_workflow_run_billing"
        )
        deployment_store.save(submitted, "")
        results.append(
            {"execution_id": active.id, "status": submitted.status, "deployment": True}
        )
    return results


def handle_workflow_run(payload: dict[str, Any], *, delivery_id: str) -> list[dict]:
    if payload.get("action") != "completed":
        return []
    repository = str(payload.get("repository", {}).get("full_name", ""))
    run_payload = payload.get("workflow_run", {})
    if _close_circuit_from_probe(repository, run_payload):
        return []
    run_id = int(run_payload.get("id", 0))
    if run_id <= 0:
        return []
    run = github_release_control.workflow_run(repository, run_id)
    deployment_results = _handle_deployment_workflow_run(repository, run_payload, run)
    event_name = str(getattr(run, "event", run_payload.get("event", "")))
    workflow_path = str(run_payload.get("path", ""))
    head_sha = str(run_payload.get("head_sha", "")).lower()
    if event_name == "pull_request_target":
        title = str(run_payload.get("display_title", ""))
        head_sha = title.removeprefix("eng-platform-quality-").lower()
    if not _SHA.fullmatch(head_sha):
        return deployment_results
    results = list(deployment_results)
    for execution in release_executions.list_for_repository(repository):
        if (
            execution.get("head_sha") != head_sha
            or execution.get("provider") != "github_actions"
            or _is_terminal(execution)
            or not release_workflow_identity.matches_workflow_path(
                str(execution.get("operation", "")), workflow_path
            )
            or (event_name == "push" and execution.get("operation") != "main_release")
            or (
                event_name == "pull_request_target"
                and execution.get("operation") != "pr_quality"
            )
        ):
            continue
        execution = release_executions.save(
            execution["execution_id"],
            github_run_id=run_id,
            provider_run_id=str(run_id),
            github_run_url=str(run_payload.get("html_url", "")),
            provider_conclusion=str(run_payload.get("conclusion", "")),
            workflow_delivery_id=delivery_id,
        )
        if github_actions_quota.is_release_quota_failure(
            run, repository=repository, expected_sha=head_sha
        ):
            _open_circuit(
                repository=repository,
                run_id=str(run_id),
                reason="github_actions_billing_rejection",
                evidence="zero-step workflow rejected by GitHub Billing",
            )
            transitioned, changed = release_executions.transition_to_cloud_build(
                execution["execution_id"], reason="github_actions_billing_rejection"
            )
            if changed:
                service = catalog.get_service(str(execution["service_name"]))
                if service is None:
                    raise ReleaseOrchestratorError("Release service disappeared")
                transitioned = _submit_if_managed(transitioned, service)
            results.append(transitioned)
            continue
        conclusion = str(run_payload.get("conclusion", ""))
        if conclusion == "success":
            release_executions.save(
                execution["execution_id"], provider_terminal_success=True
            )
            results.append(release_reconciler.reconcile(execution["execution_id"]))
        elif (
            conclusion == "failure"
            and (
                execution.get("engine_event_status")
                in {"quality_failed", "quality_passed"}
                or execution.get("release_engine_failed")
            )
            and execution.get("pending_report_hash")
        ):
            changes: dict[str, Any] = {"provider_terminal_success": True}
            if execution.get(
                "engine_event_status"
            ) == "quality_passed" or execution.get("release_engine_failed"):
                changes.update(
                    {
                        "release_engine_failed": True,
                        "release_error": str(
                            execution.get("release_error")
                            or "Release workflow failed after quality passed"
                        ),
                    }
                )
            release_executions.save(execution["execution_id"], **changes)
            results.append(release_reconciler.reconcile(execution["execution_id"]))
        else:
            results.append(
                release_executions.save(
                    execution["execution_id"],
                    status="failed",
                    error=f"GitHub workflow concluded {conclusion or 'unknown'}",
                )
            )
    return results


def request_health_probe(*, requested_by: str) -> dict[str, Any]:
    settings = config.release_orchestrator
    repository = settings.github_health_repository
    if not repository:
        raise ReleaseOrchestratorError("Health probe repository is not configured")
    circuit = executor_circuits.request_probe(
        _owner(repository),
        repository=repository,
        workflow=settings.github_health_workflow,
        requested_by=requested_by,
    )
    github_release_control.dispatch_health_probe(
        repository, nonce=str(circuit["probe"]["nonce"])
    )
    return circuit


def approve_canary(execution_id: str, *, approved_by: str) -> dict[str, Any]:
    execution = release_executions.get(execution_id)
    if execution is None:
        raise KeyError(execution_id)
    if execution.get("service_name") not in config.release_orchestrator.canary_services:
        raise ValueError("Execution is not a canary")
    if execution.get("status") != "release_planned":
        raise ValueError("Canary does not have an approved release plan")
    return release_executions.save(
        execution_id, canary_approved=True, canary_approved_by=approved_by
    )


def reconcile_verified_github_run(
    *,
    service_name: str,
    operation: str,
    head_sha: str,
    base_sha: str,
    run_id: int,
    requested_by: str,
) -> dict[str, Any]:
    """Seed a missed webhook only after re-reading all identity from GitHub."""
    service = catalog.get_service(service_name)
    if service is None or not _service_enabled(service):
        raise ReleaseOrchestratorError("Service is not enabled for release recovery")
    if operation not in {"pr_quality", "main_release"}:
        raise ReleaseOrchestratorError("Invalid release recovery operation")
    head_sha, base_sha = head_sha.lower(), base_sha.lower()
    if (
        not _SHA.fullmatch(head_sha)
        or not _SHA.fullmatch(base_sha)
        or head_sha == base_sha
        or base_sha == "0" * 40
    ):
        raise ReleaseOrchestratorError("Release recovery SHAs are invalid")
    if operation == "main_release" and not github_release_control.is_fast_forward(
        service.repository, base_sha, head_sha
    ):
        raise ReleaseOrchestratorError("Release recovery is not a fast-forward push")
    run = github_release_control.workflow_run(service.repository, run_id)
    expected_event = "push" if operation == "main_release" else "pull_request_target"
    run_matches_sha = (
        str(getattr(run, "head_sha", "")) == head_sha
        if operation == "main_release"
        else str(getattr(run, "display_title", ""))
        == f"eng-platform-quality-{head_sha}"
    )
    if (
        not run_matches_sha
        or str(getattr(run, "event", "")) != expected_event
        or str(getattr(run, "status", "")) != "completed"
        or not release_workflow_identity.matches_workflow_path(
            operation, str(getattr(run, "path", ""))
        )
    ):
        raise ReleaseOrchestratorError("GitHub run does not match release recovery")
    execution, created = _reserve(
        service=service,
        operation=operation,
        head_sha=head_sha,
        base_sha=base_sha,
        branch="main" if operation == "main_release" else "recovered-pr",
        delivery_id=f"admin-reconcile-{run_id}",
        provider="github_actions",
    )
    if created:
        _start_checks(execution)
    execution = release_executions.save(
        execution["execution_id"],
        github_run_id=run_id,
        provider_run_id=str(run_id),
        github_run_url=str(getattr(run, "html_url", "")),
        recovery_requested_by=requested_by,
    )
    if github_actions_quota.is_release_quota_failure(
        run, repository=service.repository, expected_sha=head_sha
    ):
        _open_circuit(
            repository=service.repository,
            run_id=str(run_id),
            reason="github_actions_billing_rejection",
            evidence="verified historical zero-step billing rejection",
        )
        execution, changed = release_executions.transition_to_cloud_build(
            execution["execution_id"], reason="github_actions_billing_rejection"
        )
        return _submit_if_managed(execution, service) if changed else execution
    if getattr(run, "conclusion", "") == "success":
        return release_executions.save(
            execution["execution_id"], provider_terminal_success=True
        )
    raise ReleaseOrchestratorError("GitHub run was not a Billing rejection")
