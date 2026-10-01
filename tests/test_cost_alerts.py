"""Limited notification authorization and retries; no external messages are sent."""

import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from eng_platform_api import mcp_server
from eng_platform_api.config import CostAlertsConfig, config, load_config
from eng_platform_api.models import (
    BillingQuality,
    CostChange,
    CostComparison,
    CostItem,
    CostPeriod,
    CostSummary,
)
from eng_platform_api.services import cost_alerts as alerts, mcp_store

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def approved(monkeypatch):
    settings = CostAlertsConfig(
        enabled=True,
        allowed_logins=(alerts.OWNER_LOGIN,),
        recipient_owner_login=alerts.OWNER_LOGIN,
        recipient_id=alerts.RECIPIENT_ID,
        private_destination_confirmed=True,
        recipient_address="123457589",
        communications_api_key="fake-server-service-key",
    )
    monkeypatch.setattr(config, "cost_alerts", settings)
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(mcp_store, "_collection", lambda kind: None)
    from google.cloud import firestore

    monkeypatch.setattr(
        firestore, "Client", lambda **kwargs: pytest.fail("Unexpected provider client")
    )
    monkeypatch.setattr(alerts.billing, "utc_now", lambda: NOW)
    for records in mcp_store._memory.values():
        records.clear()
    monkeypatch.setattr(
        alerts.httpx,
        "Client",
        lambda **kwargs: pytest.fail("Unexpected external transport"),
    )
    monkeypatch.setattr(mcp_store, "mutation_count", lambda subject: 0)
    monkeypatch.setattr(
        mcp_store,
        "save_audit",
        lambda value: mcp_store._memory["audit"].update({"test": value}),
    )
    monkeypatch.setattr(config.mcp, "audit_collection", "test-audit")
    return settings


@pytest.fixture
def comparison():
    def summary(cost):
        return CostSummary(
            period=CostPeriod(start="2026-10-01", end="2026-10-01"),
            total_cost=cost + 0.25,
            total_credits=-0.25,
            total_net_cost=cost,
            data_quality=BillingQuality(
                status="partial", latest_export_at=NOW.isoformat()
            ),
        )

    current = CostItem(
        project_id="cgm-assistant-prod",
        service_name="db\ninstance",
        gcp_service="Cloud SQL",
        cost=2.25,
        credits=-0.25,
        net_cost=2,
    )
    previous = current.model_copy(update={"cost": 1.25, "net_cost": 1})
    change = CostChange(
        project_id=current.project_id,
        service_name=current.service_name,
        gcp_service=current.gcp_service,
        current=current,
        previous=previous,
        comparable=True,
        net_change=1,
    )
    return CostComparison(
        current=summary(2),
        previous=summary(1),
        current_start_at="2026-10-01T00:00:00-05:00",
        current_end_at="2026-10-01T04:00:00-05:00",
        previous_start_at="2026-09-30T00:00:00-05:00",
        previous_end_at="2026-09-30T04:00:00-05:00",
        items=[change],
        comparable=True,
        net_change=1,
    )


def transport(
    monkeypatch,
    status_code=202,
    status="queued",
    message_id="test-message-1",
    error=False,
):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 30, "follow_redirects": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if error:
                raise RuntimeError("secret-provider-content")
            return SimpleNamespace(
                status_code=status_code,
                json=lambda: {"status": status, "message_id": message_id},
            )

    monkeypatch.setattr(alerts.httpx, "Client", Client)
    return calls


def test_default_disabled_and_configuration_secrets_hidden():
    default = CostAlertsConfig()
    assert not default.enabled and not default.allowed_logins
    configured = CostAlertsConfig(
        recipient_address="123457589", communications_api_key="fake-secret"
    )
    assert "123457589" not in repr(configured) and "fake-secret" not in repr(configured)


def test_environment_loading_explicit_private_owner_attestation(monkeypatch):
    values = {
        "ENABLED": "true",
        "ALLOWED_LOGINS": " DIEGOMAD14 ",
        "RECIPIENT_ID": alerts.RECIPIENT_ID,
        "OWNER_LOGIN": " DIEGOMAD14 ",
        "PRIVATE_DESTINATION_CONFIRMED": "true",
        "RECIPIENT_ADDRESS": "123457589",
        "COMMUNICATIONS_API_KEY": "fake-key",
    }
    for key, value in values.items():
        monkeypatch.setenv("ENG_PLATFORM_COST_ALERTS_" + key, value)
    loaded = load_config().cost_alerts
    assert loaded.enabled and loaded.allowed_logins == (alerts.OWNER_LOGIN,)
    assert (
        loaded.recipient_owner_login == alerts.OWNER_LOGIN
        and loaded.private_destination_confirmed
    )
    assert (
        loaded.recipient_address == "123457589"
        and loaded.communications_api_key == "fake-key"
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("enabled", False, 409),
        ("allowed_logins", (), 403),
        ("recipient_owner_login", "someone", 403),
        ("private_destination_confirmed", False, 409),
        ("recipient_id", "another-recipient", 409),
        ("recipient_address", "-123457589", 409),
        ("recipient_address", "123457588", 409),
        ("communications_api_key", "", 409),
    ],
)
def test_unapproved_configuration_never_calls_gateway(approved, field, value, code):
    setattr(approved, field, value)
    with pytest.raises(HTTPException) as exc:
        alerts.send(alerts.OWNER_LOGIN)
    assert exc.value.status_code == code


def test_other_user_mock_and_no_durable_store_block(approved, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        alerts.send("someone")
    assert exc.value.status_code == 403
    monkeypatch.setattr(config, "mock_mode", True)
    with pytest.raises(HTTPException, match="Mock billing"):
        alerts.send(alerts.OWNER_LOGIN)
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.mcp, "oauth_collection", "")
    with pytest.raises(HTTPException, match="Persistent private"):
        alerts.send(alerts.OWNER_LOGIN)


@pytest.mark.parametrize(
    "status,exported",
    [
        ("unavailable", NOW.isoformat()),
        ("no_data", NOW.isoformat()),
        ("mixed_currency", NOW.isoformat()),
        ("partial", None),
        ("partial", "invalid"),
        ("partial", "2026-10-01T12:00:00"),
        ("partial", (NOW - timedelta(hours=49)).isoformat()),
        ("partial", (NOW + timedelta(seconds=1)).isoformat()),
    ],
)
def test_unknown_stale_or_invalid_export_suppresses_alert(
    approved, comparison, monkeypatch, status, exported
):
    comparison.current.data_quality.status = status
    comparison.current.data_quality.latest_export_at = exported
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "no_comparable_change"


def test_missing_resource_or_small_change_does_not_become_savings(
    approved, comparison, monkeypatch
):
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    comparison.items[0].comparable = False
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "no_comparable_change"
    comparison.items[0].comparable = True
    comparison.items[0].net_change = 0.01
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "no_comparable_change"


def test_fixed_destination_generated_text_and_daily_idempotency(
    approved, comparison, monkeypatch
):
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    calls = transport(monkeypatch)
    result = alerts.send(alerts.OWNER_LOGIN)
    assert result["status"] == "gateway_accepted" and not result["delivery_confirmed"]
    assert not result["provider_accepted"]
    assert alerts.send(alerts.OWNER_LOGIN)["deduplicated"] and len(calls) == 1
    url, request = calls[0]
    assert url == alerts.GATEWAY_URL
    assert request["headers"]["Authorization"] == "Bearer fake-server-service-key"
    body = request["json"]
    assert body["recipient"] == {"address": "123457589"}
    assert body["message"]["format"] == "plain"
    text = body["message"]["text"]
    assert "provisionales, no tiempo real" in text and "America/Bogota" in text
    assert "db instance" in text and "Estimaciones de Cloud Build: excluidas" in text
    assert "2.0000 USD" in text and "créditos ya están descontados" in text
    assert "fake-server-service-key" not in text and "123457589" not in text
    assert body["trace_id"] == request["headers"]["Idempotency-Key"]
    monkeypatch.setattr(alerts.billing, "utc_now", lambda: NOW + timedelta(days=1))
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "gateway_accepted"
    assert (
        len(calls) == 2
        and calls[0][1]["headers"]["Idempotency-Key"]
        != calls[1][1]["headers"]["Idempotency-Key"]
    )


def test_ambiguous_retry_freezes_payload_and_idempotency(
    approved, comparison, monkeypatch
):
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    first = transport(monkeypatch, error=True)
    with pytest.raises(HTTPException) as exc:
        alerts.send(alerts.OWNER_LOGIN)
    assert exc.value.status_code == 502 and "secret-provider-content" not in str(
        exc.value
    )
    comparison.items[0].net_change = 10
    second = transport(monkeypatch, status="sent")
    result = alerts.send(alerts.OWNER_LOGIN)
    assert first[0][1] == second[0][1]
    assert result["provider_accepted"] and not result["delivery_confirmed"]
    approved.recipient_address = "234567589"
    with pytest.raises(HTTPException, match="destination changed"):
        alerts.send(alerts.OWNER_LOGIN)
    assert len(second) == 1


@pytest.mark.parametrize(
    "code,status,message_id",
    [
        (401, "queued", "private-reference"),
        (202, "unknown", "private-reference"),
        (202, "sent", "<private-content>"),
    ],
)
def test_gateway_error_contract_is_sanitized(
    approved, code, status, message_id, monkeypatch
):
    transport(monkeypatch, status_code=code, status=status, message_id=message_id)
    with pytest.raises(HTTPException) as exc:
        alerts._deliver("server-built", "opaque-key")
    assert exc.value.status_code == 502 and message_id not in str(exc.value)


def test_concurrent_reservation_allows_one_dispatch(approved, comparison, monkeypatch):
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    key = (
        "cost-alert-"
        + hashlib.sha256(f"{alerts.RECIPIENT_ID}:2026-10-01".encode()).hexdigest()
    )
    mcp_store.save(
        "cost_alert",
        key,
        {
            "claim": "other",
            "lease_until": NOW.timestamp() + 60,
            "text": "server-built earlier",
            "destination_hash": hashlib.sha256(
                approved.recipient_address.encode()
            ).hexdigest(),
        },
    )
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "in_progress"


@pytest.fixture
def identity():
    tokens = []

    def select(scopes, subject=alerts.OWNER_LOGIN):
        tokens.append(
            auth_context_var.set(
                AuthenticatedUser(
                    AccessToken(
                        token="fake-oauth-token",
                        client_id="test",
                        subject=subject,
                        scopes=scopes,
                    )
                )
            )
        )

    yield select
    for token in reversed(tokens):
        auth_context_var.reset(token)


def test_notification_tool_has_no_arbitrary_parameters_and_requires_scope(
    approved, identity
):
    assert not inspect.signature(mcp_server.send_cost_alert).parameters
    with pytest.raises(TypeError):
        mcp_server.send_cost_alert(text="caller-text", recipient="somewhere")
    identity(["eng-platform.read"])
    with pytest.raises(HTTPException) as exc:
        mcp_server.send_cost_alert()
    assert exc.value.status_code == 403


def test_notification_tool_passes_owner_and_audits_no_private_payload(
    approved, identity, monkeypatch
):
    identity(["eng-platform.cost-alerts.send"])
    subjects = []
    monkeypatch.setattr(
        alerts,
        "send",
        lambda subject: subjects.append(subject) or {"status": "no_comparable_change"},
    )
    assert mcp_server.send_cost_alert()["status"] == "no_comparable_change"
    assert subjects == [alerts.OWNER_LOGIN]
    audit = list(mcp_store._memory["audit"].values())[-1]
    assert audit["tool"] == "send_cost_alert" and audit["mutation"]
    assert "fake-server-service-key" not in str(audit) and "123457589" not in str(audit)


@pytest.mark.parametrize(
    "tool",
    [
        "get_daily_costs",
        "get_cost_by_service",
        "get_cost_by_sku",
        "get_billing_status",
        "get_cost_comparison",
    ],
)
def test_new_finops_tools_require_read_scope_and_return_quality(
    tool, identity, monkeypatch
):
    method = getattr(mcp_server, tool)
    identity([])
    with pytest.raises(HTTPException):
        method()
    identity(["eng-platform.read"])
    monkeypatch.setattr(
        mcp_server.costs,
        tool,
        lambda *args, **kwargs: {"data_quality": {"status": "partial"}},
    )
    assert method()["data_quality"]["status"] == "partial"


@pytest.mark.parametrize(
    "gateway_status,accepted",
    [
        ("provider_accepted", True),
        ("send_unknown", False),
        ("failed", False),
        ("retry_scheduled", False),
    ],
)
def test_gateway_acceptance_and_provider_outcome_are_distinct(
    approved, monkeypatch, gateway_status, accepted
):
    transport(monkeypatch, status=gateway_status)
    result = alerts._deliver("server-built", "opaque-key")
    assert result["gateway_status"] == gateway_status
    assert (
        result["provider_accepted"] is accepted
        and result["delivery_confirmed"] is False
    )


def test_nested_same_day_dispatch_observes_atomic_lease(
    approved, comparison, monkeypatch
):
    monkeypatch.setattr(alerts.billing, "get_cost_comparison", lambda **kw: comparison)
    outcomes = []

    def deliver(text, key):
        outcomes.append(alerts.send(alerts.OWNER_LOGIN)["status"])
        return {"status": "gateway_accepted", "delivery_confirmed": False}

    monkeypatch.setattr(alerts, "_deliver", deliver)
    assert alerts.send(alerts.OWNER_LOGIN)["status"] == "gateway_accepted"
    assert outcomes == ["in_progress"]
