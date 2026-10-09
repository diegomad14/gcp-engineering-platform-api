"""Mandatory syntax/coverage smoke for the published Go quality image."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from go_coverage import go_coverage


class NativeGoCoverageTest(unittest.TestCase):
    def test_select_communication_is_not_instrumented_but_bodies_are(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "internal/service.go"
            source.parent.mkdir()
            source.write_text(
                "package service\nfunc Check(c <-chan int) int {\n select {\n case n := <-c:\n  return n\n default:\n  return 0\n }\n}\n"
            )
            (root / "go.mod").write_text("module example.org/service\ngo 1.27.2\n")
            (root / ".quality-sources.json").write_text(
                json.dumps({"roots": ["internal"]})
            )
            helper = Path(__file__).with_name("go_executable_lines")
            positions = json.loads(
                subprocess.check_output([str(helper), str(source)], text=True)
            )
            self.assertEqual(positions[str(source)], [[3, 2], [5, 3], [7, 3]])
            report = root / "coverage.out"
            report.write_text(
                "mode: atomic\nexample.org/service/internal/service.go:3.2,3.10 1 1\nexample.org/service/internal/service.go:5.3,5.11 1 0\nexample.org/service/internal/service.go:7.3,7.11 1 1\n"
            )
            percent, lines = go_coverage(report, root, root)
            self.assertAlmostEqual(percent, 200 / 3)
            self.assertEqual(lines["internal/service.go"], {3: True, 5: False, 7: True})


if __name__ == "__main__":
    unittest.main()
