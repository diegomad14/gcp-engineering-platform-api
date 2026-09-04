"""Server-owned release policy; report contents cannot lower catalog requirements."""

from datetime import datetime, timezone
import math
import re

from ..models import CatalogService, QualityReportCreate

POLICY_VERSION = "oss-v2"
_REQUIRED = {
    "setup",
    "tests",
    "lint",
    "typecheck",
    "sast",
    "dependencies",
    "secrets",
    "misconfiguration",
}


def policy_errors(
    report: QualityReportCreate, service: CatalogService | None
) -> list[str]:
    if service is None:
        return ["Unknown catalog service"]
    errors = []
    if (
        report.repository != service.repository
        or report.profile != service.quality.profile
    ):
        errors.append("Repository or profile does not match the catalog")
    if report.policy_version != POLICY_VERSION:
        errors.append("Release requires oss-v2 evidence")
    if not re.fullmatch(r"[0-9a-f]{40}", report.commit_sha):
        errors.append("Release requires a full commit SHA")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", report.base_sha)
        or report.base_sha == report.commit_sha
    ):
        errors.append("Missing or invalid comparison base")
    try:
        generated = datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        if age < -300:
            errors.append("Report timestamp is in the future")
    except (ValueError, TypeError):
        errors.append("Invalid report timestamp")
    required = _REQUIRED | ({"format"} if report.profile == "python" else {"build"})
    if report.profile == "static":
        required -= {"tests", "typecheck"}
    checks = {check.category: check for check in report.checks}
    if len(checks) != len(report.checks):
        errors.append("Duplicate check categories")
    for name in required:
        if name not in checks or checks[name].status != "PASSED":
            errors.append(f"Required check not passed: {name}")
    if any(c.status == "FAILED" or c.blocking_findings > 0 for c in report.checks):
        errors.append("Blocking findings or failed checks")
    threshold = service.quality.coverage_threshold
    if report.profile != "static":
        if (
            report.coverage is None
            or not math.isfinite(report.coverage)
            or report.coverage < threshold
            or report.coverage_threshold is None
            or report.coverage_threshold < threshold
        ):
            errors.append("Global coverage does not meet catalog requirements")
        total, covered = report.changed_lines, report.covered_changed_lines
        if total is None or covered is None or covered > total:
            errors.append("Missing or inconsistent changed-line counts")
        elif total == 0:
            if (
                covered != 0
                or report.differential_coverage is not None
                or "differential_coverage" not in checks
                or checks["differential_coverage"].status != "SKIPPED"
            ):
                errors.append("Invalid no-applicable-lines result")
        else:
            expected = 100 * covered / total
            if (
                report.differential_coverage is None
                or not math.isfinite(report.differential_coverage)
                or abs(expected - report.differential_coverage) > 0.001
                or expected < 80
                or "differential_coverage" not in checks
                or checks["differential_coverage"].status != "PASSED"
            ):
                errors.append("Changed-line coverage must be at least 80%")
        if report.differential_threshold is None or report.differential_threshold < 80:
            errors.append("Changed-line threshold cannot be lowered")
    return errors
