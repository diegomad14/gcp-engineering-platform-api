"""Read-only artifact identity verification before promotion."""

import json
import os
import re
import subprocess
import urllib.request


def verify_digest(live: dict, expected: str, service: str) -> None:
    actual = live.get("status", {}).get("imageDigest", "").rsplit("@", 1)[-1]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or actual != expected:
        raise ValueError("Candidate image does not match the approved release")
    if (
        live.get("metadata", {}).get("labels", {}).get("serving.knative.dev/service")
        != service
    ):
        raise ValueError("Candidate belongs to another service")


def main() -> None:
    url, service, revision, tag, sha = (
        os.environ[k]
        for k in (
            "QUALITY_URL",
            "QUALITY_SERVICE",
            "QUALITY_REVISION",
            "QUALITY_TAG",
            "QUALITY_SHA",
        )
    )
    if not url.startswith("https://") or not all(
        re.fullmatch(r"[a-z0-9-]+", v) for v in (service, revision)
    ):
        raise ValueError("Invalid candidate identity")
    if not re.fullmatch(
        r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", tag
    ):
        raise ValueError("Invalid release tag")
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or resolved != sha:
        raise ValueError("Tag does not identify the approved source commit")
    with urllib.request.urlopen(
        f"{url.rstrip('/')}/api/catalog/services/{service}", timeout=20
    ) as response:
        record = json.load(response)
    if (record["repository"], record["service_name"]) != (
        os.environ["GITHUB_REPOSITORY"],
        service,
    ):
        raise ValueError("Candidate catalog identity mismatch")
    deployment = record["deployment"]
    image = f"{record['region']}-docker.pkg.dev/{record['project_id']}/{deployment['artifact_repository']}/{deployment['image_name']}:{tag}"
    live = json.loads(
        subprocess.check_output(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                revision,
                "--project",
                record["project_id"],
                "--region",
                record["region"],
                "--format=json",
            ],
            text=True,
        )
    )
    digest = subprocess.check_output(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--format=value(image_summary.digest)",
        ],
        text=True,
    ).strip()
    verify_digest(live, digest, service)
    print(f"Verified candidate image: {service} {revision} {sha}")


if __name__ == "__main__":
    main()
