"""One server-built cost alert per Bogota day, to one explicitly attested recipient."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException

from ..config import config
from ..models import CostComparison
from . import gcp_billing_bigquery as billing
from . import mcp_store

RECIPIENT_ID = "76bfd5f9-0a15-4cc1-87b8-ad1b74a19165"
OWNER_LOGIN = "diegomad14"
GATEWAY_URL = "https://communications-ms-pzzhmu7una-uc.a.run.app/api/v2/messages"


def _authorized(subject: str, *, mcp_authority=None) -> None:
    settings = config.cost_alerts
    if not settings.enabled:
        raise HTTPException(409, "Cost notifications are disabled")
    if mcp_authority is not None:
        from . import mcp_grants

        mcp_grants.require(mcp_authority)
        if mcp_authority.login != subject.lower():
            raise HTTPException(403, "MCP operator mismatch")
    if mcp_authority is None and (
        subject.lower() != OWNER_LOGIN
        or subject.lower() not in settings.allowed_logins
        or settings.recipient_owner_login != subject.lower()
    ):
        raise HTTPException(403, "Cost notification recipient authorization required")
    if (
        settings.recipient_owner_login != OWNER_LOGIN
        or not settings.private_destination_confirmed
        or settings.recipient_id != RECIPIENT_ID
        or not re.fullmatch(r"[1-9][0-9]{4,14}", settings.recipient_address)
        or not settings.recipient_address.endswith("7589")
    ):
        raise HTTPException(
            409, "Explicit verified private destination configuration required"
        )
    if not settings.communications_api_key:
        raise HTTPException(409, "Server notification credential is not configured")
    if not config.mcp.oauth_collection or not config.mcp.audit_collection:
        raise HTTPException(
            409, "Persistent private notification state and audit are required"
        )
    if config.mock_mode:
        raise HTTPException(409, "Mock billing cannot send cost notifications")


def _text(comparison: CostComparison, now: datetime) -> str | None:
    for summary in [comparison.current, comparison.previous]:
        quality = summary.data_quality
        if quality.status != "partial" or not quality.latest_export_at:
            return None
        exported = billing._timestamp(quality.latest_export_at)
        if exported is None or not timedelta(0) <= now - exported <= timedelta(
            hours=config.cost_alerts.maximum_export_age_hours
        ):
            return None
    changes = [
        item
        for item in comparison.items
        if item.comparable
        and item.net_change is not None
        and item.current is not None
        and item.previous is not None
        and abs(item.net_change) >= config.cost_alerts.minimum_change
    ]
    if not changes:
        return None
    lines = [
        "Costes GCP observados en el export (provisionales, no tiempo real).",
        "Zona: America/Bogota. Ventanas equivalentes:",
        f"Actual: {comparison.current_start_at} → {comparison.current_end_at}",
        f"Anterior: {comparison.previous_start_at} → {comparison.previous_end_at}",
    ]
    for item in sorted(
        changes, key=lambda item: abs(item.net_change or 0), reverse=True
    )[:10]:
        assert item.current is not None and item.previous is not None
        assert item.net_change is not None
        name = " ".join((item.service_name or "sin atribución de recurso").split())[
            :100
        ]
        service = " ".join(item.gcp_service.split())[:60]
        lines.append(
            f"{service} / {name}: {item.current.net_cost:.4f} {item.currency}; "
            f"cambio {item.net_change:+.4f} frente a {item.previous.net_cost:.4f}."
        )
    lines.append(
        "Datos ausentes o sin cobertura equivalente: excluidos. "
        "Estimaciones de Cloud Build: excluidas; los créditos ya están descontados."
    )
    return "\n".join(lines)


def _deliver(text: str, event_key: str) -> dict:
    # OAuth to eng-platform never becomes a gateway API key. No redirects,
    # caller URL/address/text, Telegram token, or Secret Manager lookup.
    body = {
        "channel": "telegram",
        "recipient": {"address": config.cost_alerts.recipient_address},
        "message": {"kind": "text", "text": text, "format": "plain"},
        "trace_id": event_key,
        "delivery": {"mode": "immediate", "priority": "normal"},
    }
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(
                GATEWAY_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {config.cost_alerts.communications_api_key}",
                    "Idempotency-Key": event_key,
                },
            )
        if response.status_code != 202:
            raise ValueError("Gateway did not accept the notification")
        result = response.json()
        status = result.get("status")
        if status not in {
            "queued",
            "sending",
            "sent",
            "provider_accepted",
            "delivered",
            "read",
            "send_unknown",
            "suppressed_quiet_hours",
            "retry_scheduled",
            "failed",
        }:
            raise ValueError("Unexpected gateway status")
        message_id = result.get("message_id", "")
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,64}", message_id
        ):
            raise ValueError("Invalid gateway reference")
        return {
            "status": "gateway_accepted",
            "gateway_status": status,
            "gateway_message_id": message_id,
            "provider_accepted": status
            in {"provider_accepted", "sent", "delivered", "read"},
            "delivery_confirmed": False,
        }
    except Exception:
        raise HTTPException(
            502, "Notification outcome unknown; retry retains gateway idempotency"
        ) from None


def send(subject: str, *, mcp_authority=None) -> dict:
    _authorized(subject, mcp_authority=mcp_authority)
    now = billing.utc_now()
    day = now.astimezone(billing._TIMEZONE).date().isoformat()
    event_key = (
        "cost-alert-" + hashlib.sha256(f"{RECIPIENT_ID}:{day}".encode()).hexdigest()
    )
    destination_hash = hashlib.sha256(
        config.cost_alerts.recipient_address.encode()
    ).hexdigest()
    existing = mcp_store.get("cost_alert", event_key)
    if existing and existing.get("destination_hash") != destination_hash:
        raise HTTPException(409, "Approved destination changed within this alert day")
    if existing and existing.get("result"):
        return {**existing["result"], "deduplicated": True}
    text = existing.get("text") if existing else None
    if not text:
        comparison = billing.get_cost_comparison(days=1, group_by="resource")
        text = _text(comparison, now)
        if not text:
            return {"status": "no_comparable_change", "delivery_confirmed": False}
    claim = secrets.token_hex(16)

    def reserve(current):
        if current and current.get("destination_hash") != destination_hash:
            raise HTTPException(
                409, "Approved destination changed within this alert day"
            )
        if (
            current.get("result")
            or float(current.get("lease_until", 0)) > now.timestamp()
        ):
            return current
        return {
            **current,
            "destination_hash": destination_hash,
            "claim": claim,
            "lease_until": now.timestamp() + 120,
            "text": current.get("text") or text,
        }

    reserved = mcp_store.update_cost_alert(event_key, reserve)
    if reserved.get("result"):
        return {**reserved["result"], "deduplicated": True}
    if reserved.get("claim") != claim:
        return {"status": "in_progress", "delivery_confirmed": False}
    try:
        result = _deliver(reserved["text"], event_key)
    except HTTPException:
        mcp_store.update_cost_alert(event_key, lambda r: {**r, "lease_until": 0})
        raise
    mcp_store.update_cost_alert(
        event_key, lambda r: {**r, "result": result, "lease_until": 0}
    )
    return result
