"""Observation transport stays outside canonical manifest and lifecycle outcomes."""

import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from eng_platform_api import quality_evidence as evidence
from tests.test_quality_evidence import observation

ROOT = Path(__file__).parents[1]


def bound_artifact(report_hash="a" * 64):
    return {
        "schema_version": 1,
        "execution_id": "fake",
        "fingerprint": "a" * 64,
        "provider_run_id": "123",
        "report_sha256": report_hash,
        "observation": observation(),
    }


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "docker/quality-executor"))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/quality"))
    monkeypatch.syspath_prepend(str(ROOT / "src/eng_platform_api"))
    return importlib.import_module("quality_executor")


def test_publisher_sends_after_callback_and_failure_never_changes_status(
    executor, monkeypatch, tmp_path
):
    events = []
    manifest = {"report": {"unchanged": True}, "report_hash": "a" * 64}
    monkeypatch.setattr(
        executor, "_read_manifest", lambda *_: (manifest, "quality_passed")
    )
    monkeypatch.setattr(
        executor, "_event", lambda *args, **kwargs: events.append((args, kwargs))
    )

    def unavailable(*args, **kwargs):
        assert len(events) == 1
        raise OSError("private-provider-error")

    # Internal best-effort catches transport errors, not the outer publisher.
    monkeypatch.setattr(executor, "_json_request", unavailable)
    monkeypatch.setattr(
        executor,
        "_identity",
        lambda: {
            "execution_id": "fake",
            "fingerprint": "a" * 64,
            "provider_run_id": "123",
        },
    )
    monkeypatch.setattr(executor, "_required", lambda _: "https://api.example.invalid")
    monkeypatch.setattr(
        executor, "_identity_token", lambda *args, **kwargs: "fake-oidc"
    )
    monkeypatch.setattr(executor, "_read_event_token", lambda _: "fake-event-token")
    path = tmp_path / "quality-result.json"
    (tmp_path / "quality-test-observation.json").write_bytes(
        evidence.canonical(bound_artifact())
    )
    assert executor._publish("example-api", path, tmp_path) == 0
    assert events == [
        (
            (2, "quality_passed", tmp_path),
            {"report": {"unchanged": True}, "report_hash": "a" * 64},
        )
    ]
    monkeypatch.setattr(
        executor, "_read_manifest", lambda *_: (manifest, "quality_failed")
    )
    assert executor._publish("example-api", path, tmp_path) == 1


def test_observation_upload_is_one_bounded_call(executor, monkeypatch, tmp_path):
    (tmp_path / "quality-test-observation.json").write_bytes(
        evidence.canonical(bound_artifact("b" * 64))
    )
    monkeypatch.setattr(
        executor,
        "_identity",
        lambda: {
            "execution_id": "fake",
            "fingerprint": "a" * 64,
            "provider_run_id": "123",
        },
    )
    monkeypatch.setattr(executor, "_required", lambda _: "https://api.example.invalid")
    monkeypatch.setattr(
        executor, "_identity_token", lambda *args, **kwargs: "fake-oidc"
    )
    monkeypatch.setattr(executor, "_read_event_token", lambda _: "fake-event-token")
    request = Mock(return_value={"accepted": True})
    monkeypatch.setattr(executor, "_json_request", request)
    executor._publish_test_observation(
        {"report_hash": "b" * 64}, tmp_path / "quality-result.json", tmp_path
    )
    request.assert_called_once()
    assert (
        request.call_args.args[0]
        == "https://api.example.invalid/api/internal/release-executions/fake/test-observations"
    )
    assert request.call_args.kwargs["timeout"] == 5
    assert set(request.call_args.kwargs["data"]) == {
        "fingerprint",
        "provider_run_id",
        "report_hash",
        "observation",
    }


def test_legacy_missing_sidecar_does_not_make_network_call(
    executor, monkeypatch, tmp_path, capsys
):
    request = Mock()
    monkeypatch.setattr(executor, "_json_request", request)
    executor._publish_test_observation(
        {"report_hash": "b" * 64}, tmp_path / "quality-result.json", tmp_path
    )
    request.assert_not_called()
    assert "publish_unavailable reuse_allowed=false" in capsys.readouterr().out


def test_export_failure_does_not_touch_result_manifest(executor, monkeypatch, tmp_path):
    path = tmp_path / "quality-result.json"
    path.write_text('{"unchanged": true}')
    (tmp_path / "quality-test-observation.json").write_bytes(
        evidence.canonical(bound_artifact())
    )
    monkeypatch.setattr(
        executor, "_observation_materials", Mock(side_effect=ValueError("private path"))
    )
    executor._export_test_observation(
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        {"head_sha": "a" * 40},
        {"runtime": "python"},
        "a" * 64,
    )
    assert not (tmp_path / "quality-test-observation.json").exists()
    assert path.read_text() == '{"unchanged": true}'


def test_material_manifest_contains_only_hashes_and_tracks_no_symlinks(
    executor, monkeypatch, tmp_path
):
    listing = (
        "100644 blob "
        + "a" * 40
        + "\tpyproject.toml\0"
        + "100644 blob "
        + "b" * 40
        + "\ttests/conftest.py\0"
        + "120000 blob "
        + "c" * 40
        + "\tsrc/link.py\0"
    )
    monkeypatch.setattr(executor, "_git", Mock(side_effect=["d" * 40, listing]))
    result = executor._observation_materials(
        tmp_path, tmp_path / "trusted.git", "e" * 40
    )
    assert result["tracked_paths"] == {"pyproject.toml", "tests/conftest.py"}
    assert all(
        call.args[1:3]
        == (f"--git-dir={tmp_path / 'trusted.git'}", f"--work-tree={tmp_path}")
        for call in executor._git.call_args_list
    )
    assert all(
        call.kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        for call in executor._git.call_args_list
    )
    assert result["source_tree"] == "d" * 40
    assert len(result["config_sha256"]) == len(result["fixture_sha256"]) == 64


def test_compact_sidecar_respects_actual_transfer_byte_limit(executor, tmp_path):
    value = observation()
    path = tmp_path / "quality-test-observation.json"
    executor._write_json(path, value, compact=True)
    assert json.loads(path.read_bytes()) == value
    assert path.stat().st_size == len(evidence.canonical(value)) + 1
    assert path.stat().st_mode & 0o222 == 0


def test_profile_commands_and_quality_policy_are_not_modified():
    source = (ROOT / "docker/quality-executor/quality_executor.py").read_text()
    assert 'commands["tests"],\n            test_environment_path,' in source
    assert '"PYTEST_PLUGINS": "pytest_evidence"' in source
    # Observation code neither invokes a pytest substitute nor controls tests.
    assert 'if observation["' not in source
    assert "compare_shadow(" not in source


def test_materials_use_verified_git_directory_even_after_gitfile_tampering(
    executor, tmp_path
):
    import subprocess

    trusted = tmp_path / "trusted.git"
    checkout = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--quiet", f"--separate-git-dir={trusted}", str(checkout)],
        check=True,
    )
    (checkout / "src").mkdir()
    (checkout / "src/example.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fake fixture",
        ],
        cwd=checkout,
        check=True,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
    ).strip()
    expected = executor._observation_materials(checkout, trusted, head)
    (checkout / ".git").write_text("gitdir: /nonexistent-fake-observation-target\n")
    actual = executor._observation_materials(checkout, trusted, head)
    assert actual == expected
    assert actual["tracked_paths"] == {"src/example.py"}


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("execution_id", "different-execution"),
        ("fingerprint", "c" * 64),
        ("provider_run_id", "different-run"),
        ("report_sha256", "c" * 64),
    ],
)
def test_stale_sidecar_is_rejected_before_auth_or_network(
    executor, monkeypatch, tmp_path, field, new_value
):
    old = bound_artifact()
    old[field] = new_value
    (tmp_path / "quality-test-observation.json").write_bytes(evidence.canonical(old))
    monkeypatch.setattr(
        executor,
        "_identity",
        lambda: {
            "execution_id": "fake",
            "fingerprint": "a" * 64,
            "provider_run_id": "123",
        },
    )
    token = Mock(side_effect=AssertionError("Stale observation requested credentials"))
    request = Mock(side_effect=AssertionError("Stale observation made network request"))
    monkeypatch.setattr(executor, "_identity_token", token)
    monkeypatch.setattr(executor, "_json_request", request)
    executor._publish_test_observation(
        {"report_hash": "a" * 64}, tmp_path / "quality-result.json", tmp_path
    )
    token.assert_not_called()
    request.assert_not_called()


@pytest.mark.parametrize("kind", ["legacy", "extra", "schema"])
def test_unbound_or_malformed_wrapper_never_requests_credentials(
    executor, monkeypatch, tmp_path, kind
):
    value = observation() if kind == "legacy" else bound_artifact()
    if kind == "extra":
        value["unapproved"] = "never-export"
    elif kind == "schema":
        value["schema_version"] = True
    (tmp_path / "quality-test-observation.json").write_bytes(evidence.canonical(value))
    monkeypatch.setattr(
        executor,
        "_identity",
        lambda: {
            "execution_id": "fake",
            "fingerprint": "a" * 64,
            "provider_run_id": "123",
        },
    )
    token = Mock(
        side_effect=AssertionError("Invalid observation requested credentials")
    )
    monkeypatch.setattr(executor, "_identity_token", token)
    executor._publish_test_observation(
        {"report_hash": "a" * 64}, tmp_path / "quality-result.json", tmp_path
    )
    token.assert_not_called()


def test_cleanup_failure_cannot_publish_previous_report_sidecar(
    executor, monkeypatch, tmp_path
):
    sidecar = tmp_path / "quality-test-observation.json"
    sidecar.write_bytes(evidence.canonical(bound_artifact()))
    original_unlink = Path.unlink

    def failed_cleanup(path, *args, **kwargs):
        if path == sidecar:
            raise PermissionError("fake denied cleanup")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_cleanup)
    identity = {
        "execution_id": "fake",
        "fingerprint": "a" * 64,
        "provider_run_id": "123",
        "head_sha": "c" * 40,
    }
    executor._export_test_observation(
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        identity,
        {"runtime": "python"},
        "b" * 64,
    )
    assert sidecar.exists()
    monkeypatch.setattr(executor, "_identity", lambda: identity)
    token = Mock(side_effect=AssertionError("Stale sidecar requested credentials"))
    request = Mock(side_effect=AssertionError("Stale sidecar made a network request"))
    monkeypatch.setattr(executor, "_identity_token", token)
    monkeypatch.setattr(executor, "_json_request", request)
    executor._publish_test_observation(
        {"report_hash": "b" * 64}, tmp_path / "quality-result.json", tmp_path
    )
    token.assert_not_called()
    request.assert_not_called()
