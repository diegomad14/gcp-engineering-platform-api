"""Secret boundary tests: authorization, request redaction and no implicit retry."""

from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from eng_platform_api.config import load_config
from eng_platform_api.main import app
from eng_platform_api.models import CatalogService, OperationalSecret
from eng_platform_api.services import operational_secrets as secrets


@pytest.fixture
def operator():
    with (
        mock.patch(
            "eng_platform_api.security.get_identity",
            return_value="angelmarin1122-coder",
        ),
        mock.patch(
            "eng_platform_api.security.config.auth.allowed_logins",
            ("angelmarin1122-coder",),
        ),
    ):
        yield TestClient(app)


def headers():
    return {
        "Origin": "http://localhost:5173",
        "X-Requested-With": "EngineeringPlatform",
        "Idempotency-Key": str(uuid4()),
    }


PATH = "/api/services/cgm-sanplat-api/secrets/WM_PASSWORD/versions"


def test_missing_allowlist_has_no_default_operator(monkeypatch):
    monkeypatch.delenv("ENG_PLATFORM_ALLOWED_GITHUB_LOGINS", raising=False)
    assert load_config().auth.allowed_logins == ()


def test_empty_allowlist_denies_authenticated_operator(operator):
    with mock.patch("eng_platform_api.security.config.auth.allowed_logins", ()):
        assert operator.get("/api/services/cgm-sanplat-api/secrets").status_code == 403


def test_unapproved_operator_cannot_save(operator):
    with mock.patch(
        "eng_platform_api.security.get_identity", return_value="unapproved"
    ):
        assert (
            operator.post(
                PATH, headers=headers(), json={"value": "private", "generation": 0}
            ).status_code
            == 403
        )


def test_cross_origin_write_is_rejected(operator):
    request_headers = headers() | {"Origin": "https://attacker.example"}
    assert (
        operator.post(
            PATH, headers=request_headers, json={"value": "private", "generation": 0}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"value": "SECRET", "generation": -1},
        {"value": "SECRET", "generation": 0, "project": "other"},
        {"value": "", "generation": 0},
    ],
)
def test_invalid_body_never_echoes_input(operator, payload):
    response = operator.post(PATH, headers=headers(), json=payload)
    assert response.status_code in {415, 422}
    assert "SECRET" not in response.text


def test_engine_secret_is_not_editable(operator):
    response = operator.post(
        PATH.replace("WM_PASSWORD", "ENG_PLATFORM_SESSION_SECRET"),
        headers=headers(),
        json={"value": "private", "generation": 0},
    )
    assert response.status_code == 403


def test_provider_failure_is_redacted(operator, caplog):
    with mock.patch.object(
        secrets, "publish", side_effect=RuntimeError("SECRET-PAYLOAD")
    ):
        response = operator.post(
            PATH, headers=headers(), json={"value": "SECRET-PAYLOAD", "generation": 0}
        )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "SECRET-PAYLOAD" not in response.text + caplog.text


def test_success_returns_only_metadata(operator):
    record = {
        "operation_id": str(uuid4()),
        "status": "SAVED",
        "version": "4",
        "generation": 1,
    }
    with mock.patch.object(secrets, "publish", return_value=record) as publish:
        response = operator.post(
            PATH, headers=headers(), json={"value": "SECRET-PAYLOAD", "generation": 0}
        )
    assert response.status_code == 201
    assert response.json() == record
    assert response.headers["cache-control"] == "no-store"
    assert publish.call_args.args[-1] == "angelmarin1122-coder"


def service():
    secret = OperationalSecret(
        key="WM_PASSWORD", secret_id="wm-password", editable=True
    )
    return CatalogService(
        service_name="cgm-sanplat-api",
        repository="owner/repo",
        owner="owner",
        project_id="project",
        region="region",
        operational_secrets=[secret],
    )


def test_publish_disables_provider_retries_and_does_not_finalize_uncertain_result():
    client = mock.Mock()
    client.add_secret_version.side_effect = TimeoutError("SECRET-PAYLOAD")
    selected = service()
    with (
        mock.patch.object(secrets, "writer", return_value=client),
        mock.patch.object(secrets, "database"),
        mock.patch.object(secrets, "reserve", return_value=None),
        mock.patch.object(secrets, "finalize") as finalize,
    ):
        with pytest.raises(TimeoutError):
            secrets.publish(
                selected,
                selected.operational_secrets[0],
                "SECRET-PAYLOAD",
                str(uuid4()),
                0,
                "angel",
            )
    assert client.add_secret_version.call_count == 1
    assert client.add_secret_version.call_args.kwargs["retry"] is None
    finalize.assert_not_called()
    client.access_secret_version.assert_not_called()


def test_saved_operation_is_returned_without_republishing():
    selected = service()
    previous = {
        "operation_id": str(uuid4()),
        "status": "SAVED",
        "version": "2",
        "generation": 1,
    }
    with (
        mock.patch.object(secrets, "writer") as writer,
        mock.patch.object(secrets, "database"),
        mock.patch.object(secrets, "reserve", return_value=previous),
    ):
        assert (
            secrets.publish(
                selected,
                selected.operational_secrets[0],
                "private",
                previous["operation_id"],
                0,
                "angel",
            )
            == previous
        )
    writer.return_value.add_secret_version.assert_not_called()
