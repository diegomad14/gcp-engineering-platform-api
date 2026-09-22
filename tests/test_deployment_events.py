"""Provider-neutral callback projection and rejection contracts."""

from unittest import mock

import pytest
from fastapi import HTTPException

from eng_platform_api.models import DeploymentExecutionEvent, DeploymentItem
from eng_platform_api.routers import deployment_events
from eng_platform_api.services import github_deployments


FINGERPRINT = "a" * 64


def _item(*, kind: str = "deploy") -> DeploymentItem:
    return DeploymentItem(
        id="deployment-1",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v1.2.3",
        sha="b" * 40,
        kind=kind,
        github_deployment_id=123,
        stages=github_deployments.default_stages(kind),
    )


def _payload(**changes) -> DeploymentExecutionEvent:
    values = {
        "build_id": "build-1",
        "fingerprint": FINGERPRINT,
        "sequence": 1,
        "stage": "promote",
        "status": "running",
    }
    values.update(changes)
    return DeploymentExecutionEvent(**values)


@pytest.fixture
def callback_mocks(monkeypatch):
    item = _item()
    execution = {
        "provider": "cloud_build",
        "fingerprint": FINGERPRINT,
        "build_id": "build-1",
        "log_url": "https://logs.invalid/build-1",
    }
    monkeypatch.setattr(deployment_events, "_verify_identity", lambda _: None)
    monkeypatch.setattr(
        deployment_events.deployment_executions, "get", lambda _: execution
    )
    monkeypatch.setattr(deployment_events.deployment_store, "get", lambda _: item)
    monkeypatch.setattr(
        deployment_events.cloud_build,
        "get_build",
        lambda _: {
            "substitutions": {
                "_REQUEST_FINGERPRINT": FINGERPRINT,
                "_DEPLOYMENT_ID": item.id,
                "_RELEASE_SHA": item.sha,
            }
        },
    )
    monkeypatch.setattr(
        deployment_events.deployment_executions,
        "accept_event",
        lambda _deployment_id, sequence, **changes: (
            {"event_sequence": sequence, **changes},
            True,
        ),
    )
    saved = mock.Mock()
    status = mock.Mock()
    monkeypatch.setattr(deployment_events.deployment_store, "save", saved)
    monkeypatch.setattr(
        deployment_events.github_deployments, "set_managed_status", status
    )
    return item, execution, saved, status


@pytest.mark.parametrize(
    ("stage", "status", "expected"),
    [
        ("build", "running", "BUILDING"),
        ("promote", "running", "PROMOTING"),
        ("validate-production", "succeeded", "SUCCEEDED"),
        ("rollback", "succeeded", "ROLLED_BACK"),
        ("validate-candidate", "failed", "FAILED"),
    ],
)
def test_callback_projects_monotonic_stage(callback_mocks, stage, status, expected):
    item, _execution, saved, github_status = callback_mocks
    result = deployment_events.accept_event(
        item.id,
        _payload(
            stage=stage,
            status=status,
            candidate_revision="candidate-1",
            production_revision="production-1",
            production_url="https://service.invalid",
            error="smoke failed" if status == "failed" else "",
        ),
        authorization="Bearer test",
    )

    assert result == {"accepted": True, "event_sequence": 1}
    assert item.status == expected
    assert item.candidate_revision == "candidate-1"
    assert item.production_revision == "production-1"
    assert item.stages
    saved.assert_called_once()
    github_status.assert_called_once()


def test_duplicate_callback_is_harmless(callback_mocks, monkeypatch):
    item, _execution, saved, github_status = callback_mocks
    monkeypatch.setattr(
        deployment_events.deployment_executions,
        "accept_event",
        lambda *_args, **_kwargs: ({"event_sequence": 9}, False),
    )

    result = deployment_events.accept_event(
        item.id, _payload(sequence=8), authorization="Bearer test"
    )

    assert result == {"accepted": True, "event_sequence": 9}
    saved.assert_not_called()
    github_status.assert_not_called()


@pytest.mark.parametrize(
    ("execution_change", "payload_change", "detail"),
    [
        ({"provider": "github_actions"}, {}, "not Cloud Build managed"),
        ({"fingerprint": "c" * 64}, {}, "fingerprint mismatch"),
        ({"build_id": "other"}, {}, "Cloud Build identity mismatch"),
    ],
)
def test_callback_rejects_wrong_execution_identity(
    callback_mocks, execution_change, payload_change, detail
):
    item, execution, _saved, _status = callback_mocks
    execution.update(execution_change)

    with pytest.raises(HTTPException, match=detail):
        deployment_events.accept_event(
            item.id, _payload(**payload_change), authorization="Bearer test"
        )


def test_callback_rejects_unknown_and_unverifiable_build(callback_mocks, monkeypatch):
    item, _execution, _saved, _status = callback_mocks
    monkeypatch.setattr(deployment_events.deployment_executions, "get", lambda _: None)
    with pytest.raises(HTTPException) as unknown:
        deployment_events.accept_event(item.id, _payload(), authorization="Bearer test")
    assert unknown.value.status_code == 404

    monkeypatch.setattr(
        deployment_events.deployment_executions,
        "get",
        lambda _: {
            "provider": "cloud_build",
            "fingerprint": FINGERPRINT,
            "build_id": "build-1",
        },
    )
    monkeypatch.setattr(
        deployment_events.cloud_build,
        "get_build",
        mock.Mock(side_effect=RuntimeError("api unavailable")),
    )
    with pytest.raises(HTTPException) as unavailable:
        deployment_events.accept_event(item.id, _payload(), authorization="Bearer test")
    assert unavailable.value.status_code == 502


def test_callback_rejects_build_substitution_mismatch(callback_mocks, monkeypatch):
    item, _execution, _saved, _status = callback_mocks
    monkeypatch.setattr(
        deployment_events.cloud_build,
        "get_build",
        lambda _: {"substitutions": {"_RELEASE_SHA": "wrong"}},
    )
    with pytest.raises(HTTPException) as mismatch:
        deployment_events.accept_event(item.id, _payload(), authorization="Bearer test")
    assert mismatch.value.status_code == 403
