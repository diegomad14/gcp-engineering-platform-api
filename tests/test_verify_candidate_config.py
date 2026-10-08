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


def test_reviewed_bundle_is_closed_to_two_quality_images_and_release_planner():
    expected = {
        "ENG_PLATFORM_QUALITY_NODE_IMAGE": "quality-node",
        "ENG_PLATFORM_QUALITY_PYTHON_IMAGE": "quality-python",
        "ENG_PLATFORM_RELEASE_PLANNER_IMAGE": "release-planner",
    }
    assert check.IMAGE_NAMES == expected
    assert set(check._tooling_images()) == set(expected)


def routing():
    return {
        "name": check.CLOUD_BUILD_ONLY_ENV,
        "value": ",".join(check.CLOUD_BUILD_ONLY_SERVICES),
    }


def test_queries_exact_revision_and_accepts_expected_writer():
    data = response(
        [writer(), *pins(), routing(), {"name": "UNRELATED", "value": "PRIVATE"}]
    )
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


def test_routing_contract_is_exactly_thirteen_artemis_plus_five_private_services():
    baseline = {
        "cgm-artemis-api",
        "cgm-artemis-job-dispatcher",
        "cgm-artemis-job-worker",
        "cgm-artemis-sync-worker",
        "cgm-artemis-clock-sync-worker",
        "cgm-artemis-data-recovery-worker",
        "cgm-artemis-fnd-ip-sync-worker",
        "cgm-artemis-fnd-observation-worker",
        "cgm-artemis-readings-export-worker",
        "cgm-artemis-smarti-prevention-worker",
        "cgm-artemis-wm-sweep-worker",
        "cgm-artemis-web",
        "cgm-artemis-mcp-worker",
    }
    additions = {
        "cgm-bot-api",
        "communications-ms",
        "eng-platform-web",
        "cgm-sanplat-api",
        "cgm-sanplat-web",
    }
    assert len(check.BASELINE_CLOUD_BUILD_ONLY_SERVICES) == 13
    assert set(check.BASELINE_CLOUD_BUILD_ONLY_SERVICES) == baseline
    assert len(check.CLOUD_BUILD_ONLY_SERVICES) == 18
    assert set(check.CLOUD_BUILD_ONLY_SERVICES) == baseline | additions
    assert "eng-platform-api" not in check.CLOUD_BUILD_ONLY_SERVICES


def test_reordered_exact_routing_is_valid_and_not_overridden_by_environment(
    monkeypatch,
):
    monkeypatch.setenv(check.CLOUD_BUILD_ONLY_ENV, "arbitrary-service")
    row = routing()
    row["value"] = ",".join(reversed(check.CLOUD_BUILD_ONLY_SERVICES))
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(response([writer(), *pins(), row]))
        assert check.verify("project", "region", REVISION)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate_field",
        "baseline",
        "missing_service",
        "extra_service",
        "duplicate_service",
        "replaced_service",
        "empty",
        "null",
        "nonstring",
        "reference",
        "value_and_reference",
        "extra_field",
        "whitespace",
        "trailing_comma",
        "delimiter",
    ],
)
def test_candidate_requires_exact_literal_eighteen_service_routing(kind, capsys):
    row = routing()
    env = [writer(), *pins(), row]
    if kind == "missing":
        env.remove(row)
    elif kind == "duplicate_field":
        env.append(dict(row))
    elif kind == "baseline":
        row["value"] = ",".join(check.BASELINE_CLOUD_BUILD_ONLY_SERVICES)
    elif kind == "missing_service":
        row["value"] = row["value"].split(",", 1)[1]
    elif kind == "extra_service":
        row["value"] += ",unknown-service"
    elif kind == "duplicate_service":
        row["value"] += ",cgm-artemis-api"
    elif kind == "replaced_service":
        row["value"] = row["value"].replace("cgm-artemis-api", "unknown-service")
    elif kind == "empty":
        row["value"] = ""
    elif kind == "null":
        row["value"] = None
    elif kind == "nonstring":
        row["value"] = list(check.CLOUD_BUILD_ONLY_SERVICES)
    elif kind in {"reference", "value_and_reference"}:
        if kind == "reference":
            row.pop("value")
        row["valueFrom"] = {"secretKeyRef": {"name": "PRIVATE", "key": "latest"}}
    elif kind == "extra_field":
        row["extra"] = "PRIVATE"
    elif kind == "whitespace":
        row["value"] += " "
    elif kind == "trailing_comma":
        row["value"] += ","
    else:
        row["value"] += "|ARBITRARY=PRIVATE"
    with mock.patch.object(check.subprocess, "run") as run:
        run.return_value.stdout = json.dumps(response(env))
        assert not check.verify("project", "region", REVISION)
    assert not capsys.readouterr().out + capsys.readouterr().err


@pytest.mark.parametrize(
    "data",
    [
        response(pins()),
        response([writer(""), *pins(), routing()]),
        response([writer("other@example.com"), *pins(), routing()]),
        response(
            [
                {"name": check.WRITER_ENV, "valueFrom": {"secretKeyRef": {}}},
                *pins(),
                routing(),
            ]
        ),
        response([writer(), writer(), *pins(), routing()]),
        response([writer(), *pins(), routing()], revision="another-revision"),
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
    env = [writer(), *pins(), routing()]
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
    data = response([writer("other@example.com"), *pins(), routing()])
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
        "image_secret",
        "tag",
        "wrong_family",
        "wrong_registry",
    ],
)
@pytest.mark.parametrize("name", list(check.IMAGE_NAMES))
def test_invalid_bundle_fails_before_any_cloud_query(bundle_files, kind, name, capsys):
    path, manifest = bundle_files
    data = json.loads(path.read_text())
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
            data["images"].pop(name)
        elif kind == "extra_image":
            data["images"]["ARBITRARY"] = "PRIVATE"
        elif kind == "image_type":
            data["images"][name] = 1
        elif kind == "image_secret":
            data["images"][name] = {"valueFrom": {"secretKeyRef": {"name": "PRIVATE"}}}
        elif kind == "tag":
            data["images"][name] = (
                f"{check.TOOLING_REGISTRY}/{check.IMAGE_NAMES[name]}:latest"
            )
        elif kind == "wrong_family":
            data["images"][name] = data["images"][name].replace(
                check.IMAGE_NAMES[name] + "@", "arbitrary-image@"
            )
        else:
            data["images"][name] = data["images"][name].replace(
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
        run.return_value.stdout = json.dumps(response([writer(), *pins(), routing()]))
        assert check.verify("project", "region", REVISION)


def test_bundle_is_packaged_without_changing_dockerfile():
    root = Path(__file__).resolve().parents[1]
    package_data = (root / "pyproject.toml").read_text()
    assert 'eng_platform_api = ["*.json", "static_examples/*.json"]' in package_data
    assert "COPY src/ src/" in (root / "Dockerfile").read_text()


@pytest.mark.parametrize(
    "scope", ["bundle", *check.IMAGE_NAMES, "manifest", "provider"]
)
def test_ambiguous_json_keys_are_rejected(bundle_files, scope):
    path, manifest = bundle_files
    bundle = json.loads(path.read_text())
    if scope == "bundle":
        path.write_text(
            path.read_text().replace(
                '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
            )
        )
    elif scope in check.IMAGE_NAMES:
        key = scope
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
        data = (
            json.dumps(response([writer(), *pins(), routing()]))
            if scope == "provider"
            else ""
        )
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
