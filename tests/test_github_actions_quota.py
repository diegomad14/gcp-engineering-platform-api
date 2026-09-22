from datetime import datetime, timezone
from unittest import mock

from eng_platform_api.services import github_actions_quota as quota


def test_public_repository_never_uses_cloud_build(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("public",))
    repo = mock.MagicMock(private=False)
    monkeypatch.setattr(
        quota, "github_client", lambda: mock.MagicMock(get_repo=lambda _: repo)
    )
    monkeypatch.setattr(
        quota,
        "current_usage",
        lambda **_: quota.Usage(9999, datetime.now(timezone.utc)),
    )
    assert quota.should_use_cloud_build("public", "owner/public") is False


def test_private_repository_uses_cloud_build_at_included_limit(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("private",))
    repo = mock.MagicMock(private=True)
    monkeypatch.setattr(
        quota, "github_client", lambda: mock.MagicMock(get_repo=lambda _: repo)
    )
    monkeypatch.setattr(
        quota,
        "current_usage",
        lambda **_: quota.Usage(2000, datetime.now(timezone.utc)),
    )
    assert quota.should_use_cloud_build("private", "owner/private") is True


def test_unknown_billing_keeps_github_as_the_conservative_choice(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("private",))
    repo = mock.MagicMock(private=True)
    monkeypatch.setattr(
        quota, "github_client", lambda: mock.MagicMock(get_repo=lambda _: repo)
    )
    monkeypatch.setattr(quota, "current_usage", lambda **_: None)
    assert quota.should_use_cloud_build("private", "owner/private") is False


def test_service_must_be_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ())
    assert quota.should_use_cloud_build("private", "owner/private") is False


def test_reactive_fallback_requires_exact_no_job_startup_failure(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("private",))
    item = mock.MagicMock(
        service_name="private",
        sha="a" * 40,
        candidate_revision="",
        production_revision="",
    )
    run = mock.MagicMock(
        conclusion="startup_failure", head_sha="a" * 40, event="workflow_dispatch"
    )
    run.jobs.return_value = []
    monkeypatch.setattr(
        quota,
        "current_usage",
        lambda **_: quota.Usage(2000, datetime.now(timezone.utc)),
    )
    assert quota.is_reactive_quota_failure(run, item) is True
    run.conclusion = "failure"
    assert quota.is_reactive_quota_failure(run, item) is False


def test_reactive_fallback_accepts_explicit_zero_step_billing_annotation(monkeypatch):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("private",))
    item = mock.MagicMock(
        service_name="private",
        repository="owner/private",
        sha="a" * 40,
        candidate_revision="",
        production_revision="",
    )
    job = mock.MagicMock(id=42, steps=[])
    run = mock.MagicMock(
        conclusion="failure", head_sha="a" * 40, event="workflow_dispatch"
    )
    run.jobs.return_value = [job]
    monkeypatch.setattr(
        quota,
        "_job_annotations_are_quota_failure",
        lambda repository, jobs: repository == "owner/private" and jobs == [job],
    )
    assert quota.is_reactive_quota_failure(run, item) is True


def test_quota_error_classifier_does_not_treat_code_failures_as_billing():
    assert quota.is_quota_error("Included minutes quota exceeded") is True
    assert quota.is_quota_error("tests failed with exit code 1") is False


def test_usage_cache_is_reused_within_month_and_rotates_at_utc_month(monkeypatch):
    class Clock:
        month = 1

        @classmethod
        def now(cls, _timezone):
            return datetime(2026, cls.month, 1, tzinfo=timezone.utc)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "usageItems": [
                    {
                        "product": "Actions",
                        "sku": "Actions Linux",
                        "unitType": "minutes",
                        "repositoryName": "private",
                        "quantity": Clock.month,
                    }
                ]
            }

    get = mock.MagicMock(return_value=Response())
    monkeypatch.setattr(quota, "datetime", Clock)
    monkeypatch.setattr(quota.httpx, "get", get)
    monkeypatch.setattr(quota, "_private_repositories", lambda: {"private"})
    monkeypatch.setattr(quota.config.github, "billing_owner", "diegomad14")
    monkeypatch.setattr(quota.config.github, "token", "token")
    monkeypatch.setattr(quota, "_cache", None)

    assert quota.current_usage().private_linux_minutes == 1
    assert quota.current_usage().private_linux_minutes == 1
    assert get.call_count == 1

    Clock.month = 2
    assert quota.current_usage().private_linux_minutes == 2
    assert get.call_count == 2
