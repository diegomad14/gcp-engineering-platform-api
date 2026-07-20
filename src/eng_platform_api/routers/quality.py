"""Quality router — normalized open-source quality gate reports."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import config
from ..models import QualityProject, QualityReport, QualityReportCreate, QualitySummary
from ..security import require_quality_ingest_token
from ..services import catalog, github_actions, quality_store

router = APIRouter(prefix="/api/quality", tags=["quality"])
_SUMMARY_CACHE_TTL_SECONDS = 60
_summary_cache: tuple[float, tuple[tuple[str, str], ...], QualitySummary] | None = None
_summary_cache_lock = Lock()


def _invalidate_summary_cache() -> None:
    global _summary_cache
    with _summary_cache_lock:
        _summary_cache = None


def _is_stale(report: QualityReport) -> bool:
    try:
        generated = datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (
        datetime.now(timezone.utc) - generated
    ).total_seconds() > config.quality.stale_after_hours * 3600


def _project(report: QualityReport) -> QualityProject:
    defects = sum(
        check.findings
        for check in report.checks
        if check.category in {"lint", "format", "typecheck", "tests", "build"}
    )
    vulnerabilities = sum(
        check.findings
        for check in report.checks
        if check.category in {"sast", "dependencies", "secrets", "misconfiguration"}
    )
    status = "STALE" if _is_stale(report) else report.quality_gate_status
    return QualityProject(
        project_key=report.service_name,
        service_name=report.service_name,
        repository=report.repository,
        commit_sha=report.commit_sha,
        branch=report.branch,
        profile=report.profile,
        quality_gate_status=status,
        coverage=report.coverage,
        bugs=defects,
        vulnerabilities=vulnerabilities,
        code_smells=sum(
            check.findings
            for check in report.checks
            if check.category in {"lint", "format"}
        ),
        url=report.workflow_run_url,
        updated_at=report.generated_at,
        checks=report.checks,
        evidence_source="normalized-report",
    )


@router.post(
    "/reports",
    response_model=QualityReport,
    status_code=201,
    dependencies=[Depends(require_quality_ingest_token)],
)
def register_quality_report(payload: QualityReportCreate):
    """Register an idempotent quality result for one service and commit."""
    report = quality_store.save_report(payload)
    _invalidate_summary_cache()
    return report


@router.get(
    "/services/{service_name}/commits/{commit_sha}", response_model=QualityReport
)
def get_quality_report(service_name: str, commit_sha: str):
    """Return the exact evidence used to authorize a deployment."""
    report = quality_store.get_report(service_name, commit_sha)
    if report is None:
        raise HTTPException(status_code=404, detail="Quality report not found")
    if _is_stale(report):
        return report.model_copy(update={"quality_gate_status": "STALE"})
    return report


@router.get("/services/{service_name}/reports", response_model=list[QualityReport])
def get_quality_history(
    service_name: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Return recent quality evidence for one independent service."""
    return quality_store.get_reports(service_name, limit=limit)


def _quality_project(service) -> QualityProject:
    report = quality_store.get_latest_report(service.service_name)
    if report and report.repository == service.repository:
        return _project(report)
    github_project = github_actions.get_ci_quality_project(service)
    return github_project or QualityProject(
        project_key=service.service_name,
        service_name=service.service_name,
        repository=service.repository,
        profile=service.quality.profile or "",
        quality_gate_status="NOT_CONFIGURED",
    )


@router.get("/summary", response_model=QualitySummary)
def get_quality_summary():
    """Get the latest normalized quality result for every service."""
    global _summary_cache
    with _summary_cache_lock:
        now = monotonic()
        services = catalog.get_services().services
        catalog_key = tuple(
            sorted((service.service_name, service.repository) for service in services)
        )
        if (
            not config.mock_mode
            and _summary_cache
            and _summary_cache[1] == catalog_key
            and now - _summary_cache[0] < _SUMMARY_CACHE_TTL_SECONDS
        ):
            return _summary_cache[2]
        workers = min(6, max(1, len(services)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            projects = list(executor.map(_quality_project, services))
        summary = QualitySummary(
            projects=sorted(projects, key=lambda project: project.service_name)
        )
        if not config.mock_mode:
            _summary_cache = (monotonic(), catalog_key, summary)
        return summary
