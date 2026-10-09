"""Private source transport never persists credentials into the quality gate."""

from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).parents[1]


def test_private_pr_fetch_uses_ephemeral_read_only_header_and_clears_before_checkout(
    tmp_path,
):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/eng-platform-quality.yml").read_text()
    )
    assert workflow["permissions"]["contents"] == "read"
    steps = workflow["jobs"]["quality"]["steps"]
    fetch = next(
        step for step in steps if step.get("name", "").startswith("Fetch exact PR")
    )
    assert fetch["env"]["SOURCE_TOKEN"] == "${{ github.token }}"
    script = fetch["run"]
    assert 'GIT_CONFIG_VALUE_0="Authorization: Basic $authorization"' in script
    assert "http.followRedirects GIT_CONFIG_VALUE_1=false" in script
    assert script.index(
        "unset SOURCE_TOKEN authorization", script.index("git init")
    ) < script.index("checkout --quiet --detach")
    assert "git config" not in script
    shell = tmp_path / "fetch.sh"
    shell.write_text(script)
    subprocess.run(["bash", "-n", str(shell)], check=True)
    gate = next(
        step
        for step in steps
        if step.get("name") == "Run credential-free quality engine"
    )
    assert "SOURCE_TOKEN" not in gate["env"]
    assert "GITHUB_TOKEN" not in gate["env"]
    assert "--env SOURCE_TOKEN" not in gate["run"]


def test_private_fetch_credentials_exist_only_for_git_fetch(tmp_path):
    import json
    import os
    import sys

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/eng-platform-quality.yml").read_text()
    )
    fetch = next(
        step
        for step in workflow["jobs"]["quality"]["steps"]
        if step.get("name", "").startswith("Fetch exact PR")
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
row = {"args": sys.argv[1:], "token": os.environ.get("SOURCE_TOKEN"), "header": os.environ.get("GIT_CONFIG_VALUE_0")}
with Path(os.environ["FETCH_TRACE"]).open("a") as handle:
    handle.write(json.dumps(row) + "\\n")
if "rev-parse" in sys.argv: print(os.environ["SOURCE_SHA"])
"""
    )
    git.chmod(0o755)
    log = tmp_path / "trace.jsonl"
    result = subprocess.run(
        ["bash", "-euc", fetch["run"]],
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "FETCH_TRACE": str(log),
            "SOURCE_TOKEN": "private-test-token",
            "SOURCE_SHA": "a" * 40,
            "BASE_SHA": "b" * 40,
            "SOURCE_REPOSITORY": "owner/private",
            "GITHUB_REPOSITORY": "owner/private",
            "GITHUB_WORKSPACE": str(tmp_path / "source"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private-test-token" not in result.stdout
    trace = [json.loads(line) for line in log.read_text().splitlines()]
    fetches = [row for row in trace if "fetch" in row["args"]]
    assert len(fetches) == 2
    assert all(
        row["token"] and row["header"].startswith("Authorization: Basic ")
        for row in fetches
    )
    checkout = next(row for row in trace if "checkout" in row["args"])
    assert checkout["token"] is None and checkout["header"] is None


def test_quality_and_publication_allow_dedicated_reader_identities_with_legacy_compatibility():
    for name, prefix in (
        ("eng-platform-quality.yml", "QUALITY"),
        ("eng-platform-release.yml", "PUBLICATION"),
    ):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        auth = next(
            step
            for step in next(iter(workflow["jobs"].values()))["steps"]
            if step.get("uses", "").startswith("google-github-actions/auth@")
        )
        assert (
            auth["with"]["workload_identity_provider"]
            == "${{ vars.GCP_"
            + prefix
            + "_WIF_PROVIDER || vars.GCP_RELEASE_WIF_PROVIDER }}"
        )
        assert (
            auth["with"]["service_account"]
            == "${{ vars.GCP_"
            + prefix
            + "_WIF_SERVICE_ACCOUNT || vars.GCP_WIF_SERVICE_ACCOUNT }}"
        )
