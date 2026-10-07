"""Real pytest execution preserves outcomes; exported data is allowlisted."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from eng_platform_api import quality_evidence as evidence
from tests.test_quality_evidence import observation

ROOT = Path(__file__).parents[1]


@pytest.fixture
def exporter(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "src/eng_platform_api"))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/quality"))
    import export_evidence

    return export_evidence


@pytest.fixture
def artifacts(tmp_path):
    root = tmp_path / "repo"
    reports = root / "quality-reports"
    reports.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/example.py").write_text("x = 1\n")
    value = observation()
    coverage = {
        "files": {
            str(root / "src/example.py"): {
                "executed_lines": [1],
                "missing_lines": [],
                "excluded_lines": [],
                "contexts": {"private": "must-not-escape"},
            }
        },
        "meta": {"private": "must-not-escape"},
    }
    (reports / "coverage.json").write_text(json.dumps(coverage))
    manifest = {
        key: value[key]
        for key in (
            "schema_version",
            "tests",
            "dependencies",
            "runtime",
            "measurement_ms",
        )
    }
    (reports / "test-manifest.json").write_text(json.dumps(manifest))
    return {
        "root": root,
        "report_directory": reports,
        "tracked_paths": {"src/example.py"},
        "source_tree": "a" * 40,
        "config_sha256": "b" * 64,
        "fixture_sha256": "c" * 64,
        "command": "python -m pytest",
        "runtime": "python",
    }


def test_export_strips_absolute_paths_and_unapproved_metadata(exporter, artifacts):
    result = exporter.export_observation(**artifacts)
    assert result["state"] == "observed"
    encoded = evidence.canonical(result).decode()
    assert str(artifacts["root"]) not in encoded
    assert "must-not-escape" not in encoded
    assert "python -m pytest" not in encoded
    assert result["coverage"] == {
        "src/example.py": {"executed": [1], "missing": [], "excluded": []}
    }


@pytest.mark.parametrize(
    "filename,reason",
    [
        ("coverage.json", "missing_coverage"),
        ("test-manifest.json", "missing_test_manifest"),
    ],
)
def test_missing_artifacts_are_explicit(exporter, artifacts, filename, reason):
    (artifacts["report_directory"] / filename).unlink()
    assert exporter.export_observation(**artifacts)["reason"] == reason


@pytest.mark.parametrize(
    "kind",
    ["outside", "untracked", "symlink", "parent_symlink", "traversal", "duplicate"],
)
def test_coverage_cannot_escape_tracked_checkout(exporter, artifacts, kind, tmp_path):
    root = artifacts["root"]
    safe = {"executed_lines": [1], "missing_lines": [], "excluded_lines": []}
    name = str(root / "src/example.py")
    if kind == "outside":
        name = str(tmp_path / "private.py")
    elif kind == "untracked":
        name = "src/other.py"
    elif kind == "symlink":
        (root / "src/example.py").unlink()
        (root / "src/example.py").symlink_to(tmp_path / "private.py")
    elif kind == "parent_symlink":
        (root / "src/example.py").unlink()
        (root / "src").rmdir()
        (root / "src").symlink_to(tmp_path)
    elif kind == "traversal":
        name = "src/../private.py"
    files = {name: safe}
    if kind == "duplicate":
        files["src/example.py"] = safe
    (artifacts["report_directory"] / "coverage.json").write_text(
        json.dumps({"files": files})
    )
    assert exporter.export_observation(**artifacts)["state"] == "unavailable"


def test_unsafe_report_files_rejected_without_reading(exporter, tmp_path):
    regular = tmp_path / "regular"
    regular.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(regular)
    with pytest.raises(OSError):
        exporter.read_json_file(link, 100)
    hardlink = tmp_path / "hardlink"
    os.link(regular, hardlink)
    with pytest.raises(exporter.EvidenceError):
        exporter.read_json_file(hardlink, 100)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(exporter.EvidenceError):
        exporter.read_json_file(fifo, 100)
    big = tmp_path / "big"
    big.write_text("x" * 101)
    with pytest.raises(exporter.EvidenceError):
        exporter.read_json_file(big, 100)


def test_real_pytest_collection_outcomes_and_exit_unchanged(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_fake.py").write_text("""import pytest
@pytest.mark.parametrize("value", ["do-not-export@example.invalid"])
def test_pass(value):
    assert value

def test_fail():
    assert False, "do-not-export-secret-text"

@pytest.mark.skip(reason="do-not-export-sensitive-skip")
def test_skip():
    pass

@pytest.mark.xfail(reason="do-not-export-sensitive-xfail")
def test_expected_failure():
    assert False
""")
    env = {"PATH": os.environ["PATH"], "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    manifest = tmp_path / "manifest.json"
    env.update(
        {
            "PYTHONPATH": str(ROOT / "scripts/quality"),
            "PYTEST_PLUGINS": "pytest_evidence",
            "ENG_PLATFORM_TEST_MANIFEST": str(manifest),
        }
    )
    observed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert baseline.returncode == observed.returncode == 1
    assert "1 failed, 1 passed, 1 skipped, 1 xfailed" in baseline.stdout
    assert "1 failed, 1 passed, 1 skipped, 1 xfailed" in observed.stdout
    result = json.loads(manifest.read_text())
    assert result["tests"]["collected"] == 4
    assert result["tests"]["exit_code"] == 1
    assert all(len(item[0]) == 64 for item in result["tests"]["items"])
    assert "do-not-export" not in manifest.read_text()
    assert result["measurement_ms"] < 10_000


def test_dependency_digest_changes_with_actual_bytes(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "pytest_evidence_fixture", ROOT / "scripts/quality/pytest_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "fake.py").write_text("old")

    class Distribution:
        files = ["fake.py"]
        metadata = {"Name": "fake-package"}
        version = "1.0"

        def locate_file(self, path):
            return tmp_path / path

    monkeypatch.setattr(
        module.importlib.metadata, "distributions", lambda: [Distribution()]
    )
    first = module._dependencies()
    (tmp_path / "fake.py").write_text("new")
    second = module._dependencies()
    assert first["complete"] and second["complete"]
    assert first["sha256"] != second["sha256"]
    Distribution.files = ["../private", "fake.py"]
    assert module._dependencies()["complete"] is False
    Distribution.files = ["fake.py"]
    monkeypatch.setattr(module, "MAX_DEPENDENCY_BYTES", 1)
    assert module._dependencies()["complete"] is False


def test_nested_pytest_does_not_replace_parent_manifest(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_child.py").write_text("def test_child():\n    assert True\n")
    (tmp_path / "test_parent.py").write_text("""import subprocess
import sys

def test_runs_child():
    subprocess.run([sys.executable, "-m", "pytest", "-q", "child/test_child.py"], check=True)

def test_parent_second():
    assert True
""")
    manifest = tmp_path / "manifest.json"
    env = {
        "PATH": os.environ["PATH"],
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(ROOT / "scripts/quality"),
        "PYTEST_PLUGINS": "pytest_evidence",
        "ENG_PLATFORM_TEST_MANIFEST": str(manifest),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_parent.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    assert json.loads(manifest.read_text())["tests"]["collected"] == 2


def test_nested_in_process_pytest_preserves_parent_and_marks_incomplete(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_child.py").write_text("def test_child():\n    assert True\n")
    (tmp_path / "test_parent.py").write_text("""import pytest

def test_runs_child():
    assert pytest.main(["-q", "child/test_child.py"]) == 0

def test_parent_second():
    assert True
""")
    manifest = tmp_path / "manifest.json"
    env = {
        "PATH": os.environ["PATH"],
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(ROOT / "scripts/quality"),
        "PYTEST_PLUGINS": "pytest_evidence",
        "ENG_PLATFORM_TEST_MANIFEST": str(manifest),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_parent.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    observed = json.loads(manifest.read_text())["tests"]
    assert observed["collected"] == 2
    assert observed["complete"] is False


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_manifest_schema_requires_exact_integer(exporter, artifacts, version):
    path = artifacts["report_directory"] / "test-manifest.json"
    value = json.loads(path.read_text())
    value["schema_version"] = version
    path.write_text(json.dumps(value))
    assert exporter.export_observation(**artifacts)["reason"] == "invalid_test_manifest"
