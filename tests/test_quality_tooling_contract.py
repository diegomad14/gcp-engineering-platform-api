"""Prevent the Python quality image from losing Semgrep's runtime dependency."""

from pathlib import Path


def test_python_quality_image_pins_pkg_resources_provider_and_smokes_semgrep():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "docker/quality-executor/Dockerfile.python"
    ).read_text(encoding="utf-8")
    assert "semgrep==1.136.0" in dockerfile
    assert "setuptools==80.9.0" in dockerfile
    assert "&& semgrep --version" in dockerfile
