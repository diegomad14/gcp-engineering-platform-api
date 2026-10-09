"""Read native Go statement coverage with trusted syntax-level line evidence."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
import subprocess

_BLOCK = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+) (\d+) (\d+)$")


def go_coverage(
    report: Path, cwd: Path, root: Path
) -> tuple[float, dict[str, dict[int, bool]]]:
    """Require every executable source statement to have native block evidence."""
    module = re.search(r"^module\s+(\S+)", (cwd / "go.mod").read_text(), re.M)
    if not module:
        raise ValueError("Go module identity is missing")
    config = json.loads((cwd / ".quality-sources.json").read_text())
    sources = []
    for path in cwd.rglob("*.go"):
        local = path.relative_to(cwd).as_posix()
        if not any(
            local == p or local.startswith(p.rstrip("/") + "/") for p in config["roots"]
        ):
            continue
        if any(fnmatch.fnmatch(local, p) for p in config.get("exclude", [])):
            continue
        sources.append(path)
    helper = Path(__file__).with_name("go_executable_lines")
    command = (
        [str(helper)]
        if helper.is_file()
        else [
            "go",
            "run",
            str(Path(__file__).with_name("go_executable_lines.go")),
            "--",
        ]
    )
    value = subprocess.check_output(
        [*command, *(str(p) for p in sources)], cwd=cwd, text=True, timeout=30
    )
    syntax = json.loads(value)
    lines = report.read_text().splitlines()
    if not lines or lines[0] not in {"mode: atomic", "mode: count", "mode: set"}:
        raise ValueError("Invalid native Go coverage mode")
    blocks: dict[str, list[tuple[tuple[int, int], tuple[int, int], int]]] = {}
    total = covered = 0
    seen = set()
    for line in lines[1:]:
        match = _BLOCK.fullmatch(line)
        if not match:
            raise ValueError("Malformed native Go coverage block")
        name, start, start_col, end, end_col, statements, hits = match.groups()
        if name.startswith(module[1] + "/"):
            path = cwd / name[len(module[1]) + 1 :]
        else:
            path = cwd / name
        path = path.resolve()
        path.relative_to(root)
        a, ac, b, bc, count, hit = map(
            int, (start, start_col, end, end_col, statements, hits)
        )
        if a < 1 or ac < 1 or b < a or bc < 1 or (a == b and bc < ac):
            raise ValueError("Invalid Go coverage block range")
        identity = (str(path), a, ac, b, bc)
        if identity in seen:
            raise ValueError("Duplicate native Go coverage block")
        seen.add(identity)
        blocks.setdefault(str(path), []).append(((a, ac), (b, bc), hit))
        total += count
        covered += count if hit else 0
    if not total:
        raise ValueError("Native Go coverage has no executable statement evidence")
    result = {}
    for path in sources:
        executable = syntax[str(path)]
        ranges = blocks.get(str(path.resolve()), [])
        coverage = {}
        for line, column in executable:
            evidence = [
                hit for start, end, hit in ranges if start <= (line, column) < end
            ]
            if not evidence:
                raise ValueError(
                    f"Go source statement missing from coverage: {path.relative_to(cwd)}:{line}"
                )
            coverage[line] = coverage.get(line, True) and any(evidence)
        result[path.resolve().relative_to(root).as_posix()] = coverage
    return 100 * covered / total, result
