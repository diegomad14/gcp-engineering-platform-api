"""Activate only the approved catalog pins through the qualified GitHub workflow."""

import copy
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse

import requests

from scripts import pin_reconnections_catalog as policy
from scripts import reconnections_catalog_preflight as preflight

APPROVAL_SHA = "409c6a7c83d8e051bf7cbff6683f6469ac793320a013d5ce71968199356ae287"
CANDIDATE_SHA = "b82cdad9d13a881b25b777d5f9f0cb76f909c17e9cb11e823805b13d88da3c1d"
NAME = "projects/cgm-assistant-prod/locations/us-central1/services/eng-platform-api"
API = "https://run.googleapis.com/v2/"
REVISION = "eng-platform-api-cfg-" + CANDIDATE_SHA[:12]
TAG = "catalog-" + CANDIDATE_SHA[:12]
REVISION_TYPE = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
OLD_REVISION = "eng-platform-api-cfg-7a0d21c78e34"


class CloudError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Existing cloud identity operation rejected")


def checkpoint(audit):
    path = os.environ.get("CGM_ACTIVATION_REPORT")
    if path:
        Path(path).write_text(json.dumps(audit, indent=2) + "\n")


def approved_audit():
    raw = (
        Path(__file__)
        .with_name("reconnections_catalog_approved_audit.json")
        .read_bytes()
    )
    if hashlib.sha256(raw).hexdigest() != APPROVAL_SHA:
        raise ValueError("reviewed audit checksum mismatch")
    result = json.loads(raw)
    if (
        result["status"] != "read_only_preflight_passed"
        or result["candidate_sha256"] != CANDIDATE_SHA
        or result["mutation_performed"]
    ):
        raise ValueError("reviewed preflight mismatch")
    return result


def env_values(template):
    containers = template.get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != preflight.BASE_IMAGE:
        raise ValueError("reviewed immutable image required")
    entries = containers[0].get("env", [])
    if len({row["name"] for row in entries}) != len(entries):
        raise ValueError("duplicate environment variable")
    return {row["name"]: row.get("value") for row in entries}


def traffic_status(service):
    rows = service.get("trafficStatuses", [])
    if service.get("reconciling") or not service.get("etag") or not rows:
        raise ValueError("stable service and etag required")
    targets = [
        {
            "type": REVISION_TYPE,
            "revision": row["revision"].rsplit("/", 1)[-1],
            "percent": row.get("percent", 0),
            **({"tag": row["tag"]} if row.get("tag") else {}),
        }
        for row in rows
    ]
    if (
        any(not row["revision"] or row["percent"] < 0 for row in targets)
        or sum(row["percent"] for row in targets) != 100
    ):
        raise ValueError("complete concrete traffic allocation required")
    return sorted(targets, key=lambda row: (row["revision"], row.get("tag", "")))


def prepare_candidate(service):
    if service.get("name") != NAME:
        raise ValueError("service identity mismatch")
    if any(
        service.get(key, "").rsplit("/", 1)[-1] != OLD_REVISION
        for key in ("latestReadyRevision", "latestCreatedRevision")
    ):
        raise ValueError(
            "a separate configuration candidate exists; inspect before writing"
        )
    original = service["template"]
    values = env_values(original)
    if (
        values.get("ENG_PLATFORM_CATALOG_SHA256") != preflight.BASE_SHA
        or values.get("ENG_PLATFORM_CATALOG_PATH") != preflight.BASE_PATH
        or service.get("buildConfig")
    ):
        raise ValueError("reviewed source required; builds forbidden")
    previous = traffic_status(service)
    if [row["revision"] for row in previous if row["percent"]] != [OLD_REVISION]:
        raise ValueError("reviewed previous serving revision required")
    if any(row.get("tag") == TAG or row["revision"] == REVISION for row in previous):
        raise ValueError("candidate already exists; inspect prior attempt")
    template = copy.deepcopy(original)
    template["revision"] = REVISION
    for row in template["containers"][0]["env"]:
        if row["name"] == "ENG_PLATFORM_CATALOG_SHA256":
            row["value"] = CANDIDATE_SHA
        elif row["name"] == "ENG_PLATFORM_CATALOG_PATH":
            row["value"] = preflight.BASE_PATH.replace(
                preflight.BASE_SHA, CANDIDATE_SHA
            )
    targets = previous + [
        {"type": REVISION_TYPE, "revision": REVISION, "percent": 0, "tag": TAG}
    ]
    return (
        template,
        previous,
        sorted(targets, key=lambda row: (row["revision"], row.get("tag", ""))),
    )


def template_identity(template):
    result = copy.deepcopy(template)
    result["revision"] = result.get("revision", "").rsplit("/", 1)[-1]
    for container in result.get("containers", []):
        container["env"] = sorted(container.get("env", []), key=lambda row: row["name"])
    return result


def assert_candidate(service, template, targets):
    if service.get("name") != NAME or template_identity(
        service["template"]
    ) != template_identity(template):
        raise ValueError("candidate configuration changed")
    if traffic_status(service) != targets:
        raise ValueError("candidate traffic changed")
    if any(
        service.get(key, "").rsplit("/", 1)[-1] != REVISION
        for key in ("latestReadyRevision", "latestCreatedRevision")
    ):
        raise ValueError("exact candidate revision readiness required")


class ExistingCloud:
    def __init__(self, session, http=None):
        self.session = session
        # Public readiness probes never receive the Google access credential.
        self.http = http if http is not None else requests.Session()

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=20, **kwargs)
        if not 200 <= response.status_code < 300:
            raise CloudError(response.status_code)
        return response.json()

    def service(self):
        return self.request("GET", API + NAME)

    def update(self, service, fields):
        operation = self.request(
            "PATCH", API + NAME, params={"updateMask": fields}, json=service
        )
        deadline = time.monotonic() + 180
        while not operation.get("done"):
            if time.monotonic() >= deadline:
                raise TimeoutError("Cloud Run update outcome uncertain")
            time.sleep(2)
            operation = self.request("GET", API + operation["name"])
        if operation.get("error"):
            raise CloudError(operation["error"].get("code"))
        return self.stable_service()

    def stable_service(self):
        deadline = time.monotonic() + 120
        while True:
            service = self.service()
            if not service.get("reconciling"):
                return service
            if time.monotonic() >= deadline:
                raise TimeoutError("Cloud Run reconciliation outcome uncertain")
            time.sleep(2)

    def smoke(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".run.app"):
            raise ValueError("existing HTTPS Cloud Run URI required")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("unambiguous HTTPS Cloud Run URI required")
        for path, predicate in (
            ("/health", lambda d: d.get("status") == "ok" and not d.get("mock_mode")),
            ("/ready", lambda d: d.get("status") == "ready"),
        ):
            response = self.http.get(
                url.rstrip("/") + path, timeout=20, allow_redirects=False
            )
            if response.status_code != 200 or len(response.content) > 32768:
                raise CloudError(response.status_code)
            if not predicate(response.json()):
                raise ValueError("health or exact catalog policy smoke failed")


def activate(cloud, upload, audit):
    original = cloud.service()
    template, previous, targets = prepare_candidate(original)
    audit["previous_traffic"] = previous
    audit["candidate_revision"] = REVISION
    audit["stage"] = "create_immutable_catalog_object"
    checkpoint(audit)
    upload()
    audit["immutable_object_verified"] = True
    # A fresh etag check after object creation prevents racing a separate release.
    current = cloud.service()
    if current != original:
        raise ValueError("service changed since preparation; no update attempted")
    audit["stage"] = "create_zero_traffic_candidate"
    checkpoint(audit)
    candidate = cloud.update(
        {
            "name": NAME,
            "etag": current["etag"],
            "template": template,
            "traffic": targets,
        },
        "template,traffic",
    )
    assert_candidate(candidate, template, targets)
    audit["candidate_zero_traffic_verified"] = True
    urls = [row["uri"] for row in candidate["trafficStatuses"] if row.get("tag") == TAG]
    expected_host = TAG + "---" + urllib.parse.urlsplit(candidate["uri"]).netloc
    if len(urls) != 1 or urllib.parse.urlsplit(urls[0]).netloc != expected_host:
        raise ValueError("exact candidate tag URI required")
    audit["stage"] = "candidate_smoke"
    cloud.smoke(urls[0])
    audit["candidate_smoke_passed"] = True
    current = cloud.service()
    assert_candidate(current, template, targets)
    promoted = [
        dict(row, percent=100 if row["revision"] == REVISION else 0) for row in targets
    ]
    audit["stage"] = "promote_with_current_etag"
    checkpoint(audit)
    # Once promotion starts, any failed verification must restore the prior traffic.
    try:
        live = cloud.update(
            {"name": NAME, "etag": current["etag"], "traffic": promoted}, "traffic"
        )
        assert_candidate(live, template, promoted)
        cloud.smoke(live["uri"])
    except Exception:
        audit["stage"] = "rollback_previous_serving_reference"
        checkpoint(audit)
        actual = cloud.stable_service()
        if template_identity(actual["template"]) != template_identity(
            template
        ) or traffic_status(actual) not in (targets, promoted):
            raise ValueError(
                "concurrent configuration change; rollback requires review"
            ) from None
        if traffic_status(actual) == promoted:
            restored = cloud.update(
                {"name": NAME, "etag": actual["etag"], "traffic": previous}, "traffic"
            )
            if traffic_status(restored) != previous:
                raise ValueError("previous traffic restoration not verified") from None
        else:
            audit["promotion_not_applied_previous_traffic_preserved"] = True
        audit["rollback_verified"] = True
        raise RuntimeError(
            "Promotion verification failed; previous serving reference restored"
        ) from None
    audit.update(
        status="promoted_verified",
        stage="completed",
        production_catalog_reference_verified=True,
    )


def main():
    path = Path(os.environ["CGM_ACTIVATION_REPORT"])
    audit = {
        "status": "blocked",
        "stage": "verify_reviewed_preflight",
        "candidate_sha256": CANDIDATE_SHA,
    }
    try:
        reviewed = approved_audit()
        preflight.main()
        current = json.loads(Path(os.environ["CGM_PREFLIGHT_REPORT"]).read_text())
        if current != reviewed:
            raise ValueError("current source or permissions differ from reviewed audit")
        runtime = json.loads(
            preflight.read_command(
                "gcloud",
                "run",
                "services",
                "describe",
                preflight.SERVICE,
                "--project",
                preflight.PROJECT,
                "--region",
                preflight.REGION,
                "--format=json",
            )
        )
        bucket, key = preflight.source_identity(runtime)
        raw = preflight.read_command(
            "gcloud",
            "storage",
            "cat",
            "gs://" + bucket + "/" + key + "#" + reviewed["source_generation"],
        )
        candidate, proof = policy.prepare(raw, preflight.BASE_SHA)
        if (
            proof["candidate_sha256"] != CANDIDATE_SHA
            or proof["exact_byte_edits"] != reviewed["exact_byte_edits"]
        ):
            raise ValueError("approved candidate bytes changed")
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import storage
        from google.api_core.exceptions import PreconditionFailed

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        cloud = ExistingCloud(AuthorizedSession(credentials))
        blob = (
            storage.Client(project=preflight.PROJECT, credentials=credentials)
            .bucket(bucket)
            .blob(
                key.removesuffix("sha256-" + preflight.BASE_SHA + ".json")
                + "sha256-"
                + CANDIDATE_SHA
                + ".json"
            )
        )

        def upload():
            try:
                blob.upload_from_string(
                    candidate,
                    content_type="application/json",
                    if_generation_match=0,
                    timeout=30,
                )
            except PreconditionFailed:
                pass  # Existing immutable object is accepted only after exact-byte verification.
            blob.reload(timeout=20)
            stored = blob.download_as_bytes(
                if_generation_match=blob.generation, timeout=30
            )
            if stored != candidate:
                raise ValueError("existing hash-addressed catalog differs")
            audit["candidate_object_generation"] = str(blob.generation)

        activate(cloud, upload, audit)
    except Exception as error:
        if isinstance(error, CloudError):
            audit["http_status"] = error.status
        elif isinstance(getattr(error, "code", None), int):
            audit["http_status"] = int(error.code)
        audit["error_type"] = type(error).__name__
        raise RuntimeError(
            "Scoped catalog activation stopped; inspect bounded audit before any retry"
        ) from None
    finally:
        path.write_text(json.dumps(audit, indent=2) + "\n")


if __name__ == "__main__":
    main()
