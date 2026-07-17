#!/usr/bin/env python3
"""Run a portable quality gate and always emit a normalized JSON report."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(command: str, cwd: Path, output_path: Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    if not command.strip():
        return {
            "returncode": 0,
            "duration": 0.0,
            "output": "Check not configured for this profile.",
            "skipped": True,
        }
    completed = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        executable="/bin/bash",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        check=False,
    )
    output = completed.stdout[-12000:]
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(completed.stdout, encoding="utf-8")
    print(f"\n$ {command}\n{output}", flush=True)
    return {
        "returncode": completed.returncode,
        "duration": round(time.monotonic() - started, 3),
        "output": output,
        "skipped": False,
    }


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _count_list_report(path: Path) -> int:
    value = _json(path)
    return len(value) if isinstance(value, list) else 0


def _count_semgrep(path: Path) -> int:
    value = _json(path)
    return len(value.get("results", [])) if isinstance(value, dict) else 0


def _count_trivy(path: Path) -> tuple[int, dict[str, int]]:
    value = _json(path)
    counts = {"dependencies": 0, "secrets": 0, "misconfiguration": 0}
    if not isinstance(value, dict):
        return 0, counts
    for result in value.get("Results", []):
        counts["dependencies"] += len(result.get("Vulnerabilities") or [])
        counts["secrets"] += len(result.get("Secrets") or [])
        counts["misconfiguration"] += len(result.get("Misconfigurations") or [])
    return sum(counts.values()), counts


def _coverage(path: Path) -> float | None:
    value = _json(path)
    if not isinstance(value, dict):
        return None
    percent = value.get("totals", {}).get("percent_covered")
    if percent is None:
        percent = value.get("total", {}).get("lines", {}).get("pct")
    try:
        return round(float(percent), 2)
    except (TypeError, ValueError):
        return None


def _version(command: str, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            shlex.split(command),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return (
        completed.stdout.strip().splitlines()[0][:200]
        if completed.stdout.strip()
        else "unavailable"
    )


def _check(
    *,
    name: str,
    category: str,
    result: dict[str, Any],
    findings: int | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    count = (
        findings if findings is not None else (0 if result["returncode"] == 0 else 1)
    )
    if result["skipped"]:
        status = "SKIPPED"
    else:
        status = "PASSED" if result["returncode"] == 0 else "FAILED"
    details = result["output"].strip().splitlines()
    return {
        "name": name,
        "category": category,
        "status": status,
        "findings": count,
        "blocking_findings": count if status == "FAILED" else 0,
        "duration_seconds": result["duration"],
        "details": details[-1][:500] if details else "",
        "report_path": str(report_path) if report_path else "",
    }


def _defaults(profile: str, report_dir: Path) -> dict[str, str]:
    coverage_file = report_dir / "coverage.json"
    ruff_file = report_dir / "ruff.json"
    eslint_file = report_dir / "eslint.json"
    semgrep_file = report_dir / "semgrep.json"
    trivy_file = report_dir / "trivy.json"
    common = {
        "semgrep": (
            "semgrep scan --config auto --severity ERROR --error "
            f"--exclude {shlex.quote(report_dir.name)} "
            f"--json --output {shlex.quote(str(semgrep_file))} ."
        ),
        "trivy": (
            "trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL "
            f"--skip-dirs {shlex.quote(str(report_dir))} "
            f"--exit-code 1 --format json --output {shlex.quote(str(trivy_file))} ."
        ),
    }
    if profile == "python":
        return {
            **common,
            "install": 'python -m pip install -e ".[dev]"',
            "tests": (
                "python -m pytest -q --cov=. "
                f"--cov-report=json:{shlex.quote(str(coverage_file))}"
            ),
            "build": "",
            "lint": f"ruff check . --output-format json --output-file {shlex.quote(str(ruff_file))}",
            "format": "ruff format --check .",
            "typecheck": "python -m compileall -q .",
        }
    if profile == "node":
        return {
            **common,
            "install": "npm ci",
            "tests": (
                "npm test -- --run --coverage --coverage.reporter=json-summary "
                f"--coverage.reportsDirectory={shlex.quote(str(report_dir))}"
            ),
            "build": "npm run build",
            "lint": f"npx eslint . --max-warnings 0 -f json -o {shlex.quote(str(eslint_file))}",
            "format": "",
            "typecheck": "npx tsc --noEmit",
        }
    return {
        **common,
        "install": "npm ci",
        "tests": "",
        "build": "npm run build",
        "lint": f"npx eslint . --max-warnings 0 -f json -o {shlex.quote(str(eslint_file))}",
        "format": "",
        "typecheck": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--branch", default="")
    parser.add_argument(
        "--profile", choices=("python", "node", "static"), required=True
    )
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--workflow-run-url", default="")
    parser.add_argument("--coverage-threshold", type=float, default=70.0)
    parser.add_argument("--output", default="quality-report.json")
    parser.add_argument("--report-directory", default="quality-reports")
    parser.add_argument("--install-command")
    parser.add_argument("--test-command")
    parser.add_argument("--build-command")
    parser.add_argument("--lint-command")
    parser.add_argument("--format-command")
    parser.add_argument("--typecheck-command")
    args = parser.parse_args()

    cwd = Path(args.working_directory).resolve()
    report_dir = (cwd / args.report_directory).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    defaults = _defaults(args.profile, report_dir)
    commands = {
        "install": args.install_command
        if args.install_command is not None
        else defaults["install"],
        "tests": args.test_command
        if args.test_command is not None
        else defaults["tests"],
        "build": args.build_command
        if args.build_command is not None
        else defaults["build"],
        "lint": args.lint_command
        if args.lint_command is not None
        else defaults["lint"],
        "format": args.format_command
        if args.format_command is not None
        else defaults["format"],
        "typecheck": args.typecheck_command
        if args.typecheck_command is not None
        else defaults["typecheck"],
        "semgrep": defaults["semgrep"],
        "trivy": defaults["trivy"],
    }

    raw: dict[str, dict[str, Any]] = {}
    for name, command in commands.items():
        raw[name] = _run(command, cwd, report_dir / f"{name}.log")

    coverage_file = report_dir / "coverage.json"
    ruff_file = report_dir / "ruff.json"
    eslint_file = report_dir / "eslint.json"
    semgrep_file = report_dir / "semgrep.json"
    trivy_file = report_dir / "trivy.json"
    coverage = _coverage(coverage_file)
    if coverage is None:
        coverage = _coverage(report_dir / "coverage-summary.json")
    lint_findings = _count_list_report(ruff_file)
    if eslint_file.exists():
        eslint = _json(eslint_file)
        if isinstance(eslint, list):
            lint_findings = sum(
                int(item.get("errorCount", 0)) + int(item.get("warningCount", 0))
                for item in eslint
                if isinstance(item, dict)
            )
    semgrep_findings = _count_semgrep(semgrep_file)
    trivy_findings, trivy_categories = _count_trivy(trivy_file)

    checks = [
        _check(name="Install dependencies", category="setup", result=raw["install"]),
        _check(
            name="Tests and coverage",
            category="tests",
            result=raw["tests"],
            report_path=coverage_file,
        ),
        _check(name="Build", category="build", result=raw["build"]),
        _check(
            name="Lint",
            category="lint",
            result=raw["lint"],
            findings=lint_findings,
            report_path=ruff_file if args.profile == "python" else eslint_file,
        ),
        _check(name="Format", category="format", result=raw["format"]),
        _check(name="Type check", category="typecheck", result=raw["typecheck"]),
        _check(
            name="Semgrep SAST",
            category="sast",
            result=raw["semgrep"],
            findings=semgrep_findings,
            report_path=semgrep_file,
        ),
    ]
    for category, label in (
        ("dependencies", "Trivy dependencies"),
        ("secrets", "Trivy secrets"),
        ("misconfiguration", "Trivy misconfigurations"),
    ):
        category_result = dict(raw["trivy"])
        category_result["returncode"] = 1 if trivy_categories[category] else 0
        if (
            raw["trivy"]["returncode"] != 0
            and trivy_findings == 0
            and category == "dependencies"
        ):
            category_result["returncode"] = raw["trivy"]["returncode"]
        checks.append(
            _check(
                name=label,
                category=category,
                result=category_result,
                findings=trivy_categories[category],
                report_path=trivy_file,
            )
        )

    if args.profile != "static":
        if coverage is None:
            checks[1]["status"] = "FAILED"
            checks[1]["blocking_findings"] = max(1, checks[1]["blocking_findings"])
            checks[1]["details"] = "Coverage report was not generated."
        elif coverage < args.coverage_threshold:
            checks[1]["status"] = "FAILED"
            checks[1]["blocking_findings"] = max(1, checks[1]["blocking_findings"])
            checks[1]["details"] = (
                f"Coverage {coverage}% is below {args.coverage_threshold}%."
            )

    report = {
        "service_name": args.service_name,
        "repository": args.repository,
        "commit_sha": args.commit_sha,
        "branch": args.branch,
        "profile": args.profile,
        "workflow_run_url": args.workflow_run_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": coverage,
        "coverage_threshold": None
        if args.profile == "static"
        else args.coverage_threshold,
        "tool_versions": {
            "python": sys.version.split()[0],
            "ruff": _version("ruff --version", cwd),
            "semgrep": _version("semgrep --version", cwd),
            "trivy": _version("trivy --version", cwd),
            "node": _version("node --version", cwd)
            if args.profile in {"node", "static"}
            else "not-used",
        },
        "checks": checks,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    failed = any(check["status"] == "FAILED" for check in checks)
    print(f"\nQuality gate: {'FAILED' if failed else 'PASSED'}")
    print(f"Normalized report: {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
