"""Quality router — normalized open-source quality gate reports."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import config
from ..models import QualityProject, QualityReport, QualityReportCreate, QualitySummary
from ..security import require_quality_ingest_token
from ..services import catalog, quality_store

router = APIRouter(prefix="/api/quality", tags=["quality"])


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
        coverage=report.coverage or 0.0,
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
    )


@router.post(
    "/reports",
    response_model=QualityReport,
    status_code=201,
    dependencies=[Depends(require_quality_ingest_token)],
)
async def register_quality_report(payload: QualityReportCreate):
    """Register an idempotent quality result for one service and commit."""
    return quality_store.save_report(payload)


@router.get(
    "/services/{service_name}/commits/{commit_sha}", response_model=QualityReport
)
async def get_quality_report(service_name: str, commit_sha: str):
    """Return the exact evidence used to authorize a deployment."""
    report = quality_store.get_report(service_name, commit_sha)
    if report is None:
        raise HTTPException(status_code=404, detail="Quality report not found")
    if _is_stale(report):
        return report.model_copy(update={"quality_gate_status": "STALE"})
    return report


@router.get("/services/{service_name}/reports", response_model=list[QualityReport])
async def get_quality_history(
    service_name: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Return recent quality evidence for one independent service."""
    return quality_store.get_reports(service_name, limit=limit)


@router.get("/summary", response_model=QualitySummary)
async def get_quality_summary():
    """Get the latest normalized quality result for every service."""
    projects = {
        report.service_name: _project(report)
        for report in quality_store.get_latest_reports()
    }
    for service in catalog.get_services().services:
        projects.setdefault(
            service.service_name,
            QualityProject(
                project_key=service.service_name,
                service_name=service.service_name,
                repository=service.repository,
                profile=service.quality.profile or "",
                quality_gate_status="NOT_CONFIGURED",
            ),
        )
    return QualitySummary(
        projects=sorted(projects.values(), key=lambda project: project.service_name)
    )
