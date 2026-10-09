"""One irreversible, server-scoped Artemis PR 172 quality bootstrap.

This policy deliberately cannot be configured by an API caller. Its approved
head must be set in reviewed source before the operation can be enabled.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
import hashlib
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from fastapi import HTTPException

from ..config import config
from . import catalog, github_release_control, release_cloud_build, release_executions
from .quality_profiles import executor_image, profile_for

POLICY_ID = "artemis-pr172-quality-bootstrap-v1"
REPOSITORY = "diegomad14/cgm-artemis-api"
REPOSITORY_ID = 1306114845
SERVICE_NAME = "cgm-artemis-api"
PULL_REQUEST_NUMBER = 172
APPROVED_BASE_SHA = "3c807a789ba78eea95068a3cc0a7a6e537b0bae9"
# Filled only after the corrected workflow commit is reviewed. Empty is closed.
APPROVED_HEAD_SHA = "73b8f336eafd0827c9fdcbcc01a9ba53cf2aeda9"
TIMEOUT_SECONDS = 3600
MAX_COMPUTE_USD = Decimal("0.36")
_BROKEN_EXECUTOR_DIGEST = (
    "1e5121ba9ebeb640d5da512e92f44507184d911cd0b81e2daf97e3519cbc682e"
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class QualityBootstrapError(RuntimeError):
    pass


def canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def public_status(execution: dict[str, Any]) -> dict[str, str]:
    result = {
        key: str(execution.get(key, ""))
        for key in ("execution_id", "status", "provider", "build_id", "provider_run_id")
    }
    url = str(execution.get("logs_url", ""))
    parsed = urlparse(url)
    result["logs_url"] = ""
    if (
        parsed.scheme == "https"
        and parsed.netloc == "console.cloud.google.com"
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and re.fullmatch(
            r"/cloud-build/builds(?:;region=[a-z0-9-]+)?/[a-zA-Z0-9-]+", parsed.path
        )
    ):
        project = parse_qs(parsed.query).get("project", [])
        if len(project) == 1 and re.fullmatch(r"[a-zA-Z0-9-]{1,64}", project[0]):
            result["logs_url"] = urlunparse(
                parsed._replace(query=urlencode({"project": project[0]}))
            )
        elif not parsed.query:
            result["logs_url"] = urlunparse(parsed)
    return result


def _authorize(actor: str, reauthorize: Callable[[], Any]) -> None:
    if (
        not config.auth.allowed_logins
        or actor.lower() not in config.auth.allowed_logins
    ):
        raise QualityBootstrapError("Bootstrap requires an allowlisted deployer")
    if reauthorize() is False:
        raise QualityBootstrapError("Bootstrap authorization was revoked")


def _validate_identity(execution: dict[str, Any]):
    if not _SHA.fullmatch(APPROVED_HEAD_SHA):
        raise QualityBootstrapError("Bootstrap approved head is not configured")
    service = catalog.get_service(SERVICE_NAME)
    if (
        service is None
        or service.repository != REPOSITORY
        or service.management_mode != "managed"
    ):
        raise QualityBootstrapError("Bootstrap catalog identity changed")
    profile = profile_for(service)
    image = executor_image(profile)
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image):
        raise QualityBootstrapError(
            "Bootstrap executor requires a complete pinned SHA256"
        )
    if image.endswith("@sha256:" + _BROKEN_EXECUTOR_DIGEST):
        raise QualityBootstrapError("Bootstrap corrected executor is not configured")
    policy_hash = canonical_hash(
        {
            "policy_version": service.release_policy,
            "profile": service.quality.profile,
            "coverage_threshold": service.quality.coverage_threshold,
            "differential_threshold": service.quality.differential_threshold,
        }
    )
    expected = {
        "repository": REPOSITORY,
        "service_name": SERVICE_NAME,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "operation": "pr_quality",
        "head_sha": APPROVED_HEAD_SHA,
        "base_sha": APPROVED_BASE_SHA,
        "profile_hash": profile.fingerprint(),
        "executor_digest": executor_image(profile),
        "policy_hash": policy_hash,
    }
    if any(execution.get(key) != value for key, value in expected.items()):
        raise QualityBootstrapError("Bootstrap execution identity changed")
    fingerprint = release_executions.fingerprint(
        repository=REPOSITORY,
        service_name=SERVICE_NAME,
        operation="pr_quality",
        head_sha=APPROVED_HEAD_SHA,
        base_sha=APPROVED_BASE_SHA,
        profile_hash=profile.fingerprint(),
        executor_digest=executor_image(profile),
        policy_hash=policy_hash,
    )
    if (
        execution.get("fingerprint") != fingerprint
        or profile.timeout_seconds != TIMEOUT_SECONDS
    ):
        raise QualityBootstrapError(
            "Bootstrap canonical profile or fingerprint changed"
        )
    price = Decimal(str(config.release_orchestrator.build_minute_price_usd))
    if not price.is_finite() or price <= 0 or price * Decimal(60) > MAX_COMPUTE_USD:
        raise QualityBootstrapError("Bootstrap compute budget is not satisfied")
    return service


def _github_conditions(*, workflow_id: int | None = None) -> dict[str, Any]:
    return github_release_control.verify_quality_bootstrap_preconditions(
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        pull_request_number=PULL_REQUEST_NUMBER,
        head_sha=APPROVED_HEAD_SHA,
        base_sha=APPROVED_BASE_SHA,
        workflow_id=workflow_id,
    )


def _request(execution: dict[str, Any], service, nonce: str) -> dict[str, Any]:
    request = release_cloud_build.build_request(execution, service)
    release_cloud_build.validate_submission(request)
    if request.get("timeout") != f"{TIMEOUT_SECONDS}s":
        raise QualityBootstrapError("Bootstrap timeout changed")
    request["substitutions"]["_BOOTSTRAP_POLICY"] = POLICY_ID
    request["substitutions"]["_BOOTSTRAP_NONCE"] = nonce
    request["tags"].append(POLICY_ID)
    return request


def request_quality_bootstrap(
    execution_id: str,
    *,
    idempotency_key: str,
    actor: str,
    reauthorize: Callable[[], Any],
) -> dict[str, str]:
    """Reserve globally once; only the transaction winner can send one POST."""
    _authorize(actor, reauthorize)
    if not _KEY.fullmatch(idempotency_key):
        raise QualityBootstrapError("Bootstrap idempotency key is invalid")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    ticket = release_executions.get_quality_bootstrap_ticket()
    if ticket is not None:
        if (
            ticket.get("execution_id") != execution_id
            or ticket.get("idempotency_key") != key_hash
            or ticket.get("requested_by") != actor.lower()
        ):
            raise QualityBootstrapError(
                "The global bootstrap ticket is already consumed"
            )
        existing = release_executions.get(execution_id)
        if existing is None:
            raise QualityBootstrapError("Consumed bootstrap state is unavailable")
        return public_status(existing)
    execution = release_executions.get(execution_id)
    if execution is None:
        raise QualityBootstrapError("Bootstrap execution is unavailable")
    service = _validate_identity(execution)
    if (
        execution.get("provider") != "github_actions"
        or execution.get("status") != "waiting_github"
    ):
        raise QualityBootstrapError("Bootstrap requires a new waiting GitHub execution")
    proof = _github_conditions()
    nonce = secrets.token_hex(32)
    dispatch_token = secrets.token_hex(32)
    request = _request(execution, service, nonce)
    binding = {
        "ticket_id": POLICY_ID,
        "authorization_policy": POLICY_ID,
        "execution_id": execution_id,
        "requested_by": actor.lower(),
        "idempotency_key": key_hash,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "service_name": SERVICE_NAME,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "head_sha": APPROVED_HEAD_SHA,
        "base_sha": APPROVED_BASE_SHA,
        "fingerprint": execution["fingerprint"],
        "profile_hash": execution["profile_hash"],
        "executor_digest": execution["executor_digest"],
        "policy_hash": execution["policy_hash"],
        "build_request": request,
        "build_request_hash": canonical_hash(request),
        "nonce": nonce,
        "dispatch_token_hash": hashlib.sha256(dispatch_token.encode()).hexdigest(),
        "max_compute_usd": str(MAX_COMPUTE_USD),
        "timeout_seconds": TIMEOUT_SECONDS,
        **proof,
    }
    _authorize(actor, reauthorize)
    reserved, won = release_executions.reserve_quality_bootstrap(
        execution_id, binding=binding
    )
    if not won:
        return public_status(reserved)
    try:
        # Reserve is durable before any submission. Drift/revocation or a
        # crash here consumes the ticket and cannot authorize a later POST.
        _authorize(actor, reauthorize)
        current = release_executions.get(execution_id)
        if current is None:
            raise QualityBootstrapError("Reserved bootstrap state is unavailable")
        service = _validate_identity(current)
        if (
            current.get("status") != "submitting"
            or current.get("provider") != "cloud_build"
            or current.get("bootstrap_nonce") != nonce
            or canonical_hash(_request(current, service, nonce))
            != binding["build_request_hash"]
        ):
            raise QualityBootstrapError("Reserved bootstrap request changed")
        _github_conditions(workflow_id=int(proof["workflow_id"]))
        # This transport performs the final live authorization immediately
        # before POST, after preparing its nonretrying session.
        result = release_cloud_build.submit_quality_bootstrap(
            current,
            request=request,
            dispatch_token=dispatch_token,
            reauthorize=lambda: _authorize(actor, reauthorize),
        )
        return public_status(result)
    except Exception as exc:
        # Do not include provider exceptions: they can contain request nonces
        # or credentials. Persistence failure still leaves the ticket consumed.
        try:
            latest = release_executions.get(execution_id)
            if latest is not None and latest.get("status") in {
                "submitting",
                "running_quality",
                "unknown",
            }:
                release_executions.update_quality_bootstrap(
                    execution_id,
                    attempt_nonce=nonce,
                    changes={
                        "status": "unknown",
                        "error": "Bootstrap attempt consumed; read-only recovery required",
                    },
                )
        except Exception:
            raise QualityBootstrapError(
                "Bootstrap attempt consumed; persistence is unavailable"
            ) from None
        if isinstance(exc, HTTPException):
            raise
        raise QualityBootstrapError(
            "Bootstrap attempt consumed; read-only recovery required"
        ) from None
