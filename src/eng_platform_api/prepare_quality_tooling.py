"""Coordinate reviewed quality pins and private deployment routing."""

import copy
import json
import subprocess

from eng_platform_api import verify_candidate_config as candidate

SERVICE = "eng-platform-api"
PROJECT = "cgm-assistant-prod"
REGION = "us-central1"
CLIENT_ANNOTATIONS = {
    "run.googleapis.com/client-name",
    "run.googleapis.com/client-version",
    "run.googleapis.com/operation-id",
    "serving.knative.dev/creator",
    "serving.knative.dev/lastModifier",
}


def _cloud(arguments: list[str], timeout: int = 60) -> dict:
    result = subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            *arguments,
            SERVICE,
            f"--project={PROJECT}",
            f"--region={REGION}",
            "--format=json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    data = json.loads(result.stdout, object_pairs_hook=candidate._unique_object)
    if not isinstance(data, dict):
        raise ValueError("invalid service response")
    return data


def _environment(data: dict) -> dict[str, dict]:
    containers = data["spec"]["template"]["spec"]["containers"]
    if not isinstance(containers, list) or len(containers) != 1:
        raise ValueError("ambiguous controller container")
    env = containers[0].get("env", [])
    if not isinstance(env, list):
        raise ValueError("invalid environment")
    result = {}
    for row in env:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row["name"] in result
        ):
            raise ValueError("ambiguous environment")
        result[row["name"]] = row
    return result


def _configuration(data: dict) -> dict:
    """Compare all config except the two pins and the fixed routing field."""
    if data["metadata"]["name"] != SERVICE:
        raise ValueError("wrong service")
    env = _environment(data)
    writer = env.get(candidate.WRITER_ENV)
    if writer != {
        "name": candidate.WRITER_ENV,
        "value": candidate.EXPECTED_WRITER,
    }:
        raise ValueError("unexpected writer")
    spec = copy.deepcopy(data["spec"])
    spec.pop("traffic", None)
    template_meta = spec["template"].setdefault("metadata", {})
    template_meta.pop("name", None)
    for key in CLIENT_ANNOTATIONS:
        template_meta.get("annotations", {}).pop(key, None)
    # Gcloud regenerates only this client revision nonce on an update.
    labels = template_meta.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError("invalid template labels")
    labels.pop("client.knative.dev/nonce", None)
    if not labels:
        template_meta.pop("labels", None)
    spec["template"]["spec"]["containers"][0]["env"] = {
        name: row
        for name, row in env.items()
        if name not in {*candidate.IMAGE_NAMES, candidate.CLOUD_BUILD_ONLY_ENV}
    }
    metadata = data["metadata"]
    annotations = copy.deepcopy(metadata.get("annotations", {}))
    for key in CLIENT_ANNOTATIONS:
        annotations.pop(key, None)
    return {
        "name": metadata["name"],
        "namespace": metadata.get("namespace"),
        "labels": metadata.get("labels", {}),
        "annotations": annotations,
        "spec": spec,
    }


def _traffic(data: dict) -> tuple[dict[str, int], dict[str, str]]:
    """Resolve LATEST without confusing representation changes with traffic."""
    status = data["status"]
    if not any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in status["conditions"]
    ):
        raise ValueError("controller is not ready")
    if status.get("observedGeneration") != data["metadata"]["generation"]:
        raise ValueError("unreconciled controller")
    entries = status["traffic"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("missing traffic")
    percentages: dict[str, int] = {}
    tags: dict[str, str] = {}
    for entry in entries:
        revision = entry.get("revisionName")
        if not revision and entry.get("latestRevision") is True:
            revision = status["latestReadyRevisionName"]
        percent = entry.get("percent", 0)
        if (
            not isinstance(revision, str)
            or not revision.startswith(SERVICE + "-")
            or type(percent) is not int
            or not 0 <= percent <= 100
        ):
            raise ValueError("invalid traffic")
        if percent:
            percentages[revision] = percentages.get(revision, 0) + percent
        if "tag" in entry:
            tag = entry["tag"]
            if not isinstance(tag, str) or not tag or tag in tags:
                raise ValueError("ambiguous traffic tag")
            tags[tag] = revision
    if sum(percentages.values()) != 100:
        raise ValueError("incomplete traffic")
    return percentages, tags


def prepare() -> bool:
    """Only reviewed pins and fixed routing may change; never print cloud data."""
    try:
        images = candidate._tooling_images()
        before = _cloud(["describe"])
        configuration = _configuration(before)
        traffic = _traffic(before)
        environment = _environment(before)
        routing = candidate._cloud_build_only_services(
            environment.get(candidate.CLOUD_BUILD_ONLY_ENV)
        )
        if routing not in (
            frozenset(candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES),
            frozenset(candidate.CLOUD_BUILD_ONLY_SERVICES),
        ):
            return False
        values = {
            **images,
            candidate.CLOUD_BUILD_ONLY_ENV: (
                environment[candidate.CLOUD_BUILD_ONLY_ENV]["value"]
                if routing == set(candidate.CLOUD_BUILD_ONLY_SERVICES)
                else ",".join(candidate.CLOUD_BUILD_ONLY_SERVICES)
            ),
        }
        desired = {
            name: {"name": name, "value": value} for name, value in values.items()
        }
        if all(environment.get(name) == row for name, row in desired.items()):
            return True
        _cloud(
            [
                "update",
                "--no-traffic",
                "--quiet",
                # These reviewed values cannot contain '|'. Gcloud's alternate
                # delimiter keeps the comma-separated service list one value.
                "--update-env-vars=^|^"
                + "|".join(f"{name}={value}" for name, value in values.items()),
            ],
            timeout=600,
        )
        after = _cloud(["describe"])
        return (
            _configuration(after) == configuration
            and _traffic(after) == traffic
            and all(
                _environment(after).get(name) == row for name, row in desired.items()
            )
        )
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        ImportError,
        RuntimeError,
        SyntaxError,
        OverflowError,
    ):
        return False


def main() -> int:
    passed = prepare()
    print("Coordinated quality tooling preparation: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
