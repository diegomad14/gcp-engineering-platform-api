#!/usr/bin/env python3
"""Guarded lifecycle operations for the local-first release engine.

The module extends phase 1 with phase 2 publication, phase 3 direct
candidate/promote/rollback operations, a phase 4 SanPlat coordination contract,
and a phase 5 adoption plan. Every remote effect requires both ``--execute``
and an explicit confirmation flag. Without them the module only calculates and
records a plan.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:  # Works both as a package import and when local_release.py is a script.
    from . import local_release
except ImportError:  # pragma: no cover - exercised by the executable wrapper.
    import local_release  # type: ignore[no-redef]


REMOTE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
RELEASE_STATUSES = {"candidate", "promoted", "rolled_back"}
SANPLAT_SERVICES = frozenset({"cgm-sanplat-api", "cgm-sanplat-web"})
LIVE_REMOTE_OPERATIONS = frozenset(
    {"publish", "candidate", "register", "promote", "rollback"}
)


class LifecycleError(local_release.ReleaseError):
    """A lifecycle precondition or reconciliaton failure."""


def load_manifest(path: Path) -> dict[str, Any]:
    value = local_release.read_json(path.resolve())
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != local_release.SCHEMA_VERSION
    ):
        raise LifecycleError(f"Invalid local release manifest: {path}")
    if not value.get("release_id") or not value.get("service_name"):
        raise LifecycleError("Manifest has no release identity")
    return value


def _source(manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise LifecycleError("Manifest has no source identity")
    return source


def _artifact(manifest: dict[str, Any]) -> dict[str, Any]:
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise LifecycleError("Manifest has no artifact identity")
    return artifact


def _deployment(manifest: dict[str, Any]) -> dict[str, Any]:
    catalog = manifest.get("catalog") or {}
    deployment = catalog.get("deployment") if isinstance(catalog, dict) else None
    if not isinstance(deployment, dict):
        raise LifecycleError("Manifest has no deployment configuration")
    return deployment


def _runtime(manifest: dict[str, Any]) -> dict[str, Any]:
    runtime = manifest.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        raise LifecycleError("Manifest runtime state is invalid")
    return runtime


def _require_remote_identity(manifest: dict[str, Any]) -> None:
    source = _source(manifest)
    if source.get("dirty") or not source.get("publishable", False):
        raise LifecycleError("Remote operations require a clean, publishable SHA")
    quality = manifest.get("quality") or {}
    if quality.get("status") != "PASSED":
        raise LifecycleError("Remote operations require passed oss-v2 evidence")
    tag = (manifest.get("version") or {}).get("tag", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise LifecycleError("Manifest has no valid release tag")
    if not local_release.SEMVER_RE.fullmatch(tag):
        raise LifecycleError(f"Manifest tag is not SemVer: {tag}")


def _require_digest(manifest: dict[str, Any]) -> str:
    digest = str(_artifact(manifest).get("digest", ""))
    normalized = digest.removeprefix("sha256:")
    if not SHA256_RE.fullmatch(normalized):
        raise LifecycleError("Manifest has no verified Artifact Registry digest")
    return f"sha256:{normalized}"


def _image_with_digest(manifest: dict[str, Any]) -> str:
    image = str(_artifact(manifest).get("image_reference", ""))
    if not image:
        raise LifecycleError("Manifest has no Artifact Registry image reference")
    return f"{image.split('@', 1)[0].split(':', 1)[0]}@{_require_digest(manifest)}"


def _candidate_tag(manifest: dict[str, Any]) -> str:
    tag = str((manifest.get("version") or {}).get("tag", ""))
    return re.sub(r"[^A-Za-z0-9-]", "-", f"candidate-{tag}")


def _service_name(manifest: dict[str, Any]) -> str:
    name = str(manifest.get("service_name", ""))
    if not name:
        raise LifecycleError("Manifest has no service name")
    return name


def live_control_plan(manifest: dict[str, Any], operation: str) -> dict[str, Any]:
    """Describe controls required before any local remote mutation.

    ``main`` currently coordinates Actions runs and platform-issued release
    authorizations. The local engine has no shared lease or authorization
    adapter yet, so the honest plan is blocked rather than treating a local
    file lock and a confirmation string as equivalent controls.
    """
    dependencies = manifest.get("dependencies") or {}
    inventory = (
        dependencies.get("workflow_inventory") if isinstance(dependencies, dict) else {}
    )
    inventory = inventory if isinstance(inventory, dict) else {}
    release_workflows = [
        item.get("path", "")
        for item in inventory.get("files", [])
        if isinstance(item, dict) and item.get("release")
    ]
    service_name = str(manifest.get("service_name", ""))
    sanplat_generic = service_name in SANPLAT_SERVICES and operation in {
        "candidate",
        "promote",
        "rollback",
    }
    return {
        "status": "BLOCKED",
        "operation": operation,
        "shared_exclusion": {
            "cli_cli": {
                "status": "BLOCKED",
                "configured": False,
                "required": "durable lease shared by independent CLI processes and hosts",
            },
            "cli_actions": {
                "status": "BLOCKED",
                "configured": False,
                "required": "platform lease coordinated with Actions concurrency and dispatch",
                "release_workflows_detected": release_workflows,
            },
        },
        "prior_authorization": {
            "status": "BLOCKED",
            "configured": False,
            "required": "platform-issued authorization bound to release_id, repository, source_sha, digest and operation",
        },
        "sanplat_generic_bypass": {
            "status": "BLOCKED" if sanplat_generic else "NOT_APPLICABLE",
            "service": service_name,
            "operation": operation,
            "adapter_required": sanplat_generic,
        },
        "reason": (
            "No reviewed local shared-exclusion and authorization adapter is "
            "configured; dry-run remains available and Actions protections stay authoritative."
        ),
    }


def _require_live_controls(manifest: dict[str, Any], operation: str) -> None:
    """Fail closed until local execution can coordinate with platform controls."""
    if operation not in LIVE_REMOTE_OPERATIONS:
        raise LifecycleError(f"Unsupported live operation: {operation}")
    service_name = _service_name(manifest)
    if service_name in SANPLAT_SERVICES and operation in {
        "candidate",
        "promote",
        "rollback",
    }:
        raise LifecycleError(
            "SanPlat generic lifecycle commands are blocked; use the reviewed "
            "SanPlat adapter and corporate-window record"
        )
    raise LifecycleError(
        f"Live {operation} is blocked: shared CLI/CLI and CLI/Actions exclusion "
        "plus prior platform authorization are not configured"
    )


def _project_region(manifest: dict[str, Any]) -> tuple[str, str]:
    catalog = manifest.get("catalog") or {}
    if not isinstance(catalog, dict):
        raise LifecycleError("Manifest catalog is invalid")
    project = str(catalog.get("project_id", ""))
    region = str(catalog.get("region", ""))
    if not project or not region:
        raise LifecycleError("Manifest has no project or region")
    return project, region


def _record_path(state_dir: Path, release_id: str, execution_id: str) -> Path:
    return local_release.state_paths(state_dir)["executions"] / f"{execution_id}.json"


def begin_execution(
    manifest: dict[str, Any], state_dir: Path, phase: str, *, dry_run: bool
) -> tuple[dict[str, Any], Path]:
    execution_id = str(uuid.uuid4())
    attempt = local_release.next_attempt(state_dir, str(manifest["release_id"]))
    execution = {
        "schema_version": local_release.SCHEMA_VERSION,
        "execution_id": execution_id,
        "release_id": manifest["release_id"],
        "attempt": attempt,
        "created_at": local_release.iso(local_release.utc_now()),
        "phase": phase,
        "status": "PLANNED" if dry_run else "RUNNING",
        "current_stage": phase,
        "events": [],
        "unknown_effects": [],
        "remote_effects": [],
        "dry_run": dry_run,
    }
    path = _record_path(state_dir, str(manifest["release_id"]), execution_id)
    local_release.write_json(path, execution)
    return execution, path


def save_execution(path: Path, execution: dict[str, Any]) -> None:
    execution["updated_at"] = local_release.iso(local_release.utc_now())
    local_release.write_json(path, execution)


def event(
    execution: dict[str, Any],
    *,
    stage: str,
    intent: str,
    result: str,
    remote_effect: bool = False,
    command: list[str] | None = None,
    detail: str = "",
) -> None:
    row: dict[str, Any] = {
        "stage": stage,
        "intent": intent,
        "result": result,
        "remote_effect": remote_effect,
    }
    if command:
        row["command"] = command
    if detail:
        row["detail"] = detail
    execution.setdefault("events", []).append(row)
    if remote_effect and result == "CONFIRMED":
        execution.setdefault("remote_effects", []).append(row)
    if remote_effect and result == "UNKNOWN":
        execution.setdefault("unknown_effects", []).append(row)


def _command(
    argv: list[str], *, effect: str, purpose: str, read_only: bool = False
) -> dict[str, Any]:
    return {
        "argv": argv,
        "effect": effect,
        "purpose": purpose,
        "read_only": read_only,
    }


def publication_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    tag = str((manifest.get("version") or {}).get("tag", ""))
    source_sha = str(_source(manifest).get("sha", ""))
    repository = str(manifest.get("repository", ""))
    artifact = _artifact(manifest)
    image = str(artifact.get("image_reference", ""))
    workflow_inventory = (manifest.get("dependencies") or {}).get(
        "workflow_inventory"
    ) or {}
    release_workflows = [
        item
        for item in workflow_inventory.get("files", [])
        if isinstance(item, dict) and item.get("release")
    ]
    return {
        "phase": "phase-2",
        "release_id": manifest["release_id"],
        "service_name": _service_name(manifest),
        "repository": repository,
        "source_sha": source_sha,
        "tag": tag,
        "publisher": "gh + git + docker",
        "publisher_count": 1,
        "remote_mutations": [
            "Artifact Registry image upload",
            "exact Git tag push",
            "GitHub Release creation or reconciliation",
        ],
        "commands": [
            _command(
                ["docker", "tag", str(artifact.get("local_image", "")), image],
                effect="remote image preparation",
                purpose="associate the verified local image with the immutable release reference",
            ),
            _command(
                ["docker", "push", image],
                effect="Artifact Registry upload",
                purpose="publish the image before candidate deployment",
            ),
            _command(
                ["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
                effect="Git tag publication",
                purpose="publish only the planned SemVer tag",
            ),
            _command(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--verify-tag",
                    "--repo",
                    repository,
                ],
                effect="GitHub Release publication",
                purpose="create the release with local deterministic notes",
            ),
        ],
        "reconciliation": [
            "read Artifact Registry digest before uploading again",
            "read remote tag and compare its peeled target to the full SHA",
            "read GitHub Release before creating it",
        ],
        "audit": {
            "required_after_publication": True,
            "read_only_commands": [
                ["gh", "api", f"repos/{repository}/events?per_page=100"],
                ["gh", "api", f"repos/{repository}/releases/tags/{tag}"],
                ["gh", "api", f"repos/{repository}/deployments?ref={tag}"],
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    repository,
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,event,status,conclusion,workflowName,url",
                ],
            ],
            "events": [
                "push",
                "create",
                "release",
                "deployment",
                "deployment_status",
                "workflow_run",
            ],
        },
        "actions_guard": {
            "release_workflows_detected": [
                item.get("path", "") for item in release_workflows
            ],
            "must_not_push_branch": True,
            "note": "Push only the exact tag; do not push main, so automatic semantic-release cannot race this publisher.",
        },
        "gates": {
            "clean_publishable_sha": not bool(_source(manifest).get("dirty"))
            and bool(_source(manifest).get("publishable")),
            "quality_passed": (manifest.get("quality") or {}).get("status") == "PASSED",
            "artifact_digest_required_before_candidate": True,
            "single_publisher": True,
            "dry_run_by_default": True,
        },
        "execution_control": live_control_plan(manifest, "publish"),
    }


def _run_command(
    argv: list[str], *, cwd: Path | None = None, check: bool = True, timeout: int = 300
):
    if not argv or not argv[0]:
        raise LifecycleError("Cannot execute an empty command")
    if not shutil.which(argv[0]):
        raise LifecycleError(f"Required executable is unavailable: {argv[0]}")
    try:
        result = local_release.run(argv, cwd=cwd, check=False, timeout=timeout)
    except local_release.ReleaseError as exc:
        raise LifecycleError(str(exc)) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise LifecycleError(f"Command failed ({argv[0]}){suffix}")
    return result


def _remote_tag_target(repo: Path, tag: str) -> str | None:
    result = _run_command(
        ["git", "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        cwd=repo,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise LifecycleError("Unable to reconcile the remote Git tag")
    direct = None
    peeled = None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not local_release.SHA_RE.fullmatch(fields[0]):
            continue
        if fields[1] == f"refs/tags/{tag}^{{}}":
            peeled = fields[0]
        elif fields[1] == f"refs/tags/{tag}":
            direct = fields[0]
    return peeled or direct


def _local_tag_target(repo: Path, tag: str) -> str | None:
    result = _run_command(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=repo,
        check=False,
        timeout=60,
    )
    if result.returncode != 0 or not local_release.SHA_RE.fullmatch(
        result.stdout.strip()
    ):
        return None
    return result.stdout.strip()


def _remote_artifact_digest(image: str) -> str | None:
    result = _run_command(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--format=value(image_summary.digest)",
        ],
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        text = (result.stderr or result.stdout).lower()
        if "not found" in text or "404" in text:
            return None
        raise LifecycleError("Unable to reconcile the Artifact Registry image")
    value = result.stdout.strip()
    match = REMOTE_DIGEST_RE.search(value)
    return match.group(0) if match else None


def _release_view(repository: str, tag: str):
    result = _run_command(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,name,body,url",
        ],
        check=False,
        timeout=60,
    )
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LifecycleError("GitHub returned an invalid release record") from exc
        if value.get("tagName") != tag:
            raise LifecycleError("GitHub Release tag does not match the planned tag")
        return value
    text = (result.stderr or result.stdout).lower()
    if "release not found" in text or "not found" in text or "404" in text:
        return None
    raise LifecycleError("Unable to reconcile the GitHub Release")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    local_release.write_json(path.resolve(), manifest)


def _mark_phase(manifest: dict[str, Any], phase: str) -> None:
    transition = manifest.setdefault("transition", {})
    if not isinstance(transition, dict):
        raise LifecycleError("Manifest transition state is invalid")
    transition.update(
        {
            "phase": phase,
            "remote_mutations_allowed": False,
            "actions_dispatch": False,
            "cloud_build": False,
            "note": "Lifecycle effects remain guarded by explicit confirmation; this records the last completed contract phase.",
        }
    )


def _require_confirmation(execute: bool, confirmed: bool) -> None:
    if execute and not confirmed:
        raise LifecycleError(
            "Remote effects require --execute together with --confirm-remote-effects"
        )


def publish(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
    confirm_remote_effects: bool,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = publication_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        _require_live_controls(manifest, "publish")
        _require_remote_identity(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "publish", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    if not execute:
        event(
            execution,
            stage="publish",
            intent="record publication plan without remote effects",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    repo = Path(_source(manifest)["path"]).resolve()
    artifact = _artifact(manifest)
    image = str(artifact.get("image_reference", ""))
    if not image:
        raise LifecycleError("Manifest has no image reference to publish")
    try:
        remote_digest = _remote_artifact_digest(image)
        if remote_digest:
            expected = str(artifact.get("digest", ""))
            if expected and remote_digest != (
                expected if expected.startswith("sha256:") else f"sha256:{expected}"
            ):
                raise LifecycleError(
                    "Artifact Registry tag already points to another digest"
                )
            artifact["digest"] = remote_digest
            artifact["remote_published"] = True
            event(
                execution,
                stage="publish-artifact",
                intent="reconcile existing Artifact Registry image",
                result="CONFIRMED",
                remote_effect=True,
                detail=f"digest={remote_digest}",
            )
        else:
            local_image = str(artifact.get("local_image", ""))
            if not local_image:
                raise LifecycleError("Manifest has no local image to upload")
            event(
                execution,
                stage="publish-artifact",
                intent="tag local image for Artifact Registry",
                result="INTENT_RECORDED",
                remote_effect=True,
                command=["docker", "tag", local_image, image],
            )
            _run_command(["docker", "tag", local_image, image], timeout=120)
            event(
                execution,
                stage="publish-artifact",
                intent="push image to Artifact Registry",
                result="INTENT_RECORDED",
                remote_effect=True,
                command=["docker", "push", image],
            )
            _run_command(["docker", "push", image], timeout=1800)
            remote_digest = _remote_artifact_digest(image)
            if not remote_digest:
                raise LifecycleError("Image push completed without a verifiable digest")
            artifact["digest"] = remote_digest
            artifact["remote_published"] = True
            event(
                execution,
                stage="publish-artifact",
                intent="verify Artifact Registry digest",
                result="CONFIRMED",
                remote_effect=True,
                detail=f"digest={remote_digest}",
            )

        tag = str((manifest.get("version") or {}).get("tag", ""))
        source_sha = str(_source(manifest)["sha"])
        remote_tag = _remote_tag_target(repo, tag)
        if remote_tag and remote_tag != source_sha:
            raise LifecycleError(
                f"Remote tag {tag} exists on another SHA; refusing to move it"
            )
        if remote_tag == source_sha:
            event(
                execution,
                stage="publish-git",
                intent="reconcile exact remote tag",
                result="CONFIRMED",
                remote_effect=True,
                detail=f"tag={tag} sha={source_sha}",
            )
        else:
            local_tag = _local_tag_target(repo, tag)
            if local_tag and local_tag != source_sha:
                raise LifecycleError(
                    f"Local tag {tag} exists on another SHA; refusing to move it"
                )
            if not local_tag:
                notes = str((manifest.get("version") or {}).get("notes", "")).strip()
                _run_command(
                    [
                        "git",
                        "tag",
                        "-a",
                        tag,
                        source_sha,
                        "-m",
                        notes or f"Release {tag}",
                    ],
                    cwd=repo,
                    timeout=60,
                )
            event(
                execution,
                stage="publish-git",
                intent="push exact tag to origin",
                result="INTENT_RECORDED",
                remote_effect=True,
                command=["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
            )
            _run_command(
                ["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
                cwd=repo,
                timeout=180,
            )
            if _remote_tag_target(repo, tag) != source_sha:
                raise LifecycleError(
                    "Remote tag verification did not match the release SHA"
                )
            event(
                execution,
                stage="publish-git",
                intent="verify exact remote tag",
                result="CONFIRMED",
                remote_effect=True,
                detail=f"tag={tag} sha={source_sha}",
            )

        repository = str(manifest["repository"])
        release = _release_view(repository, tag)
        if release is None:
            notes = str((manifest.get("version") or {}).get("notes", ""))
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".md"
            ) as notes_file:
                notes_file.write(notes)
                notes_file.flush()
                command = [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--verify-tag",
                    "--repo",
                    repository,
                    "--title",
                    tag,
                    "--notes-file",
                    notes_file.name,
                ]
                event(
                    execution,
                    stage="publish-github",
                    intent="create GitHub Release after exact tag verification",
                    result="INTENT_RECORDED",
                    remote_effect=True,
                    command=command,
                )
                _run_command(command, timeout=180)
            release = _release_view(repository, tag)
            if release is None:
                raise LifecycleError("GitHub Release was not visible after creation")
        event(
            execution,
            stage="publish-github",
            intent="reconcile GitHub Release",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"url={release.get('url', '')}",
        )
        _mark_phase(manifest, "phase-2")
        _write_manifest(manifest_path, manifest)
        execution["status"] = "SUCCEEDED"
        execution["current_stage"] = "publish-github"
    except Exception as exc:
        if _artifact(manifest).get("remote_published"):
            _mark_phase(manifest, "phase-2")
            _write_manifest(manifest_path, manifest)
        execution["status"] = (
            "UNKNOWN"
            if execution.get("remote_effects") or execution.get("events")
            else "FAILED"
        )
        execution["error"] = str(exc)
        event(
            execution,
            stage=execution.get("current_stage", "publish"),
            intent="reconcile after lifecycle interruption",
            result="UNKNOWN" if execution["status"] == "UNKNOWN" else "FAILED",
            remote_effect=execution["status"] == "UNKNOWN",
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise
    save_execution(execution_path, execution)
    return {
        "plan": publication_plan(manifest),
        "manifest": manifest,
        "execution": execution,
        "execution_path": str(execution_path),
    }


def candidate_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    image = _image_with_digest(manifest)
    return {
        "phase": "phase-3",
        "stage": "candidate",
        "release_id": manifest["release_id"],
        "service_name": service,
        "image": image,
        "commands": [
            _command(
                [
                    "gcloud",
                    "run",
                    "deploy",
                    service,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--image",
                    image,
                    "--no-traffic",
                    "--quiet",
                ],
                effect="Cloud Run candidate revision",
                purpose="deploy the already-published digest with zero production traffic",
            ),
            _command(
                [
                    "gcloud",
                    "run",
                    "services",
                    "update-traffic",
                    service,
                    "--set-tags",
                    "candidate=<exact-revision>",
                ],
                effect="candidate URL tag",
                purpose="expose the exact candidate revision without moving production traffic",
            ),
        ],
        "gates": {
            "artifact_digest_verified": True,
            "no_source_deploy": True,
            "no_actions_dispatch": True,
            "candidate_traffic_percent": 0,
            "health_path": str(_deployment(manifest).get("health_path", "/")),
        },
        "execution_control": live_control_plan(manifest, "candidate"),
    }


def _json_command(argv: list[str], *, timeout: int = 120) -> dict[str, Any]:
    result = _run_command(argv, timeout=timeout)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"Command returned invalid JSON: {argv[0]}") from exc
    if not isinstance(value, dict):
        raise LifecycleError("Expected a JSON object from gcloud")
    return value


def _revision_ready(value: dict[str, Any]) -> bool:
    status = value.get("status") or {}
    conditions = status.get("conditions") or []
    return (
        any(
            str(condition.get("type", "")) == "Ready"
            and str(condition.get("state", condition.get("status", ""))).lower()
            in {"true", "condition_true", "condition_succeeded"}
            for condition in conditions
            if isinstance(condition, dict)
        )
        or str(status.get("service", "")).lower() == "ready"
    )


def _probe(url: str, path: str, *, expected: set[int] = {200}) -> dict[str, Any]:
    target = url.rstrip("/") + "/" + path.lstrip("/")
    request = urllib.request.Request(target, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            body = response.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read(512).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        raise LifecycleError(f"Probe failed for {target}: {exc}") from exc
    if status not in expected:
        raise LifecycleError(f"Probe returned HTTP {status} for {target}")
    return {"url": target, "status": status, "body_prefix": body}


def _candidate_url(service_value: dict[str, Any], candidate_tag: str) -> str:
    traffic = (service_value.get("status") or {}).get("traffic") or []
    for item in traffic:
        if (
            isinstance(item, dict)
            and item.get("tag") == candidate_tag
            and item.get("url")
        ):
            return str(item["url"])
    raise LifecycleError("Cloud Run did not return a URL for the candidate tag")


def candidate(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
    confirm_remote_effects: bool,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = candidate_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        _require_live_controls(manifest, "candidate")
        _require_remote_identity(manifest)
        _require_digest(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "candidate", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    if not execute:
        event(
            execution,
            stage="candidate",
            intent="record candidate plan without Cloud Run effects",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    image = _image_with_digest(manifest)
    try:
        deploy_command = [
            "gcloud",
            "run",
            "deploy",
            service,
            "--project",
            project,
            "--region",
            region,
            "--image",
            image,
            "--no-traffic",
            "--quiet",
        ]
        event(
            execution,
            stage="candidate-deploy",
            intent="deploy exact digest with no traffic",
            result="INTENT_RECORDED",
            remote_effect=True,
            command=deploy_command,
        )
        _run_command(deploy_command, timeout=1800)
        service_value = _json_command(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        revision = str(
            (service_value.get("status") or {}).get("latestCreatedRevisionName", "")
        )
        if not REVISION_RE.fullmatch(revision):
            raise LifecycleError("Cloud Run did not return a valid candidate revision")
        revision_value = _json_command(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                revision,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        revision_digest = str(
            (revision_value.get("status") or {}).get("imageDigest", "")
        )
        if revision_digest != _require_digest(manifest):
            raise LifecycleError(
                "Candidate revision image digest differs from the published digest"
            )
        candidate_tag = _candidate_tag(manifest)
        traffic_command = [
            "gcloud",
            "run",
            "services",
            "update-traffic",
            service,
            "--project",
            project,
            "--region",
            region,
            "--set-tags",
            f"{candidate_tag}={revision}",
            "--quiet",
        ]
        event(
            execution,
            stage="candidate-tag",
            intent="tag exact zero-traffic candidate",
            result="INTENT_RECORDED",
            remote_effect=True,
            command=traffic_command,
        )
        _run_command(traffic_command, timeout=300)
        service_value = _json_command(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        url = _candidate_url(service_value, candidate_tag)
        probe = _probe(url, str(_deployment(manifest).get("health_path", "/")))
        runtime = _runtime(manifest)
        runtime["candidate"] = {
            "revision": revision,
            "digest": revision_digest,
            "tag": candidate_tag,
            "url": url,
            "probe": probe,
            "created_at": local_release.iso(local_release.utc_now()),
        }
        event(
            execution,
            stage="candidate-validate",
            intent="validate exact candidate URL",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"revision={revision} status={probe['status']}",
        )
        _mark_phase(manifest, "phase-3")
        _write_manifest(manifest_path, manifest)
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        execution["status"] = (
            "UNKNOWN"
            if execution.get("remote_effects") or execution.get("events")
            else "FAILED"
        )
        execution["error"] = str(exc)
        event(
            execution,
            stage="candidate",
            intent="reconcile after candidate interruption",
            result="UNKNOWN" if execution["status"] == "UNKNOWN" else "FAILED",
            remote_effect=execution["status"] == "UNKNOWN",
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise
    save_execution(execution_path, execution)
    return {
        "plan": candidate_plan(manifest),
        "manifest": manifest,
        "execution": execution,
        "execution_path": str(execution_path),
    }


def promote_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    candidate_state = _runtime(manifest).get("candidate") or {}
    revision = str(candidate_state.get("revision", "<exact-revision>"))
    return {
        "phase": "phase-3",
        "stage": "promote",
        "release_id": manifest["release_id"],
        "service_name": service,
        "candidate_revision": revision,
        "commands": [
            _command(
                [
                    "gcloud",
                    "run",
                    "services",
                    "describe",
                    service,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--format=json",
                ],
                effect="production snapshot",
                purpose="capture existing traffic before changing it",
                read_only=True,
            ),
            _command(
                [
                    "gcloud",
                    "run",
                    "services",
                    "update-traffic",
                    service,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--to-revisions",
                    f"{revision}=100",
                    "--quiet",
                ],
                effect="production traffic promotion",
                purpose="move 100% only to the validated candidate revision",
            ),
        ],
        "gates": {
            "candidate_revision_explicit": revision != "<exact-revision>",
            "candidate_digest_matches_manifest": bool(candidate_state.get("digest"))
            and candidate_state.get("digest") == _artifact(manifest).get("digest"),
            "confirmation": "PROMOTE_PROD",
            "rollback_revision_snapshot_required": True,
            "automatic_rollback": False,
        },
        "execution_control": live_control_plan(manifest, "promote"),
    }


def _traffic_snapshot(service_value: dict[str, Any]) -> list[dict[str, Any]]:
    status_traffic = (service_value.get("status") or {}).get("traffic")
    spec_traffic = (service_value.get("spec") or {}).get("traffic")
    traffic = status_traffic or spec_traffic or []
    return [item for item in traffic if isinstance(item, dict)]


def _active_revision(traffic: list[dict[str, Any]]) -> str:
    for item in traffic:
        try:
            if int(item.get("percent", 0) or 0) > 0:
                return str(item.get("revisionName") or item.get("revision") or "")
        except (TypeError, ValueError):
            continue
    return ""


def promote(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
    confirm_remote_effects: bool,
    confirmation: str,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = promote_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        _require_live_controls(manifest, "promote")
        if confirmation != "PROMOTE_PROD":
            raise LifecycleError("Promotion requires --confirm PROMOTE_PROD")
        _require_remote_identity(manifest)
        candidate_state = _runtime(manifest).get("candidate") or {}
        revision = str(candidate_state.get("revision", ""))
        if not REVISION_RE.fullmatch(revision):
            raise LifecycleError("Promotion requires an exact candidate revision")
        if str(candidate_state.get("digest", "")) != _require_digest(manifest):
            raise LifecycleError(
                "Candidate revision digest does not match the manifest"
            )
    execution, execution_path = begin_execution(
        manifest, state_dir, "promote", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    if not execute:
        event(
            execution,
            stage="promote",
            intent="record promotion plan without traffic effects",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    candidate_state = _runtime(manifest).get("candidate") or {}
    revision = str(candidate_state.get("revision", ""))
    if not REVISION_RE.fullmatch(revision):
        raise LifecycleError("Promotion requires an exact candidate revision")
    if str(candidate_state.get("digest", "")) != _require_digest(manifest):
        raise LifecycleError("Candidate revision digest does not match the manifest")
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    try:
        revision_value = _json_command(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                revision,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        if not _revision_ready(revision_value):
            raise LifecycleError("Candidate revision is not Ready")
        service_value = _json_command(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        traffic = _traffic_snapshot(service_value)
        previous_revision = _active_revision(traffic)
        if not previous_revision:
            raise LifecycleError("Unable to capture an active production revision")
        _runtime(manifest)["promotion"] = {
            "previous_traffic": traffic,
            "previous_revision": previous_revision,
            "captured_at": local_release.iso(local_release.utc_now()),
        }
        event(
            execution,
            stage="snapshot",
            intent="capture current production traffic",
            result="CONFIRMED",
            remote_effect=False,
            detail=f"previous_revision={previous_revision}",
        )
        command = [
            "gcloud",
            "run",
            "services",
            "update-traffic",
            service,
            "--project",
            project,
            "--region",
            region,
            "--to-revisions",
            f"{revision}=100",
            "--quiet",
        ]
        event(
            execution,
            stage="promote",
            intent="move production traffic to exact candidate revision",
            result="INTENT_RECORDED",
            remote_effect=True,
            command=command,
        )
        _run_command(command, timeout=600)
        after = _json_command(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        if _active_revision(_traffic_snapshot(after)) != revision:
            raise LifecycleError(
                "Production traffic verification did not match the candidate revision"
            )
        url = str((after.get("status") or {}).get("url", ""))
        probe = (
            _probe(url, str(_deployment(manifest).get("health_path", "/")))
            if url
            else None
        )
        runtime = _runtime(manifest)
        runtime["production"] = {
            "revision": revision,
            "digest": candidate_state["digest"],
            "url": url,
            "probe": probe,
            "promoted_at": local_release.iso(local_release.utc_now()),
        }
        event(
            execution,
            stage="validate-production",
            intent="validate production traffic and smoke",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"revision={revision}",
        )
        _mark_phase(manifest, "phase-3")
        _write_manifest(manifest_path, manifest)
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        execution["status"] = (
            "UNKNOWN"
            if execution.get("remote_effects") or execution.get("events")
            else "FAILED"
        )
        execution["error"] = str(exc)
        event(
            execution,
            stage="promote",
            intent="stop and require explicit rollback/reconciliation",
            result="UNKNOWN" if execution["status"] == "UNKNOWN" else "FAILED",
            remote_effect=execution["status"] == "UNKNOWN",
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise
    save_execution(execution_path, execution)
    return {
        "plan": promote_plan(manifest),
        "manifest": manifest,
        "execution": execution,
        "execution_path": str(execution_path),
    }


def rollback_plan(
    manifest: dict[str, Any], target_revision: str = ""
) -> dict[str, Any]:
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    target = target_revision or str(
        (
            (_runtime(manifest).get("promotion") or {}).get(
                "previous_revision", "<known-good-revision>"
            )
        )
    )
    return {
        "phase": "phase-3",
        "stage": "rollback",
        "release_id": manifest["release_id"],
        "service_name": service,
        "target_revision": target,
        "commands": [
            _command(
                [
                    "gcloud",
                    "run",
                    "revisions",
                    "describe",
                    target,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--format=json",
                ],
                effect="rollback target verification",
                purpose="verify the explicit known-good revision",
                read_only=True,
            ),
            _command(
                [
                    "gcloud",
                    "run",
                    "services",
                    "update-traffic",
                    service,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--to-revisions",
                    f"{target}=100",
                    "--quiet",
                ],
                effect="traffic rollback",
                purpose="restore traffic only; do not alter tags or migrations",
            ),
        ],
        "gates": {
            "target_revision_explicit": target != "<known-good-revision>",
            "confirmation": "ROLLBACK_PROD",
            "does_not_revert_migrations": True,
            "does_not_create_tag": True,
        },
        "execution_control": live_control_plan(manifest, "rollback"),
    }


def rollback(
    manifest_path: Path,
    *,
    state_dir: Path,
    target_revision: str,
    execute: bool,
    confirm_remote_effects: bool,
    confirmation: str,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = rollback_plan(manifest, target_revision)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        _require_live_controls(manifest, "rollback")
        if confirmation != "ROLLBACK_PROD":
            raise LifecycleError("Rollback requires --confirm ROLLBACK_PROD")
        target = target_revision or str(
            ((_runtime(manifest).get("promotion") or {}).get("previous_revision", ""))
        )
        if not REVISION_RE.fullmatch(target):
            raise LifecycleError("Rollback requires an explicit known-good revision")
    execution, execution_path = begin_execution(
        manifest, state_dir, "rollback", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    if not execute:
        event(
            execution,
            stage="rollback",
            intent="record rollback plan without traffic effects",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    target = target_revision or str(
        ((_runtime(manifest).get("promotion") or {}).get("previous_revision", ""))
    )
    if not REVISION_RE.fullmatch(target):
        raise LifecycleError("Rollback requires an explicit known-good revision")
    project, region = _project_region(manifest)
    service = _service_name(manifest)
    try:
        revision_value = _json_command(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                target,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        if not _revision_ready(revision_value):
            raise LifecycleError("Rollback target revision is not Ready")
        command = [
            "gcloud",
            "run",
            "services",
            "update-traffic",
            service,
            "--project",
            project,
            "--region",
            region,
            "--to-revisions",
            f"{target}=100",
            "--quiet",
        ]
        event(
            execution,
            stage="rollback",
            intent="restore traffic to explicit known-good revision",
            result="INTENT_RECORDED",
            remote_effect=True,
            command=command,
        )
        _run_command(command, timeout=600)
        after = _json_command(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service,
                "--project",
                project,
                "--region",
                region,
                "--format=json",
            ]
        )
        if _active_revision(_traffic_snapshot(after)) != target:
            raise LifecycleError(
                "Rollback traffic verification did not match the requested revision"
            )
        url = str((after.get("status") or {}).get("url", ""))
        probe = (
            _probe(url, str(_deployment(manifest).get("health_path", "/")))
            if url
            else None
        )
        _runtime(manifest)["rollback"] = {
            "target_revision": target,
            "url": url,
            "probe": probe,
            "completed_at": local_release.iso(local_release.utc_now()),
        }
        event(
            execution,
            stage="validate-rollback",
            intent="validate restored traffic and smoke",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"revision={target}",
        )
        _mark_phase(manifest, "phase-3")
        _write_manifest(manifest_path, manifest)
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        execution["status"] = (
            "UNKNOWN"
            if execution.get("remote_effects") or execution.get("events")
            else "FAILED"
        )
        execution["error"] = str(exc)
        event(
            execution,
            stage="rollback",
            intent="stop and preserve rollback state for reconciliation",
            result="UNKNOWN" if execution["status"] == "UNKNOWN" else "FAILED",
            remote_effect=execution["status"] == "UNKNOWN",
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise
    save_execution(execution_path, execution)
    return {
        "plan": rollback_plan(manifest, target),
        "manifest": manifest,
        "execution": execution,
        "execution_path": str(execution_path),
    }


def release_payload(
    manifest: dict[str, Any], *, status: str, revision: str = ""
) -> dict[str, Any]:
    if status not in RELEASE_STATUSES:
        raise LifecycleError(f"Unsupported platform release status: {status}")
    runtime = _runtime(manifest)
    if not revision:
        state_key = {
            "candidate": "candidate",
            "promoted": "production",
            "rolled_back": "rollback",
        }[status]
        state = runtime.get(state_key) or {}
        revision = str(state.get("revision") or state.get("target_revision") or "")
    action = {
        "candidate": "deployed",
        "promoted": "promoted",
        "rolled_back": "rolled_back",
    }[status]
    return {
        "release_id": manifest["release_id"],
        "repository": manifest["repository"],
        "source_sha": _source(manifest)["sha"],
        "artifact_digest": _artifact(manifest).get("digest", ""),
        "version": (manifest.get("version") or {}).get("tag", ""),
        "status": status,
        "services": [
            {
                "service_name": manifest["service_name"],
                "revision": revision,
                "action": action,
            }
        ],
        "github_run_url": "",
        "triggered_by": "local-release",
        "notes": f"Local-first release {manifest['release_id']}; no Actions run was required.",
    }


def register_release(
    manifest_path: Path,
    *,
    state_dir: Path,
    status: str,
    revision: str,
    platform_api_url: str,
    token: str,
    execute: bool,
    confirm_remote_effects: bool,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    payload = release_payload(manifest, status=status, revision=revision)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if not platform_api_url:
            raise LifecycleError(
                "Registration requires --platform-api-url or ENG_PLATFORM_API_URL"
            )
        _require_live_controls(manifest, "register")
        _require_remote_identity(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "register", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    plan = {
        "phase": "phase-3",
        "stage": "register",
        "payload": payload,
        "remote_mutations": ["Engineering Platform release record"],
    }
    if not execute:
        event(
            execution,
            stage="register",
            intent="record platform registration payload without API mutation",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    endpoint = platform_api_url.rstrip("/") + "/api/releases/"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        event(
            execution,
            stage="register",
            intent="register exact release identity in Engineering Platform",
            result="INTENT_RECORDED",
            remote_effect=True,
            detail=endpoint,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                response_status = int(response.status)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LifecycleError(
                f"Engineering Platform registration failed with HTTP {exc.code}"
            ) from exc
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LifecycleError(
                "Engineering Platform returned invalid registration JSON"
            ) from exc
        event(
            execution,
            stage="register",
            intent="verify Engineering Platform registration response",
            result="CONFIRMED",
            remote_effect=True,
            detail=f"http_status={response_status}",
        )
        _runtime(manifest)["platform_registration"] = {
            "status": status,
            "response": value,
            "registered_at": local_release.iso(local_release.utc_now()),
        }
        _mark_phase(manifest, "phase-3")
        _write_manifest(manifest_path, manifest)
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        execution["status"] = (
            "UNKNOWN"
            if execution.get("remote_effects") or execution.get("events")
            else "FAILED"
        )
        execution["error"] = str(exc)
        event(
            execution,
            stage="register",
            intent="reconcile platform registration after interruption",
            result="UNKNOWN" if execution["status"] == "UNKNOWN" else "FAILED",
            remote_effect=execution["status"] == "UNKNOWN",
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise
    save_execution(execution_path, execution)
    return {
        "plan": plan,
        "manifest": manifest,
        "execution": execution,
        "execution_path": str(execution_path),
    }


def sanplat_plan(
    api_manifest: dict[str, Any],
    web_manifest: dict[str, Any],
    *,
    release_group_id: str,
    auxiliary_services: list[str],
) -> dict[str, Any]:
    api_candidate = _runtime(api_manifest).get("candidate") or {}
    web_candidate = _runtime(web_manifest).get("candidate") or {}
    missing = []
    for label, state in (("api", api_candidate), ("web", web_candidate)):
        if not state.get("revision") or not state.get("digest"):
            missing.append(f"{label} candidate revision/digest")
    group_id = (
        release_group_id
        or f"pair-{api_manifest['release_id'][:12]}-{web_manifest['release_id'][:12]}"
    )
    return {
        "phase": "phase-4",
        "stage": "sanplat-coordinated-window",
        "release_group_id": group_id,
        "services": {
            "api": {
                "service_name": api_manifest["service_name"],
                "release_id": api_manifest["release_id"],
                "revision": api_candidate.get("revision", ""),
                "digest": api_candidate.get("digest", ""),
            },
            "web": {
                "service_name": web_manifest["service_name"],
                "release_id": web_manifest["release_id"],
                "revision": web_candidate.get("revision", ""),
                "digest": web_candidate.get("digest", ""),
            },
            "unchanged_auxiliary": auxiliary_services,
        },
        "ordered_steps": [
            {"name": "prepare", "effect": "none", "required": True},
            {
                "name": "authorize",
                "effect": "corporate authorization",
                "required": True,
            },
            {
                "name": "capture-state",
                "effect": "read Cloud Run, jobs, queues and schedulers",
                "required": True,
            },
            {
                "name": "maintenance",
                "effect": "maintenance health 200 / business 503",
                "required": True,
            },
            {
                "name": "pause-deliveries",
                "effect": "pause new deliveries",
                "required": True,
            },
            {
                "name": "drain",
                "effect": "wait for active work and leases",
                "required": True,
            },
            {
                "name": "migrations",
                "effect": "only compatible/applicable migrations",
                "required": False,
            },
            {
                "name": "promote-pair",
                "effect": "activate exact API/Web revisions",
                "required": True,
            },
            {
                "name": "validate-functional",
                "effect": "Microsoft/SanPlat, persistence and anonymous rejection",
                "required": True,
            },
            {
                "name": "resume",
                "effect": "resume only resources previously enabled",
                "required": True,
            },
        ],
        "gates": {
            "release_group_id_preserved": bool(release_group_id),
            "exact_pair_required": True,
            "missing_candidate_evidence": missing,
            "corporate_window_required": True,
            "adapter_configured": False,
            "frontend_api_base_url_declared": True,
            "candidate_url_must_not_become_web_default": True,
            "execution": "blocked until a reviewed SanPlat adapter and window record are supplied",
        },
        "execution_control": {
            "status": "BLOCKED",
            "shared_exclusion": {
                "cli_cli": {"status": "BLOCKED", "configured": False},
                "cli_actions": {"status": "BLOCKED", "configured": False},
            },
            "prior_authorization": {"status": "BLOCKED", "configured": False},
            "adapter": {"status": "BLOCKED", "configured": False},
            "reason": "SanPlat requires the reviewed adapter and corporate-window record before any mutation",
        },
        "frontend_config": {
            "source": "runtime API_BASE_URL declared by the Web deployment configuration",
            "candidate_validation": "candidate Web configuration must point to the intended API, never to an accidental candidate URL",
        },
        "remote_mutations": [
            "SanPlat maintenance/pause/drain",
            "Cloud Run paired promotion",
            "functional validation",
            "resume",
        ],
    }


def sanplat(
    api_manifest_path: Path,
    web_manifest_path: Path,
    *,
    state_dir: Path,
    release_group_id: str,
    auxiliary_services: list[str],
    execute: bool,
    confirm_remote_effects: bool,
) -> dict[str, Any]:
    api_manifest = load_manifest(api_manifest_path)
    web_manifest = load_manifest(web_manifest_path)
    if (
        api_manifest.get("service_name") != "cgm-sanplat-api"
        or web_manifest.get("service_name") != "cgm-sanplat-web"
    ):
        raise LifecycleError(
            "SanPlat coordination requires cgm-sanplat-api and cgm-sanplat-web manifests"
        )
    plan = sanplat_plan(
        api_manifest,
        web_manifest,
        release_group_id=release_group_id,
        auxiliary_services=auxiliary_services,
    )
    _require_confirmation(execute, confirm_remote_effects)
    execution, execution_path = begin_execution(
        api_manifest, state_dir, "sanplat", dry_run=not execute
    )
    execution["related_release_ids"] = [
        api_manifest["release_id"],
        web_manifest["release_id"],
    ]
    execution["manifest_paths"] = [
        str(api_manifest_path.resolve()),
        str(web_manifest_path.resolve()),
    ]
    if not execute:
        event(
            execution,
            stage="sanplat",
            intent="record paired corporate-window plan without remote effects",
            result="PLANNED",
        )
        save_execution(execution_path, execution)
        return {
            "plan": plan,
            "execution": execution,
            "execution_path": str(execution_path),
        }
    message = "SanPlat execution is intentionally gated: supply a reviewed adapter and corporate-window record before any mutation"
    execution["status"] = "FAILED"
    execution["error"] = message
    event(
        execution,
        stage="authorize",
        intent="require reviewed SanPlat adapter and corporate window",
        result="FAILED",
        detail=message,
    )
    save_execution(execution_path, execution)
    raise LifecycleError(message)


def adoption_plan(
    platform_root: Path, repo_path: Path, service_name: str
) -> dict[str, Any]:
    service = local_release.load_catalog(platform_root.resolve(), service_name)
    snapshot = local_release.repo_snapshot(repo_path)
    inventory = local_release.workflow_inventory(Path(snapshot["path"]))
    return {
        "phase": "phase-5",
        "service_name": service_name,
        "repository": service["repository"],
        "source": snapshot,
        "current_actions_dependency": bool(inventory.get("actions_dependency")),
        "adoption_artifacts": [
            ".cgm/local-release.yaml",
            "docs/release/local-release.md",
            "local-release-manifest.schema.json",
        ],
        "steps": [
            "review generated local-release policy and service coordinates",
            "run doctor/plan/quality/prepare in a clean checkout",
            "exercise publish/candidate plans with fake or disposable targets",
            "compare local evidence to protected pre-merge checks",
            "request separate approval before changing triggers or removing duplicate workflows",
        ],
        "remote_mutations": [],
        "gates": {
            "actions_checks_remain_authoritative_until_migrated": True,
            "workflow_removal_requires_approval": True,
            "ui_changes_not_auto_generated": True,
            "shared_exclusion_ready": False,
            "prior_authorization_ready": False,
            "pilot_status": "PROPOSED_NOT_EXECUTED",
        },
    }


def _manifest_for_release(
    state_dir: Path, release_id: str
) -> tuple[Path, dict[str, Any]]:
    matches = []
    for path in local_release.state_paths(state_dir)["manifests"].glob("*.json"):
        try:
            value = load_manifest(path)
        except LifecycleError:
            continue
        if value.get("release_id") == release_id:
            matches.append((path, value))
    if len(matches) != 1:
        raise LifecycleError("Release identity is missing or not unique")
    return matches[0]


def _execution_records(state_dir: Path, release_id: str) -> list[dict[str, Any]]:
    records = []
    for path in local_release.state_paths(state_dir)["executions"].glob("*.json"):
        try:
            value = local_release.read_json(path)
        except local_release.ReleaseError:
            continue
        if value.get("release_id") == release_id:
            records.append(value)
    return sorted(
        records,
        key=lambda item: (int(item.get("attempt", 0)), str(item.get("updated_at", ""))),
    )


def _next_lifecycle_stage(manifest: dict[str, Any]) -> str:
    quality = manifest.get("quality") or {}
    if quality.get("status") != "PASSED":
        return "quality"
    artifact = _artifact(manifest)
    if not artifact.get("status") == "AVAILABLE" and not artifact.get("digest"):
        return "build"
    if not artifact.get("remote_published"):
        return "publish"
    runtime = _runtime(manifest)
    if not runtime.get("candidate"):
        return "candidate"
    if not runtime.get("platform_registration"):
        return "register"
    if not runtime.get("production"):
        return "promote"
    return "close"


def _next_command(manifest_path: Path, manifest: dict[str, Any], stage: str) -> str:
    if stage == "close":
        return "review release evidence and close the release manually"
    if stage == "quality":
        args = [
            "local-release",
            "quality",
            "--service",
            _service_name(manifest),
            "--repo-path",
            str(_source(manifest).get("path", "")),
        ]
    else:
        args = ["local-release", stage, "--manifest", str(manifest_path)]
        if stage == "register":
            args.extend(["--status", "candidate"])
        elif stage == "promote":
            args.extend(["--confirm", "PROMOTE_PROD"])
    return shlex.join(args)


def _reconcile_read_only(manifest: dict[str, Any]) -> dict[str, Any]:
    """Inspect external state without changing it; failures remain explicit."""
    source = _source(manifest)
    artifact = _artifact(manifest)
    version = manifest.get("version") or {}
    result: dict[str, Any] = {
        "artifact_registry": {"status": "NOT_CHECKED"},
        "git_tag": {"status": "NOT_CHECKED"},
        "github_release": {"status": "NOT_CHECKED"},
        "cloud_run": {"status": "NOT_CHECKED"},
    }
    image = str(artifact.get("image_reference", ""))
    if image:
        try:
            digest = _remote_artifact_digest(image)
            expected = _require_digest(manifest)
            result["artifact_registry"] = {
                "status": "CONFIRMED"
                if digest == expected
                else "MISMATCH"
                if digest
                else "NOT_FOUND",
                "digest": digest or "",
                "expected_digest": expected,
            }
        except LifecycleError as exc:
            result["artifact_registry"] = {"status": "UNAVAILABLE", "detail": str(exc)}
    repo_path = Path(str(source.get("path", ""))).expanduser()
    tag = str(version.get("tag", ""))
    if repo_path.is_dir() and tag:
        try:
            target = _remote_tag_target(repo_path, tag)
            expected_sha = str(source.get("sha", ""))
            result["git_tag"] = {
                "status": "CONFIRMED"
                if target == expected_sha
                else "MISMATCH"
                if target
                else "NOT_FOUND",
                "target": target or "",
                "expected_sha": expected_sha,
            }
        except LifecycleError as exc:
            result["git_tag"] = {"status": "UNAVAILABLE", "detail": str(exc)}
    repository = str(manifest.get("repository", ""))
    if repository and tag:
        try:
            release = _release_view(repository, tag)
            result["github_release"] = {
                "status": "CONFIRMED" if release else "NOT_FOUND",
                "release": release or {},
            }
        except LifecycleError as exc:
            result["github_release"] = {"status": "UNAVAILABLE", "detail": str(exc)}
    runtime = _runtime(manifest)
    candidate_state = runtime.get("candidate") or {}
    production_state = runtime.get("production") or {}
    if candidate_state or production_state:
        try:
            project, region = _project_region(manifest)
            service = _service_name(manifest)
            service_value = _json_command(
                [
                    "gcloud",
                    "run",
                    "services",
                    "describe",
                    service,
                    "--project",
                    project,
                    "--region",
                    region,
                    "--format=json",
                ]
            )
            traffic = _traffic_snapshot(service_value)
            result["cloud_run"] = {
                "status": "CONFIRMED",
                "active_revision": _active_revision(traffic),
                "traffic": traffic,
            }
        except LifecycleError as exc:
            result["cloud_run"] = {"status": "UNAVAILABLE", "detail": str(exc)}
    return result


def resume(
    state_dir: Path, release_id: str, *, reconcile: bool = False
) -> dict[str, Any]:
    """Summarize the first safe continuation point without performing mutations."""
    manifest_path, manifest = _manifest_for_release(state_dir, release_id)
    executions = _execution_records(state_dir, release_id)
    latest = executions[-1] if executions else None
    unknown = bool(
        latest and (latest.get("status") == "UNKNOWN" or latest.get("unknown_effects"))
    )
    remote_state = _reconcile_read_only(manifest) if reconcile else {}
    remote_query_performed = reconcile
    reconciliation_status = "NOT_REQUESTED"
    if reconcile:
        statuses = [
            value.get("status")
            for value in remote_state.values()
            if value.get("status") != "NOT_CHECKED"
        ]
        reconciliation_status = (
            "CONFIRMED"
            if statuses and all(status == "CONFIRMED" for status in statuses)
            else "INCONCLUSIVE"
        )
    next_stage = _next_lifecycle_stage(manifest)
    safe_to_continue = not unknown and next_stage in {
        "quality",
        "build",
        "publish",
        "register",
        "candidate",
        "promote",
        "close",
    }
    if unknown:
        safe_to_continue = reconcile and reconciliation_status == "CONFIRMED"
    return {
        "release_id": release_id,
        "manifest_path": str(manifest_path),
        "latest_execution": latest,
        "first_pending_stage": next_stage,
        "next_command": _next_command(manifest_path, manifest, next_stage),
        "unknown_effect_requires_reconciliation": unknown,
        "remote_query_performed": remote_query_performed,
        "reconciliation_status": reconciliation_status,
        "remote_state": remote_state,
        "safe_to_continue": safe_to_continue,
        "note": "resume reports the next safe command; it never mutates remote state automatically",
    }
