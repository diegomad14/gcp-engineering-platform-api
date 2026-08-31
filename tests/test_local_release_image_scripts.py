import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE_DIR = Path(__file__).parents[1] / "scripts" / "ops" / "local-release-runner"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)


def _copy_tool(tmp_path: Path, script_name: str, manifest: dict) -> Path:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    for name in (script_name, "image-provision.sh"):
        source = SOURCE_DIR / name
        if source.exists():
            shutil.copy2(source, tool_dir / name)
    (tool_dir / "approved-artifacts.json").write_text(json.dumps(manifest))
    return tool_dir / script_name


def _run(script: Path, output: Path, fake_bin: Path, **environment: str):
    return subprocess.run(
        [str(script), str(output)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", **environment},
    )


@pytest.fixture
def build_environment(tmp_path):
    manifest = json.loads((SOURCE_DIR / "approved-artifacts.json").read_text())
    script = _copy_tool(tmp_path, "build-image.sh", manifest)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "jq",
        "[[ \"$2\" == *url* ]] && echo 'https://cloud-images.ubuntu.com/noble/20260814/base.img' || echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n",
    )
    _write_executable(
        fake_bin / "curl",
        '[[ "${CGM_FAIL_TOOL:-}" == curl ]] && exit 1\n'
        'while [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && { shift; printf base > "$1"; exit; }; shift; done\n',
    )
    _write_executable(
        fake_bin / "sha256sum",
        'if [[ "${1:-}" == --check ]]; then cat >/dev/null; exit "${CGM_BAD_SHA:-0}"; fi\n'
        "printf '%064d  %s\\n' 0 \"$1\"\n",
    )
    _write_executable(
        fake_bin / "qemu-img",
        "operation=$1; shift\n"
        '[[ "${CGM_FAIL_TOOL:-}" == "qemu-$operation" ]] && exit 1\n'
        'case "$operation" in\n'
        '  create) printf build > "${@: -2:1}" ;;\n'
        '  convert) cp "${@: -2:1}" "${@: -1}" ;;\n'
        '  check) test -s "$1" ;;\n'
        "esac\n",
    )
    for tool in ("virt-resize", "virt-customize"):
        _write_executable(
            fake_bin / tool,
            f'[[ "${{CGM_FAIL_TOOL:-}}" == {tool} ]] && exit 1\nexit 0\n',
        )
    return script, fake_bin


def test_build_publishes_only_after_success(build_environment, tmp_path):
    script, fake_bin = build_environment
    output = tmp_path / "runner.qcow2"

    result = _run(script, output, fake_bin)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert output.read_bytes() == b"build"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "IMAGE_SHA256=" in result.stdout


@pytest.mark.parametrize(
    "failure", ["curl", "virt-resize", "virt-customize", "qemu-check", "qemu-convert"]
)
def test_build_failure_never_publishes_partial_image(
    build_environment, tmp_path, failure
):
    script, fake_bin = build_environment
    output = tmp_path / "runner.qcow2"

    result = _run(script, output, fake_bin, CGM_FAIL_TOOL=failure)

    assert result.returncode != 0
    assert not output.exists()
    assert not list(tmp_path.glob("runner.qcow2.*"))


def test_build_rejects_base_image_sha_mismatch(build_environment, tmp_path):
    script, fake_bin = build_environment
    output = tmp_path / "runner.qcow2"

    result = _run(script, output, fake_bin, CGM_BAD_SHA="1")

    assert result.returncode != 0
    assert not output.exists()


@pytest.fixture
def fetch_environment(tmp_path):
    payload = b"compressed qcow2"
    digest = "sha256:" + "b" * 64
    manifest = {
        "runner_image": {
            "reference": "ghcr.io/diegomad14/cgm-release-runner-image",
            "digest": digest,
            "filename": "cgm-release-local-ubuntu-24.04-amd64.qcow2",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": "application/vnd.cgm.release-runner.qcow2",
        }
    }
    script = _copy_tool(tmp_path, "fetch-image.sh", manifest)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "oras",
        '[[ "${CGM_FAIL_ORAS:-0}" == 1 ]] && exit 1\n'
        '[[ "$1" == pull ]]\n'
        '[[ "$2" == "ghcr.io/diegomad14/cgm-release-runner-image@${CGM_EXPECTED_DIGEST}" ]]\n'
        'shift 2; [[ "$1" == --output ]]; mkdir -p "$2"\n'
        'printf \'%s\' "${CGM_PAYLOAD}" > "$2/cgm-release-local-ubuntu-24.04-amd64.qcow2"\n',
    )
    return script, fake_bin, payload, digest


def test_fetch_verifies_and_atomically_publishes(fetch_environment, tmp_path):
    script, fake_bin, payload, digest = fetch_environment
    output = tmp_path / "images" / "runner.qcow2"

    result = _run(
        script,
        output,
        fake_bin,
        CGM_EXPECTED_DIGEST=digest,
        CGM_PAYLOAD=payload.decode(),
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == payload
    assert output.stat().st_mode & 0o777 == 0o600


def test_fetch_failure_never_publishes_partial_image(fetch_environment, tmp_path):
    script, fake_bin, payload, digest = fetch_environment
    output = tmp_path / "runner.qcow2"

    result = _run(
        script,
        output,
        fake_bin,
        CGM_EXPECTED_DIGEST=digest,
        CGM_PAYLOAD=payload.decode(),
        CGM_FAIL_ORAS="1",
    )

    assert result.returncode != 0
    assert not output.exists()


def test_fetch_rejects_downloaded_sha_mismatch(fetch_environment, tmp_path):
    script, fake_bin, _, digest = fetch_environment
    output = tmp_path / "runner.qcow2"

    result = _run(
        script,
        output,
        fake_bin,
        CGM_EXPECTED_DIGEST=digest,
        CGM_PAYLOAD="tampered",
    )

    assert result.returncode != 0
    assert not output.exists()
