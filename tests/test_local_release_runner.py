import base64
import importlib.util
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/ops/local-release-runner/runner.py"
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
        runner_version="2.000.0",
        runner_sha256="a" * 64,
    )

    assert "registration-token-secret" not in data
    bootstrap = (SCRIPT.parent / "guest-bootstrap.sh").read_bytes()
    assert base64.b64encode(bootstrap).decode() in data
    assert "CGM_RUNNER_TOKEN" in data


def test_target_run_rejects_pull_requests():
    run = {
        "databaseId": 10,
        "event": "pull_request",
        "headBranch": "feature/test",
    }
    with mock.patch.object(local_runner, "gh_json", return_value=run):
        with pytest.raises(local_runner.ControllerError, match="Pull request"):
            local_runner.target_run("owner/repo", 10)


def test_target_run_rejects_untrusted_ref():
    run = {
        "databaseId": 10,
        "event": "workflow_dispatch",
        "headBranch": "feature/test",
        "workflowName": "Platform Deploy",
    }
    with mock.patch.object(local_runner, "gh_json", return_value=run):
        with pytest.raises(local_runner.ControllerError, match="main or a version tag"):
            local_runner.target_run("owner/repo", 10)


def test_target_run_rejects_non_release_workflow():
    run = {
        "databaseId": 10,
        "event": "push",
        "headBranch": "main",
        "workflowName": "CI",
    }
    with mock.patch.object(local_runner, "gh_json", return_value=run):
        with pytest.raises(
            local_runner.ControllerError, match="release/deploy/rollback"
        ):
            local_runner.target_run("owner/repo", 10)


def test_runner_version_must_be_numeric_semver():
    with pytest.raises(local_runner.ControllerError, match="semantic version"):
        local_runner.validate_runner_version("2.0.0/../../unexpected")


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


def test_verify_runner_labels_accepts_expected_labels():
    with mock.patch.object(
        local_runner,
        "runner_by_name",
        return_value={
            "status": "online",
            "labels": [
                {"name": "self-hosted"},
                {"name": "linux"},
                {"name": "x64"},
                {"name": "cgm-release-local"},
            ],
        },
    ):
        local_runner.verify_runner_labels("owner/repo", "cgm-release-local-1-1")


def test_verify_runner_labels_rejects_missing_label():
    with mock.patch.object(
        local_runner,
        "runner_by_name",
        return_value={
            "status": "online",
            "labels": [
                {"name": "self-hosted"},
                {"name": "linux"},
                {"name": "x64"},
            ],
        },
    ):
        with pytest.raises(local_runner.ControllerError, match="missing labels"):
            local_runner.verify_runner_labels("owner/repo", "cgm-release-local-1-1")


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
            local_runner.restore_variable("owner/repo", None)

    set_variable.assert_not_called()


@pytest.mark.parametrize("previous", [None, "ubuntu-latest"])
def test_restore_is_idempotent_before_remote_change(previous):
    with (
        mock.patch.object(local_runner, "variable_value", return_value=previous),
        mock.patch.object(local_runner, "set_variable") as set_variable,
    ):
        local_runner.restore_variable("owner/repo", previous)

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
