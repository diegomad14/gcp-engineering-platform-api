"""Authentication boundaries for private release operations."""

from unittest import mock

import pytest
from fastapi import HTTPException

from eng_platform_api.routers import release_execution_events
from eng_platform_api.routers import release_operations


def test_scheduler_reconciliation_uses_dedicated_identity(monkeypatch):
    expected = "reconciler@test-project.iam.gserviceaccount.com"
    observed: dict[str, str] = {}
    monkeypatch.setattr(
        release_operations.config.release_orchestrator,
        "reconciler_service_account",
        expected,
    )

    def verify(token: str, *, expected_identity: str = ""):
        observed.update(token=token, expected_identity=expected_identity)
        return {"email": expected_identity}

    monkeypatch.setattr(release_operations, "_verify_google", verify)
    monkeypatch.setattr(
        release_operations.release_executions, "list_due", lambda **_kwargs: []
    )

    assert release_operations.reconcile_due("Bearer scheduler-token") == {
        "reconciled": 0,
        "items": [],
        "deployments": {"reconciled": 0, "items": []},
    }
    assert observed == {
        "token": "scheduler-token",
        "expected_identity": expected,
    }


def test_quality_build_identity_cannot_authenticate_as_reconciler(monkeypatch):
    quality = "quality@test-project.iam.gserviceaccount.com"
    reconciler = "reconciler@test-project.iam.gserviceaccount.com"
    monkeypatch.setattr(release_execution_events.config, "mock_mode", False)
    with mock.patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value={"email": quality, "sub": "build"},
    ):
        with pytest.raises(HTTPException) as error:
            release_execution_events._verify_google(
                "stolen-build-token", expected_identity=reconciler
            )

    assert error.value.status_code == 401
