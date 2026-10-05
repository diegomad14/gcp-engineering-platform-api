"""Fresh GitHub OAuth sessions for durable database work.

The signed browser cookie holds an opaque nonce; the control store only holds
its SHA-256 identity. There is no process-local production session fallback.
Workers validate that identity without receiving the browser's credentials.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from time import time
from typing import Any

from fastapi import HTTPException, Request

from ..config import config

COOKIE_FIELD = "database_session_nonce"
SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")


def _unavailable() -> HTTPException:
    return HTTPException(503, "Database session is unavailable")


def _unauthorized() -> HTTPException:
    return HTTPException(401, "Sign in with GitHub again to query databases")


def _store():
    # Shared, explicitly configured durable control storage. Its in-memory
    # adapter is available only when a test injects it deliberately.
    from . import database_job_store

    return database_job_store


def _enabled() -> None:
    if config.mock_mode or not getattr(config.databases, "executions_enabled", False):
        raise _unauthorized()


def _login(value: Any) -> str:
    if not isinstance(value, str) or not _LOGIN.fullmatch(value):
        raise _unauthorized()
    return value.lower()


def _session_id(nonce: Any) -> str:
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise _unauthorized()
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _timestamp(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def create(login: str) -> str:
    """Mint only after a real OAuth callback, with fixed server-side expiry."""
    _enabled()
    from ..security import log_auth_configured

    if not log_auth_configured():
        raise _unavailable()
    identity = _login(login)
    nonce = secrets.token_urlsafe(32)
    session_id = _session_id(nonce)
    now = time()
    record = {
        "kind": "session",
        "id": session_id,
        "login": identity,
        "created_at": now,
        "expires_at": now + SESSION_LIFETIME_SECONDS,
        "revoked_at": 0.0,
    }
    key = f"session:{session_id}"

    def insert(values):
        if values[key] is not None:
            raise ValueError("Database session identity is already reserved")
        return {key: record}

    try:
        _store().mutate([("session", session_id)], insert)
    except Exception:
        raise _unavailable() from None
    return nonce


def validate(session_id: str, login: str) -> dict[str, Any]:
    """Re-read durable revocation/expiry for an API request or worker phase."""
    _enabled()
    identity = _login(login)
    if not isinstance(session_id, str) or not _IDENTITY.fullmatch(session_id):
        raise _unauthorized()
    if not config.databases.enabled or identity not in config.databases.allowed_logins:
        raise HTTPException(403, "You are not allowed to query databases")
    try:
        record = _store().get("session", session_id)
    except Exception:
        raise _unavailable() from None
    now = time()
    if (
        not isinstance(record, dict)
        or record.get("kind") != "session"
        or record.get("id") != session_id
        or record.get("login") != identity
        or not _timestamp(record.get("created_at"))
        or not _timestamp(record.get("expires_at"))
        or not _timestamp(record.get("revoked_at"))
        or record["revoked_at"] != 0
        or record["created_at"] > now
        or record["expires_at"] <= now
        or record["expires_at"] - record["created_at"] > SESSION_LIFETIME_SECONDS
    ):
        raise _unauthorized()
    return dict(record)


def require(request: Request) -> dict[str, Any]:
    """Require the fresh nonce in a genuine signed GitHub OAuth cookie."""
    _enabled()
    if request.session.get("github_auth_provider") != "github_oauth":
        raise _unauthorized()
    identity = _login(request.session.get("github_login"))
    session_id = _session_id(request.session.get(COOKIE_FIELD))
    record = validate(session_id, identity)
    request.state.database_session = record
    return record


def revoke(request: Request) -> str | None:
    """Durably revoke before cookie removal; an uncertain write blocks logout.

    This also revokes previously minted sessions when executions have since been
    disabled. Missing or malformed nonces cannot identify an issued session.
    """
    nonce = request.session.get(COOKIE_FIELD)
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        return None
    session_id = _session_id(nonce)
    key = f"session:{session_id}"
    revoked_at = time()

    def revocation(values):
        record = values[key]
        if record is None:
            return {}
        # Preserve revocation on duplicate logout; never recreate a live session.
        return {key: {**record, "revoked_at": record.get("revoked_at") or revoked_at}}

    try:
        _store().mutate([("session", session_id)], revocation)
    except Exception:
        raise _unavailable() from None
    return session_id
