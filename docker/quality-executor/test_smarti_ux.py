from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import smarti_ux


def report(*, executed: bool = True) -> dict:
    return {
        "suites": [
            {
                "file": Path(spec).name,
                "specs": [
                    {
                        "file": Path(spec).name,
                        "title": "Smarti " + viewport,
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "results": [{"status": "passed", "retry": 0}]
                                if executed
                                else [],
                            }
                        ],
                    }
                    for viewport in ("desktop", "mobile")
                ],
            }
            for spec in smarti_ux.SPECS
        ],
        "errors": [],
        "stats": {"expected": 6, "skipped": 0, "unexpected": 0, "flaky": 0},
    }


class SmartiUXTest(unittest.TestCase):
    def test_requires_all_specs_and_both_viewports(self) -> None:
        smarti_ux.validate_report(report(executed=False), executed=False)
        smarti_ux.validate_report(report(), executed=True)
        for whole_file in (True, False):
            broken = report()
            if whole_file:
                broken["suites"].pop()
            else:
                broken["suites"][0]["specs"].pop()
            with self.assertRaisesRegex(smarti_ux.SmartiUXError, "exactly six"):
                smarti_ux.validate_report(broken, executed=True)
        broken = report()
        broken["suites"][0]["specs"][0]["title"] = "Smarti tablet"
        with self.assertRaisesRegex(smarti_ux.SmartiUXError, "desktop.*mobile"):
            smarti_ux.validate_report(broken, executed=True)

    def test_does_not_accept_skip_expected_failure_empty_or_failed_results(
        self,
    ) -> None:
        for status in ("skipped", "failed", "timedOut", "interrupted"):
            broken = report()
            broken["suites"][0]["specs"][0]["tests"][0]["results"][0]["status"] = status
            with self.assertRaises(smarti_ux.SmartiUXError):
                smarti_ux.validate_report(broken, executed=True)
        for expected in ("failed", "skipped"):
            broken = report()
            broken["suites"][0]["specs"][0]["tests"][0]["expectedStatus"] = expected
            with self.assertRaises(smarti_ux.SmartiUXError):
                smarti_ux.validate_report(broken, executed=False)
        with self.assertRaises(smarti_ux.SmartiUXError):
            smarti_ux.validate_report(report(executed=False), executed=True)

    def test_rejects_browser_errors_invalid_reports_and_retries(self) -> None:
        for field in ("skipped", "unexpected", "flaky"):
            broken = report()
            broken["stats"][field] = 1
            with self.assertRaises(smarti_ux.SmartiUXError):
                smarti_ux.validate_report(broken, executed=True)
        broken = report()
        broken["errors"] = [{"message": "browser launch failed"}]
        with self.assertRaises(smarti_ux.SmartiUXError):
            smarti_ux.validate_report(broken, executed=True)
        with self.assertRaises(smarti_ux.SmartiUXError):
            smarti_ux.cases({"suites": "invalid"})
        with self.assertRaises(smarti_ux.SmartiUXError):
            smarti_ux.cases({"suites": [None]})
        broken = report()
        broken["stats"]["expected"] = 5
        with self.assertRaises(smarti_ux.SmartiUXError):
            smarti_ux.validate_report(broken, executed=True)

    def test_rejects_silent_retries_even_when_aggregate_flaky_is_zero(self) -> None:
        for results in (
            [{"status": "passed", "retry": 0}, {"status": "passed", "retry": 0}],
            [{"status": "passed", "retry": 1}],
            [{"status": "passed"}],
            [None],
        ):
            broken = report()
            broken["suites"][0]["specs"][0]["tests"][0]["results"] = results
            with self.assertRaises(smarti_ux.SmartiUXError):
                smarti_ux.validate_report(broken, executed=True)

    def test_run_fails_closed_before_collection_for_missing_specs_package_or_browser_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(smarti_ux.SmartiUXError, "rebase"):
                smarti_ux.run(root)
            for spec in smarti_ux.SPECS:
                path = root / spec
                path.parent.mkdir(exist_ok=True)
                path.touch()
            with self.assertRaisesRegex(smarti_ux.SmartiUXError, "dependency"):
                smarti_ux.run(root)
            package = root / "node_modules/@playwright/test/package.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({"version": "1.63.0"}))
            with self.assertRaisesRegex(smarti_ux.SmartiUXError, "1.62.1"):
                smarti_ux.run(root)
            package.write_text(json.dumps({"version": smarti_ux.PLAYWRIGHT_VERSION}))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(smarti_ux.SmartiUXError, "immutable"):
                    smarti_ux.run(root)

    def test_run_records_six_passes_and_blocks_nonzero_collection_or_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for spec in smarti_ux.SPECS:
                path = root / spec
                path.parent.mkdir(exist_ok=True)
                path.touch()
            package = root / "node_modules/@playwright/test/package.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({"version": smarti_ux.PLAYWRIGHT_VERSION}))
            with mock.patch.dict(
                os.environ, {"PLAYWRIGHT_BROWSERS_PATH": smarti_ux.BROWSERS_PATH}
            ):
                with mock.patch.object(
                    smarti_ux,
                    "_run_json",
                    side_effect=[(0, report(executed=False)), (0, report())],
                ) as run:
                    smarti_ux.run(root)
                    self.assertIn("--forbid-only", run.call_args_list[0].args[1])
                    self.assertIn("--workers=1", run.call_args_list[1].args[1])
                    self.assertTrue((root / "quality-reports/smarti-ux.json").is_file())
                with mock.patch.object(smarti_ux, "_run_json", return_value=(1, {})):
                    with self.assertRaisesRegex(smarti_ux.SmartiUXError, "collection"):
                        smarti_ux.run(root)
                with mock.patch.object(
                    smarti_ux,
                    "_run_json",
                    side_effect=[(0, report(executed=False)), (1, report())],
                ):
                    with self.assertRaisesRegex(smarti_ux.SmartiUXError, "execution"):
                        smarti_ux.run(root)

    def test_json_subprocess_and_main_return_actual_outcome(self) -> None:
        for stdout in ("not json", "[]"):
            with mock.patch.object(
                smarti_ux.subprocess, "run", return_value=mock.Mock(stdout=stdout)
            ):
                with self.assertRaises(smarti_ux.SmartiUXError):
                    smarti_ux._run_json(Path.cwd(), [])
        with mock.patch.object(
            smarti_ux.subprocess,
            "run",
            return_value=mock.Mock(stdout=json.dumps(report()), returncode=0),
        ) as run:
            self.assertEqual(smarti_ux._run_json(Path.cwd(), ["--list"]), (0, report()))
            self.assertEqual(run.call_args.kwargs["env"]["CI"], "true")
        with (
            mock.patch.object(smarti_ux, "run"),
            mock.patch.object(smarti_ux.sys, "stdout"),
        ):
            self.assertEqual(smarti_ux.main(), 0)
        with (
            mock.patch.object(
                smarti_ux, "run", side_effect=smarti_ux.SmartiUXError("blocked")
            ),
            mock.patch.object(smarti_ux.sys, "stderr"),
        ):
            self.assertEqual(smarti_ux.main(), 1)


if __name__ == "__main__":
    unittest.main()
