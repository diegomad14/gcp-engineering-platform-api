"""One-time administrator setup; grants only on catalog runtimes and their secrets.

Defaults to a metadata-only audit. --apply adds resource-level bindings without
changing service definitions, traffic, secret versions or existing IAM members.
"""

import argparse
import json
from pathlib import Path
import subprocess

PROJECT = "cgm-assistant-prod"
REGION = "us-central1"
MEMBER = (
    "serviceAccount:eng-platform-deployer@cgm-assistant-prod.iam.gserviceaccount.com"
)


def cloud(*args):
    completed = subprocess.run(
        ["gcloud", *args, "--project=" + PROJECT, "--quiet", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(
            "IAM bootstrap command failed; inspect the named resource permissions"
        )
    return json.loads(completed.stdout or "{}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    services = json.loads(
        (root / "src/eng_platform_api/static_examples/mock_catalog.json").read_text()
    )["services"]
    names = {"services": set(), "jobs": set()}
    for service in services:
        if service["project_id"] != PROJECT or service["region"] != REGION:
            raise SystemExit("Unexpected catalog destination")
        names["services"].update(
            [
                service["service_name"],
                *service["deployment"].get("auxiliary_services", []),
            ]
        )
        names["jobs"].update(service["deployment"].get("auxiliary_jobs", []))
    secrets, identities = set(), set()
    for kind, resources in names.items():
        for name in sorted(resources):
            resource = cloud("run", kind, "describe", name, "--region=" + REGION)
            spec = resource["spec"]["template"]["spec"]
            if kind == "jobs":
                spec = spec["template"]["spec"]
            identities.add(spec["serviceAccountName"])
            for container in spec["containers"]:
                for variable in container.get("env", []):
                    reference = variable.get("valueFrom", {}).get("secretKeyRef")
                    if reference:
                        secrets.add(reference["name"])
            if args.apply:
                cloud(
                    "run",
                    kind,
                    "add-iam-policy-binding",
                    name,
                    "--region=" + REGION,
                    "--member=" + MEMBER,
                    "--role=roles/run.developer",
                )
            print("Catalog runtime checked:", kind, name)
    for service in services:
        secrets.update(
            secret["secret_id"] for secret in service.get("operational_secrets", [])
        )
    for name in sorted(secrets):
        if "/" in name:
            raise SystemExit("Cross-project secret reference requires explicit review")
        if args.apply:
            cloud(
                "secrets",
                "add-iam-policy-binding",
                name,
                "--member=" + MEMBER,
                "--role=roles/secretmanager.viewer",
            )
    for identity in sorted(identities):
        if not (
            identity.endswith("@" + PROJECT + ".iam.gserviceaccount.com")
            or identity == "546821492326-compute@developer.gserviceaccount.com"
        ):
            raise SystemExit("Unexpected runtime identity")
        if args.apply:
            cloud(
                "iam",
                "service-accounts",
                "add-iam-policy-binding",
                identity,
                "--member=" + MEMBER,
                "--role=roles/iam.serviceAccountUser",
            )
    print(
        "Verified metadata-only secret grants:",
        len(secrets),
        "runtime identities:",
        len(identities),
        "applied:",
        args.apply,
    )


if __name__ == "__main__":
    main()
