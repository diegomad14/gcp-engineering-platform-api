"""The authenticated Streamable HTTP MCP surface for eng-platform."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .config import config
from .routers import costs, metrics, releases
from .routers.quality import get_quality_report
from .services import (
    catalog,
    deployment_commands,
    deployment_store,
    github_deployments,
    mcp_store,
)
from .services.mcp_auth import _SCOPES, provider

_BASE_URL = config.mcp.public_base_url or "http://localhost:8000"
_RESOURCE_URL = f"{_BASE_URL}/mcp"
_PUBLIC_ORIGIN = urlparse(_BASE_URL)

mcp = FastMCP(
    "eng-platform",
    instructions=(
        "Use eng-platform only for read-only release insight and authorized tagged "
        "deployments or rollbacks. The platform, not the client, chooses its executor."
    ),
    auth_server_provider=provider,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_PUBLIC_ORIGIN.netloc],
        allowed_origins=[_BASE_URL],
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(config.mcp.issuer_url or _BASE_URL),
        resource_server_url=AnyHttpUrl(_RESOURCE_URL),
        validate_token_resource=False,
        required_scopes=["eng-platform.read"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=sorted(_SCOPES),
            default_scopes=["eng-platform.read"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


def _serialized(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _fingerprint(value: dict[str, Any]) -> str:
    """Audit only a stable hash of inputs, never prompts or opaque token values."""
    safe = {
        key: value[key]
        for key in sorted(value)
        if key not in {"token", "secret", "authorization"}
    }
    return hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(scope: str) -> tuple[str, str]:
    token = get_access_token()
    if token is None or not token.subject:
        raise HTTPException(status_code=401, detail="MCP authentication required")
    if scope not in token.scopes:
        raise HTTPException(status_code=403, detail=f"MCP scope '{scope}' is required")
    return token.subject, token.client_id


def _audit(
    *,
    subject: str,
    client_id: str,
    tool: str,
    payload: dict[str, Any],
    mutation: bool,
    result: str,
    deployment_id: str = "",
) -> None:
    mcp_store.save_audit(
        {
            "subject": subject,
            "client_id": client_id,
            "tool": tool,
            "input_fingerprint": _fingerprint(payload),
            "mutation": mutation,
            "result": result,
            "deployment_id": deployment_id,
        }
    )


def _read(tool: str, payload: dict[str, Any], action):
    subject, client_id = _identity("eng-platform.read")
    try:
        value = action()
    except Exception:
        _audit(
            subject=subject,
            client_id=client_id,
            tool=tool,
            payload=payload,
            mutation=False,
            result="error",
        )
        raise
    _audit(
        subject=subject,
        client_id=client_id,
        tool=tool,
        payload=payload,
        mutation=False,
        result="ok",
    )
    return _serialized(value)


def _mutate(tool: str, payload: dict[str, Any], scope: str, action):
    subject, client_id = _identity(scope)
    if subject.lower() not in config.auth.allowed_logins:
        raise HTTPException(
            status_code=403, detail="GitHub user is not allowed to operate eng-platform"
        )
    if mcp_store.mutation_count(subject) >= config.mcp.mutation_limit_per_hour:
        raise HTTPException(
            status_code=429, detail="MCP mutation limit reached; retry after one hour"
        )
    try:
        value = action(subject)
    except Exception:
        _audit(
            subject=subject,
            client_id=client_id,
            tool=tool,
            payload=payload,
            mutation=True,
            result="error",
        )
        raise
    deployment_id = str(getattr(value, "id", ""))
    _audit(
        subject=subject,
        client_id=client_id,
        tool=tool,
        payload=payload,
        mutation=True,
        result="accepted",
        deployment_id=deployment_id,
    )
    return _serialized(value)


@mcp.tool()
def list_services() -> dict[str, Any]:
    """List services registered in the platform catalog."""
    return _read("list_services", {}, catalog.get_services)


@mcp.tool()
def get_service(service_name: str) -> dict[str, Any]:
    """Get public deployment metadata and current state for one service."""

    def action():
        service = catalog.get_service_detail(service_name)
        if service is None:
            raise HTTPException(status_code=404, detail="Service not found")
        return service

    return _read("get_service", {"service_name": service_name}, action)


@mcp.tool()
def get_service_health(service_name: str) -> dict[str, Any]:
    """Get the live health, revision, URL and traffic DTO for a service."""
    return get_service(service_name)


@mcp.tool()
def list_eligible_tags(service_name: str, limit: int = 20) -> dict[str, Any]:
    """List exact semantic-release tags that can be considered for deployment."""

    def action():
        service = catalog.get_service(service_name)
        if service is None or not service.repository:
            raise HTTPException(status_code=404, detail="Service not found")
        page = github_deployments.list_tags(
            service.repository, service_name, limit=max(1, min(limit, 100))
        )
        return {
            "items": [_serialized(tag) for tag in page.items if tag.eligible],
            "next_cursor": page.next_cursor,
        }

    return _read(
        "list_eligible_tags", {"service_name": service_name, "limit": limit}, action
    )


@mcp.tool()
def get_release_evidence(service_name: str, tag: str) -> dict[str, Any]:
    """Return the public oss-v2 evidence for one eligible tagged commit."""

    def action():
        service = catalog.get_service(service_name)
        if service is None or not service.repository:
            raise HTTPException(status_code=404, detail="Service not found")
        release_tag = github_deployments.get_tag(service.repository, service_name, tag)
        if release_tag is None or not release_tag.eligible:
            raise HTTPException(status_code=409, detail="Tag is not eligible")
        return get_quality_report(service_name, release_tag.sha, for_release=True)

    return _read(
        "get_release_evidence", {"service_name": service_name, "tag": tag}, action
    )


@mcp.tool()
def list_releases(service_name: str = "", limit: int = 20) -> dict[str, Any]:
    """List public release history, optionally restricted to a service."""
    return _read(
        "list_releases",
        {"service_name": service_name, "limit": limit},
        lambda: releases.list_releases(service_name or None, max(1, min(limit, 100))),
    )


@mcp.tool()
def start_deployment(
    service_name: str, tag: str, reason: str, idempotency_key: str
) -> dict[str, Any]:
    """Start one eligible tagged deployment. Executor choice stays private to the backend."""
    if not reason.strip() or not idempotency_key.strip():
        raise HTTPException(
            status_code=422, detail="reason and idempotency_key are required"
        )
    payload = {
        "service_name": service_name,
        "tag": tag,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }
    return _mutate(
        "start_deployment",
        payload,
        "eng-platform.deploy",
        lambda subject: deployment_commands.start_deployment(
            service_name=service_name,
            tag_name=tag,
            requested_by=subject,
            idempotency_key=idempotency_key,
        ),
    )


@mcp.tool()
def get_deployment(deployment_id: str) -> dict[str, Any]:
    """Get a public deployment DTO by id."""

    def action():
        # GitHub Actions reports terminal state through its Deployment and run.
        # The REST query performs the same verified reconciliation before
        # returning a DTO; MCP must not leave an otherwise completed deploy
        # permanently QUEUED until somebody opens the browser.
        from .routers.deployments import get_deployment as get_rest_deployment

        return get_rest_deployment(deployment_id)

    return _read("get_deployment", {"deployment_id": deployment_id}, action)


@mcp.tool()
def list_deployments(service_name: str, limit: int = 20) -> dict[str, Any]:
    """List deployment history for one service."""

    def action():
        if catalog.get_service(service_name) is None:
            raise HTTPException(status_code=404, detail="Service not found")
        items, total = deployment_store.list_for_service_with_total(
            service_name, limit=max(1, min(limit, 100))
        )
        return {"items": [_serialized(item) for item in items], "total": total}

    return _read(
        "list_deployments", {"service_name": service_name, "limit": limit}, action
    )


@mcp.tool()
def start_rollback(
    service_name: str, deployment_id: str, reason: str, idempotency_key: str
) -> dict[str, Any]:
    """Start a rollback to one recorded successful production deployment."""
    if not reason.strip() or not idempotency_key.strip():
        raise HTTPException(
            status_code=422, detail="reason and idempotency_key are required"
        )
    payload = {
        "service_name": service_name,
        "deployment_id": deployment_id,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }
    return _mutate(
        "start_rollback",
        payload,
        "eng-platform.rollback",
        lambda subject: deployment_commands.start_rollback(
            service_name=service_name,
            target_deployment_id=deployment_id,
            requested_by=subject,
            idempotency_key=idempotency_key,
        ),
    )


@mcp.tool()
def get_cost_summary(days: int = 30, month_to_date: bool = False) -> dict[str, Any]:
    """Get public billing summary; values come from the existing FinOps source."""
    return _read(
        "get_cost_summary",
        {"days": days, "month_to_date": month_to_date},
        lambda: costs.get_cost_summary(max(1, min(days, 365)), month_to_date),
    )


@mcp.tool()
async def get_metrics_summary(window: Literal["1h", "24h"] = "24h") -> dict[str, Any]:
    """Get public Cloud Run operational metrics from the existing monitoring source."""
    subject, client_id = _identity("eng-platform.read")
    payload = {"window": window}
    try:
        value = await metrics.get_cloud_run_metrics(window)
    except Exception:
        _audit(
            subject=subject,
            client_id=client_id,
            tool="get_metrics_summary",
            payload=payload,
            mutation=False,
            result="error",
        )
        raise
    _audit(
        subject=subject,
        client_id=client_id,
        tool="get_metrics_summary",
        payload=payload,
        mutation=False,
        result="ok",
    )
    return _serialized(value)
