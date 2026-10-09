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


def _bootstrap_guard_fixture(
    monkeypatch, *, runs=None, pr_changes=None, workflow_changes=None
):
    repository = "diegomad14/cgm-artemis-api"
    pr = {
        "state": "open",
        "number": 172,
        "head": {"sha": HEAD, "repo": {"id": 1306114845}},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"id": 1306114845}},
    }
    pr.update(pr_changes or {})
    workflow = {
        "id": 123,
        "state": "disabled_manually",
        "path": ".github/workflows/eng-platform-quality.yml",
    }
    workflow.update(workflow_changes or {})
    requester = mock.Mock()
    pages = runs if runs is not None else [[]]
    requester.requestJsonAndCheck.side_effect = [
        ({}, pr),
        ({}, workflow),
        *[({}, {"workflow_runs": page}) for page in pages],
    ]
    _repository(
        monkeypatch, SimpleNamespace(id=1306114845, private=True, _requester=requester)
    )
    args = {
        "repository": repository,
        "repository_id": 1306114845,
        "pull_request_number": 172,
        "head_sha": HEAD,
        "base_sha": "b" * 40,
    }
    return requester, args


def _canonical_run(**changes):
    return {
        "id": 1,
        "workflow_id": 123,
        "path": ".github/workflows/eng-platform-quality.yml",
        "event": "pull_request_target",
        "status": "completed",
        "head_sha": "b" * 40,
        "display_title": "eng-platform-quality-" + "c" * 40,
        "pull_requests": [],
        **changes,
    }


def test_bootstrap_guard_requires_disabled_exact_workflow_and_current_pr(monkeypatch):
    requester, args = _bootstrap_guard_fixture(monkeypatch)
    assert control.verify_quality_bootstrap_preconditions(**args) == {
        "workflow_id": 123,
        "workflow_path": ".github/workflows/eng-platform-quality.yml",
    }
    assert requester.requestJsonAndCheck.call_args.kwargs["parameters"] == {
        "per_page": 100,
        "page": 1,
    }


@pytest.mark.parametrize("state", ["active", "disabled_inactivity", "deleted"])
def test_bootstrap_guard_rejects_active_or_unexpected_disabled_state(
    monkeypatch, state
):
    _, args = _bootstrap_guard_fixture(monkeypatch, workflow_changes={"state": state})
    with pytest.raises(control.GitHubReleaseConflict, match="disabled manually"):
        control.verify_quality_bootstrap_preconditions(**args)


@pytest.mark.parametrize(
    "run",
    [
        _canonical_run(status="queued"),
        _canonical_run(status="in_progress"),
        _canonical_run(display_title=f"eng-platform-quality-{HEAD}"),
        _canonical_run(pull_requests=[{"number": 172, "head": {"sha": HEAD}}]),
        _canonical_run(display_title="unverifiable base-sha run"),
        _canonical_run(event="workflow_dispatch"),
        _canonical_run(path=".github/workflows/other.yml"),
    ],
)
def test_bootstrap_guard_rejects_active_existing_or_unverifiable_runs(monkeypatch, run):
    _, args = _bootstrap_guard_fixture(monkeypatch, runs=[[run]])
    with pytest.raises(control.GitHubReleaseConflict):
        control.verify_quality_bootstrap_preconditions(**args)


def test_bootstrap_guard_paginates_without_head_sha_filter(monkeypatch):
    requester, args = _bootstrap_guard_fixture(
        monkeypatch,
        runs=[
            [_canonical_run(id=index) for index in range(100)],
            [_canonical_run(display_title=f"eng-platform-quality-{HEAD}")],
        ],
    )
    with pytest.raises(control.GitHubReleaseConflict, match="already exists"):
        control.verify_quality_bootstrap_preconditions(**args)
    assert requester.requestJsonAndCheck.call_args.kwargs["parameters"] == {
        "per_page": 100,
        "page": 2,
    }
    assert all(
        "head_sha" not in call.kwargs.get("parameters", {})
        for call in requester.requestJsonAndCheck.call_args_list
    )


def test_bootstrap_guard_rejects_live_pr_drift_and_workflow_id_drift(monkeypatch):
    _, args = _bootstrap_guard_fixture(
        monkeypatch, pr_changes={"head": {"sha": "d" * 40, "repo": {"id": 1306114845}}}
    )
    with pytest.raises(control.GitHubReleaseConflict, match="pull request"):
        control.verify_quality_bootstrap_preconditions(**args)
    _, args = _bootstrap_guard_fixture(monkeypatch)
    with pytest.raises(control.GitHubReleaseConflict, match="disabled manually"):
        control.verify_quality_bootstrap_preconditions(**args, workflow_id=124)


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


def test_repository_execution_mode_reads_the_server_owned_actions_variable(
    monkeypatch,
):
    requester = mock.Mock()
    requester.requestJsonAndCheck.return_value = (
        {},
        {"name": "ENG_PLATFORM_CI_EXECUTOR", "value": "cloud_build"},
    )
    repo = SimpleNamespace(_requester=requester)
    _repository(monkeypatch, repo)
    monkeypatch.setattr(
        control.config.release_orchestrator,
        "github_mode_variable",
        "ENG_PLATFORM_CI_EXECUTOR",
    )

    mode = control.repository_execution_mode("owner/repo")

    assert mode == "cloud_build"
    requester.requestJsonAndCheck.assert_called_once_with(
        "GET", "/repos/owner/repo/actions/variables/ENG_PLATFORM_CI_EXECUTOR"
    )


def test_source_token_uses_public_integration_requester_and_read_only_scope(
    monkeypatch,
):
    _repository(monkeypatch, SimpleNamespace(id=123))
    monkeypatch.setattr(control.config.github, "app_id", "4321207")
    monkeypatch.setattr(control.config.github, "installation_id", "159121904")
    monkeypatch.setattr(control.config.github, "private_key", "test-key")
    requester = mock.Mock()
    requester.requestJsonAndCheck.return_value = (
        {},
        {"token": "read-token", "expires_at": "2026-09-23T21:00:00Z"},
    )
    integration = SimpleNamespace(requester=requester)
    monkeypatch.setattr(control, "GithubIntegration", lambda *_: integration)

    token, expires_at = control.installation_read_token("owner/repo")

    assert token == "read-token"
    assert expires_at == "2026-09-23T21:00:00Z"
    requester.requestJsonAndCheck.assert_called_once_with(
        "POST",
        "/app/installations/159121904/access_tokens",
        input={"repository_ids": [123], "permissions": {"contents": "read"}},
    )


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
