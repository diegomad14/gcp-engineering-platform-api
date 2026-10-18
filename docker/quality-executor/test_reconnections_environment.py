import os
import unittest
from pathlib import Path
import tempfile
from unittest import mock

import quality_executor as executor
import quality_profiles


class EnvironmentContract(unittest.TestCase):
    def test_go_profile_hash_changes_and_requires_fixture(self):
        profile = quality_profiles.profile_for("cgm-reconnections-api")
        self.assertTrue(profile["postgres"])
        self.assertEqual(
            profile["commands"]["tests"],
            "python3 /opt/eng-platform/reconnections_pg.py",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(executor.QualityExecutorError):
                executor._postgres_environment(profile)

    def test_go_only_receives_loopback_fixture_and_required_marker(self):
        with mock.patch.dict(
            os.environ,
            {
                "POSTGRES_TEST_DSN": "postgresql://postgres:synthetic@127.0.0.1:5432/postgres",
                "PGHOST": "production",
                "GOOGLE_APPLICATION_CREDENTIALS": "/credentials",
                "POSTGRES_DSN": "production",
            },
        ):
            env = executor._postgres_environment({"runtime": "go"})
        self.assertEqual(set(env), {"POSTGRES_TEST_DSN", "REQUIRE_POSTGRES_TESTS"})
        self.assertEqual(env["REQUIRE_POSTGRES_TESTS"], "1")

    def test_child_environment_keeps_go_fixture_without_credentials(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {
                    "POSTGRES_TEST_DSN": "postgresql://postgres@127.0.0.1:5432/postgres",
                    "PGHOST": "production",
                    "POSTGRES_DSN": "production",
                },
            ),
        ):
            path = Path(temporary)
            env = executor._child_environment(
                path,
                {"head_sha": "a" * 40, "base_sha": "b" * 40},
                {"runtime": "go", "postgres": True},
                go_temporary_directory=path,
            )
        self.assertEqual(env["REQUIRE_POSTGRES_TESTS"], "1")
        self.assertFalse({"PGHOST", "POSTGRES_DSN"} & set(env))
