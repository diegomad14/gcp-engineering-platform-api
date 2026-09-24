"""Consolidate provisional engine output only after its provider succeeds."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from ..config import config
from . import (
    catalog,
    executor_circuits,
    github_release_control,
    quality_store,
    release_cloud_build,
    release_executions,
)
from .quality_policy import policy_errors

_FAILED_BUILD_STATES = {
    "FAILURE",
    "TIMEOUT",
    "CANCELLED",
    "EXPIRED",
    "INTERNAL_ERROR",
}
logger = logging.getLogger(__name__)


def _complete_check(
    execution: dict[str, Any], kind: str, conclusion: str, summary: str
) -> None:
    check_id = (execution.get("check_ids") or {}).get(kind)
    if not check_id:
        return
    github_release_control.upsert_check(
        repository=str(execution["repository"]),
        head_sha=str(execution["head_sha"]),
        kind=kind,
        status="completed",
        conclusion=conclusion,
        details_url=str(
            execution.get("logs_url") or execution.get("github_run_url") or ""
        ),
        summary=summary,
        check_run_id=int(check_id),
    )


def _verify_build(execution: dict[str, Any], build: dict[str, Any]) -> None:
    substitutions = build.get("substitutions", {})
    source = build.get("source", {}).get("connectedRepository", {})
    expected_repository = config.cloud_build.repositories.get(
        str(execution["service_name"]), ""
    )
    if (
        str(build.get("id", "")) != str(execution.get("build_id", ""))
        or not expected_repository
        or source.get("repository") != expected_repository
        or source.get("revision") != execution.get("head_sha")
    ):
        raise ValueError("Cloud Build release source does not match execution")
    expected = {
        "_EXECUTION_ID": execution["execution_id"],
        "_REQUEST_FINGERPRINT": execution["fingerprint"],
        "_SERVICE_NAME": execution["service_name"],
        "_REPOSITORY": execution["repository"],
        "_HEAD_SHA": execution["head_sha"],
        "_BASE_SHA": execution["base_sha"],
        "_EXECUTOR_DIGEST": execution["executor_digest"],
        "_OPERATION": execution["operation"],
        "_PROFILE_SHA256": execution["profile_hash"],
    }
    if execution.get("operation") == "main_release":
        expected.update(
            {
                "_PLANNER_SHA256": execution["planner_hash"],
                "_PLANNER_DIGEST": config.release_orchestrator.release_planner_image,
            }
        )
    if any(substitutions.get(key) != value for key, value in expected.items()):
        raise ValueError("Cloud Build release identity does not match execution")
    if build.get("serviceAccount") != config.release_orchestrator.service_account:
        raise ValueError("Cloud Build release service account does not match")


def _record_build_timing(execution_id: str, build: dict[str, Any]) -> None:
    started = str(build.get("startTime", ""))
    finished = str(build.get("finishTime", ""))
    if not started or not finished:
        return
    try:
        seconds = max(
            0.0,
            (
                datetime.fromisoformat(finished.replace("Z", "+00:00"))
                - datetime.fromisoformat(started.replace("Z", "+00:00"))
            ).total_seconds(),
        )
    except ValueError:
        return
    created = str(build.get("createTime", ""))
    queue_seconds = 0.0
    if created:
        try:
            queue_seconds = max(
                0.0,
                (
                    datetime.fromisoformat(started.replace("Z", "+00:00"))
                    - datetime.fromisoformat(created.replace("Z", "+00:00"))
                ).total_seconds(),
            )
        except ValueError:
            queue_seconds = 0.0
    minutes = seconds / 60
    release_executions.save(
        execution_id,
        build_duration_seconds=round(seconds, 3),
        build_queue_seconds=round(queue_seconds, 3),
        build_minutes_estimate=round(minutes, 3),
        estimated_compute_cost_usd=round(
            minutes * config.release_orchestrator.build_minute_price_usd, 6
        ),
        build_minute_price_usd=config.release_orchestrator.build_minute_price_usd,
        cost_category="release_quality",
        build_finished_at=finished,
    )
    month = finished[:7]
    total = release_executions.monthly_cloud_build_minutes(month)
    execution = release_executions.get(execution_id) or {}
    owner = (
        config.github.billing_owner
        or str(execution.get("repository", "")).split("/", 1)[0]
    )
    for threshold in config.release_orchestrator.usage_alert_minutes:
        if total >= threshold and executor_circuits.claim_usage_alert(
            owner,
            month=month,
            threshold=threshold,
            observed_minutes=total,
        ):
            logger.warning(
                "cloud_build_usage_threshold month=%s threshold_minutes=%s observed_minutes=%.3f",
                month,
                threshold,
                total,
            )


def _provider_success(execution: dict[str, Any]) -> bool:
    if execution.get("provider") == "github_actions":
        return bool(execution.get("provider_terminal_success"))
    build_id = str(execution.get("build_id", ""))
    if not build_id:
        service = catalog.get_service(str(execution["service_name"]))
        if service is None:
            raise ValueError("Release service disappeared")
        execution = release_cloud_build.reconcile_uncertain_submission(
            str(execution["execution_id"]), service
        )
        build_id = str(execution.get("build_id", ""))
        if not build_id:
            return False
    build = release_cloud_build.get_build(build_id)
    _verify_build(execution, build)
    state = str(build.get("status", ""))
    if state == "SUCCESS":
        _record_build_timing(str(execution["execution_id"]), build)
        return True
    if state in _FAILED_BUILD_STATES:
        if (
            state == "FAILURE"
            and execution.get("engine_event_status") == "quality_failed"
            and execution.get("pending_report_hash")
        ):
            _record_build_timing(str(execution["execution_id"]), build)
            return True
        if (
            state == "FAILURE"
            and (
                execution.get("engine_event_status") == "quality_passed"
                or execution.get("release_engine_failed")
            )
            and execution.get("pending_report_hash")
        ):
            release_executions.save(
                str(execution["execution_id"]),
                release_engine_failed=True,
                release_error=str(
                    execution.get("release_error") or "Release planner failed"
                ),
            )
            _record_build_timing(str(execution["execution_id"]), build)
            return True
        release_executions.save(
            str(execution["execution_id"]),
            status="failed",
            provider_status=state,
            error=f"Quality build concluded {state}",
        )
        _complete_check(execution, "quality", "failure", f"Quality build: {state}")
        _complete_check(execution, "workflows", "failure", f"Quality build: {state}")
    return False


def _commit_quality(execution: dict[str, Any]) -> dict[str, Any]:
    report_hash = str(execution.get("pending_report_hash", ""))
    if not report_hash:
        raise ValueError("Successful provider did not produce a quality report")
    report = quality_store.get_pending_report(
        str(execution["execution_id"]), report_hash
    )
    if report is None:
        raise ValueError("Pending quality report is missing")
    if (
        report.service_name != execution.get("service_name")
        or report.repository != execution.get("repository")
        or report.commit_sha.lower() != execution.get("head_sha")
        or report.base_sha.lower() != execution.get("base_sha")
        or report.policy_version != "oss-v2"
    ):
        raise ValueError("Quality report does not match release execution")
    stored, committed_hash = quality_store.save_immutable_report(
        report,
        fingerprint=str(execution["fingerprint"]),
        provider=str(execution["provider"]),
        provider_run_id=str(execution.get("provider_run_id", "")),
        executor_digest=str(execution["executor_digest"]),
        profile_hash=str(execution["profile_hash"]),
        policy_hash=str(execution["policy_hash"]),
        operation=str(execution["operation"]),
        expected_hash=report_hash,
    )
    quality_store.save_execution_summary(execution, stored, committed_hash)
    service = catalog.get_service(str(execution["service_name"]))
    errors = policy_errors(stored, service)
    if stored.quality_gate_status != "PASSED" or errors:
        result = release_executions.save(
            str(execution["execution_id"]),
            status="quality_failed",
            report_hash=committed_hash,
            quality_errors=errors,
        )
        _complete_check(result, "quality", "failure", "Quality policy failed.")
        _complete_check(result, "workflows", "failure", "Quality policy failed.")
        return result
    result = release_executions.save(
        str(execution["execution_id"]),
        status="quality_passed",
        report_hash=committed_hash,
        evidence_committed=True,
    )
    _complete_check(result, "quality", "success", "Exact oss-v2 evidence passed.")
    _complete_check(result, "workflows", "success", "Service contracts passed.")
    return result


def _publish_if_allowed(execution: dict[str, Any]) -> dict[str, Any]:
    service_name = str(execution["service_name"])
    auto = service_name in config.release_orchestrator.enabled_services
    if not auto and not execution.get("canary_approved"):
        return execution
    if execution.get("status") == "release_planned":
        if not release_executions.claim_publish(str(execution["execution_id"])):
            return release_executions.get(str(execution["execution_id"])) or execution
        execution = release_executions.get(str(execution["execution_id"])) or execution
    if execution.get("status") not in {"publish_pending", "unknown"}:
        return execution
    if execution.get("status") == "unknown" and not execution.get(
        "publication_uncertain"
    ):
        return execution
    try:
        published = github_release_control.publish_release(execution)
    except github_release_control.GitHubReleaseConflict as exc:
        result = release_executions.save(
            str(execution["execution_id"]), status="unknown", error=str(exc)
        )
        _complete_check(result, "release", "action_required", str(exc))
        return result
    except Exception as exc:
        # The GitHub response may be uncertain. A later reconciliation calls
        # publish_release again, which first inspects both the ref and Release
        # and only completes the missing half of the idempotent publication.
        result = release_executions.save(
            str(execution["execution_id"]),
            status="unknown",
            error="GitHub release publication is uncertain",
            publication_uncertain=True,
            publication_error=str(exc)[:1000],
        )
        _complete_check(result, "release", "action_required", str(exc)[:500])
        return result
    result = release_executions.save(
        str(execution["execution_id"]),
        status="released",
        publication_uncertain=False,
        **published,
    )
    _complete_check(result, "release", "success", f"Published {published['tag']}.")
    return result


def reconcile(execution_id: str) -> dict[str, Any]:
    execution = release_executions.get(execution_id)
    if execution is None:
        raise KeyError(execution_id)
    if execution.get("status") in {
        "quality_failed",
        "no_release",
        "released",
        "failed",
    }:
        return execution
    if execution.get("status") == "unknown" and execution.get("publication_uncertain"):
        return _publish_if_allowed(execution)
    if execution.get("status") in {"release_planned", "publish_pending"}:
        return _publish_if_allowed(execution)
    if (
        execution.get("provider") == "cloud_build"
        and execution.get("status") == "submission_pending"
    ):
        service = catalog.get_service(str(execution["service_name"]))
        if service is None:
            raise ValueError("Release service disappeared before build submission")
        try:
            execution = release_cloud_build.submit(execution_id, service)
        except release_cloud_build.ReleaseCloudBuildError as exc:
            # Webhook handlers can fail after reserving an execution (for
            # example, while GitHub is temporarily unavailable to create its
            # canonical checks). The durable reservation must remain
            # retryable. Cloud Build submit owns the idempotency claim and
            # records definitive/uncertain outcomes before raising.
            latest = release_executions.get(execution_id) or execution
            logger.warning(
                "release_cloud_build_submission_reconciled execution_id=%s status=%s error=%s",
                execution_id,
                latest.get("status", "unknown"),
                str(exc)[:500],
            )
            if latest.get("status") == "failed":
                _complete_check(
                    latest,
                    "quality",
                    "failure",
                    "Release quality could not start. No build was created.",
                )
                _complete_check(
                    latest,
                    "workflows",
                    "failure",
                    "Release quality could not start. No build was created.",
                )
            return latest
    if not _provider_success(execution):
        return release_executions.get(execution_id) or execution
    execution = _commit_quality(release_executions.get(execution_id) or execution)
    if execution.get("status") != "quality_passed":
        return execution
    if execution.get("operation") == "pr_quality":
        return execution
    if execution.get("release_engine_failed"):
        result = release_executions.save(
            execution_id,
            status="failed",
            error=str(execution.get("release_error") or "Release planner failed")[
                :1000
            ],
        )
        _complete_check(
            result,
            "release",
            "failure",
            str(result.get("error") or "Release planner failed")[:500],
        )
        return result
    plan = execution.get("release_plan") or {}
    if (
        execution.get("engine_event_status") == "no_release"
        or plan.get("release_type") == "none"
    ):
        result = release_executions.save(execution_id, status="no_release")
        _complete_check(result, "release", "neutral", "No releasable changes.")
        return result
    if not plan:
        result = release_executions.save(
            execution_id, status="unknown", error="Release plan is missing"
        )
        _complete_check(
            result, "release", "action_required", "Release plan is missing."
        )
        return result
    execution = release_executions.save(execution_id, status="release_planned")
    return _publish_if_allowed(execution)
