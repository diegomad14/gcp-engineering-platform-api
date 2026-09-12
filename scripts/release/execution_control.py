"""Client adapter for the Engineering Platform release-control contract.

The adapter requests server-issued capabilities using the authenticated session.
It never signs a ticket or activates a provider operation itself.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse


class ExecutionControlError(RuntimeError):
    """A control-plane request was rejected or could not be completed."""


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ExecutionControlError(
            "Platform credential-bearing requests must not redirect"
        )


def authenticated_urlopen(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_NoCredentialRedirect()).open(
        request, timeout=timeout
    )


def _protected_auth_headers() -> dict[str, str]:
    """Load session credentials without putting secrets in command arguments."""
    path_value = os.environ.get("ENG_PLATFORM_AUTH_HEADERS_FILE", "").strip()
    if not path_value:
        raise ExecutionControlError(
            "A protected platform session header file is required"
        )
    path = Path(path_value).expanduser()
    try:
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077 or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ExecutionControlError(
                "Platform credential file must not be group-readable"
            )
        if info.st_size > 65536:
            raise ExecutionControlError("Platform credential file is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionControlError(
            "Protected platform credential file is unreadable"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and key.lower() in {"cookie", "authorization"}
        and isinstance(item, str)
        and item
        and "\n" not in item
        and "\r" not in item
        for key, item in value.items()
    ):
        raise ExecutionControlError(
            "Protected platform credential file has invalid headers"
        )
    return dict(value)


def _require_secure_base_url(base_url: str) -> str:
    """Accept HTTPS control planes and explicit local HTTP fixture doubles only."""
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExecutionControlError(
            "Platform URL must not contain credentials, query or fragment"
        )
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https" and parsed.hostname:
        return normalized
    if parsed.scheme == "http" and parsed.hostname in loopback_hosts:
        return normalized
    raise ExecutionControlError(
        "Engineering Platform control URL must use HTTPS (HTTP is limited to loopback fixtures)"
    )


def publication_scope_key(repository: str, versioning_policy: str) -> str:
    """Build the shared key for one repository/versioning-policy publisher."""
    return f"publication:{repository}:{versioning_policy}"


def deployment_scope_key(target: str) -> str:
    """Build the shared key for one canonical service/environment target."""
    return f"deployment:{target}"


@dataclass(frozen=True)
class ExecutionContext:
    release_id: str
    repository: str
    source_sha: str
    tag: str
    operation: str
    actor_id: str
    service_name: str = ""
    release_group_id: str = ""
    artifact_digest: str = ""
    target: str = ""
    configuration_hash: str = ""

    def as_payload(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "repository": self.repository,
            "service_name": self.service_name,
            "release_group_id": self.release_group_id,
            "source_sha": self.source_sha,
            "tag": self.tag,
            "artifact_digest": self.artifact_digest,
            "target": self.target,
            "operation": self.operation,
            "configuration_hash": self.configuration_hash,
            "actor_id": self.actor_id,
        }


@dataclass(frozen=True)
class LeaseHandle:
    lease_id: str
    scope: str
    scope_key: str
    owner_id: str
    generation: int
    version: int
    expires_at: str
    status: str = ""
    takeover_allowed: bool = False

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "LeaseHandle":
        try:
            return cls(
                lease_id=str(value["lease_id"]),
                scope=str(value["scope"]),
                scope_key=str(value["scope_key"]),
                owner_id=str(value["owner_id"]),
                generation=int(value["generation"]),
                version=int(value["version"]),
                expires_at=str(value["expires_at"]),
                status=str(value.get("status", "")),
                takeover_allowed=bool(value.get("takeover_allowed", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionControlError(
                "Control plane returned an invalid lease"
            ) from exc


@dataclass(frozen=True)
class IntentHandle:
    intent_id: str
    status: str
    reconciliation_required: bool
    created: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    effect_digest: str = ""
    result_digest: str = ""
    scope_key: str = ""
    owner_id: str = ""
    lease_id: str = ""
    lease_generation: int = 0
    lease_version: int = 0

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "IntentHandle":
        try:
            return cls(
                intent_id=str(value["intent_id"]),
                status=str(value["status"]),
                reconciliation_required=bool(value["reconciliation_required"]),
                created=value.get("created") is True,
                context=dict(value.get("context") or {}),
                effect_digest=str(value.get("effect_digest", "")),
                result_digest=str(value.get("result_digest", "")),
                scope_key=str(value.get("scope_key", "")),
                owner_id=str(value.get("owner_id", "")),
                lease_id=str(value.get("lease_id", "")),
                lease_generation=int(value.get("lease_generation", 0) or 0),
                lease_version=int(value.get("lease_version", 0) or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionControlError(
                "Control plane returned an invalid intent"
            ) from exc


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]
ReadTransport = Callable[[str], dict[str, Any] | None]


class PlatformExecutionControlClient:
    """Independent CLI/Actions-compatible client for the shared contract."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        read_transport: ReadTransport | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = _require_secure_base_url(base_url) if base_url else ""
        self.timeout = timeout
        self._transport = transport
        self._read_transport = read_transport
        self._auth_headers = (
            dict(auth_headers)
            if auth_headers is not None
            else ({} if transport is not None else _protected_auth_headers())
        )

    def issue_capability(self, context: ExecutionContext) -> str:
        """Issue one authenticated capability; retain it only in memory until consume."""
        if not context.service_name or context.release_group_id:
            raise ExecutionControlError(
                "A capability requires one service and no release group"
            )
        payload = context.as_payload()
        payload.pop("actor_id", None)
        payload.pop("release_group_id", None)
        value = self._post(
            "/api/internal/release-execution/authorizations/issue", payload
        )
        token = value.get("token")
        if not isinstance(token, str) or not token:
            raise ExecutionControlError("Control plane returned no capability")
        return token

    def authenticated_actor(self) -> str:
        value = self._get("/api/auth/me") or {}
        if (
            not value.get("authenticated")
            or not value.get("can_deploy")
            or not value.get("login")
        ):
            raise ExecutionControlError(
                "An authenticated platform deployer session is required"
            )
        return str(value["login"])

    def authenticated_headers(self, base_url: str) -> dict[str, str]:
        if _require_secure_base_url(base_url) != self.base_url:
            raise ExecutionControlError(
                "Registration must use the authenticated control origin"
            )
        return dict(self._auth_headers)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url and self._transport is None:
            raise ExecutionControlError("Engineering Platform control URL is required")
        if self._transport is not None:
            value = self._transport(path, payload)
            if not isinstance(value, dict):
                raise ExecutionControlError(
                    "Control plane returned a non-object response"
                )
            return value
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self._auth_headers,
            },
            method="POST",
        )
        try:
            with authenticated_urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ExecutionControlError(
                f"Control plane rejected {path} ({exc.code})"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ExecutionControlError(
                f"Control plane unavailable for {path}"
            ) from exc
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExecutionControlError("Control plane returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ExecutionControlError("Control plane returned a non-object response")
        return value

    def consume_authorization(
        self, token: str, context: ExecutionContext
    ) -> dict[str, Any]:
        if not token:
            raise ExecutionControlError("A platform-issued authorization is required")
        return self._post(
            "/api/internal/release-execution/authorizations/consume",
            {"token": token, **context.as_payload()},
        )

    def acquire_lease(
        self,
        context: ExecutionContext,
        *,
        scope: str,
        scope_key: str,
        owner_id: str,
        authorization_jti: str,
        ttl_seconds: int = 900,
        reconciliation_id: str = "",
    ) -> LeaseHandle:
        value = self._post(
            "/api/internal/release-execution/leases/acquire",
            {
                **context.as_payload(),
                "scope": scope,
                "scope_key": scope_key,
                "owner_id": owner_id,
                "authorization_jti": authorization_jti,
                "ttl_seconds": ttl_seconds,
                "reconciliation_id": reconciliation_id,
            },
        )
        return LeaseHandle.from_payload(value)

    def renew_lease(self, lease: LeaseHandle, *, ttl_seconds: int = 900) -> LeaseHandle:
        value = self._post(
            "/api/internal/release-execution/leases/renew",
            {
                "lease_id": lease.lease_id,
                "scope_key": lease.scope_key,
                "owner_id": lease.owner_id,
                "generation": lease.generation,
                "version": lease.version,
                "ttl_seconds": ttl_seconds,
            },
        )
        return LeaseHandle.from_payload(value)

    def release_lease(self, lease: LeaseHandle, *, final_status: str) -> LeaseHandle:
        value = self._post(
            "/api/internal/release-execution/leases/release",
            {
                "lease_id": lease.lease_id,
                "scope_key": lease.scope_key,
                "owner_id": lease.owner_id,
                "generation": lease.generation,
                "version": lease.version,
                "ttl_seconds": 900,
                "final_status": final_status,
            },
        )
        return LeaseHandle.from_payload(value)

    def reconcile_lease(
        self,
        lease: LeaseHandle,
        *,
        reconciliation_id: str,
        observation: str,
        observation_digest: str,
    ) -> LeaseHandle:
        value = self._post(
            "/api/internal/release-execution/leases/reconcile",
            {
                "lease_id": lease.lease_id,
                "scope_key": lease.scope_key,
                "owner_id": lease.owner_id,
                "generation": lease.generation,
                "version": lease.version,
                "reconciliation_id": reconciliation_id,
                "observation": observation,
                "observation_digest": observation_digest,
            },
        )
        return LeaseHandle.from_payload(value)

    def create_intent(
        self,
        context: ExecutionContext,
        *,
        idempotency_key: str,
        effect_digest: str,
        lease: LeaseHandle,
        authorization_jti: str,
    ) -> IntentHandle:
        value = self._post(
            "/api/internal/release-execution/intents",
            {
                **context.as_payload(),
                "idempotency_key": idempotency_key,
                "effect_digest": effect_digest,
                "scope": lease.scope,
                "scope_key": lease.scope_key,
                "owner_id": lease.owner_id,
                "lease_id": lease.lease_id,
                "lease_generation": lease.generation,
                "lease_version": lease.version,
                "authorization_jti": authorization_jti,
            },
        )
        return IntentHandle.from_payload(value)

    def record_result(
        self,
        intent: IntentHandle,
        *,
        lease: LeaseHandle,
        status: str,
        result_digest: str = "",
        error_code: str = "",
    ) -> IntentHandle:
        value = self._post(
            "/api/internal/release-execution/intents/result",
            {
                "intent_id": intent.intent_id,
                "scope_key": lease.scope_key,
                "owner_id": lease.owner_id,
                "lease_id": lease.lease_id,
                "lease_generation": lease.generation,
                "lease_version": lease.version,
                "status": status,
                "result_digest": result_digest,
                "error_code": error_code,
            },
        )
        return IntentHandle.from_payload(value)

    def reconcile_intent(
        self,
        intent: IntentHandle,
        *,
        reconciliation_id: str,
        outcome: str,
        observation_digest: str,
        lease: LeaseHandle | None = None,
    ) -> IntentHandle:
        value = self._post(
            "/api/internal/release-execution/intents/reconcile",
            {
                "intent_id": intent.intent_id,
                "reconciliation_id": reconciliation_id,
                "outcome": outcome,
                "observation_digest": observation_digest,
                "scope_key": intent.scope_key,
                "owner_id": intent.owner_id,
                "lease_id": intent.lease_id,
                "lease_generation": intent.lease_generation,
                "lease_version": lease.version if lease else intent.lease_version,
            },
        )
        return IntentHandle.from_payload(value)

    def get_lease(self, scope_key: str) -> LeaseHandle | None:
        value = self._get(
            f"/api/internal/release-execution/leases/{quote(scope_key, safe='')}"
        )
        if value is None:
            return None
        return LeaseHandle.from_payload(value)

    def get_intent(self, intent_id: str) -> IntentHandle | None:
        value = self._get(
            f"/api/internal/release-execution/intents/{quote(intent_id, safe='')}"
        )
        if value is None:
            return None
        return IntentHandle.from_payload(value)

    def _get(self, path: str) -> dict[str, Any] | None:
        if self._read_transport is not None:
            value = self._read_transport(path)
            if value is not None and not isinstance(value, dict):
                raise ExecutionControlError(
                    "Control plane returned a non-object response"
                )
            return value
        if not self.base_url:
            raise ExecutionControlError("Engineering Platform control URL is required")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", **self._auth_headers},
            method="GET",
        )
        try:
            with authenticated_urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise ExecutionControlError(
                f"Control plane rejected {path} ({exc.code})"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ExecutionControlError(
                f"Control plane unavailable for {path}"
            ) from exc
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExecutionControlError("Control plane returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ExecutionControlError("Control plane returned a non-object response")
        return value
