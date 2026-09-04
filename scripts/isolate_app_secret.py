"""One-time admin migration; excludes only the new App key from shared readers.

Preserves every other binding and uses the policy etag for an atomic update.
Does not access secret payloads. Dry-run unless --apply is supplied.
"""

import argparse
import json
import subprocess

PROJECT = "cgm-assistant-prod"
RESOURCE = "projects/546821492326/secrets/eng-platform-github-private-key"
SHARED = {
    "serviceAccount:546821492326-compute@developer.gserviceaccount.com",
    "serviceAccount:cgm-sanplat-runtime@cgm-assistant-prod.iam.gserviceaccount.com",
}
TITLE = "exclude-engine-github-app-key"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    policy = json.loads(
        subprocess.check_output(
            [
                "gcloud",
                "projects",
                "get-iam-policy",
                PROJECT,
                "--format=json",
            ],
            text=True,
        )
    )
    assert policy.get("etag"), "Missing concurrency token"
    found = set()
    for binding in policy["bindings"]:
        if binding["role"] != "roles/secretmanager.secretAccessor":
            continue
        members = set(binding["members"])
        if not members.intersection(SHARED):
            continue
        assert members == SHARED, "Unexpected shared-reader membership; review manually"
        assert (
            not binding.get("condition") or binding["condition"].get("title") == TITLE
        ), "Unexpected existing condition"
        binding["condition"] = {
            "title": TITLE,
            "description": "Shared application runtimes retain existing secrets but cannot read the engine App key",
            "expression": f"resource.name != '{RESOURCE}' && !resource.name.startsWith('{RESOURCE}/')",
        }
        found.update(members)
    assert found == SHARED, "Expected shared-reader bindings were not found"
    policy["version"] = 3
    if args.apply:
        subprocess.run(
            [
                "gcloud",
                "projects",
                "set-iam-policy",
                PROJECT,
                "/dev/stdin",
                "--quiet",
                "--format=value(etag)",
            ],
            input=json.dumps(policy),
            text=True,
            check=True,
        )
    print("Applied" if args.apply else "Validated dry-run", TITLE)


if __name__ == "__main__":
    main()
