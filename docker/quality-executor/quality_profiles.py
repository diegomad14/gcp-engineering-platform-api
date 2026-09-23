"""Load and verify the server-owned quality profile manifest.

The API and the executor both consume the same JSON document. An execution is
accepted only when the hash authorized by the API describes the exact profile
baked into this image.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any


_IMAGE_PROFILE_PATH = Path("/opt/eng-platform/release_quality_profiles.json")
_SOURCE_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "eng_platform_api"
    / "release_quality_profiles.json"
)
_COMMAND_NAMES = {"install", "tests", "build", "lint", "format", "typecheck"}
_PROFILE_FIELDS = {
    "runtime",
    "working_directory",
    "coverage_threshold",
    "timeout_seconds",
    "container_smoke",
    "postgres",
    "commands",
    "extra",
}
_EXTRA_FIELDS = {"name", "category", "command", "blocking"}


class QualityProfileError(RuntimeError):
    """The baked profile manifest is missing or does not match its contract."""


def _profile_path() -> Path:
    override = os.environ.get("ENG_PLATFORM_RELEASE_PROFILE_PATH", "")
    if override:
        return Path(override)
    if _IMAGE_PROFILE_PATH.is_file():
        return _IMAGE_PROFILE_PATH
    return _SOURCE_PROFILE_PATH


def _relative_directory(value: Any, *, service: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualityProfileError(f"{service}: working_directory must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise QualityProfileError(
            f"{service}: working_directory must remain inside the checkout"
        )
    return value


def _validate_profile(service: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _PROFILE_FIELDS:
        raise QualityProfileError(
            f"{service}: profile fields must be exactly {sorted(_PROFILE_FIELDS)}"
        )
    runtime = raw.get("runtime")
    if runtime not in {"node", "python"}:
        raise QualityProfileError(f"{service}: unsupported runtime {runtime!r}")
    _relative_directory(raw.get("working_directory"), service=service)
    threshold = raw.get("coverage_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise QualityProfileError(f"{service}: invalid coverage threshold")
    if not 0 <= float(threshold) <= 100:
        raise QualityProfileError(f"{service}: invalid coverage threshold")
    timeout = raw.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise QualityProfileError(f"{service}: invalid timeout")
    for flag in ("container_smoke", "postgres"):
        if not isinstance(raw.get(flag), bool):
            raise QualityProfileError(f"{service}: {flag} must be boolean")
    commands = raw.get("commands")
    if not isinstance(commands, dict) or set(commands) != _COMMAND_NAMES:
        raise QualityProfileError(
            f"{service}: commands must be exactly {sorted(_COMMAND_NAMES)}"
        )
    if not all(isinstance(command, str) for command in commands.values()):
        raise QualityProfileError(f"{service}: commands must be strings")
    extras = raw.get("extra")
    if not isinstance(extras, list):
        raise QualityProfileError(f"{service}: extra checks must be a list")
    for index, extra in enumerate(extras):
        if not isinstance(extra, dict) or set(extra) != _EXTRA_FIELDS:
            raise QualityProfileError(
                f"{service}: extra[{index}] fields must be exactly "
                f"{sorted(_EXTRA_FIELDS)}"
            )
        if not all(
            isinstance(extra.get(field), str)
            for field in ("name", "category", "command")
        ):
            raise QualityProfileError(f"{service}: invalid extra[{index}]")
        if not extra["name"] or not extra["category"] or not extra["command"]:
            raise QualityProfileError(f"{service}: invalid empty extra[{index}]")
        if not isinstance(extra.get("blocking"), bool):
            raise QualityProfileError(
                f"{service}: extra[{index}].blocking must be boolean"
            )
    return copy.deepcopy(raw)


@lru_cache(maxsize=1)
def profile_document() -> dict[str, Any]:
    path = _profile_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityProfileError(
            f"Unable to load quality profiles from {path}"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"}:
        raise QualityProfileError("Invalid quality profile document")
    if raw.get("schema_version") != 1 or not isinstance(raw.get("profiles"), dict):
        raise QualityProfileError("Unsupported quality profile schema")
    if not raw["profiles"]:
        raise QualityProfileError("Quality profile document is empty")
    profiles = {
        service: _validate_profile(service, profile)
        for service, profile in raw["profiles"].items()
        if isinstance(service, str) and service
    }
    if len(profiles) != len(raw["profiles"]):
        raise QualityProfileError("Quality profile service names must be strings")
    return {"schema_version": 1, "profiles": profiles}


def available_services() -> tuple[str, ...]:
    return tuple(sorted(profile_document()["profiles"]))


def profile_for(service: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(profile_document()["profiles"][service])
    except KeyError as exc:
        raise QualityProfileError(f"No quality profile for {service!r}") from exc


def profile_payload(service: str) -> dict[str, Any]:
    document = profile_document()
    return {
        "schema_version": document["schema_version"],
        "service_name": service,
        "profile": profile_for(service),
    }


def profile_hash(service: str) -> str:
    encoded = json.dumps(
        profile_payload(service),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_profile_hash(service: str, expected: str) -> dict[str, Any]:
    actual = profile_hash(service)
    if not expected or not hmac.compare_digest(actual, expected):
        raise QualityProfileError(
            "Authorized profile hash does not match the commands baked into the image"
        )
    return profile_for(service)
