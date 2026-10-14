"""Exercise the publisher shell with fake Docker/cloud transports only."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize("valid_image", [True, False])
def test_engine_code_must_match_reviewed_source_before_push(tmp_path, valid_image):
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/publish-release-tooling.yml").read_text()
    )
    script = next(
        s["run"]
        for s in workflow["jobs"]["publish"]["steps"]
        if s.get("name") == "Build and publish immutable tooling images"
    )
    expected = hashlib.sha256(
        (root / "docker/release-executor/release_executor.py").read_bytes()
    ).hexdigest()
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "commands.jsonl"
    docker = binary / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['MOCK_COMMAND_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')\n"
        "if '/usr/bin/sha256sum' in args: print(os.environ['MOCK_ENGINE_HASH'] + '  engine')\n"
        "if args[0] == 'push': print('digest: sha256:' + 'd' * 64)\n"
    )
    docker.chmod(0o755)
    gcloud = binary / "gcloud"
    gcloud.write_text("#!/bin/sh\nexit 0\n")
    gcloud.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(binary) + ":" + os.environ["PATH"],
            "REGISTRY": "registry.example/tooling",
            "SOURCE_SHA": "a" * 40,
            "COMPONENT": "release-executor",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "MOCK_COMMAND_LOG": str(log),
            "MOCK_ENGINE_HASH": expected if valid_image else "0" * 64,
        },
    )
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    probes = [x for x in calls if "/usr/bin/sha256sum" in x]
    assert len(probes) == 1
    assert all(
        x in probes[0]
        for x in ["--network", "none", "--read-only", "--cap-drop", "ALL"]
    )
    assert not any(
        "GOOGLE_APPLICATION_CREDENTIALS" in arg for call in calls for arg in call
    )
    pushes = [x for x in calls if x[0] == "push"]
    if valid_image:
        assert result.returncode == 0, result.stderr
        assert len(pushes) == 1
        assert (
            (tmp_path / "tooling-digests")
            .read_text()
            .endswith("@sha256:" + "d" * 64 + "\n")
        )
        assert (
            f"release_executor_sha256={expected}\n"
            in (tmp_path / "tooling-source").read_text()
        )
    else:
        assert result.returncode != 0
        assert not pushes
        assert not (tmp_path / "tooling-source").exists()
