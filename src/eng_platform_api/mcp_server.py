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
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import AnyHttpUrl, ValidationError

from .config import catalog_source_identity, config
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
from .services import mcp_grants, log_catalog
from .services import database_jobs, database_views, database_registry, database_console
from .services.database_sql import QueryRejected
from .services.database_registry import DatabaseUnavailable

_BASE_URL = config.mcp.public_base_url or "http://localhost:8000"
_RESOURCE_URL = f"{_BASE_URL}/mcp"
_PUBLIC_ORIGIN = urlparse(_BASE_URL)
_ACCESS_SCOPES = [mcp_grants.SCOPE]


class _ScopedFastMCP(FastMCP):
    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for index, tool in enumerate(tools):
            if tool.name:
                schemes = [{"type": "oauth2", "scopes": _ACCESS_SCOPES}]
                tools[index] = tool.model_copy(
                    update={
                        "securitySchemes": schemes,
                        "meta": {**(tool.meta or {}), "securitySchemes": schemes},
                    }
                )
        return tools


mcp = _ScopedFastMCP(
    "eng-platform",
    instructions=(
        "Use eng-platform for platform insight, tagged deployments, rollbacks, cost alerts "
        "and read-only PostgreSQL queries. Queries are asynchronous; poll status and "
        "read individual pages, never automatically retrieve every page. "
        "The platform, not the client, chooses its executor."
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
        required_scopes=["eng-platform.access"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=sorted(_SCOPES),
            default_scopes=["eng-platform.access"],
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
        if key
        not in {
            "token",
            "secret",
            "authorization",
            "sql",
            "statement",
            "rows",
            "reason",
        }
    }
    return hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _principal() -> mcp_grants.Principal:
    token = get_access_token()
    if token is None or not token.subject:
        raise HTTPException(401, "MCP authentication required")
    if token.scopes != [mcp_grants.SCOPE]:
        raise HTTPException(403, "Reconnect MCP to authorize full access")
    record = provider._credential("access", token.token)
    if (
        not record
        or record.get("subject") != token.subject
        or record.get("client_id") != token.client_id
    ):
        raise HTTPException(401, "MCP authentication was revoked")
    return mcp_grants.principal(
        mcp_grants.validate(record["session_id"], token.subject)
    )


def _identity(scope: str) -> tuple[str, str]:
    value = _principal()
    return value.login, value.client_id


def _catalog_access(
    subject: str, service_name: str | None = None, *, filtered_catalog=False
):
    _principal()
    try:
        resources = log_catalog.resources()
    except log_catalog.CatalogUnavailable:
        raise HTTPException(503, "Runtime catalog is unavailable") from None
    visible = {item.service_id for item in resources}
    if service_name and service_name not in visible:
        raise HTTPException(404, "Service not found")
    return visible


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


def _read(tool: str, payload: dict[str, Any], action, *, filtered_catalog=False):
    subject, client_id = _identity("eng-platform.access")
    source = catalog_source_identity()
    visible = _catalog_access(
        subject, payload.get("service_name"), filtered_catalog=filtered_catalog
    )
    try:
        value = action(visible) if filtered_catalog else action()
        _catalog_access(
            subject, payload.get("service_name"), filtered_catalog=filtered_catalog
        )
        if source != catalog_source_identity():
            raise HTTPException(503, "Runtime catalog changed; retry the request")
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
    if mcp_store.mutation_count(subject) >= config.mcp.mutation_limit_per_hour:
        raise HTTPException(
            status_code=429, detail="MCP mutation limit reached; retry after one hour"
        )
    try:
        with mcp_grants.authority(_principal()):
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
    return _read("list_services", {}, catalog.get_services, filtered_catalog=True)


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
        "eng-platform.access",
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
        "eng-platform.access",
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
def get_cost_comparison(
    days: int = 1,
    month_to_date: bool = False,
    group_by: Literal["resource", "service", "sku"] = "resource",
) -> dict[str, Any]:
    """Compare equivalent Bogota exported-usage windows; absent data is unknown."""
    return _read(
        "get_cost_comparison",
        {"days": days, "month_to_date": month_to_date, "group_by": group_by},
        lambda: costs.get_cost_comparison(
            max(1, min(days, 365)), month_to_date, group_by
        ),
    )


@mcp.tool()
def get_daily_costs(days: int = 30, month_to_date: bool = False) -> dict[str, Any]:
    """Bogota consumption days, explicit missing data and export freshness."""
    return _read(
        "get_daily_costs",
        {"days": days, "month_to_date": month_to_date},
        lambda: costs.get_daily_costs(max(1, min(days, 365)), month_to_date),
    )


@mcp.tool()
def get_cost_by_service(days: int = 30, month_to_date: bool = False) -> dict[str, Any]:
    """Exported cost by GCP service; estimates are separate."""
    return _read(
        "get_cost_by_service",
        {"days": days, "month_to_date": month_to_date},
        lambda: costs.get_cost_by_service(max(1, min(days, 365)), month_to_date),
    )


@mcp.tool()
def get_cost_by_sku(days: int = 30, month_to_date: bool = False) -> dict[str, Any]:
    """Exported SKU cost, currency and freshness."""
    return _read(
        "get_cost_by_sku",
        {"days": days, "month_to_date": month_to_date},
        lambda: costs.get_cost_by_sku(max(1, min(days, 365)), month_to_date),
    )


@mcp.tool()
def get_billing_status() -> dict[str, Any]:
    """Billing export coverage and availability; no realtime price promise."""
    return _read("get_billing_status", {}, costs.get_billing_status)


def send_cost_alert() -> dict[str, Any]:
    """Send only a server-built cost alert to the explicitly approved private Diego recipient."""
    from .services import cost_alerts

    return _mutate(
        "send_cost_alert",
        {},
        "eng-platform.access",
        lambda subject: cost_alerts.send(subject, mcp_authority=_principal()),
    )


@mcp.tool(name="send_cost_alert", structured_output=False)
def _send_cost_alert_tool() -> CallToolResult:
    """Send a server-built cost alert after explicit private-recipient authorization."""
    token = get_access_token()
    if (
        token is None
        or not token.subject
        or not set(_ACCESS_SCOPES).issubset(token.scopes)
    ):
        error = (
            "invalid_token"
            if token is None or not token.subject
            else "insufficient_scope"
        )
        challenge = (
            f'Bearer error="{error}", error_description="Full Engineering Platform authorization required", '
            f'resource_metadata="{_BASE_URL}/.well-known/oauth-protected-resource/mcp/cost-alerts", '
            f'scope="{" ".join(_ACCESS_SCOPES)}"'
        )
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text="Reconnect MCP to authorize all platform actions.",
                )
            ],
            _meta={"mcp/www_authenticate": [challenge]},
        )
    result = send_cost_alert()
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structuredContent=result,
    )


@mcp.tool()
async def get_metrics_summary(window: Literal["1h", "24h"] = "24h") -> dict[str, Any]:
    """Get public Cloud Run operational metrics from the existing monitoring source."""
    subject, client_id = _identity("eng-platform.access")
    source = catalog_source_identity()
    _catalog_access(subject)
    payload = {"window": window}
    try:
        value = await metrics.get_cloud_run_metrics(window)
        _catalog_access(subject)
        if source != catalog_source_identity():
            raise HTTPException(503, "Runtime catalog changed; retry the request")
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


def _database_payload(model, **value):
    try:
        return model(**value)
    except ValidationError:
        raise HTTPException(422, "Invalid database request") from None


# These are storage operations against the existing engine, not deployment
# mutations. They use its own distributed admission and sanitized audit trail.
def _database_call(tool: str, payload: dict, action):
    principal = _principal()
    try:
        database_jobs._session(principal)
        value = action(principal)
        database_jobs._session(principal)
    except QueryRejected:
        _audit(
            subject=principal.login,
            client_id=principal.client_id,
            tool=tool,
            payload=payload,
            mutation=False,
            result="error",
        )
        raise HTTPException(
            422, "Query is outside the supported read-only SQL language"
        ) from None
    except DatabaseUnavailable:
        _audit(
            subject=principal.login,
            client_id=principal.client_id,
            tool=tool,
            payload=payload,
            mutation=False,
            result="error",
        )
        raise HTTPException(503, "Database operation is unavailable") from None
    except Exception:
        _audit(
            subject=principal.login,
            client_id=principal.client_id,
            tool=tool,
            payload=payload,
            mutation=False,
            result="error",
        )
        raise
    _audit(
        subject=principal.login,
        client_id=principal.client_id,
        tool=tool,
        payload=payload,
        mutation=False,
        result="ok",
    )
    return value


@mcp.tool()
def list_databases() -> dict[str, Any]:
    """List enabled PostgreSQL databases. SQL is read-only; complete captures expire in one hour."""

    def action(principal):
        return {
            "databases": [
                {
                    **database.public(),
                    "max_rows": None,
                    "timeout_seconds": 240,
                }
                for database in database_registry.databases()
            ],
            "workspace_enabled": True,
            "global_sort_enabled": True,
            "workspace_limits": {
                "timeout_seconds": 240,
                "page_sizes": [25, 50, 100],
                "retention_seconds": 3600,
            },
        }

    return _database_call("list_databases", {}, action)


@mcp.tool()
def get_database_schema(database_id: str) -> dict[str, Any]:
    """Get tables, complete columns/types and associated services for an enabled database."""

    def action(principal):
        session = database_jobs._session(principal)
        database = database_jobs._database(database_id, principal.login, session)

        def authorize():
            current = database_jobs._session(principal)
            if database != database_jobs._database(
                database_id, principal.login, current
            ):
                raise HTTPException(403, "Database permission changed")

        return database_console.read_schema(
            database,
            authorize,
            admission=database_jobs.connection_slot(
                database, "metadata", principal.login, authorize
            ),
        )

    return _database_call("get_database_schema", {"database_id": database_id}, action)


@mcp.tool()
def create_database_workspace(database_id: str) -> dict[str, Any]:
    """Create a private workspace belonging to this OAuth connection."""
    return _database_call(
        "create_database_workspace",
        {"database_id": database_id},
        lambda principal: database_jobs.create_workspace(principal, database_id),
    )


@mcp.tool()
def start_database_query(
    database_id: str, workspace_id: str, sql: str, client_request_id: str
) -> dict[str, Any]:
    """Start asynchronous read-only SQL once. Poll get_database_query; do not resubmit to poll."""
    from .routers.databases import ExecutionRequest

    payload = _database_payload(
        ExecutionRequest, sql=sql, client_request_id=client_request_id
    )
    return _database_call(
        "start_database_query",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "client_request_id": client_request_id,
        },
        lambda principal: database_jobs.create_execution(
            principal, database_id, workspace_id, payload.sql, payload.client_request_id
        ),
    )


@mcp.tool()
def get_database_query(
    database_id: str, workspace_id: str, execution_id: str
) -> dict[str, Any]:
    """Get execution status, typed columns, final row count and expiry. Poll pending work with backoff."""
    return _database_call(
        "get_database_query",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
        },
        lambda principal: database_jobs.execution(
            principal, database_id, workspace_id, execution_id
        ),
    )


@mcp.tool()
def get_database_page(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    page_index: int = 0,
    page_size: Literal[25, 50, 100] = 25,
    view_id: str | None = None,
) -> dict[str, Any]:
    """Read one page of a completed immutable capture/view. Zero-based pages; default 25 rows."""
    from .routers.databases import PageRequest

    payload = _database_payload(
        PageRequest, page_index=page_index, page_size=page_size, view_id=view_id
    )
    return _database_call(
        "get_database_page",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
            **payload.model_dump(),
        },
        lambda principal: database_jobs.page(
            principal,
            database_id,
            workspace_id,
            execution_id,
            payload.page_index,
            payload.page_size,
            payload.view_id,
        ),
    )


@mcp.tool()
def cancel_database_query(
    database_id: str, workspace_id: str, execution_id: str
) -> dict[str, Any]:
    """Cancel pending execution; never executes SQL again."""
    return _database_call(
        "cancel_database_query",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
        },
        lambda principal: database_jobs.cancel(
            principal, database_id, workspace_id, execution_id
        ),
    )


@mcp.tool()
def create_database_view(
    database_id: str,
    workspace_id: str,
    execution_id: str,
    column_key: str,
    direction: Literal["asc", "desc"],
    client_request_id: str,
) -> dict[str, Any]:
    """Sort the entire completed capture asynchronously, by unique column key. NULL last; stable ties."""
    from .routers.databases import ViewRequest

    payload = _database_payload(
        ViewRequest,
        column_key=column_key,
        direction=direction,
        client_request_id=client_request_id,
    )
    return _database_call(
        "create_database_view",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
            **payload.model_dump(),
        },
        lambda principal: database_views.create_view(
            principal,
            database_id,
            workspace_id,
            execution_id,
            payload.column_key,
            payload.direction,
            payload.client_request_id,
        ),
    )


@mcp.tool()
def get_database_view(
    database_id: str, workspace_id: str, execution_id: str, view_id: str
) -> dict[str, Any]:
    """Get sorted-view status and inherited capture expiry. Use pages only after completion."""
    return _database_call(
        "get_database_view",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
            "view_id": view_id,
        },
        lambda principal: database_views.view(
            principal, database_id, workspace_id, execution_id, view_id
        ),
    )


@mcp.tool()
def cancel_database_view(
    database_id: str, workspace_id: str, execution_id: str, view_id: str
) -> dict[str, Any]:
    """Cancel or purge a sorted view; the original capture remains available."""
    return _database_call(
        "cancel_database_view",
        {
            "database_id": database_id,
            "workspace_id": workspace_id,
            "execution_id": execution_id,
            "view_id": view_id,
        },
        lambda principal: database_views.delete_view(
            principal, database_id, workspace_id, execution_id, view_id
        ),
    )


@mcp.tool()
def purge_database_workspace(database_id: str, workspace_id: str) -> dict[str, Any]:
    """Invalidate a workspace and purge its private captures and derived objects."""
    return _database_call(
        "purge_database_workspace",
        {"database_id": database_id, "workspace_id": workspace_id},
        lambda principal: database_jobs.purge_workspace(
            principal, database_id, workspace_id
        ),
    )
