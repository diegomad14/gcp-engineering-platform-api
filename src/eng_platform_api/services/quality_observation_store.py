"""Write-once shadow observations using the existing quality-store permissions.

This namespace is never consulted by admission, release or promotion policy.
An authenticated receipt identifies who submitted observations, not whether
repository code produced truthful or hermetic measurements.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

from ..quality_evidence import (
    EvidenceError,
    HASH,
    compare_shadow,
    digest,
    seal_observation,
    validate_receipt,
)
from . import quality_store
from .resource_access import require_managed_service_name


_lock = threading.RLock()


def _index_name(fingerprint: str) -> str:
    if not HASH.fullmatch(fingerprint):
        raise EvidenceError("Invalid observation execution identity")
    return (
        f"{quality_store._prefix()}/test-observations/by-execution/{fingerprint}.json"
    )


def save_observation(
    observation: dict[str, Any], execution: dict[str, Any], report_hash: str
) -> dict[str, Any]:
    """Accept server-owned context only, after the canonical callback was saved."""
    require_managed_service_name(str(execution.get("service_name", "")))
    if not HASH.fullmatch(report_hash) or report_hash != execution.get(
        "pending_report_hash"
    ):
        raise EvidenceError("Observation requires the execution's accepted report")
    report = quality_store.get_pending_report(
        str(execution["execution_id"]), report_hash
    )
    if (
        report is None
        or quality_store._report_hash(report) != report_hash
        or report.repository != execution.get("repository")
        or report.service_name != execution.get("service_name")
        or report.commit_sha.lower() != execution.get("head_sha")
        or report.base_sha.lower() != execution.get("base_sha")
    ):
        raise EvidenceError("Accepted quality report is missing or mismatched")
    receipt = seal_observation(
        observation, execution, report_hash, datetime.now(timezone.utc).isoformat()
    )
    index = _index_name(receipt["context"]["fingerprint"])
    content = f"{quality_store._prefix()}/test-observations/blobs/{receipt['observation_sha256']}.json"
    with _lock:
        # Claim this execution before creating content blobs. Cross-process
        # compare-and-set losers cannot fill storage with rejected replay data.
        if not quality_store._write_object(index, receipt, if_absent=True):
            existing = validate_receipt(quality_store._read_object(index))
            if any(
                existing[key] != receipt[key]
                for key in ("context", "report_sha256", "observation_sha256")
            ):
                raise quality_store.QualityEvidenceConflict(
                    "Observation execution conflicts"
                )
            receipt = existing
        # Retrying an identical receipt can repair an interrupted blob write.
        if not quality_store._write_object(content, observation, if_absent=True):
            existing_content = quality_store._read_object(content)
            if (
                existing_content is None
                or digest(existing_content) != receipt["observation_sha256"]
            ):
                raise quality_store.QualityEvidenceConflict(
                    "Observation blob conflicts"
                )
    return receipt


def get_observation(fingerprint: str) -> dict[str, Any] | None:
    """Internal read only; no public artifact locator or read endpoint exists."""
    value = quality_store._read_object(_index_name(fingerprint))
    if value is None:
        return None
    receipt = validate_receipt(value)
    if receipt["context"]["fingerprint"] != fingerprint:
        raise EvidenceError("Observation index identity mismatch")
    return receipt


def shadow_for_executions(
    previous_fingerprint: str,
    current_fingerprint: str,
    *,
    now: datetime,
    revoked_fingerprints: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Internal explicit comparison only; no candidate discovery or reuse policy."""
    return compare_shadow(
        get_observation(previous_fingerprint),
        get_observation(current_fingerprint),
        now=now,
        revoked_fingerprints=revoked_fingerprints,
    )
