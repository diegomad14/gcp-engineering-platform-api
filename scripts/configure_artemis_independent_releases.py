#!/usr/bin/env python3
"""Review or apply executor routing and exact Scheduler destinations.

Only the three routing variables and Scheduler URI are patched. Runtime IAM,
secrets, commands, schedules, authentication and paused states are preserved.
Run after publishing independent activation support, before migrating jobs.
"""

import argparse
import json
import subprocess
from pathlib import Path

PROJECT = "cgm-assistant-prod"
REGION = "us-central1"
REPOSITORY = f"projects/{PROJECT}/locations/{REGION}/connections/eng-platform-github/repositories/cgm-artemis-api"
SCHEDULES = {
    "cgm-artemis-fnd-observation-five-minutes": "cgm-artemis-fnd-observation-worker",
    "cgm-artemis-readings-export-five-minutes": "cgm-artemis-readings-export-worker",
    "cgm-artemis-smarti-prevention-daytime": "cgm-artemis-smarti-prevention-worker",
    "cgm-artemis-wm-sweep-hour": "cgm-artemis-wm-sweep-worker",
    "cgm-artemis-wm-sweep-half": "cgm-artemis-wm-sweep-worker",
}


def job_uri(job):
    if job not in SCHEDULES.values():
        raise ValueError("Job is not in the Artemis allowlist")
    return f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job}:run"


def command(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True
    ).stdout.strip()


def routing_variables(current, resources):
    result = {}
    for suffix in ("ENABLED_SERVICES", "ONLY_SERVICES"):
        key = "ENG_PLATFORM_CLOUD_BUILD_" + suffix
        result[key] = ",".join(
            sorted(set(filter(None, current.get(key, "").split(","))) | set(resources))
        )
    key = "ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON"
    repositories = json.loads(current.get(key, "{}"))
    repositories.update({resource: REPOSITORY for resource in resources})
    result[key] = json.dumps(repositories, separators=(",", ":"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--routing-only", action="store_true")
    phases.add_argument("--schedulers-only", action="store_true")
    args = parser.parse_args()
    catalog = json.loads(
        (
            Path(__file__).parents[1]
            / "src/eng_platform_api/static_examples/mock_catalog.json"
        ).read_text()
    )
    resources = [
        row["service_name"]
        for row in catalog["services"]
        if row["repository"] == "diegomad14/cgm-artemis-api"
    ]
    if len(resources) != 12:
        raise ValueError("Expected all twelve Artemis backend resources")
    live = json.loads(
        command(
            "gcloud",
            "run",
            "services",
            "describe",
            "eng-platform-api",
            "--project",
            PROJECT,
            "--region",
            REGION,
            "--format=json",
        )
    )
    current = {
        item["name"]: item.get("value", "")
        for item in live["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    variables = routing_variables(current, resources)
    print(
        json.dumps({"resources": resources, "routing_variables": variables}, indent=2)
    )
    if args.apply and not args.schedulers_only:
        updates = "^|^" + "|".join(f"{key}={value}" for key, value in variables.items())
        command(
            "gcloud",
            "run",
            "services",
            "update",
            "eng-platform-api",
            "--project",
            PROJECT,
            "--region",
            REGION,
            "--update-env-vars",
            updates,
            "--quiet",
        )
    if args.routing_only:
        return
    for schedule, job in SCHEDULES.items():
        base = ("--project", PROJECT, "--location", REGION)
        before = json.loads(
            command(
                "gcloud",
                "scheduler",
                "jobs",
                "describe",
                schedule,
                *base,
                "--format=json",
            )
        )
        if before["httpTarget"]["httpMethod"] != "POST" or not before["httpTarget"].get(
            "oauthToken"
        ):
            raise ValueError(
                "Unexpected Scheduler authentication or method: " + schedule
            )
        if (
            schedule
            in {
                "cgm-artemis-readings-export-five-minutes",
                "cgm-artemis-smarti-prevention-daytime",
            }
            and before["state"] != "PAUSED"
        ):
            raise ValueError("Expected paused Scheduler: " + schedule)
        target = job_uri(job)
        print(
            json.dumps({"scheduler": schedule, "state": before["state"], "uri": target})
        )
        if args.apply:
            command(
                "gcloud",
                "scheduler",
                "jobs",
                "update",
                "http",
                schedule,
                *base,
                "--uri",
                target,
                "--quiet",
            )
            after = json.loads(
                command(
                    "gcloud",
                    "scheduler",
                    "jobs",
                    "describe",
                    schedule,
                    *base,
                    "--format=json",
                )
            )
            for field in ("state", "schedule", "timeZone"):
                if after[field] != before[field]:
                    raise RuntimeError(
                        "Scheduler operational state changed: " + schedule
                    )
            for field in ("oauthToken", "httpMethod", "body"):
                if after["httpTarget"].get(field) != before["httpTarget"].get(field):
                    raise RuntimeError(
                        "Scheduler request configuration changed: " + schedule
                    )
            if after["httpTarget"]["uri"] != target:
                raise RuntimeError("Scheduler URI verification failed: " + schedule)


if __name__ == "__main__":
    main()
