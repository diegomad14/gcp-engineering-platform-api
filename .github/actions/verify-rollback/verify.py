"""Read-only provenance verification before restoring production traffic."""

import json
import os
import re
import subprocess
import urllib.request


def main():
    url, service, revision = (
        os.environ[k] for k in ("QUALITY_URL", "QUALITY_SERVICE", "QUALITY_REVISION")
    )
    if not url.startswith("https://") or not all(
        re.fullmatch(r"[a-z0-9-]+", v) for v in (service, revision)
    ):
        raise ValueError("Invalid rollback identity")
    with urllib.request.urlopen(
        f"{url.rstrip('/')}/api/quality/services/{service}/rollback-targets/{revision}",
        timeout=20,
    ) as response:
        record = json.load(response)
    if (record["repository"], record["service_name"], record["revision"]) != (
        os.environ["GITHUB_REPOSITORY"],
        service,
        revision,
    ):
        raise ValueError("Rollback evidence identity mismatch")
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
    expected = subprocess.check_output(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            record["image"],
            "--format=value(image_summary.digest)",
        ],
        text=True,
    ).strip()
    actual = live.get("status", {}).get("imageDigest", "").rsplit("@", 1)[-1]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected) or actual != expected:
        raise ValueError("Rollback revision image does not match the recorded release")
    if (
        live.get("metadata", {}).get("labels", {}).get("serving.knative.dev/service")
        != service
    ):
        raise ValueError("Revision belongs to another service")
    print(f"Verified rollback provenance: {service} {revision}")


if __name__ == "__main__":
    main()
