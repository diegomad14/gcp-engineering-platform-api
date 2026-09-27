#!/usr/bin/env python3
"""Record an externally deployed Artemis service after live provenance checks.

Adds an immutable-identity deployment event; never rewrites historical attempts.
Requires operator IAM to read Run/Registry and append to the deployment store.
"""

import argparse
import hashlib
import json
import subprocess
import urllib.request
from datetime import datetime, timezone

from eng_platform_api.models import DeploymentItem
from eng_platform_api.services import catalog, deployment_store


def command(*args):
    return subprocess.run(
        args, text=True, capture_output=True, check=True
    ).stdout.strip()


def verify(service, tag, platform_url):
    args = (
        "--project",
        service.project_id,
        "--region",
        service.region,
        "--format=json",
    )
    live = json.loads(
        command("gcloud", "run", "services", "describe", service.service_name, *args)
    )
    serving = [row for row in live["status"]["traffic"] if row.get("percent", 0)]
    if len(serving) != 1 or serving[0]["percent"] != 100:
        raise ValueError("External registration requires one fully serving revision")
    revision = serving[0]["revisionName"]
    runtime = json.loads(
        command("gcloud", "run", "revisions", "describe", revision, *args)
    )
    container = runtime["spec"]["containers"][0]
    values = {item["name"]: item.get("value", "") for item in container.get("env", [])}
    sha = values.get("APP_RELEASE_SHA", "")
    expected = command(
        "gh", "api", f"repos/{service.repository}/commits/{tag}", "--jq", ".sha"
    )
    if len(sha) != 40 or sha != expected:
        raise ValueError("Serving runtime does not match the exact release tag")
    image = runtime.get("status", {}).get("imageDigest", "") or container["image"]
    if "@sha256:" not in image:
        raise ValueError("Serving runtime image is not immutable")
    command("docker", "pull", image)
    labels = json.loads(
        command("docker", "inspect", "--format", "{{json .Config.Labels}}", image)
    )
    if (
        labels.get("org.opencontainers.image.revision") != sha
        or labels.get("org.opencontainers.image.source") != service.repository
    ):
        raise ValueError(
            "Image provenance does not match the catalog repository and commit"
        )
    endpoint = f"{platform_url.rstrip('/')}/api/quality/services/{service.service_name}/commits/{sha}?for_release=true"
    with urllib.request.urlopen(endpoint, timeout=30) as response:
        evidence = json.load(response)
    if (
        evidence.get("commit_sha") != sha
        or evidence.get("repository") != service.repository
        or evidence.get("policy_version") != "oss-v2"
        or evidence.get("quality_gate_status") != "PASSED"
    ):
        raise ValueError("Exact passed oss-v2 evidence is required")
    return revision, sha, image, endpoint, live["status"]["url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    service = catalog.get_service(args.service)
    if (
        not service
        or service.repository != "diegomad14/cgm-artemis-api"
        or service.deployment.runtime_kind != "cloud_run_service"
    ):
        raise ValueError("Expected a catalogued Artemis backend service")
    revision, sha, image, evidence, url = verify(service, args.tag, args.platform_url)
    identity = hashlib.sha256(
        f"{service.service_name}:{revision}:{sha}:{image}".encode()
    ).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    item = DeploymentItem(
        id="external-" + identity,
        service_name=service.service_name,
        repository=service.repository,
        tag=args.tag,
        sha=sha,
        status="SUCCEEDED",
        current_stage="external-verified",
        production_revision=revision,
        production_url=url,
        requested_by=args.actor,
        created_at=now,
        updated_at=now,
        origin="external_verified",
        reason=args.reason,
        image_digest=image,
        evidence_url=evidence,
    )
    existing = deployment_store.get(item.id)
    if existing:
        print(json.dumps({"id": existing.id, "status": "already_recorded"}))
        return
    print(item.model_dump_json(indent=2))
    if args.apply:
        deployment_store.save(item, item.id)


if __name__ == "__main__":
    main()
