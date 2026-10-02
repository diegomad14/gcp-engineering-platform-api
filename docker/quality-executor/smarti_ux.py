"""Require all six Smarti browser/accessibility cases in the isolated runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PLAYWRIGHT_VERSION = "1.62.1"
BROWSERS_PATH = "/opt/eng-platform/playwright-browsers"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SPECS = (
    "e2e/smarti-prevention.ux.spec.ts",
    "e2e/smarti-source-warnings.ux.spec.ts",
    "e2e/smarti-calendar-coverage.ux.spec.ts",
)


class SmartiUXError(RuntimeError):
    """Missing, skipped or unsuccessful browser evidence must block quality."""


def _diagnostic_text(value: Any, limit: int = 1600) -> str:
    """Keep diagnostics single-line and bounded without terminal controls."""
    value = _ANSI_ESCAPE.sub("", str(value))
    return "".join(
        character for character in " ".join(value.split()) if character.isprintable()
    )[:limit]


def _failure_diagnostics(report: dict[str, Any]) -> list[str]:
    # Diagnostics are best-effort after a failed execution. Malformed output
    # must not replace the original gate failure with an unrelated traceback.
    try:
        return _collect_failure_diagnostics(report)
    except (SmartiUXError, TypeError, ValueError, AttributeError, KeyError):
        return ["Playwright returned malformed failure diagnostics"]


def _collect_failure_diagnostics(report: dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    for error in report.get("errors", [])[:6]:
        message = error.get("message", error) if isinstance(error, dict) else error
        diagnostics.append("configuration: " + _diagnostic_text(message))
    for file, title, test in cases(report):
        for result in test.get("results", []):
            if not isinstance(result, dict) or result.get("status") == "passed":
                continue
            error = result.get("error", {})
            errors = result.get("errors", [])
            if not error and errors:
                error = errors[0]
            message = error.get("message", error) if isinstance(error, dict) else error
            # Browser launch command lines can hide the useful stderr behind
            # thousands of flag characters. Retain the actual browser errors.
            browser_errors = [
                line for line in str(message).splitlines() if "[err]" in line
            ]
            if browser_errors:
                message = (
                    str(message).splitlines()[0] + "; " + "; ".join(browser_errors)
                )
            diagnostics.append(
                f"{file} | {_diagnostic_text(title, 180)} | "
                f"{_diagnostic_text(result.get('status'), 30)}: "
                f"{_diagnostic_text(message)}"
            )
            if len(diagnostics) == 12:
                return diagnostics
    return diagnostics


def cases(report: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []

    def visit(suites: Any) -> None:
        if not isinstance(suites, list):
            raise SmartiUXError("Invalid Playwright suite report")
        for suite in suites:
            if not isinstance(suite, dict):
                raise SmartiUXError("Invalid Playwright suite report")
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    found.append(
                        (
                            Path(spec.get("file", suite.get("file", ""))).name,
                            str(spec.get("title", "")),
                            test,
                        )
                    )
            visit(suite.get("suites", []))

    visit(report.get("suites", []))
    return found


def validate_report(report: dict[str, Any], *, executed: bool) -> None:
    if report.get("errors"):
        raise SmartiUXError("Playwright reported browser or configuration errors")
    found = cases(report)
    expected = {Path(spec).name for spec in SPECS}
    if len(found) != 6 or {file for file, _, _ in found} != expected:
        raise SmartiUXError("Smarti UX requires exactly six cases from all three specs")
    for file in expected:
        selected = [(title, test) for name, title, test in found if name == file]
        viewports = [
            re.findall(r"\b(desktop|mobile)\b", title) for title, _ in selected
        ]
        if len(selected) != 2 or sorted(viewports) != [["desktop"], ["mobile"]]:
            raise SmartiUXError(
                "Every Smarti spec requires one desktop and one mobile case"
            )
        for _, test in selected:
            if test.get("expectedStatus") != "passed":
                raise SmartiUXError(
                    "Skipped or expected-failure Smarti cases are forbidden"
                )
            if executed:
                results = test.get("results", [])
                if (
                    not isinstance(results, list)
                    or len(results) != 1
                    or not isinstance(results[0], dict)
                    or results[0].get("status") != "passed"
                    or results[0].get("retry") != 0
                ):
                    raise SmartiUXError(
                        "All six Smarti browser/accessibility cases must pass"
                    )
    if executed:
        stats = report.get("stats", {})
        if stats.get("expected") != 6 or any(
            stats.get(field, 0) for field in ("skipped", "unexpected", "flaky")
        ):
            raise SmartiUXError(
                "Smarti UX requires six executed passes without skips or retries"
            )


def _run_json(repository: Path, arguments: list[str]) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        ["node", "node_modules/@playwright/test/cli.js", "test", *arguments],
        cwd=repository,
        env={**os.environ, "CI": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = _diagnostic_text(completed.stderr or completed.stdout, 350)
        raise SmartiUXError(
            "Playwright did not produce a JSON report: " + detail
        ) from exc
    if not isinstance(report, dict):
        raise SmartiUXError("Playwright did not produce an object report")
    return completed.returncode, report


def run(repository: Path) -> None:
    if any(not (repository / spec).is_file() for spec in SPECS):
        raise SmartiUXError(
            "Required Smarti specs are missing; rebase before profile activation"
        )
    try:
        package = json.loads(
            (repository / "node_modules/@playwright/test/package.json").read_text()
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SmartiUXError("Pinned Playwright dependency is missing") from exc
    if package.get("version") != PLAYWRIGHT_VERSION:
        raise SmartiUXError("Smarti UX requires Playwright " + PLAYWRIGHT_VERSION)
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") != BROWSERS_PATH:
        raise SmartiUXError("Smarti UX requires the immutable image browser directory")
    arguments = [
        "--config=playwright.config.ts",
        *SPECS,
        "--forbid-only",
        "--retries=0",
    ]
    code, listed = _run_json(repository, [*arguments, "--list", "--reporter=json"])
    if code:
        raise SmartiUXError("Playwright collection failed")
    validate_report(listed, executed=False)
    code, report = _run_json(repository, [*arguments, "--workers=1", "--reporter=json"])
    directory = repository / "quality-reports"
    directory.mkdir(exist_ok=True)
    temporary = directory / "smarti-ux.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False))
    temporary.replace(directory / "smarti-ux.json")
    if code:
        diagnostics = _failure_diagnostics(report)
        for diagnostic in diagnostics:
            print("Smarti UX diagnostic: " + diagnostic, file=sys.stderr)
        detail = diagnostics[0] if diagnostics else "no error detail in JSON report"
        raise SmartiUXError(
            "Playwright execution failed: "
            + _diagnostic_text(detail, 380)
            + "; see quality-reports/smarti-ux.json"
        )
    validate_report(report, executed=True)


def main() -> int:
    try:
        run(Path.cwd())
    except (OSError, SmartiUXError) as exc:
        print("Smarti UX FAILED: " + str(exc), file=sys.stderr)
        return 1
    print("Smarti UX PASSED: six desktop/mobile browser and axe cases executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
