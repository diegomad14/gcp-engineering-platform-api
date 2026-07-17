#!/usr/bin/env python3
"""Fail closed on a SonarQube Cloud quality gate without exposing its token."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _project_key(explicit: str) -> str:
    if explicit:
        return explicit
    properties = Path("sonar-project.properties")
    if properties.exists():
        for line in properties.read_text(encoding="utf-8").splitlines():
            if line.startswith("sonar.projectKey="):
                return line.partition("=")[2].strip()
    raise RuntimeError(
        "Pass --project-key or run from a repo with sonar-project.properties"
    )


def _token(secret: str, gcp_project: str) -> str:
    if value := os.getenv("SONAR_TOKEN", "").strip():
        return value
    result = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={secret}",
            f"--project={gcp_project}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Secret {secret} returned an empty value")
    return value


def _get(path: str, params: dict[str, str], token: str) -> dict:
    url = f"https://sonarcloud.io{path}?{urlencode(params)}"
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted host
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-key", default="")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pull-request")
    target.add_argument("--branch")
    parser.add_argument("--secret", default="sonarcloud-api-maintenance")
    parser.add_argument("--gcp-project", default="cgm-assistant-prod")
    args = parser.parse_args()

    try:
        project = _project_key(args.project_key)
        token = _token(args.secret, args.gcp_project)
        selector = (
            {"pullRequest": args.pull_request}
            if args.pull_request
            else {"branch": args.branch}
        )
        gate = _get(
            "/api/qualitygates/project_status",
            {"projectKey": project, **selector},
            token,
        )["projectStatus"]
        issues = _get(
            "/api/issues/search",
            {
                "componentKeys": project,
                "resolved": "false",
                "ps": "500",
                **selector,
            },
            token,
        )
    except (
        KeyError,
        RuntimeError,
        subprocess.CalledProcessError,
        HTTPError,
        URLError,
    ) as exc:
        print(f"SonarCloud validation unavailable: {exc}", file=sys.stderr)
        return 2

    status = str(gate.get("status", "NONE"))
    print(f"SonarCloud {project}: {status}; open issues: {issues.get('total', 0)}")
    for issue in issues.get("issues", []):
        component = str(issue.get("component", "")).removeprefix(f"{project}:")
        location = (
            f"{component}:{issue.get('line')}" if issue.get("line") else component
        )
        print(
            f"- {issue.get('severity', 'UNKNOWN')} {issue.get('type', 'ISSUE')} "
            f"{location} — {issue.get('message', '')}"
        )
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
