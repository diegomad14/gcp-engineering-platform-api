"""Administrator-only import of numeric WM bootstrap metadata, never payloads."""

import argparse
from datetime import datetime, timezone
import json
import subprocess

from google.cloud import firestore
from google.oauth2.credentials import Credentials

PROJECT = "cgm-assistant-prod"
KEYS = {
    "WM_BASE_URL": "wm-base-url",
    "WM_USERNAME": "wm-username",
    "WM_PASSWORD": "wm-password",
    "WM_API_KEY": "wm-api-key",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-run", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    versions = {}
    for key, name in KEYS.items():
        response = subprocess.run(
            [
                "gcloud",
                "secrets",
                "versions",
                "list",
                name,
                "--project=" + PROJECT,
                "--format=json(name,state)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        enabled = [
            item["name"].rsplit("/", 1)[-1]
            for item in json.loads(response.stdout)
            if item["state"] == "ENABLED"
        ]
        if not enabled and key != "WM_API_KEY":
            raise SystemExit("Required WM bootstrap version is absent")
        if enabled:
            versions[key] = str(max(map(int, enabled)))
    if args.apply:
        # Capture administrative auth in memory; never print it or secret payloads.
        auth = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=True,
        )
        db = firestore.Client(
            project=PROJECT, credentials=Credentials(auth.stdout.strip())
        )
        ref = db.collection("eng_platform_service_configurations").document(
            "cgm-sanplat-api"
        )

        @firestore.transactional
        def commit(transaction):
            current = ref.get(transaction=transaction).to_dict() or {}
            if current.get("versions") or current.get("active_operation"):
                raise SystemExit(
                    "Configuration already exists; refusing to overwrite it"
                )
            record = {
                "generation": 1,
                "versions": versions,
                "applied_versions": {},
                "imported_by": "diegomad14",
                "bootstrap_run": str(args.bootstrap_run),
                "imported_at": datetime.now(timezone.utc).isoformat(),
            }
            transaction.set(ref, record)
            transaction.create(ref.collection("manifests").document("1"), record)

        commit(db.transaction())
    print("WM numeric manifest verified:", versions, "applied:", args.apply)


if __name__ == "__main__":
    main()
