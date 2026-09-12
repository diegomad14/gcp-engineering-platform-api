"""Durable control adapter used by the local release lifecycle.

This module deliberately does not execute a provider operation.  It composes
the existing platform execution-control client around an injected effect:
authorization, lease, durable intent, effect, durable result, and lease
release.  Provider adapters remain responsible for the effect itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from .execution_control import (
    ExecutionContext,
    LeaseHandle,
    PlatformExecutionControlClient,
)


class LifecycleControlError(RuntimeError):
    """The durable lifecycle control protocol could not be completed."""


Effect = Callable[[], Any]
Persist = Callable[[], None]
Emit = Callable[..., None]


def effect_digest(value: Any) -> str:
    """Return the stable digest used to bind one exact lifecycle effect."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class LifecycleControlSession:
    """One lifecycle operation guarded by the shared durable control plane."""

    client: PlatformExecutionControlClient
    context: ExecutionContext
    owner_id: str
    scope: str
    scope_key: str
    authorization_jti: str
    lease: LeaseHandle
    intent_count: int = 0
    unknown: bool = False
    finished: bool = False

    @classmethod
    def open(
        cls,
        client: PlatformExecutionControlClient,
        context: ExecutionContext,
        *,
        token: str = "",
        owner_id: str,
        scope: str,
        scope_key: str,
        ttl_seconds: int = 900,
    ) -> "LifecycleControlSession":
        if not owner_id:
            raise LifecycleControlError("A lifecycle control owner is required")
        if token and client._transport is None:
            raise LifecycleControlError(
                "Direct capability tokens are permitted only for local fixtures"
            )
        capability = token or client.issue_capability(context)
        authorization = client.consume_authorization(capability, context)
        jti = str(authorization.get("jti", ""))
        if not jti:
            raise LifecycleControlError("Control plane returned no authorization id")
        lease = client.acquire_lease(
            context,
            scope=scope,
            scope_key=scope_key,
            owner_id=owner_id,
            authorization_jti=jti,
            ttl_seconds=ttl_seconds,
        )
        return cls(
            client=client,
            context=context,
            owner_id=owner_id,
            scope=scope,
            scope_key=scope_key,
            authorization_jti=jti,
            lease=lease,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "context": self.context.as_payload(),
            "scope": self.scope,
            "scope_key": self.scope_key,
            "owner_id": self.owner_id,
            "authorization_jti": self.authorization_jti,
            "lease": {
                "lease_id": self.lease.lease_id,
                "generation": self.lease.generation,
                "version": self.lease.version,
                "status": self.lease.status,
                "expires_at": self.lease.expires_at,
            },
        }

    def run_effect(
        self,
        *,
        execution: dict[str, Any],
        stage: str,
        intent: str,
        effect_key: str,
        effect: Effect,
        emit: Emit,
        persist: Persist,
        command: list[str] | None = None,
    ) -> Any:
        """Run one effect only after a durable intent has been recorded."""
        if self.finished or self.unknown:
            raise LifecycleControlError("Session is closed or requires reconciliation")
        idempotency_key = (
            f"{self.context.release_id}:{self.context.operation}:{effect_key}"
        )
        digest = effect_digest(
            {
                "context": self.context.as_payload(),
                "scope_key": self.scope_key,
                "stage": stage,
                "effect_key": effect_key,
                "command": command or [],
            }
        )
        try:
            durable_intent = self.client.create_intent(
                self.context,
                idempotency_key=idempotency_key,
                effect_digest=digest,
                lease=self.lease,
                authorization_jti=self.authorization_jti,
            )
        except Exception as exc:  # no provider effect was attempted
            raise LifecycleControlError(
                f"Unable to persist lifecycle intent for {stage}: {exc}"
            ) from exc
        if not durable_intent.created or durable_intent.status != "INTENDED":
            # A replay is an observation, never a fresh grant to mutate. This
            # includes INTENDED: the original caller may still be executing.
            self.unknown = True
            raise LifecycleControlError(
                f"Intent {durable_intent.intent_id} already exists; reconcile before continuing"
            )
        self.intent_count += 1
        execution.setdefault("control_intents", []).append(
            {
                "stage": stage,
                "intent_id": durable_intent.intent_id,
                "effect_digest": digest,
                "status": durable_intent.status,
            }
        )
        emit(
            execution,
            stage=stage,
            intent=intent,
            result="INTENT_RECORDED",
            remote_effect=True,
            command=command,
            detail=f"intent_id={durable_intent.intent_id}",
        )
        persist()
        try:
            result = effect()
        except BaseException as exc:
            self.unknown = True
            error_code = type(exc).__name__.lower()
            try:
                self.client.record_result(
                    durable_intent,
                    lease=self.lease,
                    status="UNKNOWN",
                    error_code=error_code[:128],
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
            emit(
                execution,
                stage=stage,
                intent="record uncertain provider result",
                result="UNKNOWN",
                remote_effect=True,
                detail=str(exc),
            )
            persist()
            raise

        result_hash = effect_digest(
            {"intent_id": durable_intent.intent_id, "result": result}
        )
        try:
            confirmed = self.client.record_result(
                durable_intent,
                lease=self.lease,
                status="CONFIRMED",
                result_digest=result_hash,
            )
        except BaseException as exc:
            self.unknown = True
            try:
                self.client.record_result(
                    durable_intent,
                    lease=self.lease,
                    status="UNKNOWN",
                    error_code="control_result_persistence_lost",
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
            emit(
                execution,
                stage=stage,
                intent="record uncertain durable result",
                result="UNKNOWN",
                remote_effect=True,
                detail=str(exc),
            )
            persist()
            raise LifecycleControlError(
                f"Unable to persist lifecycle result for {stage}: {exc}"
            ) from exc
        emit(
            execution,
            stage=stage,
            intent="record durable provider result",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"intent_id={confirmed.intent_id} result_digest={result_hash}",
        )
        persist()
        return result

    def finish(self, *, final_status: str) -> LeaseHandle | None:
        """Release only a session with a definitive result."""
        if self.finished or self.unknown:
            return None
        if final_status not in {"CONFIRMED", "FAILED"}:
            raise LifecycleControlError(
                f"Unsupported final lifecycle status: {final_status}"
            )
        released = self.client.release_lease(self.lease, final_status=final_status)
        self.finished = True
        return released
