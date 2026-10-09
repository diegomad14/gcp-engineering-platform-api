"""Prevent the Python quality image from losing Semgrep's runtime dependency."""

import os
from pathlib import Path
import subprocess
import textwrap
import pytest


def test_python_quality_image_pins_pkg_resources_provider_and_smokes_semgrep():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "docker/quality-executor/Dockerfile.python"
    ).read_text(encoding="utf-8")
    assert "semgrep==1.136.0" in dockerfile
    assert "setuptools==80.9.0" in dockerfile
    assert "&& semgrep --version" in dockerfile


def test_publisher_smokes_python_runtime_before_any_image_push():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/publish-release-tooling.yml"
    ).read_text(encoding="utf-8")
    build = workflow.index('docker build --pull --file "$dockerfile"')
    python_branch = workflow.index('if [[ "$name" == quality-python ]]')
    smoke = workflow.index(
        'python3 docker/quality-executor/smoke_python_isolation.py "$tag"'
    )
    push = workflow.index('docker push "$tag"')
    assert build < python_branch < smoke < push
    assert "set -euo pipefail" in workflow


@pytest.mark.parametrize("component", ["quality-python", "quality-go"])
def test_failed_runtime_smoke_stops_publisher_before_push(tmp_path, component):
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/publish-release-tooling.yml"
    ).read_text(encoding="utf-8")
    publisher = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls"
    for name, exit_code in (("docker", 0), ("gcloud", 0), ("python3", 73)):
        executable = binaries / name
        executable.write_text(
            '#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$PUBLISHER_CALLS"\n'
            f"exit {exit_code}\n"
        )
        executable.chmod(0o700)
    completed = subprocess.run(
        ["/bin/bash", "-c", publisher],
        env={
            **os.environ,
            "PATH": f"{binaries}:/usr/bin:/bin",
            "PUBLISHER_CALLS": str(calls),
            "REGISTRY": "example.invalid/tooling",
            "SOURCE_SHA": "a" * 40,
            "COMPONENT": component,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 73, completed.stderr
    executed = calls.read_text()
    runtime = component.removeprefix("quality-")
    assert (
        f"docker build --pull --file docker/quality-executor/Dockerfile.{runtime}"
        in executed
    )
    assert f"python3 docker/quality-executor/smoke_{runtime}_isolation.py" in executed
    assert "docker push" not in executed


def test_go_publisher_smoke_uses_existing_volume_without_caller_changes():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/publish-release-tooling.yml").read_text()
    assert 'if [[ "$name" == quality-go ]]' in workflow
    assert 'python3 docker/quality-executor/smoke_go_isolation.py "$tag"' in workflow
    probe = (root / "docker/quality-executor/smoke_go_isolation.py").read_text()
    assert "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=6g" in probe
    assert 'f"{volume}:{OUTPUT}:rw"' in probe
    assert (
        "sealed manifest writable" in probe and "sealed manifest replaceable" in probe
    )
    assert "_remove_go_temporary_directory" in probe


def test_go_supervisor_root_exception_is_path_scoped_in_both_trivy_policies():
    """Root seals scanner evidence; repository commands still run as UID 65532."""
    import yaml

    root = Path(__file__).parents[1]
    for name in (".trivyignore.yaml", "docker/quality-executor/trivyignore.yaml"):
        policy = yaml.safe_load((root / name).read_text())
        rows = [
            row
            for row in policy["misconfigurations"]
            if "docker/quality-executor/Dockerfile.go" in row["paths"]
        ]
        assert len(rows) == 1
        assert rows[0]["id"] == "AVD-DS-0002"
        assert set(rows[0]["paths"]) == {
            "docker/quality-executor/Dockerfile.python",
            "docker/quality-executor/Dockerfile.node",
            "docker/quality-executor/Dockerfile.go",
        }
        assert "UID 65532" in rows[0]["statement"]
        assert "read-only" in rows[0]["statement"]
    dockerfile = (root / "docker/quality-executor/Dockerfile.go").read_text()
    assert (
        'ENTRYPOINT ["python3", "/opt/eng-platform/quality_executor.py"]' in dockerfile
    )
    assert "chmod 0444" in dockerfile and "trivyignore.yaml" in dockerfile
