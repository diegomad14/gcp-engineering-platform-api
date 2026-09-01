import base64
import importlib.util
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/ops/local-release-runner/runner.py"
COMMIT_SHA = "a" * 40
RUNNER_LABEL = f"cgm-release-local-{COMMIT_SHA}"
SPEC = importlib.util.spec_from_file_location("local_release_runner", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
local_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_runner)


def test_user_data_does_not_include_registration_token_in_plaintext():
    data = local_runner.make_user_data(
        repository="diegomad14/example",
        runner_token="registration-token-secret",
        runner_name="cgm-release-local-123",
        runner_label=RUNNER_LABEL,
        runner_version="2.000.0",
        runner_sha256="a" * 64,
    )

    assert "registration-token-secret" not in data
    bootstrap = (SCRIPT.parent / "guest-bootstrap.sh").read_bytes()
    assert base64.b64encode(bootstrap).decode() in data
    assert "CGM_RUNNER_TOKEN" in data
    provision = (SCRIPT.parent / "image-provision.sh").read_bytes()
    assert base64.b64encode(provision).decode() in data
    assert "cgm-release-runner-provision.sh" in data
    manifest = (SCRIPT.parent / "approved-artifacts.json").read_bytes()
    assert base64.b64encode(manifest).decode() in data
    assert "/tmp/approved-artifacts.json" in data


def test_deploy_profile_rejects_pull_requests():
    run = {
        "databaseId": 10,
        "event": "pull_request",
        "headBranch": "feature/test",
        "headSha": COMMIT_SHA,
        "workflowName": "Platform Deploy",
    }
    with mock.patch.object(local_runner, "gh_json", return_value=run):
        with pytest.raises(local_runner.ControllerError, match="version-tag"):
            local_runner.target_run("owner/repo", 10, "deploy", COMMIT_SHA)


def test_ci_profile_accepts_internal_pull_request():
    run = {
        "databaseId": 10,
        "event": "pull_request",
        "headBranch": "feature/test",
        "headSha": COMMIT_SHA,
        "workflowName": "Web CI",
    }
    pull = {
        "state": "open",
        "head": {"sha": COMMIT_SHA, "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main"},
    }
    with mock.patch.object(local_runner, "gh_json", side_effect=[run, [pull]]):
        assert local_runner.target_run("owner/repo", 10, "ci", COMMIT_SHA) == run


def test_ci_profile_rejects_fork_pull_request():
    run = {
        "databaseId": 10,
        "event": "pull_request",
        "headBranch": "feature/test",
        "headSha": COMMIT_SHA,
        "workflowName": "Web CI",
    }
    pull = {
        "state": "open",
        "head": {"sha": COMMIT_SHA, "repo": {"full_name": "fork/repo"}},
        "base": {"ref": "main"},
    }
    with mock.patch.object(local_runner, "gh_json", side_effect=[run, [pull]]):
        with pytest.raises(local_runner.ControllerError, match="rejects forks"):
            local_runner.target_run("owner/repo", 10, "ci", COMMIT_SHA)


def test_runner_version_must_be_numeric_semver():
    with pytest.raises(local_runner.ControllerError, match="semantic version"):
        local_runner.validate_runner_version("2.0.0/../../unexpected")


def test_runner_label_is_bound_to_exact_commit_sha():
    assert local_runner.runner_label_for_sha(COMMIT_SHA) == RUNNER_LABEL
    with pytest.raises(local_runner.ControllerError, match="40-character"):
        local_runner.runner_label_for_sha("abc123")


def test_billing_failure_is_detected_from_job_annotation():
    jobs = {"jobs": [{"databaseId": 123}]}
    annotations = [
        {
            "message": (
                "The job was not started because recent account payments have "
                "failed or your spending limit needs to be increased."
            )
        }
    ]
    with mock.patch.object(local_runner, "gh_json", side_effect=[jobs, annotations]):
        assert local_runner.run_has_billing_failure("owner/repo", 10)


def test_non_billing_failure_is_not_recoverable_as_billing():
    jobs = {"jobs": [{"databaseId": 123}]}
    annotations = [{"message": "Unit tests failed"}]
    with mock.patch.object(local_runner, "gh_json", side_effect=[jobs, annotations]):
        assert not local_runner.run_has_billing_failure("owner/repo", 10)


def test_trusted_run_actor_requires_write_access():
    with mock.patch.object(
        local_runner,
        "gh_json",
        side_effect=[{"actor": {"login": "reader"}}, {"permission": "read"}],
    ):
        with pytest.raises(local_runner.ControllerError, match="write access"):
            local_runner.trusted_run_actor("owner/repo", 10)


def test_trusted_run_actor_accepts_maintainer():
    with mock.patch.object(
        local_runner,
        "gh_json",
        side_effect=[{"actor": {"login": "maintainer"}}, {"permission": "maintain"}],
    ):
        assert local_runner.trusted_run_actor("owner/repo", 10) == "maintainer"


def test_drill_audit_requires_every_job_to_use_expected_runner():
    jobs = {
        "jobs": [
            {
                "name": "quality",
                "conclusion": "success",
                "runner_name": "hosted-runner",
                "labels": ["ubuntu-latest"],
            }
        ]
    }
    with mock.patch.object(local_runner, "gh_json", return_value=jobs):
        with pytest.raises(local_runner.ControllerError, match="did not use"):
            local_runner.verify_runs_used_runner(
                "owner/repo",
                [{"databaseId": 10}],
                "cgm-release-local-10-1",
                RUNNER_LABEL,
            )


def test_drill_audit_accepts_sha_bound_runner():
    jobs = {
        "jobs": [
            {
                "name": "quality",
                "conclusion": "success",
                "runner_name": "cgm-release-local-10-1",
                "labels": ["self-hosted", "linux", "x64", RUNNER_LABEL],
            }
        ]
    }
    with mock.patch.object(local_runner, "gh_json", return_value=jobs):
        local_runner.verify_runs_used_runner(
            "owner/repo",
            [{"databaseId": 10}],
            "cgm-release-local-10-1",
            RUNNER_LABEL,
        )


def test_drill_requires_explicit_supervision_acknowledgement():
    with pytest.raises(local_runner.ControllerError, match="confirm-drill"):
        local_runner.validate_drill_confirmation("drill", "")

    local_runner.validate_drill_confirmation("drill", "SCRUM-54-DRILL")
    local_runner.validate_drill_confirmation("billing", "")


def test_approved_runner_manifest_has_valid_pins():
    version, sha256 = local_runner.approved_runner_artifact()

    assert version == "2.337.0"
    assert sha256 == "70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613"


def test_validate_approved_runner_accepts_manifest_values():
    version, sha256 = local_runner.approved_runner_artifact()
    local_runner.validate_approved_runner(version, sha256)


def test_validate_approved_runner_rejects_unknown_version():
    with pytest.raises(local_runner.ControllerError, match="approved-artifacts.json"):
        local_runner.validate_approved_runner("2.000.0", "a" * 64)


def test_wait_for_runner_ready_tolerates_registration_transitions(tmp_path):
    runner_id = 123
    responses = [
        None,
        {"id": runner_id, "status": "offline", "labels": []},
        {
            "id": runner_id,
            "status": "online",
            "labels": [{"name": "self-hosted"}, {"name": "linux"}],
        },
        {
            "id": runner_id,
            "status": "online",
            "labels": [
                {"name": "self-hosted"},
                {"name": "linux"},
                {"name": "x64"},
                {"name": RUNNER_LABEL},
            ],
        },
    ]
    state = {"runner_id": None}
    state_path = tmp_path / "state.json"
    with (
        mock.patch.object(local_runner, "runner_by_name", side_effect=responses),
        mock.patch.object(local_runner, "write_state") as write_state,
        mock.patch.object(local_runner.time, "sleep"),
        mock.patch.object(
            local_runner.time,
            "monotonic",
            side_effect=[0, 0, 1, 2, 3],
        ),
    ):
        local_runner.wait_for_runner_ready(
            "owner/repo",
            "cgm-release-local-1-1",
            RUNNER_LABEL,
            10,
            state,
            state_path,
        )

    assert state["runner_id"] == runner_id
    write_state.assert_called_once_with(state_path, state)


def test_wait_for_runner_ready_reports_last_status_without_real_sleep(tmp_path):
    state = {"runner_id": None}
    with (
        mock.patch.object(
            local_runner,
            "runner_by_name",
            return_value={"id": 123, "status": "offline", "labels": []},
        ),
        mock.patch.object(local_runner, "write_state"),
        mock.patch.object(local_runner.time, "sleep"),
        mock.patch.object(local_runner.time, "monotonic", side_effect=[0, 0, 6]),
    ):
        with pytest.raises(
            local_runner.ControllerError, match="status=offline.*missing_labels"
        ):
            local_runner.wait_for_runner_ready(
                "owner/repo",
                "cgm-release-local-1-1",
                RUNNER_LABEL,
                5,
                state,
                tmp_path / "state.json",
            )


def test_validate_profile_defaults_to_release():
    arguments = local_runner.parser().parse_args(
        [
            "validate",
            "--repo",
            "owner/repo",
            "--image",
            "image.qcow2",
            "--image-sha256",
            "a" * 64,
        ]
    )

    assert arguments.profile == "release"


def test_explicit_selection_preserves_same_queued_run(tmp_path):
    state = {"variable_change_started": False, "variable_changed": False}
    with (
        mock.patch.object(local_runner, "set_variable") as set_variable,
        mock.patch.object(local_runner, "rerun_same_run") as rerun,
        mock.patch.object(local_runner, "write_state") as write_state,
    ):
        local_runner.activate_runs(
            repo="owner/repo",
            runs=[{"databaseId": 10, "status": "queued"}],
            timeout=30,
            selection_mode="explicit",
            runner_label=RUNNER_LABEL,
            state=state,
            path=tmp_path / "state.json",
        )

    set_variable.assert_not_called()
    rerun.assert_not_called()
    write_state.assert_not_called()


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", True),
        ("completed", "failure", False),
        ("in_progress", None, False),
    ],
)
def test_controller_success_requires_successful_target_run(
    status, conclusion, expected
):
    assert (
        local_runner.run_succeeded({"status": status, "conclusion": conclusion})
        is expected
    )


def test_up_never_disables_docker():
    source = SCRIPT.read_text(encoding="utf-8")
    run_up = source[source.index("def run_up") : source.index("def run_validate")]

    assert "start_docker=False" not in run_up


def test_guest_bootstrap_runs_runner_as_ubuntu_and_checks_docker():
    bootstrap = (SCRIPT.parent / "guest-bootstrap.sh").read_text(encoding="utf-8")

    assert "runuser -u ubuntu -- docker info" in bootstrap
    assert "runuser -u ubuntu -- ./config.sh" in bootstrap
    assert "exec runuser -u ubuntu -- ./run.sh" in bootstrap
    assert '--labels "$CGM_RUNNER_LABEL"' in bootstrap
    assert "set -x" not in bootstrap


def test_release_workflows_require_sha_bound_runner_labels():
    root = SCRIPT.parents[3]
    for relative in (
        ".github/workflows/platform-deploy.yml",
        ".github/workflows/platform-rollback.yml",
        ".github/workflows/semantic-release.yml",
        "templates/github-actions/platform-deploy.yml",
        "templates/github-actions/platform-rollback.yml",
    ):
        workflow = (root / relative).read_text(encoding="utf-8")
        assert "format('cgm-release-local-{0}', github.sha)" in workflow
        assert "== 'cgm-release-local'" not in workflow


def test_validate_ctrl_c_cleans_up_and_returns_130(tmp_path):
    args = Namespace(
        repo="owner/repo",
        image=str(tmp_path / "image.qcow2"),
        image_sha256="a" * 64,
        runner_version="2.337.0",
        runner_sha256="70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613",
        runner_timeout=10,
        profile="registration",
        state_file=str(tmp_path / "state.json"),
    )
    process = mock.Mock(pid=123)
    with (
        mock.patch.object(local_runner, "validate_repo"),
        mock.patch.object(local_runner, "validate_approved_runner"),
        mock.patch.object(local_runner, "require_prerequisites"),
        mock.patch.object(
            local_runner, "gh_json", return_value={"token": "short-lived"}
        ),
        mock.patch.object(local_runner, "make_user_data", return_value="cloud-init"),
        mock.patch.object(
            local_runner,
            "create_vm_files",
            return_value=(tmp_path / "vm.qcow2", tmp_path / "seed.iso"),
        ),
        mock.patch.object(local_runner, "launch_vm", return_value=process),
        mock.patch.object(
            local_runner, "wait_for_runner_ready", side_effect=KeyboardInterrupt
        ),
        mock.patch.object(local_runner, "write_state"),
        mock.patch.object(local_runner, "cleanup", return_value=[]) as cleanup,
    ):
        result = local_runner.run_validate(args)

    assert result == 130
    cleanup.assert_called_once()
    persisted_state = cleanup.call_args.args[1]
    assert "short-lived" not in repr(persisted_state)


def test_down_recovers_persisted_state(tmp_path):
    path = tmp_path / "state.json"
    state = {"repo": "owner/repo", "runner_name": "temporary"}
    with (
        mock.patch.object(local_runner, "read_state", return_value=state),
        mock.patch.object(local_runner, "cleanup", return_value=[]) as cleanup,
    ):
        result = local_runner.run_down(Namespace(state_file=str(path)))

    assert result == 0
    cleanup.assert_called_once_with(path.resolve(), state)


def test_powershell_wrapper_never_registers_runner_on_host():
    wrapper = (SCRIPT.parent / "start.ps1").read_text(encoding="utf-8")

    assert "runner.py" in wrapper
    assert "config.cmd" not in wrapper
    assert "config.sh" not in wrapper


def test_restore_does_not_overwrite_another_operator_value():
    with (
        mock.patch.object(local_runner, "variable_value", return_value="ubuntu-latest"),
        mock.patch.object(local_runner, "set_variable") as set_variable,
    ):
        with pytest.raises(local_runner.ControllerError, match="another operator"):
            local_runner.restore_variable("owner/repo", None, RUNNER_LABEL)

    set_variable.assert_not_called()


@pytest.mark.parametrize("previous", [None, "ubuntu-latest"])
def test_restore_is_idempotent_before_remote_change(previous):
    with (
        mock.patch.object(local_runner, "variable_value", return_value=previous),
        mock.patch.object(local_runner, "set_variable") as set_variable,
    ):
        local_runner.restore_variable("owner/repo", previous, RUNNER_LABEL)

    set_variable.assert_not_called()


def test_delete_runner_surfaces_provider_failure():
    failed = mock.Mock(returncode=1, stdout="", stderr="permission denied")
    with mock.patch.object(local_runner, "gh", return_value=failed):
        with pytest.raises(local_runner.ControllerError, match="Could not remove"):
            local_runner.delete_runner("owner/repo", 123, "cgm-release-local-123")


def test_rerun_same_run_cancels_queued_run_without_creating_a_new_id():
    cancelled = {
        "databaseId": 10,
        "event": "workflow_dispatch",
        "headBranch": "main",
        "workflowName": "Platform Deploy",
        "status": "completed",
        "conclusion": "cancelled",
    }
    with (
        mock.patch.object(local_runner, "gh") as gh,
        mock.patch.object(local_runner, "wait_for_run", return_value=cancelled),
    ):
        local_runner.rerun_same_run(
            "owner/repo",
            10,
            {**cancelled, "status": "queued", "conclusion": None},
            30,
        )

    assert gh.call_args_list == [
        mock.call(["run", "cancel", "10", "--repo", "owner/repo"]),
        mock.call(["run", "rerun", "10", "--repo", "owner/repo"]),
    ]


def test_rerun_same_run_refuses_active_run():
    with pytest.raises(local_runner.ControllerError, match="already in progress"):
        local_runner.rerun_same_run(
            "owner/repo",
            10,
            {
                "databaseId": 10,
                "event": "workflow_dispatch",
                "headBranch": "main",
                "workflowName": "Platform Deploy",
                "status": "in_progress",
            },
            30,
        )


def test_create_seed_iso_prefers_cloud_localds(tmp_path):
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")
    meta_data = tmp_path / "meta-data"
    meta_data.write_text("instance-id: cgm-release-local\n")
    seed_iso = tmp_path / "seed.iso"

    with (
        mock.patch.object(
            local_runner,
            "command_exists",
            side_effect=lambda name: name == "cloud-localds",
        ),
        mock.patch.object(
            local_runner, "run_command", return_value=mock.Mock(returncode=0)
        ) as run,
    ):
        local_runner.create_seed_iso(user_data, meta_data, seed_iso)

    run.assert_called_once()
    assert run.call_args.args[0][:2] == ["cloud-localds", str(seed_iso)]


def test_create_seed_iso_uses_hdiutil_on_macos(tmp_path):
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")
    meta_data = tmp_path / "meta-data"
    meta_data.write_text("instance-id: cgm-release-local\n")
    seed_iso = tmp_path / "seed.iso"

    with (
        mock.patch.object(
            local_runner,
            "command_exists",
            side_effect=lambda name: name == "hdiutil",
        ),
        mock.patch.object(
            local_runner, "run_command", return_value=mock.Mock(returncode=0)
        ) as run,
    ):
        local_runner.create_seed_iso(user_data, meta_data, seed_iso)

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[:3] == ["hdiutil", "makehybrid", "-iso"]
    assert "-default-volume-name" in args
    seed_dir = seed_iso.parent / "seed"
    assert (seed_dir / "user-data").read_text() == "#cloud-config\n"
    assert (seed_dir / "meta-data").read_text() == "instance-id: cgm-release-local\n"


def test_create_seed_iso_requires_an_iso_tool(tmp_path):
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")
    meta_data = tmp_path / "meta-data"
    meta_data.write_text("instance-id: cgm-release-local\n")
    seed_iso = tmp_path / "seed.iso"

    with mock.patch.object(local_runner, "command_exists", return_value=False):
        with pytest.raises(local_runner.ControllerError, match="ISO tool"):
            local_runner.create_seed_iso(user_data, meta_data, seed_iso)


def test_require_prerequisites_rejects_missing_iso_tool(tmp_path):
    image = tmp_path / "image.qcow2"

    def command_exists(name: str) -> bool:
        return name in {"gh", "qemu-system-x86_64", "qemu-img"}

    with mock.patch.object(local_runner, "command_exists", side_effect=command_exists):
        with pytest.raises(
            local_runner.ControllerError, match="cloud-localds or hdiutil"
        ):
            local_runner.require_prerequisites(image, "a" * 64)


def test_create_vm_files_writes_metadata_and_uses_hdiutil(tmp_path):
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"base")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with (
        mock.patch.object(
            local_runner,
            "command_exists",
            side_effect=lambda name: name == "hdiutil",
        ),
        mock.patch.object(
            local_runner, "run_command", return_value=mock.Mock(returncode=0)
        ) as run,
    ):
        local_runner.create_vm_files(image, "#cloud-config\n", work_dir)

    assert (work_dir / "meta-data").read_text() == "instance-id: cgm-release-local\n"
    assert (work_dir / "user-data").read_text() == "#cloud-config\n"
    assert any(call.args[0][0] == "hdiutil" for call in run.call_args_list)
