"""Exercise the actual fixed orchestration shell with a disposable fake Docker."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "docker/quality-executor/reconnections_fixture.sh"


@pytest.mark.parametrize(
    "mode,success",
    [
        ("ready", True),
        ("wrong-version", False),
        ("unready", False),
        ("start-failed", False),
    ],
)
def test_fixture_waits_and_verifies_version_or_cleans_only_owned_resources(
    tmp_path, mode, success
):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "docker.jsonl"
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,sys
args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ['FIXTURE_TEST_MODE']
if args[0]=='run' and mode=='start-failed':sys.exit(1)
if 'pg_isready' in args and mode=='unready':sys.exit(1)
if 'psql' in args:print('160001' if mode=='wrong-version' else '150019')
"""
    )
    docker.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(FIXTURE)],
        env={
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
            "CALL_LOG": str(log),
            "FIXTURE_TEST_MODE": mode,
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) == success
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    run = next(c for c in calls if c[0] == "run")
    assert "--publish" not in run and "-p" not in run and "--privileged" not in run
    assert run[run.index("--network") + 1] == "eng-platform-pg-net-123-2"
    assert run[run.index("--user") + 1] == "postgres"
    assert "--volume" not in run and "-v" not in run
    cleaned = [c for c in calls if c[0] == "rm" or c[:2] == ["network", "rm"]]
    if success:
        assert cleaned == []
        assert any("psql" in c for c in calls)
    else:
        assert ["rm", "--force", "eng-platform-pg-123-2"] in cleaned
        assert ["network", "rm", "eng-platform-pg-net-123-2"] in cleaned


@pytest.mark.parametrize(
    "run_id,image",
    [
        ("123;curl bad", "postgres@sha256:" + "a" * 64),
        ("123", "postgres:15"),
        ("", "postgres@sha256:" + "a" * 64),
    ],
)
def test_untrusted_identity_or_unpinned_image_fails_before_docker(
    tmp_path, run_id, image
):
    result = subprocess.run(
        ["/bin/bash", str(FIXTURE)],
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_RUN_ID": run_id,
            "GITHUB_RUN_ATTEMPT": "1",
            "POSTGRES_IMAGE": image,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0


def test_fixture_image_is_in_go_executor_and_proxy_keeps_isolation():
    dockerfile = (ROOT / "docker/quality-executor/Dockerfile.go").read_text()
    assert "socat" in dockerfile and "reconnections_fixture.sh" in dockerfile
    assert "test_reconnections_pg_proxy.py" in dockerfile
