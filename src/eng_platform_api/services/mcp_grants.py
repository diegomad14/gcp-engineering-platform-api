"""Durable MCP authority, also the atomic publication fence for BD workers.

The database control store holds only credential hashes. OAuth rotation changes
the credential generation, never the grant/session identity or result retention.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
import time

from fastapi import HTTPException

from ..config import config
from . import database_job_store as store

SCOPE = "eng-platform.access"
POLICY = "mcp-uniform-v1"
_current: ContextVar["Principal | None"] = ContextVar("mcp_authority", default=None)


@dataclass(frozen=True)
class Principal:
    id: str
    login: str
    client_id: str
    resource: str
    generation: int


def resource(value: str | None) -> str:
    base = config.mcp.public_base_url.rstrip("/")
    canonical = f"{base}/mcp"
    if value not in (None, canonical, f"{base}/mcp/cost-alerts"):
        raise HTTPException(401, "Invalid MCP resource")
    return canonical


def validate(identity: str, login: str | None = None) -> dict:
    if not config.mcp.enabled or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise HTTPException(401, "Reconnect MCP to authorize full access")
    value = store.get("session", identity)
    if (
        not value
        or value.get("kind") != "session"
        or value.get("id") != identity
        or value.get("source") != "mcp"
        or value.get("authority_policy") != POLICY
        or value.get("scopes") != [SCOPE]
        or value.get("revoked_at") != 0
        or value.get("expires_at", 0) <= time.time()
        or value.get("resource") != resource(None)
        or not value.get("client_id")
        or (login is not None and value.get("login") != login.lower())
    ):
        raise HTTPException(401, "Reconnect MCP to authorize full access")
    return value


def require(principal: Principal) -> dict:
    value = validate(principal.id, principal.login)
    if any(
        value.get(key) != getattr(principal, key)
        for key in ("client_id", "resource", "generation")
    ):
        raise HTTPException(401, "MCP credential has been rotated or revoked")
    return value


def principal(value: dict) -> Principal:
    return Principal(
        value["id"],
        value["login"],
        value["client_id"],
        value["resource"],
        value["generation"],
    )


@contextmanager
def authority(value: Principal):
    require(value)
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def release_claims(requested_by: str) -> dict:
    value = _current.get()
    if value is None:
        return {}
    require(value)
    if value.login != requested_by.lower():
        raise HTTPException(403, "MCP operator mismatch")
    return {"mcp_grant_id": value.id, "mcp_authority_policy": POLICY}


def revoke(identity: str) -> None:
    def change(values):
        value = values[f"session:{identity}"]
        if not value or value.get("source") != "mcp":
            return {}
        value["revoked_at"] = value.get("revoked_at") or time.time()
        return {f"session:{identity}": value}

    store.mutate([("session", identity)], change)
    # Authority is already revoked even if task dispatch needs a retry.
    from . import database_jobs

    database_jobs.purge_session(identity)
