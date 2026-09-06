"""Candidate validation rejects missing configuration before promotion."""

import json
import subprocess
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


def test_queries_exact_revision_and_accepts_expected_writer():
    data = response([writer(), {"name": "UNRELATED", "value": "PRIVATE"}])
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
        response([]),
        response([writer("")]),
        response([writer("other@example.com")]),
        response([{"name": check.WRITER_ENV, "valueFrom": {"secretKeyRef": {}}}]),
        response([writer(), writer()]),
        response([writer()], revision="another-revision"),
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
    assert capsys.readouterr().out == f"Candidate secrets writer check: {message}\n"
