"""Persistence for normalized quality reports.

Production uses Cloud Storage when ENG_PLATFORM_QUALITY_BUCKET is configured.
Development and tests use a local directory with the same object layout.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..models import QualityReport, QualityReportCreate

_lock = threading.RLock()


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


def _write_object(name: str, data: dict) -> None:
    bucket_name = _bucket_name()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if bucket_name:
        from google.cloud import storage

        storage.Client().bucket(bucket_name).blob(name).upload_from_string(
            payload,
            content_type="application/json",
        )
        return

    path = _local_root() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _read_object(name: str) -> dict | None:
    bucket_name = _bucket_name()
    if bucket_name:
        from google.cloud import storage
        from google.api_core.exceptions import NotFound

        try:
            payload = storage.Client().bucket(bucket_name).blob(name).download_as_text()
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
        from google.cloud import storage

        for blob in storage.Client().list_blobs(bucket_name, prefix=prefix):
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
        from google.cloud import storage

        blobs = storage.Client().list_blobs(bucket_name, prefix=prefix)
        payloads = (blob.download_as_text() for blob in blobs)
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


def _status(payload: QualityReportCreate) -> str:
    coverage_failed = (
        payload.coverage_threshold is not None
        and payload.coverage is not None
        and payload.coverage < payload.coverage_threshold
    )
    checks_failed = any(check.status == "FAILED" for check in payload.checks)
    return "FAILED" if coverage_failed or checks_failed else "PASSED"


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
        _write_object(_object_name(report.service_name, report.commit_sha), data)
        current = _read_object(_latest_name(report.service_name))
        if not current or str(current.get("generated_at", "")) <= report.generated_at:
            _write_object(_latest_name(report.service_name), data)
    return report


def get_report(service_name: str, commit_sha: str) -> QualityReport | None:
    data = _read_object(_object_name(service_name, commit_sha))
    return QualityReport.model_validate(data) if data else None


def get_latest_reports() -> list[QualityReport]:
    reports = [QualityReport.model_validate(value) for value in _list_latest_objects()]
    reports.sort(key=lambda report: report.generated_at, reverse=True)
    return reports


def get_reports(service_name: str, limit: int = 20) -> list[QualityReport]:
    reports = [
        QualityReport.model_validate(value)
        for value in _list_report_objects(service_name)
    ]
    reports.sort(key=lambda report: report.generated_at, reverse=True)
    return reports[:limit]
