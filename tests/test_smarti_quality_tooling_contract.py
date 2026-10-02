"""Image and unit-test contract, without pretending to launch a local browser."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_node_image_installs_versioned_browsers_before_untrusted_commands():
    dockerfile = (ROOT / "docker/quality-executor/Dockerfile.node").read_text()
    assert "ARG PLAYWRIGHT_VERSION=1.62.1" in dockerfile
    assert (
        "PLAYWRIGHT_BROWSERS_PATH=/opt/eng-platform/playwright-browsers" in dockerfile
    )
    assert "playwright@${PLAYWRIGHT_VERSION}" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "chromium-1234" in dockerfile
    assert "chromium_headless_shell-1234" in dockerfile
    assert "chmod a+r,a-w" in dockerfile
    assert dockerfile.index("playwright install") < dockerfile.index("COPY src/")
    assert "smarti_ux.py" in dockerfile
    assert "test_smarti_ux.py test_smarti_pg.py" in dockerfile
    runtime = (ROOT / "docker/quality-executor/untrusted_command.py").read_text()
    assert "65532" in runtime
    assert (
        "test_smarti_pg.py"
        in (ROOT / "docker/quality-executor/Dockerfile.python").read_text()
    )


def test_smarti_trusted_checkers_are_also_run_by_repository_ci():
    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "test_smarti_ux.py",
            "test_smarti_pg.py",
            "test_quality_executor.MandatorySmartiEvidenceTest",
            "test_quality_executor.QualityIsolationTest.test_postgres_profile_passes_all_disposable_loopback_dsns",
            "test_quality_executor.QualityIsolationTest.test_postgres_profile_rejects_missing_or_non_loopback_dsn",
            "test_quality_executor.QualityIsolationTest.test_smarti_postgres_url_is_required_and_fail_closed",
            "test_quality_executor.QualityIsolationTest.test_postgres_child_environment_propagates_smarti_without_pg_overrides",
            "test_quality_executor.QualityIsolationTest.test_smarti_browser_environment_uses_only_the_immutable_image_path",
            "test_quality_executor.QualityIsolationTest.test_failed_smarti_extra_is_blocking_without_changing_its_category",
        ],
        cwd=ROOT / "docker/quality-executor",
        check=True,
        capture_output=True,
        text=True,
    )
