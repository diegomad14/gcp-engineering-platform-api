"""Portable extras execute real commands and fail closed without weakening baseline checks."""

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts/quality"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "gate_extras", scripts / "quality_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(runner, tmp_path, monkeypatch):
    command = shlex.join([sys.executable, "-c", "print('baseline check executed')"])
    monkeypatch.setattr(
        runner,
        "_defaults",
        lambda *_args, **_kwargs: dict.fromkeys(
            (
                "install",
                "tests",
                "build",
                "lint",
                "format",
                "typecheck",
                "semgrep",
                "trivy",
            ),
            command,
        ),
    )
    monkeypatch.setattr(runner, "_coverage", lambda _path: 90)
    monkeypatch.setattr(runner, "_version", lambda *_args: "test")
    monkeypatch.setattr(runner, "resolve_base", lambda *_args: "b" * 40)
    monkeypatch.setattr(
        runner,
        "differential",
        lambda *_args: {
            "policy_version": "oss-v2",
            "base_sha": "b" * 40,
            "changed_lines": 1,
            "covered_changed_lines": 1,
            "differential_coverage": 100,
            "differential_threshold": 80,
        },
    )
    output = tmp_path / "quality.json"
    argv = [
        "quality_gate.py",
        "--service-name",
        "cgm-sanplat-api",
        "--repository",
        "test/api",
        "--commit-sha",
        "a" * 40,
        "--base-sha",
        "b" * 40,
        "--profile",
        "python",
        "--working-directory",
        str(tmp_path),
        "--output",
        str(output),
    ]

    def execute(extras=None):
        arguments = (
            argv
            if extras is None
            else [*argv, "--extra-checks-json", json.dumps(extras)]
        )
        monkeypatch.setattr(sys, "argv", arguments)
        status = runner.main()
        return status, json.loads(output.read_text())

    return execute


def extra(command):
    return {
        "name": "Smarti PostgreSQL",
        "category": "smarti_postgres",
        "command": command,
        "blocking": True,
    }


def test_default_empty_does_not_duplicate_canonical_executor_extras(gate):
    status, report = gate()
    assert status == 0
    assert not any(check["category"] == "smarti_postgres" for check in report["checks"])
    assert all(check["status"] == "PASSED" for check in report["checks"])


@pytest.mark.parametrize("returncode", [0, 7])
def test_extra_executes_and_records_actual_blocking_result(gate, returncode):
    command = shlex.join(
        [
            sys.executable,
            "-c",
            f"import sys; print('trusted check executed'); sys.exit({returncode})",
        ]
    )
    status, report = gate([extra(command)])
    check = next(
        check for check in report["checks"] if check["category"] == "smarti_postgres"
    )
    assert status == int(returncode != 0)
    assert check["status"] == ("PASSED" if returncode == 0 else "FAILED")
    assert check["blocking_findings"] == int(returncode != 0)
    assert check["details"] == "trusted check executed"
    assert (
        next(check for check in report["checks"] if check["category"] == "tests")[
            "status"
        ]
        == "PASSED"
    )


@pytest.mark.parametrize("configured", [False, True])
def test_real_smarti_helper_missing_env_or_modules_is_blocking(
    gate, monkeypatch, configured
):
    helper = Path(__file__).parents[1] / "docker/quality-executor/smarti_pg.py"
    if configured:
        monkeypatch.setenv(
            "SMARTI_TEST_POSTGRES_URL",
            "postgresql://postgres@127.0.0.1:5432/smarti_test",
        )
    else:
        monkeypatch.delenv("SMARTI_TEST_POSTGRES_URL", raising=False)
    status, report = gate([extra(shlex.join([sys.executable, str(helper)]))])
    check = next(
        check for check in report["checks"] if check["category"] == "smarti_postgres"
    )
    assert status == 1
    assert check["status"] == "FAILED"
    assert check["blocking_findings"] == 1
    assert "postgresql://" not in check["details"]


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "null",
        "{}",
        "[42]",
        "[{}]",
        json.dumps([extra("")]),
        json.dumps([{**extra("true"), "blocking": False}]),
        json.dumps([{**extra("true"), "blocking": "true"}]),
        json.dumps([{**extra("true"), "category": "tests"}]),
        json.dumps([{**extra("true"), "category": "../injected"}]),
        json.dumps([{**extra("true"), "command": None}]),
        json.dumps([extra("true"), extra("false")]),
    ],
)
def test_malformed_empty_advisory_or_duplicate_extra_rejected_before_execution(
    runner, value, monkeypatch
):
    def run(*_args, **_kwargs):
        pytest.fail("malformed extra must not execute")

    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quality_gate.py",
            "--service-name",
            "test",
            "--repository",
            "test/api",
            "--commit-sha",
            "a" * 40,
            "--profile",
            "python",
            "--extra-checks-json",
            value,
        ],
    )
    with pytest.raises(SystemExit) as rejected:
        runner.main()
    assert rejected.value.code == 2
