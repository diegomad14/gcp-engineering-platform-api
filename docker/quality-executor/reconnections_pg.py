#!/usr/bin/env python3
"""Fail closed unless the native Go/PostgreSQL contract executes at both pools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.parse

PREFIX = "github.com/diegomad14/cgm-reconnections-api/"
REQUIRED = {
    (PREFIX + "internal/store", name)
    for name in (
        "TestMigrationPreservesAllOpaqueRecordsAndResumesExactly",
        "TestFirestoreFreezeInvalidatesAnInFlightWriterTransaction",
        "TestPostgresEngineCrashBoundariesNeverRepeatVendorPOST",
        "TestCutoverFreezeBetweenIntentAndPOSTKeepsIntentAndNeverPosts",
        "TestFrozenCloudAndSQLCannotCreateOrWriteSnapshot",
    )
}
OPTIONAL = (PREFIX + "internal/input", "TestPrivateWorkbooksOptional")
MAX_REPORT = 64 * 1024 * 1024


class FixtureError(RuntimeError):
    pass


def fixture_environment() -> dict[str, str]:
    value = os.environ.get("POSTGRES_TEST_DSN", "")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (
            parsed.scheme == "postgresql"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 5432
            and parsed.path == "/postgres"
            and "?" not in value
            and "#" not in value
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise FixtureError("Disposable loopback PostgreSQL fixture required")
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("PG") or key in {"GOFLAGS", "GOWORK"}:
            environment.pop(key)
    environment["REQUIRE_POSTGRES_TESTS"] = "1"
    environment["GOTOOLCHAIN"] = "local"
    return environment


def verify_events(content: bytes) -> dict[str, int]:
    if not content or len(content) > MAX_REPORT:
        raise FixtureError("Native Go evidence missing or oversized")
    passed: set[tuple[str, str]] = set()
    packages: set[str] = set()
    skips = 0
    try:
        for line in content.splitlines():
            item = json.loads(line)
            action = item.get("Action")
            key = (item.get("Package", ""), item.get("Test", ""))
            if action == "fail":
                raise FixtureError("Native Go contract failed")
            if action == "skip" and key[1]:
                if key != OPTIONAL:
                    raise FixtureError("Mandatory Go/PostgreSQL test skipped")
                skips += 1
            if action == "pass":
                if key[1]:
                    passed.add(key)
                else:
                    packages.add(key[0])
    except (ValueError, AttributeError) as error:
        raise FixtureError("Native Go evidence malformed") from error
    if not REQUIRED.issubset(passed) or not all(
        package in packages for package, _ in REQUIRED
    ):
        raise FixtureError("Required migration/cutover cases did not execute")
    return {"passed_test_events": len(passed), "optional_private_workbook_skips": skips}


def main() -> None:
    environment = fixture_environment()
    output = Path("quality-reports")
    output.mkdir(exist_ok=True)
    matrix = {}
    for pool in (1, 2):
        environment["POSTGRES_TEST_MAX_CONNECTIONS"] = str(pool)
        coverage = "coverage.out" if pool == 1 else "coverage-pool-2.out"
        command = [
            "go",
            "test",
            "-json",
            "-race",
            "-count=1",
            "-covermode=atomic",
            "-coverpkg=./...",
            "-coverprofile=" + str(output / coverage),
            "./...",
        ]
        with tempfile.TemporaryFile() as events:
            completed = subprocess.run(
                command,
                env=environment,
                stdout=events,
                stderr=subprocess.DEVNULL,
                timeout=840,
                check=False,
            )
            if completed.returncode != 0:
                raise FixtureError("Native Go/PostgreSQL matrix failed")
            events.seek(0)
            matrix[str(pool)] = verify_events(events.read(MAX_REPORT + 1))
    print(
        json.dumps(
            {"fixture": "postgresql-15-disposable", "pool_matrix": matrix},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
