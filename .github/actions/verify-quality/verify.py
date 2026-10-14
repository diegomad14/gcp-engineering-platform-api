"""Verify release evidence independently of optional branch protection."""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


# Reviewed with the twelve Artemis release catalog entries. This registry ships
# with the SHA-pinned action, never with caller source, inputs or environment.
# Repository equality remains exact; repository aliases are not authorization.
EVIDENCE_OWNERS = {
    ("diegomad14/cgm-artemis-api", service): "cgm-artemis-api"
    for service in (
        "cgm-artemis-job-dispatcher",
        "cgm-artemis-job-worker",
        "cgm-artemis-sync-worker",
        "cgm-artemis-clock-sync-worker",
        "cgm-artemis-data-recovery-worker",
        "cgm-artemis-fnd-ip-sync-worker",
        "cgm-artemis-fnd-observation-worker",
        "cgm-artemis-readings-export-worker",
        "cgm-artemis-smarti-prevention-worker",
        "cgm-artemis-wm-sweep-worker",
        "cgm-artemis-mcp-worker",
    )
}


def verify_release_tag(tag: str, sha: str) -> None:
    """Quality is published before tagging; bind the tag through the checkout."""
    if not re.fullmatch(
        r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", tag
    ):
        raise ValueError("A semantic release tag is required")
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], text=True
    ).strip()
    if resolved != sha:
        raise ValueError("Release tag does not match the evidence SHA")


def validate(report: dict, service: str, repository: str, sha: str) -> None:
    owner = EVIDENCE_OWNERS.get((repository, service), service)
    if (
        report.get("service_name"),
        report.get("repository"),
        report.get("commit_sha"),
    ) != (owner, repository, sha):
        raise ValueError("Evidence identity does not match the release")
    if (
        report.get("policy_version") != "oss-v2"
        or report.get("quality_gate_status") != "PASSED"
    ):
        raise ValueError("Release requires a passed oss-v2 report")
    age = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    ).total_seconds()
    if age < -300 or age > 168 * 3600:
        raise ValueError("Quality evidence is stale or has an invalid timestamp")
    if not report.get("checks") or any(
        c["status"] == "FAILED" for c in report["checks"]
    ):
        raise ValueError("Quality checks did not pass")


def main() -> None:
    url, service, sha = (
        os.environ[k] for k in ("QUALITY_URL", "QUALITY_SERVICE", "QUALITY_SHA")
    )
    if (
        not url.startswith("https://")
        or not re.fullmatch(r"[a-z0-9-]+", service)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
    ):
        raise ValueError("HTTPS API URL, service and full SHA are required")
    tag = os.environ.get("QUALITY_TAG", "")
    if tag:
        verify_release_tag(tag, sha)
    deadline = time.monotonic() + min(
        1800, max(0, int(os.environ.get("QUALITY_WAIT", "0")))
    )
    endpoint = f"{url.rstrip('/')}/api/quality/services/{service}/commits/{sha}?for_release=true"
    while True:
        try:
            with urllib.request.urlopen(endpoint, timeout=20) as response:
                report = json.load(response)
            validate(report, service, os.environ["GITHUB_REPOSITORY"], sha)
            print(f"Verified oss-v2 evidence: {service} {sha}")
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or time.monotonic() >= deadline:
                raise ValueError(
                    f"Quality service rejected release evidence (HTTP {exc.code})"
                ) from exc
        time.sleep(min(15, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
