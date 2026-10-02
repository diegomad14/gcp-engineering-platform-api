"""The Smarti PG gate requires real cases from both files and rejects skips."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import smarti_pg


SAFE_URL = "postgresql://postgres:quality-only@127.0.0.1:5432/smarti_test"


class SmartiPostgresTest(unittest.TestCase):
    def _publication_cases(
        self, count: int = 20, backends: tuple[str, ...] = ("postgres", "sqlite")
    ) -> str:
        return "import pytest\n" + "\n".join(
            f"@pytest.mark.parametrize('backend', {list(backends)!r})\n"
            f"def test_publication_{number}(backend): assert True\n"
            for number in range(count)
        )

    def _write_modules(self, repository: Path, first: str, second: str) -> None:
        (repository / "tests").mkdir()
        for name, body in zip(smarti_pg.TEST_FILES, (first, second), strict=True):
            (repository / name).write_text(body)

    def test_executes_both_modules_and_counts_actual_cases(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(
                repository,
                "def test_prevention(): assert True\n",
                self._publication_cases(),
            )
            self.assertEqual(
                dict(zip(smarti_pg.TEST_FILES, (1, 40), strict=True)),
                smarti_pg.run_checks(repository),
            )

    def test_missing_env_or_unsafe_url_never_launches_pytest(self) -> None:
        values = (
            "",
            "postgresql+psycopg://postgres@127.0.0.1/smarti_test",
            "postgresql://postgres@production.example/smarti_test",
            "postgresql://postgres@127.0.0.1:5433/smarti_test",
            "postgresql://postgres@127.0.0.1:invalid/smarti_test",
            "postgresql://postgres@127.0.0.1/wm_test",
            "postgresql://postgres@127.0.0.1/smarti_test?hostaddr=10.0.0.1",
            "postgresql://postgres@127.0.0.1/smarti_test#fragment",
        )
        for value in values:
            with (
                self.subTest(url=value),
                mock.patch.dict(
                    os.environ, {"SMARTI_TEST_POSTGRES_URL": value}, clear=True
                ),
                mock.patch.object(smarti_pg.subprocess, "run") as run,
                self.assertRaises(smarti_pg.SmartiPostgresError),
            ):
                smarti_pg.run_checks(Path.cwd())
            run.assert_not_called()

    def test_missing_module_never_launches_pytest(self) -> None:
        for missing in smarti_pg.TEST_FILES:
            with (
                self.subTest(module=missing),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
                mock.patch.object(smarti_pg.subprocess, "run") as run,
            ):
                repository = Path(temporary)
                self._write_modules(repository, "", "")
                (repository / missing).unlink()
                with self.assertRaisesRegex(smarti_pg.SmartiPostgresError, "missing"):
                    smarti_pg.run_checks(repository)
                run.assert_not_called()

    def test_skip_is_failure_even_when_pytest_exits_successfully(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(
                repository,
                "import pytest\n@pytest.mark.skip(reason='database unavailable')\ndef test_prevention(): assert True\n",
                self._publication_cases(),
            )
            with self.assertRaisesRegex(smarti_pg.SmartiPostgresError, "skipped"):
                smarti_pg.run_checks(repository)

    def test_pytest_failure_and_collection_error_are_failures(self) -> None:
        for body in (
            "def test_prevention(): assert False\n",
            "raise RuntimeError('collection failed')\n",
        ):
            with (
                self.subTest(body=body),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
            ):
                repository = Path(temporary)
                self._write_modules(repository, body, self._publication_cases())
                with self.assertRaisesRegex(smarti_pg.SmartiPostgresError, "failed"):
                    smarti_pg.run_checks(repository)

    def test_a_module_with_no_collected_cases_is_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(repository, "", self._publication_cases())
            with self.assertRaisesRegex(smarti_pg.SmartiPostgresError, "standalone"):
                smarti_pg.run_checks(repository)

    def test_invalid_or_incomplete_module_evidence_is_failure(self) -> None:
        reports = (
            "<invalid/>",
            "not xml",
            "<!DOCTYPE testsuites [<!ENTITY data 'expanded'>]><testsuites>&data;</testsuites>",
            "<testsuites>\x00</testsuites>",
            "<testsuites><testsuite tests='2'/></testsuites>",
            "<testsuites><testsuite tests='invalid'/></testsuites>",
            "<testsuites><testsuite tests='0' skipped='1'/></testsuites>",
            "<testsuites><testsuite tests='0'><skipped/></testsuite></testsuites>",
            "<testsuites><testsuite tests='0'><failure/></testsuite></testsuites>",
            "<testsuites><testsuite tests='0'><error/></testsuite></testsuites>",
            "<testsuites><testcase name='test_a' file='tests/test_smarti_prevention_postgres.py'/><testcase name='test_b' file='tests/test_smarti_publication.py'/></testsuites>",
            "<testsuites><testsuite tests='1'><testcase name='test_only'/></testsuite></testsuites>",
            "<testsuites><testsuite tests='1'><testcase name='test_only' file='tests/unrelated.py'/></testsuite></testsuites>",
            "<testsuites><testsuite tests='1'><testcase name='test_only' file='../outside.py'/></testsuite></testsuites>",
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            report = repository / "report.xml"
            with self.assertRaises(smarti_pg.SmartiPostgresError):
                smarti_pg._executed_cases(report, repository)
            for content in reports:
                with self.subTest(report=content):
                    report.write_text(content)
                    with self.assertRaises(smarti_pg.SmartiPostgresError):
                        smarti_pg._executed_cases(report, repository)

    def test_no_connection_url_is_printed_on_success_or_failure(self) -> None:
        for passed in (True, False):
            with (
                self.subTest(passed=passed),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
                mock.patch.object(smarti_pg.Path, "cwd", return_value=Path(temporary)),
            ):
                repository = Path(temporary)
                body = (
                    "import os\ndef test_prevention():\n"
                    "    print(os.environ['SMARTI_TEST_POSTGRES_URL'])\n"
                    f"    assert {passed}\n"
                )
                self._write_modules(repository, body, self._publication_cases())
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(0 if passed else 1, smarti_pg.main())
                self.assertNotIn(SAFE_URL, output.getvalue() + errors.getvalue())
                self.assertNotIn("quality-only", output.getvalue() + errors.getvalue())
                if passed:
                    for filename, count in zip(
                        smarti_pg.TEST_FILES, (1, 40), strict=True
                    ):
                        self.assertIn(
                            f"{filename}: {count} executed", output.getvalue()
                        )

    def test_additional_cases_are_allowed_without_reducing_either_backend(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(
                repository,
                "def test_prevention(): assert True\n",
                self._publication_cases(21),
            )
            self.assertEqual(
                dict(zip(smarti_pg.TEST_FILES, (1, 42), strict=True)),
                smarti_pg.run_checks(repository),
            )

    def test_quiet_reduction_or_sqlite_only_selection_cannot_pass(self) -> None:
        for publication in (
            "def test_publication(): assert True\n",
            self._publication_cases(19),
            self._publication_cases(40, ("sqlite",)),
            self._publication_cases(20, ("notpostgres", "sqlite")),
        ):
            with (
                self.subTest(publication=publication[:80]),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
            ):
                repository = Path(temporary)
                self._write_modules(
                    repository, "def test_prevention(): assert True\n", publication
                )
                with self.assertRaisesRegex(
                    smarti_pg.SmartiPostgresError, "20 publication"
                ):
                    smarti_pg.run_checks(repository)

    def test_composite_backend_ids_are_classified_by_exact_first_token(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            publication = "import pytest\n" + "\n".join(
                "@pytest.mark.parametrize('backend', ['postgres', 'sqlite'], "
                f"ids={['postgres-' + variant, 'sqlite-' + variant]!r})\n"
                f"def test_publication_{number}(backend): assert True\n"
                for number in range(20)
                for variant in [
                    ("False-communication_reports" if number % 2 else "expired")
                ]
            )
            self._write_modules(
                repository, "def test_prevention(): assert True\n", publication
            )
            self.assertEqual(
                dict(zip(smarti_pg.TEST_FILES, (1, 40), strict=True)),
                smarti_pg.run_checks(repository),
            )

    def test_deselection_is_blocking_even_above_minimum_case_counts(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(
                repository,
                "def test_prevention(): assert True\n",
                self._publication_cases(21),
            )
            (repository / "conftest.py").write_text(
                "def pytest_collection_modifyitems(config, items):\n"
                "    removed = [items.pop()]\n"
                "    config.hook.pytest_deselected(items=removed)\n"
            )
            with self.assertRaisesRegex(smarti_pg.SmartiPostgresError, "deselected"):
                smarti_pg.run_checks(repository)

    def test_execution_must_match_collection_even_if_reduction_is_quiet(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"SMARTI_TEST_POSTGRES_URL": SAFE_URL}),
        ):
            repository = Path(temporary)
            self._write_modules(
                repository,
                "def test_prevention(): assert True\n",
                self._publication_cases(21),
            )
            (repository / "conftest.py").write_text(
                "def pytest_collection_modifyitems(config, items):\n"
                "    if not config.option.collectonly: items.pop()\n"
            )
            with self.assertRaisesRegex(
                smarti_pg.SmartiPostgresError, "complete collected inventory"
            ):
                smarti_pg.run_checks(repository)


if __name__ == "__main__":
    unittest.main()
