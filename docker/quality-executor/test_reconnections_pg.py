import json
import os
import unittest
from unittest import mock

import reconnections_pg as fixture


def evidence():
    entries = [
        {"Action": "pass", "Package": package, "Test": name}
        for package, name in sorted(fixture.REQUIRED)
    ]
    entries += [
        {"Action": "pass", "Package": p} for p in {p for p, _ in fixture.REQUIRED}
    ]
    return entries


def encoded(entries):
    return b"\n".join(json.dumps(x).encode() for x in entries)


class FixtureContract(unittest.TestCase):
    def test_missing_remote_override_and_wrong_database_are_rejected(self):
        for value in (
            "",
            "postgresql://postgres@production/postgres",
            "postgresql://postgres@127.0.0.1:5432/postgres?hostaddr=10.0.0.1",
            "postgresql://postgres@127.0.0.1:5432/production",
            "postgresql://postgres@127.0.0.1:55440/postgres",
        ):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"POSTGRES_TEST_DSN": value}),
            ):
                with self.assertRaises(fixture.FixtureError):
                    fixture.fixture_environment()

    def test_fixture_cannot_inherit_connection_or_go_test_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "POSTGRES_TEST_DSN": "postgresql://postgres@127.0.0.1:5432/postgres",
                "PGHOST": "production",
                "GOFLAGS": "-run=^$",
                "GOWORK": "/untrusted/go.work",
            },
        ):
            env = fixture.fixture_environment()
        self.assertEqual(env["REQUIRE_POSTGRES_TESTS"], "1")
        self.assertFalse({"PGHOST", "GOFLAGS", "GOWORK"} & set(env))

    def test_complete_required_matrix_cases_and_optional_private_skip(self):
        events = evidence() + [
            {
                "Action": "skip",
                "Package": fixture.OPTIONAL[0],
                "Test": fixture.OPTIONAL[1],
            }
        ]
        self.assertEqual(
            fixture.verify_events(encoded(events))["optional_private_workbook_skips"], 1
        )

    def test_skip_failure_missing_case_or_package_terminal_are_rejected(self):
        mutations = [
            evidence()[1:],
            [x for x in evidence() if x.get("Test")],
            evidence() + [{"Action": "fail", "Package": fixture.PREFIX + "cmd/server"}],
            evidence()
            + [
                {
                    "Action": "skip",
                    "Package": fixture.PREFIX + "internal/store",
                    "Test": "TestPostgresSkipped",
                }
            ],
        ]
        for events in mutations:
            with self.subTest(events=events), self.assertRaises(fixture.FixtureError):
                fixture.verify_events(encoded(events))

    def test_malformed_evidence_rejected(self):
        for content in (b"", b"not json", b"[]", b"{}"):
            with self.subTest(content=content), self.assertRaises(fixture.FixtureError):
                fixture.verify_events(content)
