#!/usr/bin/env python3
"""Validate or atomically register a local Factory or inventory YAML in the runtime catalog.

The default is a read-only check. Only --write changes the local JSON source;
this command never creates GCP resources or grants access to logs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator

import yaml
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent
from yaml.nodes import MappingNode

from eng_platform_api.services import log_catalog

MAX_PROPOSAL_BYTES = 64 * 1024
MAX_YAML_EVENTS = 4096
MAX_YAML_DEPTH = 32


class RegistrationError(ValueError):
    """A proposal cannot be safely registered."""


@dataclass(frozen=True)
class RegistrationSummary:
    total: int
    services: int
    jobs: int
    projects: int
    added: int = 1
    managed: int = 0
    observability_only: int = 0


class _UniqueSafeLoader(yaml.SafeLoader):
    """Reject ambiguous keys, YAML merges and non-string mapping keys."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise RegistrationError("YAML mapping keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def read_proposal(path: Path) -> dict:
    """Read one bounded, unambiguous factory entry without log grants."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_PROPOSAL_BYTES + 1)
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise RegistrationError("Factory proposal exceeds the 64 KiB limit")
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(raw), start=1):
            if isinstance(event, AliasEvent):
                raise RegistrationError("YAML aliases are not accepted")
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if depth > MAX_YAML_DEPTH or count > MAX_YAML_EVENTS:
                raise RegistrationError("Factory proposal is too complex")
        entry = yaml.load(raw, Loader=_UniqueSafeLoader)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise RegistrationError("Factory proposal is not valid YAML") from exc
    if not isinstance(entry, dict):
        raise RegistrationError("Expected a single factory catalog-entry mapping")
    # Missing policy is denied by default; supplied policy must also deny.
    logs = entry.get("logs", {"enabled": False, "allowed_logins": []})
    if (
        not isinstance(logs, dict)
        or set(logs) != {"enabled", "allowed_logins"}
        or logs["enabled"] is not False
        or logs["allowed_logins"] != []
    ):
        raise RegistrationError(
            "Registration cannot grant log access: require logs.enabled=false "
            "and logs.allowed_logins=[]"
        )
    entry["logs"] = {"enabled": False, "allowed_logins": []}
    return log_catalog.validate_catalog({"services": [entry]})[0]


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating writers without a persistent lock-file artifact."""
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _catalog_update(catalog_path: Path, entry: dict) -> tuple[bytes, bytes]:
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise RegistrationError(
            "Catalog must be an existing regular file, not a symlink"
        )
    with catalog_path.open("rb") as stream:
        before = stream.read(log_catalog.MAX_CATALOG_BYTES + 1)
    if len(before) > log_catalog.MAX_CATALOG_BYTES:
        raise RegistrationError("Catalog exceeds the size limit")
    # The runtime reader checks duplicate JSON keys, shape, and complete metadata.
    current = log_catalog.load_catalog(catalog_path)
    with catalog_path.open("rb") as stream:
        if stream.read(log_catalog.MAX_CATALOG_BYTES + 1) != before:
            raise RegistrationError(
                "Catalog changed during validation; rerun the check"
            )
    if any(item["service_name"] == entry["service_name"] for item in current):
        raise RegistrationError(
            f"service_name {entry['service_name']!r} already exists; "
            "names are globally unique across projects, regions and runtime kinds"
        )
    # Preserve all existing metadata exactly instead of injecting defaults or
    # rewriting log policies in unrelated entries.
    document = json.loads(before)
    document["services"].append(entry)
    log_catalog.validate_catalog(document)
    after = (
        json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(after) > log_catalog.MAX_CATALOG_BYTES:
        raise RegistrationError("Updated catalog exceeds the size limit")
    return before, after


def _atomic_write(path: Path, before: bytes, after: bytes) -> None:
    """A failed write leaves the original file intact and removes the temp file."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        # Also detect editors that do not participate in the advisory lock.
        if path.is_symlink() or path.read_bytes() != before:
            raise RegistrationError(
                "Catalog changed during registration; rerun the check"
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def register(
    proposal: Path, catalog_path: Path | None = None, *, write: bool = False
) -> RegistrationSummary:
    """Register a new identity; existing entries can never be replaced here."""
    path = catalog_path if catalog_path is not None else log_catalog.CATALOG_PATH
    entry = read_proposal(proposal)
    if write:
        with _write_lock(path):
            before, after = _catalog_update(path, entry)
            _atomic_write(path, before, after)
    else:
        _, after = _catalog_update(path, entry)
    entries = json.loads(after)["services"]
    jobs = sum(
        item["deployment"]["runtime_kind"] == "cloud_run_job" for item in entries
    )
    return RegistrationSummary(
        total=len(entries),
        services=len(entries) - jobs,
        jobs=jobs,
        projects=len({item["project_id"] for item in entries}),
        managed=sum(
            item.get("management_mode", "managed") == "managed" for item in entries
        ),
        observability_only=sum(
            item.get("management_mode") == "observability_only" for item in entries
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "proposal", type=Path, help="Generated catalog/services/<name>.yaml proposal"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Alternative local JSON catalog for offline checks/tests",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Validate only (the default)"
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Explicitly append to the local catalog atomically",
    )
    args = parser.parse_args(argv)
    try:
        summary = register(args.proposal, args.catalog, write=args.write)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Registration failed: {exc}\n")
    action = "Registered locally" if args.write else "Check passed; no files changed"
    print(action)
    print(
        f"Resulting catalog: total={summary.total} services={summary.services} "
        f"jobs={summary.jobs} projects={summary.projects} added={summary.added} "
        f"managed={summary.managed} observability_only={summary.observability_only}"
    )
    print(
        "Log access remains disabled. No GCP resource, IAM, deployment or external state changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
