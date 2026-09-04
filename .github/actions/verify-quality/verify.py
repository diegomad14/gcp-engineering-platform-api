"""Verify release evidence independently of optional branch protection."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def validate(report: dict, service: str, repository: str, sha: str) -> None:
    if (
        report.get("service_name"),
        report.get("repository"),
        report.get("commit_sha"),
    ) != (service, repository, sha):
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
