"""Candidate validation rejects missing configuration before promotion."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from eng_platform_api import verify_candidate_config as check

REVISION = "eng-platform-api-00057-test"


def response(env, revision=REVISION):
    return {
        "metadata": {"name": revision},
        "spec": {"containers": [{"env": env}]},
    }


def writer(value=check.EXPECTED_WRITER):
    return {"name": check.WRITER_ENV, "value": value}


def pins():
    return [
        {"name": name, "value": value}
        for name, value in check._tooling_images().items()
    ]


def test_queries_exact_revision_and_accepts_expected_writer():
    data = response([writer(), *pins(), {"name": "UNRELATED", "value": "PRIVATE"}])
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(data)
        assert check.verify("project", "region", REVISION)
    assert run.call_args.args[0] == [
        "gcloud",
        "run",
        "revisions",
        "describe",
        REVISION,
        "--project=project",
        "--region=region",
        "--format=json",
    ]
    assert run.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "check": True,
        "timeout": 60,
    }


@pytest.mark.parametrize(
    "data",
    [
        response(pins()),
        response([writer(""), *pins()]),
        response([writer("other@example.com"), *pins()]),
        response(
            [{"name": check.WRITER_ENV, "valueFrom": {"secretKeyRef": {}}}, *pins()]
        ),
        response([writer(), writer(), *pins()]),
        response([writer(), *pins()], revision="another-revision"),
        {},
        None,
        [],
        {"metadata": {"name": REVISION}, "spec": {"containers": []}},
    ],
)
def test_rejects_invalid_configuration(data):
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(data)
        assert not check.verify("project", "region", REVISION)


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "gcloud", output="PRIVATE", stderr="PRIVATE"),
        subprocess.TimeoutExpired("gcloud", 60, output="PRIVATE"),
        FileNotFoundError("PRIVATE"),
    ],
)
def test_query_errors_are_redacted(failure, capsys):
    with mock.patch.object(check.subprocess, "run", side_effect=failure):
        assert not check.verify("project", "region", REVISION)
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_malformed_provider_response_is_rejected():
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = "PRIVATE-not-json"
        assert not check.verify("project", "region", REVISION)


@pytest.mark.parametrize(
    "args",
    [("", "region", REVISION), ("project", "", REVISION), ("project", "region", " ")],
)
def test_missing_inputs_never_query_cloud(args):
    with mock.patch.object(check.subprocess, "run") as run:
        assert not check.verify(*args)
        run.assert_not_called()


@pytest.mark.parametrize(
    "passed,exit_code,message", [(True, 0, "PASS"), (False, 1, "FAIL")]
)
def test_cli_reports_only_fixed_result(passed, exit_code, message, capsys):
    with (
        mock.patch(
            "sys.argv",
            [
                "check",
                "--project",
                "project",
                "--region",
                "region",
                "--revision",
                REVISION,
            ],
        ),
        mock.patch.object(check, "verify", return_value=passed) as verify,
    ):
        assert check.main() == exit_code
        verify.assert_called_once_with("project", "region", REVISION)
    assert capsys.readouterr().out == (
        f"Candidate writer and quality tooling check: {message}\n"
    )


@pytest.mark.parametrize("name", list(check.IMAGE_NAMES))
@pytest.mark.parametrize("kind", ["missing", "old", "duplicate", "secret", "extra"])
def test_rejects_missing_stale_duplicate_or_nonliteral_pin(name, kind, capsys):
    env = [writer(), *pins()]
    row = next(item for item in env if item["name"] == name)
    if kind == "missing":
        env.remove(row)
    elif kind == "old":
        row["value"] = row["value"].split("@sha256:")[0] + "@sha256:" + "a" * 64
    elif kind == "duplicate":
        env.append(dict(row))
    elif kind == "secret":
        row.pop("value")
        row["valueFrom"] = {"secretKeyRef": {"name": "PRIVATE", "key": "latest"}}
    else:
        row["valueFrom"] = {"secretKeyRef": {"name": "PRIVATE", "key": "latest"}}
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(response(env))
        assert not check.verify("project", "region", REVISION)
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_correct_pins_do_not_relax_writer_check():
    data = response([writer("other@example.com"), *pins()])
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(data)
        assert not check.verify("project", "region", REVISION)


@pytest.mark.parametrize("env", [None, {}, "PRIVATE", ["PRIVATE"]])
def test_malformed_environment_is_redacted(env, capsys):
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(response(env))
        assert not check.verify("project", "region", REVISION)
    captured = capsys.readouterr()
    assert not captured.out + captured.err


@pytest.fixture
def bundle_files(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle.json"
    manifest = tmp_path / "manifest.json"
    bundle.write_bytes(check.BUNDLE_PATH.read_bytes())
    manifest.write_bytes(check.MANIFEST_PATH.read_bytes())
    monkeypatch.setattr(check, "BUNDLE_PATH", bundle)
    monkeypatch.setattr(check, "MANIFEST_PATH", manifest)
    return bundle, manifest


@pytest.mark.parametrize(
    "kind",
    [
        "missing_bundle",
        "missing_manifest",
        "json",
        "null",
        "unknown_key",
        "version",
        "bool_version",
        "source_sha",
        "source_type",
        "manifest_hash",
        "manifest_type",
        "manifest_changed",
        "images_type",
        "missing_image",
        "extra_image",
        "image_type",
        "tag",
        "wrong_family",
        "wrong_registry",
    ],
)
def test_invalid_bundle_fails_before_any_cloud_query(bundle_files, kind, capsys):
    path, manifest = bundle_files
    data = json.loads(path.read_text())
    node = "ENG_PLATFORM_QUALITY_NODE_IMAGE"
    if kind == "missing_bundle":
        path.unlink()
    elif kind == "missing_manifest":
        manifest.unlink()
    elif kind == "json":
        path.write_text("PRIVATE-not-json")
    elif kind == "null":
        path.write_text("null")
    elif kind == "manifest_changed":
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    else:
        if kind == "unknown_key":
            data["advisory"] = True
        elif kind == "version":
            data["schema_version"] = 2
        elif kind == "bool_version":
            data["schema_version"] = True
        elif kind == "source_sha":
            data["tooling_source_sha"] = "not-a-sha"
        elif kind == "source_type":
            data["tooling_source_sha"] = 1
        elif kind == "manifest_hash":
            data["manifest_sha256"] = "not-a-hash"
        elif kind == "manifest_type":
            data["manifest_sha256"] = 1
        elif kind == "images_type":
            data["images"] = []
        elif kind == "missing_image":
            data["images"].pop(node)
        elif kind == "extra_image":
            data["images"]["ARBITRARY"] = "PRIVATE"
        elif kind == "image_type":
            data["images"][node] = 1
        elif kind == "tag":
            data["images"][node] = f"{check.TOOLING_REGISTRY}/quality-node:latest"
        elif kind == "wrong_family":
            data["images"][node] = data["images"]["ENG_PLATFORM_QUALITY_PYTHON_IMAGE"]
        else:
            data["images"][node] = data["images"][node].replace(
                "cgm-sanplat-repo", "other"
            )
        path.write_text(json.dumps(data))
    with mock.patch.object(check.subprocess, "run") as run:
        assert not check.verify("project", "region", REVISION)
        run.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_bundle_identifies_tooling_source_not_next_release_sha(
    bundle_files, monkeypatch
):
    path, manifest = bundle_files
    data = json.loads(path.read_text())
    monkeypatch.setenv("CGM_RELEASE_SHA", "b" * 40)
    assert data["tooling_source_sha"] != "b" * 40
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == data["manifest_sha256"]
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(response([writer(), *pins()]))
        assert check.verify("project", "region", REVISION)


def test_bundle_is_packaged_without_changing_dockerfile():
    root = Path(__file__).resolve().parents[1]
    package_data = (root / "pyproject.toml").read_text()
    assert 'eng_platform_api = ["*.json", "static_examples/*.json"]' in package_data
    assert "COPY src/ src/" in (root / "Dockerfile").read_text()


@pytest.mark.parametrize("scope", ["bundle", "images", "manifest", "provider"])
def test_ambiguous_json_keys_are_rejected(bundle_files, scope):
    path, manifest = bundle_files
    bundle = json.loads(path.read_text())
    if scope == "bundle":
        path.write_text(
            path.read_text().replace(
                '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
            )
        )
    elif scope == "images":
        key = "ENG_PLATFORM_QUALITY_NODE_IMAGE"
        encoded = json.dumps(key) + ": " + json.dumps(bundle["images"][key])
        path.write_text(path.read_text().replace(encoded, encoded + ", " + encoded))
    elif scope == "manifest":
        manifest.write_text(
            manifest.read_text().replace(
                '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
            )
        )
        bundle["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        path.write_text(json.dumps(bundle))
    with mock.patch.object(check.subprocess, "run") as run:
        data = json.dumps(response([writer(), *pins()])) if scope == "provider" else ""
        if scope == "provider":
            data = data.replace(
                '"name": "' + REVISION + '"',
                '"name": "wrong", "name": "' + REVISION + '"',
            )
        run.return_value.stdout = data
        assert not check.verify("project", "region", REVISION)
        if scope != "provider":
            run.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    [
        "not_json",
        "null",
        "extra",
        "version",
        "bool_version",
        "empty",
        "profiles_type",
        "empty_name",
        "profile_type",
        "runtime",
        "threshold",
        "threshold_overflow",
        "commands",
        "duplicate_extra",
    ],
)
def test_invalid_manifest_is_rejected_even_with_matching_hash(
    bundle_files, kind, capsys
):
    path, manifest = bundle_files
    data = json.loads(manifest.read_text())
    profile = data["profiles"]["eng-platform-api"]
    if kind == "not_json":
        manifest.write_text("PRIVATE-not-json")
    elif kind == "null":
        manifest.write_text("null")
    else:
        if kind == "extra":
            data["advisory"] = True
        elif kind == "version":
            data["schema_version"] = 2
        elif kind == "bool_version":
            data["schema_version"] = True
        elif kind == "empty":
            data["profiles"] = {}
        elif kind == "profiles_type":
            data["profiles"] = []
        elif kind == "empty_name":
            data["profiles"] = {"": profile}
        elif kind == "profile_type":
            data["profiles"]["eng-platform-api"] = None
        elif kind == "runtime":
            profile["runtime"] = "invalid"
        elif kind == "threshold":
            profile["coverage_threshold"] = 101
        elif kind == "threshold_overflow":
            profile["coverage_threshold"] = 10**400
        elif kind == "commands":
            profile["commands"].pop("tests")
        else:
            profile["extra"].append(dict(profile["extra"][0]))
        manifest.write_text(json.dumps(data))
    bundle = json.loads(path.read_text())
    bundle["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    path.write_text(json.dumps(bundle))
    with mock.patch.object(check.subprocess, "run") as run:
        assert not check.verify("project", "region", REVISION)
        run.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_missing_source_validator_is_redacted(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(check, "PROFILE_VALIDATOR_PATH", tmp_path / "absent.py")
    with mock.patch.object(check.subprocess, "run") as run:
        assert not check.verify("project", "region", REVISION)
        run.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_unloadable_source_validator_fails_before_cloud_query(capsys):
    with (
        mock.patch.object(
            check.importlib.util, "spec_from_file_location", return_value=None
        ),
        mock.patch.object(check.subprocess, "run") as run,
    ):
        assert not check.verify("project", "region", REVISION)
        run.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out + captured.err
