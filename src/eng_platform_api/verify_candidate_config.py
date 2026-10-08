"""Read-only check of the API candidate's writer, quality bundle and routing."""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

WRITER_ENV = "ENG_PLATFORM_SECRETS_WRITER_SERVICE_ACCOUNT"
EXPECTED_WRITER = (
    "eng-platform-secret-writer@cgm-assistant-prod.iam.gserviceaccount.com"
)
BUNDLE_PATH = Path(__file__).with_name("quality_executor_bundle.json")
MANIFEST_PATH = Path(__file__).with_name("release_quality_profiles.json")
TOOLING_REGISTRY = "us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo"
IMAGE_NAMES = {
    "ENG_PLATFORM_QUALITY_NODE_IMAGE": "quality-node",
    "ENG_PLATFORM_QUALITY_PYTHON_IMAGE": "quality-python",
}
CLOUD_BUILD_ONLY_ENV = "ENG_PLATFORM_CLOUD_BUILD_ONLY_SERVICES"
BASELINE_CLOUD_BUILD_ONLY_SERVICES = (
    "cgm-artemis-api",
    "cgm-artemis-job-dispatcher",
    "cgm-artemis-job-worker",
    "cgm-artemis-sync-worker",
    "cgm-artemis-clock-sync-worker",
    "cgm-artemis-data-recovery-worker",
    "cgm-artemis-fnd-ip-sync-worker",
    "cgm-artemis-fnd-observation-worker",
    "cgm-artemis-readings-export-worker",
    "cgm-artemis-smarti-prevention-worker",
    "cgm-artemis-wm-sweep-worker",
    "cgm-artemis-web",
    "cgm-artemis-mcp-worker",
)
CLOUD_BUILD_ONLY_SERVICES = BASELINE_CLOUD_BUILD_ONLY_SERVICES + (
    "cgm-bot-api",
    "communications-ms",
    "eng-platform-web",
    "cgm-sanplat-api",
    "cgm-sanplat-web",
)
PROFILE_VALIDATOR_PATH = (
    Path(__file__).resolve().parents[2] / "docker/quality-executor/quality_profiles.py"
)


def _cloud_build_only_services(row: object) -> frozenset[str]:
    """Require one literal, unambiguous list; callers check the fixed set."""
    if (
        not isinstance(row, dict)
        or set(row) != {"name", "value"}
        or row["name"] != CLOUD_BUILD_ONLY_ENV
        or not isinstance(row["value"], str)
    ):
        raise ValueError("invalid private deployment routing")
    services = row["value"].split(",")
    if len(services) != len(set(services)) or any(
        not re.fullmatch(r"[a-z][a-z0-9-]*", name) for name in services
    ):
        raise ValueError("ambiguous private deployment routing")
    return frozenset(services)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("ambiguous JSON object")
        result[name] = value
    return result


def _validate_manifest(raw: object) -> None:
    """Reuse the unchanged executor validator from the same tagged checkout."""
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"}:
        raise ValueError("invalid manifest")
    profiles = raw["profiles"]
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or not isinstance(profiles, dict)
        or not profiles
    ):
        raise ValueError("invalid manifest schema")
    spec = importlib.util.spec_from_file_location(
        "candidate_quality_profile_validator", PROFILE_VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise ValueError("source profile validator is unavailable")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name:
            raise ValueError("invalid manifest service")
        validator._validate_profile(name, profile)


def _tooling_images() -> dict[str, str]:
    """Load the reviewed source bundle, not settings from the running API."""
    bundle = json.loads(
        BUNDLE_PATH.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "tooling_source_sha",
        "manifest_sha256",
        "images",
    }:
        raise ValueError("invalid bundle")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("invalid bundle schema")
    source_sha = bundle["tooling_source_sha"]
    manifest_sha = bundle["manifest_sha256"]
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("invalid tooling source")
    if not isinstance(manifest_sha, str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest_sha
    ):
        raise ValueError("invalid manifest hash")
    manifest_bytes = MANIFEST_PATH.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha:
        raise ValueError("manifest does not match bundle")
    _validate_manifest(json.loads(manifest_bytes, object_pairs_hook=_unique_object))
    images = bundle["images"]
    if not isinstance(images, dict) or set(images) != set(IMAGE_NAMES):
        raise ValueError("invalid tooling images")
    for name, image in IMAGE_NAMES.items():
        value = images[name]
        prefix = re.escape(f"{TOOLING_REGISTRY}/{image}@sha256:")
        if not isinstance(value, str) or not re.fullmatch(
            prefix + r"[0-9a-f]{64}", value
        ):
            raise ValueError("tooling image must be a reviewed immutable digest")
    return {name: images[name] for name in IMAGE_NAMES}


def verify(project: str, region: str, revision: str) -> bool:
    """Fail closed without exposing container settings or provider diagnostics."""
    if not all(value.strip() for value in (project, region, revision)):
        return False
    try:
        required = {WRITER_ENV: EXPECTED_WRITER, **_tooling_images()}
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
        data = json.loads(result.stdout, object_pairs_hook=_unique_object)
        if data["metadata"]["name"] != revision:
            return False
        env = data["spec"]["containers"][0].get("env", [])
        if not isinstance(env, list) or not all(isinstance(item, dict) for item in env):
            return False
        for name, value in required.items():
            matches = [item for item in env if item.get("name") == name]
            if len(matches) != 1 or matches[0] != {"name": name, "value": value}:
                return False
        routing = [item for item in env if item.get("name") == CLOUD_BUILD_ONLY_ENV]
        return len(routing) == 1 and _cloud_build_only_services(routing[0]) == set(
            CLOUD_BUILD_ONLY_SERVICES
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    passed = verify(args.project, args.region, args.revision)
    print(
        "Candidate writer and quality tooling check: " + ("PASS" if passed else "FAIL")
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
