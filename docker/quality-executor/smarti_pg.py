#!/usr/bin/env python3
"""Require executed, non-skipped Smarti PostgreSQL tests in both core modules."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter

# Only bounded, DTD/entity-free local JUnit is parsed below.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path


TEST_FILES = (
    "tests/test_smarti_prevention_postgres.py",
    "tests/test_smarti_publication.py",
)


class SmartiPostgresError(RuntimeError):
    """The mandatory Smarti PostgreSQL execution could not be verified."""


def _required_url() -> None:
    value = os.environ.get("SMARTI_TEST_POSTGRES_URL", "")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SmartiPostgresError("Invalid SMARTI_TEST_POSTGRES_URL") from None
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port not in {None, 5432}
        or parsed.path != "/smarti_test"
        or "?" in value
        or "#" in value
        or any(character.isspace() for character in value)
    ):
        raise SmartiPostgresError(
            "SMARTI_TEST_POSTGRES_URL must configure the isolated smarti_test database"
        )


def _required_cases(cases: Counter[tuple[str, str]]) -> dict[str, int]:
    counts = dict.fromkeys(TEST_FILES, 0)
    standalone = postgres = sqlite = 0
    for (module, name), count in cases.items():
        counts[module] += count
        if module == TEST_FILES[0] and "[" not in name:
            standalone += count
        elif module == TEST_FILES[1]:
            parameters = re.search(r"\[([^-\]]+)(?:-[^\]]*)?\]$", name)
            backend = parameters[1] if parameters is not None else ""
            if backend == "postgres":
                postgres += count
            elif backend == "sqlite":
                sqlite += count
    # These are minimums from the reviewed Smarti test contract. Extra cases
    # are welcome, but neither backend nor the standalone PG case may shrink.
    if (
        standalone < 1
        or counts[TEST_FILES[1]] < 40
        or postgres < 20
        or sqlite < 20
        or sum(counts.values()) < 41
    ):
        raise SmartiPostgresError(
            "Smarti PostgreSQL requires one standalone case and at least "
            "20 publication[postgres] plus 20 publication[sqlite] cases "
            f"(standalone={standalone}, publication={counts[TEST_FILES[1]]}, "
            f"postgres={postgres}, sqlite={sqlite})"
        )
    return counts


def _collected_cases(output: str) -> Counter[tuple[str, str]]:
    if re.search(r"\b\d+\s+deselected\b", output):
        raise SmartiPostgresError("Smarti PostgreSQL collection deselected cases")
    cases: Counter[tuple[str, str]] = Counter()
    for line in output.splitlines():
        for filename in TEST_FILES:
            if line.startswith(f"{filename}::"):
                cases[(filename, line.rsplit("::", 1)[-1])] += 1
    _required_cases(cases)
    return cases


def _executed_cases(
    report: Path,
    repository: Path,
    expected: Counter[tuple[str, str]] | None = None,
) -> dict[str, int]:
    if not report.is_file() or report.stat().st_size > 4 * 1024 * 1024:
        raise SmartiPostgresError(
            "Smarti PostgreSQL execution report is missing or invalid"
        )
    try:
        content = report.read_bytes()
        # Pytest writes UTF-8 JUnit. Reject UTF-16/32 and all DTD/entity
        # declarations before parsing repository-controlled test output.
        if b"\x00" in content or b"<!DOCTYPE" in content or b"<!ENTITY" in content:
            raise SmartiPostgresError("Smarti PostgreSQL execution report is invalid")
        root = ET.fromstring(content)  # nosec B314
    except (ET.ParseError, OSError):
        raise SmartiPostgresError(
            "Smarti PostgreSQL execution report is invalid"
        ) from None
    if root.tag not in {"testsuite", "testsuites"}:
        raise SmartiPostgresError("Smarti PostgreSQL execution report is invalid")
    if any(
        next(root.iter(tag), None) is not None
        for tag in ("skipped", "failure", "error")
    ):
        raise SmartiPostgresError(
            "Smarti PostgreSQL tests contain skipped or failed cases"
        )
    suites = list(root.iter("testsuite"))
    cases = list(root.iter("testcase"))
    if not suites or len(cases) != sum(
        len(suite.findall("testcase")) for suite in suites
    ):
        raise SmartiPostgresError("Smarti PostgreSQL execution report is invalid")
    for suite in suites:
        try:
            invalid = any(
                int(suite.get(field, "0")) != 0
                for field in ("skipped", "failures", "errors")
            )
            complete = int(suite.attrib["tests"]) == len(suite.findall("testcase"))
        except (KeyError, ValueError):
            raise SmartiPostgresError(
                "Smarti PostgreSQL execution report is invalid"
            ) from None
        if invalid:
            raise SmartiPostgresError(
                "Smarti PostgreSQL tests contain skipped or failed cases"
            )
        if not complete:
            raise SmartiPostgresError(
                "Smarti PostgreSQL execution report is incomplete"
            )
    executed: Counter[tuple[str, str]] = Counter()
    for case in cases:
        source = case.get("file", "")
        if not source or not case.get("name"):
            raise SmartiPostgresError(
                "Smarti PostgreSQL case is missing module evidence"
            )
        path = Path(source)
        path = path if path.is_absolute() else repository / path
        try:
            module = path.resolve().relative_to(repository).as_posix()
        except ValueError:
            raise SmartiPostgresError(
                "Smarti PostgreSQL case has invalid module evidence"
            ) from None
        if module not in TEST_FILES:
            raise SmartiPostgresError(
                "Smarti PostgreSQL report contains an unexpected module"
            )
        executed[(module, case.attrib["name"])] += 1
    if expected is not None and executed != expected:
        raise SmartiPostgresError(
            "Smarti PostgreSQL execution differs from the complete collected inventory"
        )
    return _required_cases(executed)


def run_checks(repository: Path) -> dict[str, int]:
    _required_url()
    repository = repository.resolve()
    for filename in TEST_FILES:
        path = repository / filename
        if not path.is_file() or not path.resolve().is_relative_to(repository):
            raise SmartiPostgresError(
                f"Required Smarti PostgreSQL test module is missing: {filename}"
            )
    with tempfile.TemporaryDirectory(prefix="smarti-postgres-") as temporary:
        report = Path(temporary) / "execution.xml"
        environment = dict(os.environ)
        # Repository config can still define fixtures and markers, but cannot
        # silently replace this explicit selection via inherited CLI options.
        for name in ("PYTEST_ADDOPTS", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
            environment.pop(name, None)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *TEST_FILES,
            "-o",
            "addopts=",
            "--color=no",
        ]
        collected = subprocess.run(
            [*command, "--collect-only"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if collected.returncode != 0:
            raise SmartiPostgresError("Smarti PostgreSQL collection failed")
        expected = _collected_cases(collected.stdout)
        completed = subprocess.run(
            [*command, "-o", "junit_family=legacy", f"--junitxml={report}"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        # Pytest output and JUnit failure text may contain connection URLs.
        # Keep the report temporary and publish only verified module counts.
        if completed.returncode != 0:
            raise SmartiPostgresError(
                f"Smarti PostgreSQL pytest execution failed (exit {completed.returncode})"
            )
        return _executed_cases(report, repository, expected)


def main() -> int:
    try:
        counts = run_checks(Path.cwd())
    except SmartiPostgresError as exc:
        print(f"Smarti PostgreSQL verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Smarti PostgreSQL verification failed; unable to read or execute required evidence",
            file=sys.stderr,
        )
        return 1
    modules = "; ".join(
        f"{filename}: {count} executed" for filename, count in counts.items()
    )
    print(f"Smarti PostgreSQL: {modules}; no skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
