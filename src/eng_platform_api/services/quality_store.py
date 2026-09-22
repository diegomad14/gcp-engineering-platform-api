"""Persistence for normalized quality reports.

Production uses Cloud Storage when ENG_PLATFORM_QUALITY_BUCKET is configured.
Development and tests use a local directory with the same object layout.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from collections.abc import Iterable
from typing import Any

from ..models import QualityGateStatus, QualityReport, QualityReportCreate

_lock = threading.RLock()


class QualityEvidenceConflict(RuntimeError):
    """The immutable evidence identity already contains different content."""


@lru_cache(maxsize=1)
def _storage_client():
    from google.cloud.storage import Client  # type: ignore[import-untyped]

    return Client()


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _prefix() -> str:
    return os.getenv("ENG_PLATFORM_QUALITY_PREFIX", "quality").strip("/")


def _local_root() -> Path:
    configured = Path(os.getenv("ENG_PLATFORM_QUALITY_STORE_PATH", "data"))
    return configured if configured.is_absolute() else Path.cwd() / configured


def _object_name(service_name: str, commit_sha: str) -> str:
    return f"{_prefix()}/reports/{_safe(service_name)}/{commit_sha.lower()}.json"


def _latest_name(service_name: str) -> str:
    return f"{_prefix()}/latest/{_safe(service_name)}.json"


def _bucket_name() -> str:
    return os.getenv("ENG_PLATFORM_QUALITY_BUCKET", "")


def _write_object(name: str, data: dict, *, if_absent: bool = False) -> bool:
    bucket_name = _bucket_name()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if bucket_name:
        try:
            _storage_client().bucket(bucket_name).blob(name).upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0 if if_absent else None,
            )
        except Exception as exc:
            if if_absent and exc.__class__.__name__ in {
                "PreconditionFailed",
                "Conflict",
            }:
                return False
            raise
        return True

    path = _local_root() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if if_absent:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError:
            return False
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    return True


def _read_object(name: str) -> dict | None:
    bucket_name = _bucket_name()
    if bucket_name:
        from google.api_core.exceptions import NotFound

        try:
            payload = (
                _storage_client().bucket(bucket_name).blob(name).download_as_text()
            )
        except NotFound:
            return None
    else:
        path = _local_root() / name
        if not path.exists():
            return None
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _list_latest_objects() -> list[dict]:
    bucket_name = _bucket_name()
    prefix = f"{_prefix()}/latest/"
    values: list[dict] = []
    if bucket_name:
        for blob in _storage_client().list_blobs(bucket_name, prefix=prefix):
            try:
                value = json.loads(blob.download_as_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(value, dict):
                values.append(value)
        return values

    root = _local_root() / prefix
    if not root.exists():
        return []
    for path in root.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _list_report_objects(service_name: str) -> list[dict]:
    bucket_name = _bucket_name()
    prefix = f"{_prefix()}/reports/{_safe(service_name)}/"
    values: list[dict] = []
    if bucket_name:
        blobs = _storage_client().list_blobs(bucket_name, prefix=prefix)
        payloads: Iterable[str] = (blob.download_as_text() for blob in blobs)
    else:
        root = _local_root() / prefix
        payloads = (
            (path.read_text(encoding="utf-8") for path in root.glob("*.json"))
            if root.exists()
            else ()
        )
    for payload in payloads:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _status(payload: QualityReportCreate) -> QualityGateStatus:
    if payload.policy_version != "oss-v1":
        from . import catalog
        from .quality_policy import policy_errors

        return (
            "FAILED"
            if policy_errors(payload, catalog.get_service(payload.service_name))
            else "PASSED"
        )
    coverage_failed = (
        payload.coverage_threshold is not None
        and payload.coverage is not None
        and payload.coverage < payload.coverage_threshold
    )
    checks_failed = any(check.status == "FAILED" for check in payload.checks)
    return "FAILED" if coverage_failed or checks_failed else "PASSED"


def _report_payload(value: dict[str, Any]) -> dict[str, Any]:
    report = value.get("report")
    return report if isinstance(report, dict) else value


def _report_hash(payload: QualityReportCreate) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _as_create(value: dict[str, Any]) -> QualityReportCreate:
    if "quality_gate_status" in value or "received_at" in value:
        report = QualityReport.model_validate(value)
        value = report.model_dump(
            exclude={"quality_gate_status", "received_at"}, mode="json"
        )
    return QualityReportCreate.model_validate(value)


def _evidence_name(service_name: str, commit_sha: str, report_hash: str) -> str:
    return (
        f"{_prefix()}/evidence/{_safe(service_name)}/"
        f"{commit_sha.lower()}/{report_hash}.json"
    )


def _execution_evidence_name(
    service_name: str, commit_sha: str, fingerprint: str
) -> str:
    return (
        f"{_prefix()}/execution-evidence/{_safe(service_name)}/"
        f"{commit_sha.lower()}/{_safe(fingerprint)}.json"
    )


def _pending_name(execution_id: str, report_hash: str) -> str:
    return f"{_prefix()}/pending/release-executions/{_safe(execution_id)}/{report_hash}.json"


def _execution_summary_name(execution_id: str) -> str:
    return f"{_prefix()}/summaries/release-executions/{_safe(execution_id)}.json"


def save_pending_report(
    execution_id: str,
    payload: QualityReportCreate,
    *,
    expected_hash: str = "",
) -> str:
    """Stage a callback report without making it deployment-authorizing."""
    calculated_hash = _report_hash(payload)
    if expected_hash and not hmac_compare(calculated_hash, expected_hash):
        raise QualityEvidenceConflict("Quality report hash does not match callback")
    wrapper = {
        "schema_version": 2,
        "report_hash": calculated_hash,
        "report": payload.model_dump(mode="json"),
    }
    name = _pending_name(execution_id, calculated_hash)
    with _lock:
        if not _write_object(name, wrapper, if_absent=True):
            existing = _read_object(name) or {}
            existing_payload = _report_payload(existing)
            if _report_hash(_as_create(existing_payload)) != calculated_hash:
                raise QualityEvidenceConflict("Pending quality report conflicts")
    return calculated_hash


def get_pending_report(
    execution_id: str, report_hash: str
) -> QualityReportCreate | None:
    value = _read_object(_pending_name(execution_id, report_hash))
    return _as_create(_report_payload(value)) if value else None


def save_report(payload: QualityReportCreate) -> QualityReport:
    """Create or replace the report for one service and commit."""
    values = payload.model_dump()
    values["commit_sha"] = payload.commit_sha.lower()
    report = QualityReport(
        **values,
        quality_gate_status=_status(payload),
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    data = report.model_dump()
    with _lock:
        existing = _read_object(_object_name(report.service_name, report.commit_sha))
        if existing and isinstance(existing.get("provenance"), dict):
            existing_report = QualityReport.model_validate(_report_payload(existing))
            existing_create = _as_create(existing_report.model_dump(mode="json"))
            if _report_hash(existing_create) == _report_hash(payload):
                return existing_report
            raise QualityEvidenceConflict(
                "Orchestrated quality evidence cannot be overwritten"
            )
        _write_object(_object_name(report.service_name, report.commit_sha), data)
        current = _read_object(_latest_name(report.service_name))
        if not current or str(current.get("generated_at", "")) <= report.generated_at:
            _write_object(_latest_name(report.service_name), data)
    return report


def save_immutable_report(
    payload: QualityReportCreate,
    *,
    fingerprint: str,
    provider: str,
    provider_run_id: str,
    executor_digest: str,
    profile_hash: str,
    policy_hash: str,
    operation: str,
    expected_hash: str = "",
) -> tuple[QualityReport, str]:
    """Store orchestrated evidence once without allowing a conflicting rewrite."""
    calculated_hash = _report_hash(payload)
    if expected_hash and not hmac_compare(calculated_hash, expected_hash):
        raise QualityEvidenceConflict("Quality report hash does not match callback")
    values = payload.model_dump()
    values["commit_sha"] = payload.commit_sha.lower()
    report = QualityReport(
        **values,
        quality_gate_status=_status(payload),
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    wrapper = {
        "schema_version": 2,
        "report_hash": calculated_hash,
        "provenance": {
            "fingerprint": fingerprint,
            "provider": provider,
            "provider_run_id": provider_run_id,
            "executor_digest": executor_digest,
            "profile_hash": profile_hash,
            "policy_hash": policy_hash,
            "operation": operation,
        },
        "report": report.model_dump(mode="json"),
    }
    index_name = (
        _object_name(report.service_name, report.commit_sha)
        if operation == "main_release"
        else _execution_evidence_name(
            report.service_name, report.commit_sha, fingerprint
        )
    )
    with _lock:
        current = _read_object(index_name)
        if current:
            current_payload = _report_payload(current)
            try:
                current_create = _as_create(current_payload)
                current_hash = _report_hash(current_create)
            except Exception as exc:
                raise QualityEvidenceConflict(
                    "Existing quality evidence is unreadable"
                ) from exc
            current_provenance = current.get("provenance", {})
            current_fingerprint = (
                current_provenance.get("fingerprint")
                if isinstance(current_provenance, dict)
                else ""
            )
            if current_hash == calculated_hash and current_fingerprint == fingerprint:
                return QualityReport.model_validate(current_payload), calculated_hash
            if current_hash == calculated_hash and not current_fingerprint:
                raise QualityEvidenceConflict(
                    "Legacy quality evidence cannot authorize an orchestrated release"
                )
            raise QualityEvidenceConflict(
                "Conflicting quality evidence already exists for service and commit"
            )

        _write_object(
            _evidence_name(report.service_name, report.commit_sha, calculated_hash),
            wrapper,
            if_absent=True,
        )
        if not _write_object(index_name, wrapper, if_absent=True):
            # Another process won the compare-and-set. Re-read and compare under
            # the same rules rather than overwriting its evidence.
            current = _read_object(index_name) or {}
            current_payload = _report_payload(current)
            current_create = _as_create(current_payload)
            provenance = current.get("provenance", {})
            current_fingerprint = (
                provenance.get("fingerprint")
                if isinstance(provenance, dict)
                else ""
            )
            if (
                _report_hash(current_create) != calculated_hash
                or current_fingerprint != fingerprint
            ):
                raise QualityEvidenceConflict(
                    "Concurrent quality evidence conflicts with this report"
                )
            return QualityReport.model_validate(current_payload), calculated_hash
        latest = _read_object(_latest_name(report.service_name))
        latest_payload = _report_payload(latest) if latest else None
        if (
            not latest_payload
            or str(latest_payload.get("generated_at", "")) <= report.generated_at
        ):
            _write_object(_latest_name(report.service_name), wrapper)
    return report, calculated_hash


def save_execution_summary(
    execution: dict[str, Any], report: QualityReport, report_hash: str
) -> None:
    """Persist one compact, immutable private summary for lifecycle-managed GCS."""
    value = {
        "schema_version": 1,
        "execution_id": str(execution["execution_id"]),
        "fingerprint": str(execution["fingerprint"]),
        "service_name": str(execution["service_name"]),
        "repository": str(execution["repository"]),
        "operation": str(execution["operation"]),
        "head_sha": str(execution["head_sha"]),
        "base_sha": str(execution["base_sha"]),
        "profile_hash": str(execution["profile_hash"]),
        "policy_hash": str(execution["policy_hash"]),
        "executor_digest": str(execution["executor_digest"]),
        "provider": str(execution["provider"]),
        "provider_run_id": str(execution.get("provider_run_id", "")),
        "report_hash": report_hash,
        "quality_gate_status": report.quality_gate_status,
        "recorded_at": report.received_at,
    }
    name = _execution_summary_name(str(execution["execution_id"]))
    with _lock:
        if _write_object(name, value, if_absent=True):
            return
        if _read_object(name) != value:
            raise QualityEvidenceConflict("Release execution summary conflicts")


def hmac_compare(left: str, right: str) -> bool:
    # Local helper avoids importing request-auth concerns into this storage layer.
    import hmac

    return hmac.compare_digest(left, right)


def get_report(service_name: str, commit_sha: str) -> QualityReport | None:
    data = _read_object(_object_name(service_name, commit_sha))
    return QualityReport.model_validate(_report_payload(data)) if data else None


def get_latest_reports() -> list[QualityReport]:
    reports = [
        QualityReport.model_validate(_report_payload(value))
        for value in _list_latest_objects()
    ]
    reports.sort(key=lambda report: report.generated_at, reverse=True)
    return reports


def get_latest_report(service_name: str) -> QualityReport | None:
    data = _read_object(_latest_name(service_name))
    return QualityReport.model_validate(_report_payload(data)) if data else None


def get_reports(service_name: str, limit: int = 20) -> list[QualityReport]:
    reports = [
        QualityReport.model_validate(_report_payload(value))
        for value in _list_report_objects(service_name)
    ]
    reports.sort(key=lambda report: report.generated_at, reverse=True)
    return reports[:limit]
