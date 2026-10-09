"""Python preflight avoids ineligible suites without suppressing other controls."""

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from eng_platform_api.models import CatalogService, QualityReportCreate
from eng_platform_api.services.quality_policy import policy_errors


ORIGINAL_ORDER = (
    "install",
    "tests",
    "build",
    "lint",
    "format",
    "typecheck",
    "semgrep",
    "trivy",
)
PYTHON_ORDER = (
    "install",
    "format",
    "lint",
    "tests",
    "build",
    "typecheck",
    "semgrep",
    "trivy",
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path, monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts/quality"
    monkeypatch.syspath_prepend(str(scripts))
    runner = load_module("gate_order", scripts / "quality_gate.py")
    root = tmp_path / "repository"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (root / ".quality-sources.json").write_text(json.dumps({"roots": ["code.py"]}))
    (root / "code.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (root / "code.py").write_text("value = 2\n")
    git("add", ".")
    git("commit", "-qm", "change")
    head = git("rev-parse", "HEAD")
    config = tmp_path / "config.json"
    config.write_text("{}")
    helper = tmp_path / "fake_check.py"
    helper.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "stage = sys.argv[1]\n"
        "config = json.loads(Path(sys.argv[2]).read_text())\n"
        "with Path('order.log').open('a') as order:\n"
        "    order.write(stage + '\\n')\n"
        "reports = Path('quality-reports')\n"
        "if stage == 'tests' and not config.get('missing_coverage'):\n"
        "    coverage = {'totals': {'percent_covered': config.get('coverage', 100)},\n"
        "                'files': {'code.py': {'executed_lines': [1], 'missing_lines': []}}}\n"
        "    (reports / 'coverage.json').write_text(json.dumps(coverage))\n"
        "    (reports / 'lcov.info').write_text('SF:code.py\\nDA:1,1\\nLF:1\\nLH:1\\nend_of_record\\n')\n"
        "for check, filename, value in [('lint', 'ruff.json', []),\n"
        "                                ('semgrep', 'semgrep.json', {'results': []}),\n"
        "                                ('trivy', 'trivy.json', {'Results': []})]:\n"
        "    if stage == check and not config.get('missing_' + stage):\n"
        "        (reports / filename).write_text(json.dumps(value))\n"
        "if stage == 'typecheck' and config.get('mutate_source'):\n"
        "    Path('code.py').write_text('value = 3\\n')\n"
        "print(stage + ' diagnostics')\n"
        "sys.exit(config.get(stage, 0))\n"
    )

    def command(stage):
        return shlex.join([sys.executable, str(helper), stage, str(config)])

    commands = {stage: command(stage) for stage in ORIGINAL_ORDER}
    monkeypatch.setattr(runner, "_defaults", lambda *_args, **_kwargs: commands)
    monkeypatch.setattr(runner, "_version", lambda *_args: "fixture-version")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    output = tmp_path / "report.json"
    argv = [
        "quality_gate.py",
        "--service-name",
        "test-api",
        "--repository",
        "test/api",
        "--commit-sha",
        head,
        "--base-sha",
        base,
        "--working-directory",
        str(root),
        "--output",
        str(output),
    ]

    def execute(settings=None, profile="python", extras=None):
        config.write_text(json.dumps(settings or {}))
        arguments = [*argv, "--profile", profile]
        if extras:
            arguments += ["--extra-checks-json", json.dumps(extras)]
        monkeypatch.setattr(sys, "argv", arguments)
        status = runner.main()
        return status, json.loads(output.read_text())

    return SimpleNamespace(
        runner=runner,
        execute=execute,
        root=root,
        reports=root / "quality-reports",
        command=command,
        commands=commands,
        config=config,
        base=base,
        head=head,
        order=lambda: (root / "order.log").read_text().splitlines(),
    )


def checks(report):
    return {check["category"]: check for check in report["checks"]}


def eligibility_errors(report):
    service = CatalogService(
        service_name="test-api",
        repository="test/api",
        owner="test",
        project_id="test",
        region="test",
        quality={"enabled": True, "profile": "python", "coverage_threshold": 70},
    )
    return policy_errors(QualityReportCreate(**report), service)


def extra(gate):
    return {
        "name": "Database contracts",
        "category": "smarti_postgres",
        "command": gate.command("extra"),
        "blocking": True,
    }


@pytest.mark.parametrize(
    "failures",
    [
        {"format": 1},
        {"lint": 1},
        {"format": 1, "lint": 1},
        {"format": 2},
        {"lint": 2, "missing_lint": True},
        {"format": 124},  # Supervised command deadline.
        {"lint": 124},
        {"format": 127},  # Command unavailable.
        {"lint": 137},  # Terminated child.
    ],
)
def test_failed_preflight_skips_tests_and_cannot_reuse_stale_evidence(
    gate, failures, capsys
):
    gate.reports.mkdir()
    for name in ("coverage.json", "coverage-summary.json", "ruff.json"):
        (gate.reports / name).write_text('{"totals": {"percent_covered": 100}}')
    (gate.reports / "lcov.info").write_text("old coverage")
    (gate.reports / "tests.log").write_text("old suite PASSED")
    status, report = gate.execute(failures, extras=[extra(gate)])
    recorded = checks(report)
    assert status == 1
    assert "Quality gate: FAILED" in capsys.readouterr().out
    assert gate.order() == [stage for stage in PYTHON_ORDER if stage != "tests"] + [
        "extra"
    ]
    for category in ("tests", "differential_coverage"):
        assert recorded[category]["status"] == "SKIPPED"
        assert "not executed" in recorded[category]["details"]
        assert "preflight failed" in recorded[category]["details"]
        assert "No modified executable lines" not in recorded[category]["details"]
    assert report["coverage"] is None
    assert report["differential_coverage"] is None
    assert report["changed_lines"] is None
    assert report["covered_changed_lines"] is None
    assert report["coverage_threshold"] == 70
    assert report["differential_threshold"] == 80
    assert report["base_sha"] == gate.base
    assert report["commit_sha"] == gate.head
    assert eligibility_errors(report)
    for stage in ("format", "lint"):
        assert (gate.reports / f"{stage}.log").read_text().strip() == (
            f"{stage} diagnostics"
        )
        assert recorded[stage]["status"] == (
            "FAILED" if failures.get(stage) else "PASSED"
        )
    assert "old suite" not in (gate.reports / "tests.log").read_text()
    for name in ("coverage.json", "coverage-summary.json", "lcov.info"):
        assert not (gate.reports / name).exists()
    for category in (
        "typecheck",
        "sast",
        "dependencies",
        "secrets",
        "misconfiguration",
        "smarti_postgres",
    ):
        assert recorded[category]["status"] == "PASSED"


def test_success_runs_original_commands_once_in_preflight_order(gate, monkeypatch):
    original_run = gate.runner._run
    executed = []

    def run(command, *args):
        executed.append(command)
        return original_run(command, *args)

    monkeypatch.setattr(gate.runner, "_run", run)
    status, report = gate.execute()
    assert status == 0
    assert gate.order() == list(PYTHON_ORDER)
    assert executed == [gate.commands[stage] for stage in PYTHON_ORDER]
    assert all(check["status"] == "PASSED" for check in report["checks"])
    assert report["coverage"] == report["differential_coverage"] == 100
    assert not eligibility_errors(report)


@pytest.mark.parametrize(
    "settings",
    [{"tests": 2}, {"tests": 124}, {"missing_coverage": True}, {"coverage": 69}],
)
def test_test_errors_missing_report_and_low_coverage_remain_blocking(gate, settings):
    status, report = gate.execute(settings)
    assert status == 1
    assert gate.order() == list(PYTHON_ORDER)
    assert checks(report)["tests"]["status"] == "FAILED"
    assert eligibility_errors(report)


@pytest.mark.parametrize("profile", ["node", "static"])
def test_non_python_order_and_test_execution_are_unchanged(gate, profile):
    status, report = gate.execute({"format": 1, "lint": 1}, profile=profile)
    assert status == 1
    assert gate.order() == list(ORIGINAL_ORDER)
    assert checks(report)["tests"]["status"] == "PASSED"


def test_later_failures_are_retained_after_failed_preflight(gate):
    status, report = gate.execute(
        {"format": 1, "typecheck": 2, "semgrep": 2, "trivy": 2, "extra": 2},
        extras=[extra(gate)],
    )
    assert status == 1
    recorded = checks(report)
    for category in ("format", "typecheck", "sast", "dependencies", "smarti_postgres"):
        assert recorded[category]["status"] == "FAILED"
    assert recorded["tests"]["status"] == "SKIPPED"
    assert gate.order()[-4:] == ["typecheck", "semgrep", "trivy", "extra"]


@pytest.mark.parametrize("mutate_source", [False, True])
def test_executor_retains_extras_integrity_and_failed_manifest(
    gate, tmp_path, monkeypatch, mutate_source
):
    """Exercise the executor's post-gate path without privileged setup or network."""
    directory = Path(__file__).parents[1] / "docker/quality-executor"
    monkeypatch.syspath_prepend(str(directory))
    executor = load_module("executor_order", directory / "quality_executor.py")
    identity = {
        "service_name": "test-api",
        "repository": "test/api",
        "head_sha": gate.head,
        "base_sha": gate.base,
        "operation": "pr_quality",
        "profile_hash": "a" * 64,
    }
    profile = {
        "working_directory": ".",
        "timeout_seconds": 60,
        "coverage_threshold": 70,
        "runtime": "python",
        "commands": gate.commands,
        "extra": [extra(gate)],
    }
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = tmp_path / "output"
    gate.config.write_text(json.dumps({"format": 1, "mutate_source": mutate_source}))
    monkeypatch.setattr(executor, "_identity", lambda: identity)
    monkeypatch.setattr(executor, "verify_profile_hash", lambda *_args: profile)
    monkeypatch.setattr(
        executor,
        "_isolated_checkout",
        lambda *_args: (gate.root, gate.root / ".git", runtime),
    )
    monkeypatch.setattr(executor, "_child_environment", lambda *_args: {})
    monkeypatch.setattr(
        executor,
        "_prepare_scanner_runtime_parent",
        lambda *_args: scratch / "trusted-scanner-runtime",
    )
    monkeypatch.setattr(executor, "_chown_tree", lambda *_args: None)
    monkeypatch.setattr(executor, "_anchor_report_exchange", lambda *_args: None)
    monkeypatch.setattr(executor, "_trusted_gate_environment", lambda *_args: {})
    monkeypatch.setattr(
        executor, "_supervised_command", lambda command, *_args: command
    )

    def run_gate(argv, *_args):
        monkeypatch.setattr(sys, "argv", argv[1:])
        return gate.runner.main(), False

    def run_extra(check, cwd, _environment, reports, _deadline):
        return gate.runner._check(
            name=check["name"],
            category=check["category"],
            result=gate.runner._run(check["command"], cwd, reports / "extra.log"),
        )

    monkeypatch.setattr(executor, "_run_trusted_gate", run_gate)
    monkeypatch.setattr(executor, "_extra_check", run_extra)
    assert executor._run_quality("test-api", gate.root, scratch, output, tmp_path) == 0
    manifest = json.loads((output / "quality-result.json").read_text())
    assert manifest["status"] == "quality_failed"
    recorded = checks(manifest["report"])
    assert recorded["tests"]["status"] == "SKIPPED"
    assert recorded["smarti_postgres"]["status"] == "PASSED"
    assert recorded["identity"]["status"] == ("FAILED" if mutate_source else "PASSED")
    assert gate.order()[-1] == "extra"
    assert eligibility_errors(manifest["report"])
