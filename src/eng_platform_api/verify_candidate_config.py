"""Read-only release check for the exact API candidate's secrets writer."""

import argparse
import json
import subprocess

WRITER_ENV = "ENG_PLATFORM_SECRETS_WRITER_SERVICE_ACCOUNT"
EXPECTED_WRITER = (
    "eng-platform-secret-writer@cgm-assistant-prod.iam.gserviceaccount.com"
)


def verify(project: str, region: str, revision: str) -> bool:
    """Fail closed without exposing container settings or provider diagnostics."""
    if not all(value.strip() for value in (project, region, revision)):
        return False
    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                revision,
                f"--project={project}",
                f"--region={region}",
                "--format=json",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        data = json.loads(result.stdout)
        if data["metadata"]["name"] != revision:
            return False
        env = data["spec"]["containers"][0].get("env", [])
        matches = [item for item in env if item.get("name") == WRITER_ENV]
        return len(matches) == 1 and matches[0] == {
            "name": WRITER_ENV,
            "value": EXPECTED_WRITER,
        }
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    passed = verify(args.project, args.region, args.revision)
    print("Candidate secrets writer check: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
