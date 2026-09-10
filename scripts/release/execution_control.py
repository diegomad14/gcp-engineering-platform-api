"""Client adapter for the Engineering Platform release-control contract.

The adapter only calls control-plane endpoints.  It never issues a ticket and
it has no method that activates a provider operation; lifecycle activation is a
separate, currently disabled gate.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class ExecutionControlError(RuntimeError):
    """A control-plane request was rejected or could not be completed."""


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
    artifact_digest: str = ""
    target: str = ""
    configuration_hash: str = ""

    def as_payload(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "repository": self.repository,
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
    result_digest: str = ""

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "IntentHandle":
        try:
            return cls(
                intent_id=str(value["intent_id"]),
                status=str(value["status"]),
                reconciliation_required=bool(value["reconciliation_required"]),
                result_digest=str(value.get("result_digest", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionControlError(
                "Control plane returned an invalid intent"
            ) from exc


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class PlatformExecutionControlClient:
    """Independent CLI/Actions-compatible client for the shared contract."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

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
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise ExecutionControlError(
                f"Control plane rejected {path} ({exc.code}){suffix}"
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
    ) -> IntentHandle:
        value = self._post(
            "/api/internal/release-execution/intents/reconcile",
            {
                "intent_id": intent.intent_id,
                "reconciliation_id": reconciliation_id,
                "outcome": outcome,
                "observation_digest": observation_digest,
            },
        )
        return IntentHandle.from_payload(value)
