"""Exercise real subprocess failures and live evidence from the portable gate."""

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts/quality"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "gate_output", scripts / "quality_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def python_command(code):
    return shlex.join([sys.executable, "-c", code])


def test_progress_is_saved_before_the_process_exits(runner, tmp_path, capsys):
    log = tmp_path / "reports" / "tests.log"
    # Child waits for its own first line to appear in the log. Buffered-only
    # collection fails this handshake without relying on a timing assertion.
    code = (
        "from pathlib import Path; import time\n"
        "print('suite started', flush=True)\n"
        f"log = Path({str(log)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while 'suite started' not in log.read_text():\n"
        "    assert time.monotonic() < deadline, 'progress was buffered'\n"
        "    time.sleep(0.01)\n"
        "print('suite finished')\n"
    )
    result = runner._run(python_command(code), tmp_path, log)
    assert result["returncode"] == 0
    assert log.read_text() == result["output"] == "suite started\nsuite finished\n"
    assert "suite finished" in capsys.readouterr().out


def test_full_log_survives_report_tail_limit_and_command_failure(runner, tmp_path):
    log = tmp_path / "tests.log"
    result = runner._run(
        python_command(
            "import sys; print('x' * 13000); print('failed', file=sys.stderr); sys.exit(7)"
        ),
        tmp_path,
        log,
    )
    assert result["returncode"] == 7
    assert result["output"] == log.read_text()[-12000:]
    assert len(log.read_text()) > 13000
    assert "failed" in result["output"]


def test_pipefail_and_no_log_file(runner, tmp_path):
    result = runner._run("false | cat", tmp_path)
    assert result["returncode"] != 0
    assert result["skipped"] is False


def test_empty_command_remains_skipped(runner, tmp_path):
    log = tmp_path / "unused.log"
    result = runner._run(" ", tmp_path, log)
    assert result["skipped"] is True
    assert result["duration"] == 0
    assert not log.exists()
