"""GitHub App effects remain idempotent across uncertain responses."""

from types import SimpleNamespace
from unittest import mock

import pytest
from github.GithubException import GithubException

from eng_platform_api.services import github_release_control as control


HEAD = "a" * 40


def _not_found():
    return GithubException(404, {"message": "Not Found"}, None)


def _repository(monkeypatch, repo):
    client = SimpleNamespace(get_repo=mock.Mock(return_value=repo))
    monkeypatch.setattr(control, "github_client", lambda: client)
    return client


def test_execution_variable_is_private_only_and_recovers_missing_variable(monkeypatch):
    requester = mock.Mock()
    repo = SimpleNamespace(private=True, _requester=requester)
    _repository(monkeypatch, repo)
    monkeypatch.setattr(
        control.config.release_orchestrator, "github_mode_variable", "CI_EXECUTOR"
    )
    requester.requestJsonAndCheck.side_effect = [_not_found(), None]

    control.set_repository_execution_mode("owner/repo", "cloud_build")

    assert requester.requestJsonAndCheck.call_args_list == [
        mock.call(
            "PATCH",
            "/repos/owner/repo/actions/variables/CI_EXECUTOR",
            input={"name": "CI_EXECUTOR", "value": "cloud_build"},
        ),
        mock.call(
            "POST",
            "/repos/owner/repo/actions/variables",
            input={"name": "CI_EXECUTOR", "value": "cloud_build"},
        ),
    ]
    repo.private = False
    requester.reset_mock()
    control.set_repository_execution_mode("owner/repo", "github_actions")
    requester.requestJsonAndCheck.assert_not_called()
    with pytest.raises(ValueError, match="Invalid repository execution mode"):
        control.set_repository_execution_mode("owner/repo", "other")


def test_upsert_check_recovers_lost_create_by_external_id(monkeypatch):
    existing = SimpleNamespace(id=71, external_id="execution:quality")
    check = SimpleNamespace(id=71, edit=mock.Mock())
    repo = mock.Mock()
    repo.get_commit.return_value.get_check_runs.return_value = [existing]
    repo.get_check_run.return_value = check
    _repository(monkeypatch, repo)

    result = control.upsert_check(
        repository="owner/repo",
        head_sha=HEAD,
        kind="quality",
        status="completed",
        conclusion="success",
        external_id="execution:quality",
        summary="verified",
    )

    assert result == 71
    check.edit.assert_called_once()
    repo.create_check_run.assert_not_called()
    assert check.edit.call_args.kwargs["output"]["summary"] == "verified"
    assert "details_url" not in check.edit.call_args.kwargs


def test_upsert_check_creates_and_validates_status(monkeypatch):
    repo = mock.Mock()
    repo.create_check_run.return_value = SimpleNamespace(id=72)
    _repository(monkeypatch, repo)

    assert (
        control.upsert_check(
            repository="owner/repo", head_sha=HEAD, kind="release", status="queued"
        )
        == 72
    )
    assert (
        repo.create_check_run.call_args.kwargs["name"] == control.CHECK_NAMES["release"]
    )
    assert "details_url" not in repo.create_check_run.call_args.kwargs
    assert "conclusion" not in repo.create_check_run.call_args.kwargs
    assert "external_id" not in repo.create_check_run.call_args.kwargs
    with pytest.raises(ValueError, match="Unknown Engineering Platform check"):
        control.upsert_check(
            repository="owner/repo", head_sha=HEAD, kind="other", status="queued"
        )
    with pytest.raises(ValueError, match="Invalid check status"):
        control.upsert_check(
            repository="owner/repo", head_sha=HEAD, kind="quality", status="other"
        )
    with pytest.raises(ValueError, match="valid conclusion"):
        control.upsert_check(
            repository="owner/repo", head_sha=HEAD, kind="quality", status="completed"
        )


def test_upsert_check_includes_only_populated_optional_fields(monkeypatch):
    repo = mock.Mock()
    repo.get_commit.return_value.get_check_runs.return_value = []
    repo.create_check_run.return_value = SimpleNamespace(id=73)
    _repository(monkeypatch, repo)

    control.upsert_check(
        repository="owner/repo",
        head_sha=HEAD,
        kind="quality",
        status="in_progress",
        details_url="https://example.test/quality",
        external_id="execution:quality",
    )

    fields = repo.create_check_run.call_args.kwargs
    assert fields["details_url"] == "https://example.test/quality"
    assert fields["external_id"] == "execution:quality"
    assert "started_at" in fields
    assert "completed_at" not in fields


def _execution():
    return {
        "repository": "owner/repo",
        "head_sha": HEAD,
        "release_plan": {
            "git_tag": "v1.2.3",
            "release_type": "patch",
            "notes": "Release notes",
        },
    }


def test_publish_release_completes_partial_tag_without_creating_second_tag(monkeypatch):
    reference = SimpleNamespace(object=SimpleNamespace(sha=HEAD, type="commit"))
    release = SimpleNamespace(id=32, html_url="https://github.test/release/32")
    repo = mock.Mock()
    repo.get_git_ref.return_value = reference
    repo.get_release.side_effect = _not_found()
    repo.create_git_release.return_value = release
    _repository(monkeypatch, repo)
    monkeypatch.setattr(control, "current_default_sha", lambda _: HEAD)

    result = control.publish_release(_execution())

    assert result["tag"] == "v1.2.3"
    assert result["tag_sha"] == HEAD
    assert result["release_id"] == 32
    repo.create_git_ref.assert_not_called()
    assert repo.create_git_release.call_args.kwargs["target_commitish"] == HEAD


def test_publish_release_is_idempotent_for_annotated_tag_and_existing_release(
    monkeypatch,
):
    repo = mock.Mock()
    repo.get_git_ref.return_value = SimpleNamespace(
        object=SimpleNamespace(sha="b" * 40, type="tag")
    )
    repo.get_git_tag.return_value = SimpleNamespace(object=SimpleNamespace(sha=HEAD))
    repo.get_release.return_value = SimpleNamespace(
        tag_name="v1.2.3",
        draft=False,
        prerelease=False,
        body="Release notes",
        id=33,
        html_url="https://github.test/release/33",
    )
    _repository(monkeypatch, repo)
    monkeypatch.setattr(control, "current_default_sha", lambda _: HEAD)

    assert control.publish_release(_execution())["release_id"] == 33
    repo.create_git_ref.assert_not_called()
    repo.create_git_release.assert_not_called()


def test_publish_release_rejects_moved_main_or_conflicting_tag(monkeypatch):
    repo = mock.Mock()
    _repository(monkeypatch, repo)
    monkeypatch.setattr(control, "current_default_sha", lambda _: "b" * 40)
    with pytest.raises(control.GitHubReleaseConflict, match="Default branch moved"):
        control.publish_release(_execution())
    repo.get_git_ref.assert_not_called()

    monkeypatch.setattr(control, "current_default_sha", lambda _: HEAD)
    repo.get_git_ref.return_value = SimpleNamespace(
        object=SimpleNamespace(sha="c" * 40, type="commit")
    )
    with pytest.raises(control.GitHubReleaseConflict, match="another commit"):
        control.publish_release(_execution())
    repo.create_git_release.assert_not_called()
