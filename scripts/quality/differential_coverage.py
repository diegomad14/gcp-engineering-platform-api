"""Reproducible changed-line coverage, independent of analysis vendors."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

POLICY_VERSION = "oss-v2"


def git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def resolve_base(cwd: Path, head: str, event: dict, explicit: str = "") -> str:
    actual = git(cwd, "rev-parse", "HEAD")
    if actual != head or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("Coverage must describe the exact checked-out commit")
    if explicit:
        base = git(cwd, "rev-parse", f"{explicit}^{{commit}}")
    elif event.get("pull_request"):
        base = git(cwd, "merge-base", head, event["pull_request"]["base"]["sha"])
    else:
        before = event.get("before", "")
        if not re.fullmatch(r"[0-9a-f]{40}", before) or before == "0" * 40:
            raise ValueError(
                "An explicit base SHA is required without a PR or push base"
            )
        base = git(cwd, "rev-parse", f"{before}^{{commit}}")
    if base == head:
        raise ValueError("Coverage base cannot equal the tested commit")
    return base


def changed_lines(cwd: Path, base: str, head: str) -> dict[str, set[int]]:
    # Query one file at a time to handle spaces, unicode, renames and quoted paths.
    names = (
        subprocess.check_output(
            ["git", "diff", "--name-only", "-z", "--diff-filter=ACMR", base, head],
            cwd=cwd,
        )
        .decode()
        .split("\0")
    )
    result = {}
    for name in filter(None, names):
        diff = git(cwd, "diff", "--no-ext-diff", "--unified=0", base, head, "--", name)
        lines: set[int] = set()
        for start, count in re.findall(r"^@@ .*? \+(\d+)(?:,(\d+))? @@", diff, re.M):
            first = int(start)
            lines.update(range(first, first + int(count or "1")))
        result[name] = lines
    return result


def line_coverage(report: Path, cwd: Path, root: Path) -> dict[str, dict[int, bool]]:
    value = json.loads(report.read_text())
    if not isinstance(value, dict) or not value.get("files"):
        raise ValueError("Detailed coverage report is missing file-level evidence")
    result = {}
    for name, item in value["files"].items():
        path = (cwd / name).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("Coverage contains a path outside the repository") from exc
        executed, missing = item["executed_lines"], item["missing_lines"]
        if not all(isinstance(n, int) and n > 0 for n in executed + missing):
            raise ValueError("Invalid executable line number")
        if set(executed) & set(missing):
            raise ValueError("Conflicting line coverage")
        result[relative] = {n: True for n in executed} | {n: False for n in missing}
    return result


def lcov_coverage(report: Path, cwd: Path, root: Path) -> dict[str, dict[int, bool]]:
    result: dict[str, dict[int, bool]] = {}
    current = None
    for line in report.read_text().splitlines():
        if line.startswith("SF:"):
            if current is not None:
                raise ValueError("Incomplete LCOV record")
            current = (cwd / line[3:]).resolve().relative_to(root).as_posix()
            result.setdefault(current, {})
        elif line.startswith("DA:"):
            if current is None:
                raise ValueError("LCOV line without source file")
            number, hits, *_ = line[3:].split(",")
            if int(number) < 1 or int(hits) < 0:
                raise ValueError("Invalid LCOV line data")
            result[current][int(number)] = (
                result[current].get(int(number), False) or int(hits) > 0
            )
        elif line == "end_of_record":
            current = None
    if current is not None or not result:
        raise ValueError("Empty or incomplete LCOV report")
    return result


def differential(
    cwd: Path, report_dir: Path, profile: str, base: str, head: str
) -> dict:
    root = Path(git(cwd, "rev-parse", "--show-toplevel"))
    config = json.loads((cwd / ".quality-sources.json").read_text())
    roots, excludes = config["roots"], config.get("exclude", [])
    if not roots:
        raise ValueError("Coverage source roots are required")
    coverage = (
        line_coverage(report_dir / "coverage.json", cwd, root)
        if profile == "python"
        else lcov_coverage(report_dir / "lcov.info", cwd, root)
    )
    changed = changed_lines(root, base, head)
    total = covered = 0
    for name, lines in changed.items():
        path = root / name
        try:
            local = path.relative_to(cwd).as_posix()
        except ValueError:
            continue
        if path.suffix not in (
            {".py"} if profile == "python" else {".js", ".jsx", ".ts", ".tsx"}
        ):
            continue
        if not any(local == p or local.startswith(p.rstrip("/") + "/") for p in roots):
            continue
        if any(fnmatch.fnmatch(local, pattern) for pattern in excludes):
            continue
        if name not in coverage:
            # A source file omitted from instrumentation must never become N/A.
            raise ValueError(f"Changed source file missing from coverage: {local}")
        executable = lines & coverage[name].keys()
        total += len(executable)
        covered += sum(coverage[name][n] for n in executable)
    return {
        "policy_version": POLICY_VERSION,
        "base_sha": base,
        "changed_lines": total,
        "covered_changed_lines": covered,
        "differential_coverage": 100 * covered / total if total else None,
        "differential_threshold": 80.0,
    }
