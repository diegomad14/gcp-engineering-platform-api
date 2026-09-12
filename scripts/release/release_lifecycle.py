#!/usr/bin/env python3
"""Guarded lifecycle operations for the local-first release engine.

The module extends phase 1 with phase 2 publication, phase 3 direct
candidate/promote/rollback operations and an individual adoption plan.
Every remote effect requires both ``--execute``
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
from urllib.parse import quote

try:  # Works both as a package import and when local_release.py is a script.
    from . import local_release
except ImportError:  # pragma: no cover - exercised by the executable wrapper.
    import local_release  # type: ignore[no-redef]

try:
    from .execution_control import (
        ExecutionContext,
        PlatformExecutionControlClient,
        _require_secure_base_url,
        authenticated_urlopen,
        deployment_scope_key,
        publication_scope_key,
    )
    from .lifecycle_control import LifecycleControlSession, effect_digest
except ImportError:  # pragma: no cover - exercised by the executable wrapper.
    from execution_control import (  # type: ignore[no-redef]
        ExecutionContext,
        PlatformExecutionControlClient,
        _require_secure_base_url,
        authenticated_urlopen,
        deployment_scope_key,
        publication_scope_key,
    )
    from lifecycle_control import LifecycleControlSession, effect_digest  # type: ignore[no-redef]


REMOTE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
RELEASE_STATUSES = {"candidate", "promoted", "rolled_back"}
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
    if source.get("repository") and source.get("repository") != manifest.get(
        "repository"
    ):
        raise LifecycleError(
            "Manifest source repository differs from release repository"
        )
    if source.get("reviewed_sha") != source.get("sha"):
        raise LifecycleError("Manifest reviewed SHA differs from source SHA")
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


def lifecycle_execution_context(
    manifest: dict[str, Any], operation: str, actor_id: str, *, target: str = ""
) -> ExecutionContext:
    """Build the immutable control-plane context for one manifest operation."""
    source = _source(manifest)
    version = manifest.get("version") or {}
    artifact = _artifact(manifest)
    project, region = _project_region(manifest)
    target = str(
        target
        or (manifest.get("execution_control") or {}).get("target", "")
        or f"{project}/{region}/{_service_name(manifest)}"
    )
    configuration_hash = str(
        (manifest.get("execution_control") or {}).get("configuration_hash", "")
    )
    if not configuration_hash:
        configuration_hash = local_release.digest(_deployment(manifest))
    if operation == "publish":
        target = str(
            (manifest.get("execution_control") or {}).get(
                "target", f"{manifest.get('repository', '')}:oss-v2"
            )
        )
    return ExecutionContext(
        release_id=str(manifest["release_id"]),
        repository=str(manifest["repository"]),
        service_name=_service_name(manifest),
        release_group_id="",
        source_sha=str(source.get("sha", "")),
        tag=str(version.get("tag", "")),
        operation=operation,
        actor_id=actor_id,
        artifact_digest=str(artifact.get("digest", "")),
        target=target,
        configuration_hash=configuration_hash,
    )


def _control_scope(manifest: dict[str, Any], operation: str) -> tuple[str, str]:
    if operation == "publish":
        return "publication", publication_scope_key(
            str(manifest["repository"]), "oss-v2"
        )
    context = lifecycle_execution_context(manifest, operation, "control-owner")
    return "deployment", deployment_scope_key(context.target)


def _open_lifecycle_control(
    manifest: dict[str, Any],
    operation: str,
    *,
    execution_control: LifecycleControlSession | None,
    control_client: PlatformExecutionControlClient | None,
    authorization_token: str,
    actor_id: str,
    owner_id: str,
    local_fixture: bool,
    target: str = "",
) -> LifecycleControlSession | None:
    if execution_control is not None:
        if not local_fixture:
            raise LifecycleError(
                "Injected lifecycle control requires explicit local fixture mode"
            )
        expected = lifecycle_execution_context(
            manifest,
            operation,
            actor_id or execution_control.context.actor_id,
            target=target,
        )
        if execution_control.context != expected:
            raise LifecycleError(
                "Injected control context does not match lifecycle identity"
            )
        return execution_control
    if control_client is None:
        _require_live_controls(manifest, operation)
        return None
    context = lifecycle_execution_context(manifest, operation, actor_id, target=target)
    scope, scope_key = _control_scope(manifest, operation)
    return LifecycleControlSession.open(
        control_client,
        context,
        token=authorization_token,
        owner_id=owner_id,
        scope=scope,
        scope_key=scope_key,
    )


def _open_control_or_record(
    manifest: dict[str, Any],
    operation: str,
    execution: dict[str, Any],
    execution_path: Path,
    **kwargs: Any,
) -> LifecycleControlSession | None:
    """Persist a failed attempt when control acquisition is rejected."""
    try:
        return _open_lifecycle_control(manifest, operation, **kwargs)
    except Exception as exc:
        execution["status"] = "FAILED"
        execution["error"] = str(exc)
        event(
            execution,
            stage=operation,
            intent="require authorization and durable lifecycle lease",
            result="FAILED",
            remote_effect=False,
            detail=str(exc),
        )
        save_execution(execution_path, execution)
        raise


def _attach_control(
    execution: dict[str, Any],
    execution_path: Path,
    control: LifecycleControlSession | None,
) -> None:
    if control is not None:
        execution["control"] = control.metadata()
        save_execution(execution_path, execution)


def _controlled_effect(
    control: LifecycleControlSession | None,
    execution: dict[str, Any],
    execution_path: Path,
    *,
    stage: str,
    intent: str,
    effect_key: str,
    command: list[str],
    callback: Any,
) -> Any:
    if control is not None:
        return control.run_effect(
            execution=execution,
            stage=stage,
            intent=intent,
            effect_key=effect_key,
            effect=callback,
            emit=event,
            persist=lambda: save_execution(execution_path, execution),
            command=command,
        )
    event(
        execution,
        stage=stage,
        intent=intent,
        result="INTENT_RECORDED",
        remote_effect=True,
        command=command,
    )
    return callback()


def _finish_control(
    execution: dict[str, Any],
    execution_path: Path,
    control: LifecycleControlSession | None,
    *,
    final_status: str,
) -> None:
    if control is None:
        return
    released = control.finish(final_status=final_status)
    if released is not None:
        execution.setdefault("control_events", []).append(
            {"stage": "release", "result": "CONFIRMED", "status": released.status}
        )
        event(
            execution,
            stage="release",
            intent="release lifecycle lease after terminal result",
            result="CONFIRMED",
            remote_effect=False,
            detail=f"lease_id={released.lease_id}",
        )
        save_execution(execution_path, execution)


def live_control_plan(manifest: dict[str, Any], operation: str) -> dict[str, Any]:
    """Describe controls required before any local remote mutation.

    ``main`` currently coordinates Actions runs and platform-issued release
    authorizations. The reusable platform adapter is present, but the local
    engine has no accepted CLI/Actions handshake and remote activation remains
    disabled, so the honest plan stays blocked.
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
            "adapter": "Engineering Platform signed local-release ticket",
            "contract_implemented": True,
        },
        "adapter_contract": {
            "status": "IMPLEMENTED",
            "lease_store": "Engineering Platform local durable state or configured Firestore",
            "intent_store": "same shared persistence as the lease",
            "authorization": "existing Ed25519 one-time consumption mechanism",
            "external_effects": [],
        },
        "remote_activation": {
            "enabled": False,
            "authority": "Engineering Platform authenticated per-service authorization",
            "cli_override_supported": False,
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
    raise LifecycleError(
        f"Live {operation} is blocked: remote activation is disabled; shared "
        "CLI/CLI and CLI/Actions exclusion plus prior platform authorization "
        "are not configured"
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
    execution_control: LifecycleControlSession | None = None,
    control_client: PlatformExecutionControlClient | None = None,
    authorization_token: str = "",
    actor_id: str = "",
    owner_id: str = "",
    local_fixture: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = publication_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if execution_control is None and control_client is None:
            _require_live_controls(manifest, "publish")
        _require_remote_identity(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "publish", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    control = (
        _open_control_or_record(
            manifest,
            "publish",
            execution,
            execution_path,
            execution_control=execution_control,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=actor_id,
            owner_id=owner_id,
            local_fixture=local_fixture,
        )
        if execute
        else None
    )
    _attach_control(execution, execution_path, control)
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
        expected_digest = _require_digest(manifest)
        remote_digest = _remote_artifact_digest(image)
        if remote_digest:
            if remote_digest != expected_digest:
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
            _controlled_effect(
                control,
                execution,
                execution_path,
                stage="publish-artifact",
                intent="tag local image for Artifact Registry",
                effect_key="artifact-tag",
                command=["docker", "tag", local_image, image],
                callback=lambda: _run_command(
                    ["docker", "tag", local_image, image], timeout=120
                ),
            )
            _controlled_effect(
                control,
                execution,
                execution_path,
                stage="publish-artifact",
                intent="push image to Artifact Registry",
                effect_key="artifact-push",
                command=["docker", "push", image],
                callback=lambda: _run_command(["docker", "push", image], timeout=1800),
            )
            remote_digest = _remote_artifact_digest(image)
            if not remote_digest:
                raise LifecycleError("Image push completed without a verifiable digest")
            if remote_digest != expected_digest:
                raise LifecycleError("Published image differs from the expected digest")
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
                _controlled_effect(
                    control,
                    execution,
                    execution_path,
                    stage="publish-git",
                    intent="create exact local release tag",
                    effect_key="git-tag",
                    command=["git", "tag", "-a", tag, source_sha],
                    callback=lambda: _run_command(
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
                    ),
                )
            _controlled_effect(
                control,
                execution,
                execution_path,
                stage="publish-git",
                intent="push exact tag to origin",
                effect_key="git-push",
                command=["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
                callback=lambda: _run_command(
                    ["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
                    cwd=repo,
                    timeout=180,
                ),
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
                _controlled_effect(
                    control,
                    execution,
                    execution_path,
                    stage="publish-github",
                    intent="create GitHub Release after exact tag verification",
                    effect_key="github-release",
                    command=command,
                    callback=lambda: _run_command(command, timeout=180),
                )
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
        _finish_control(execution, execution_path, control, final_status="CONFIRMED")
        execution["status"] = "SUCCEEDED"
        execution["current_stage"] = "publish-github"
    except Exception as exc:
        if _artifact(manifest).get("remote_published"):
            _mark_phase(manifest, "phase-2")
            _write_manifest(manifest_path, manifest)
        if control is not None and not control.unknown:
            try:
                _finish_control(
                    execution, execution_path, control, final_status="FAILED"
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
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


def _provider_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """Immutable provider identity; never derive ownership from service latest."""
    return {
        "repository": manifest["repository"],
        "source_sha": _source(manifest)["sha"],
        "release_id": manifest["release_id"],
        "service": _service_name(manifest),
        "project_region": list(_project_region(manifest)),
        "digest": _require_digest(manifest),
        "configuration": local_release.digest(
            {
                "catalog": manifest.get("catalog"),
                "configuration_hash": (manifest.get("execution_control") or {}).get(
                    "configuration_hash"
                ),
                "base_sha": _source(manifest).get("base_sha"),
                "reuse_key": _artifact(manifest).get("reuse_key"),
            }
        ),
    }


def _provider_labels(identity: dict[str, Any]) -> dict[str, str]:
    return {
        "cgm-repository": local_release.digest(identity["repository"])[:63],
        "cgm-sha": identity["source_sha"],
        "cgm-release": local_release.digest(identity["release_id"])[:63],
        "cgm-config": identity["configuration"][:63],
    }


def _candidate_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    identity = _provider_identity(manifest)
    suffix = "cgm-" + local_release.digest(identity)[:20]
    revision = f"{identity['service']}-{suffix}"
    if not REVISION_RE.fullmatch(revision):
        raise LifecycleError("Service name is too long for a deterministic revision")
    return {**identity, "revision": revision}


def _describe_provider_revision(identity: dict[str, Any]) -> dict[str, Any]:
    project, region = identity["project_region"]
    return _json_command(
        [
            "gcloud",
            "run",
            "revisions",
            "describe",
            identity["revision"],
            "--project",
            project,
            "--region",
            region,
            "--format=json",
        ]
    )


def _verify_provider_revision(value: dict[str, Any], identity: dict[str, Any]) -> None:
    metadata = value.get("metadata") or {}
    labels = metadata.get("labels") or {}
    if metadata.get("name") != identity["revision"]:
        raise LifecycleError("Provider revision identity mismatch")
    if labels.get("serving.knative.dev/service") != identity["service"]:
        raise LifecycleError("Provider service identity mismatch")
    if any(
        labels.get(key) != expected
        for key, expected in _provider_labels(identity).items()
    ):
        raise LifecycleError("Provider repository/SHA/config/release labels mismatch")
    if _revision_digest(value) != identity["digest"]:
        raise LifecycleError("Provider revision digest mismatch")
    if not _revision_ready(value):
        raise LifecycleError("Provider revision is not Ready")


def _known_revision(
    manifest: dict[str, Any], revision: str, state_dir: Path | None = None
) -> dict[str, Any]:
    record = (_runtime(manifest).get("known_revisions") or {}).get(revision)
    if record is None and state_dir is not None:
        matches = []
        for path in local_release.state_paths(state_dir)["manifests"].glob("*.json"):
            try:
                previous = load_manifest(path)
                production = _runtime(previous).get("production") or {}
                if (
                    previous.get("repository") != manifest["repository"]
                    or _service_name(previous) != _service_name(manifest)
                    or _project_region(previous) != _project_region(manifest)
                    or production.get("revision") != revision
                    or production.get("digest") != _require_digest(previous)
                ):
                    continue
                identity = {**_provider_identity(previous), "revision": revision}
                matches.append(identity)
            except (LifecycleError, KeyError, TypeError):
                continue
        unique = {local_release.digest(value): value for value in matches}
        if len(unique) > 1:
            raise LifecycleError(
                "Previous revision has conflicting local identity records"
            )
        if unique:
            record = next(iter(unique.values()))
            _runtime(manifest).setdefault("known_revisions", {})[revision] = record
    if not isinstance(record, dict) or record.get("revision") != revision:
        raise LifecycleError("Revision has no prior known identity record")
    if any(
        record.get(key) != expected
        for key, expected in {
            "repository": manifest["repository"],
            "service": _service_name(manifest),
            "project_region": list(_project_region(manifest)),
        }.items()
    ):
        raise LifecycleError(
            "Known revision belongs to another repository or destination"
        )
    if (
        not record.get("release_id")
        or not re.fullmatch(r"[0-9a-f]{40}", str(record.get("source_sha", "")))
        or not REMOTE_DIGEST_RE.fullmatch(str(record.get("digest", "")))
        or not SHA256_RE.fullmatch(str(record.get("configuration", "")))
    ):
        raise LifecycleError("Known revision identity is incomplete")
    return record


def reconcile_candidate_deploy(
    manifest_path: Path,
    *,
    control_client: PlatformExecutionControlClient,
    execution_path: Path,
    intent_id: str,
    reconciliation_id: str,
) -> dict[str, Any]:
    """Observe a lost deploy response without deploying or assigning traffic."""
    manifest = load_manifest(manifest_path)
    identity = _candidate_identity(manifest)
    if _runtime(manifest).get("candidate_deployment") != identity:
        raise LifecycleError("Candidate has no matching persisted deployment identity")
    intent = control_client.get_intent(intent_id)
    if intent is None:
        raise LifecycleError("Candidate deploy intent was not found")
    # The intent must belong to this exact deploy, not another release/effect.
    command = candidate_plan(manifest)["commands"][0]["argv"]
    execution = local_release.read_json(execution_path)
    metadata = execution.get("control") or {}
    context = metadata.get("context") or {}
    expected_context = lifecycle_execution_context(
        manifest, "candidate", context.get("actor_id", "")
    )
    expected_effect = effect_digest(
        {
            "context": expected_context.as_payload(),
            "scope_key": _control_scope(manifest, "candidate")[1],
            "stage": "candidate-deploy",
            "effect_key": "candidate-deploy",
            "command": command,
        }
    )
    matches = [
        item
        for item in execution.get("control_intents", [])
        if item.get("intent_id") == intent_id
        and item.get("stage") == "candidate-deploy"
        and item.get("effect_digest") == expected_effect
    ]
    if (
        context != expected_context.as_payload()
        or intent.context != expected_context.as_payload()
        or intent.effect_digest != expected_effect
        or len(matches) != 1
        or intent.scope_key != metadata.get("scope_key")
        or intent.owner_id != metadata.get("owner_id")
        or intent.lease_id != (metadata.get("lease") or {}).get("lease_id")
        or intent.lease_generation != (metadata.get("lease") or {}).get("generation")
    ):
        raise LifecycleError("Candidate deploy intent identity mismatch")
    observation = _describe_provider_revision(identity)
    _verify_provider_revision(observation, identity)
    reconciled = reconcile_control_intent(
        control_client,
        intent_id,
        reconciliation_id=reconciliation_id,
        outcome="CONFIRMED",
        observation_digest=local_release.digest(observation),
    )
    _runtime(manifest)["candidate_deploy_reconciled"] = identity
    _write_manifest(manifest_path, manifest)
    return {"status": "CONFIRMED", "identity": identity, "intent": reconciled}


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
                    "--revision-suffix",
                    _candidate_identity(manifest)["revision"][len(service) + 1 :],
                    "--labels",
                    ",".join(
                        f"{key}={value}"
                        for key, value in _provider_labels(
                            _provider_identity(manifest)
                        ).items()
                    ),
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


def _candidate_url(
    service_value: dict[str, Any], candidate_tag: str, revision: str = ""
) -> str:
    traffic = (service_value.get("status") or {}).get("traffic") or []
    for item in traffic:
        if (
            isinstance(item, dict)
            and item.get("tag") == candidate_tag
            and item.get("url")
        ):
            if revision and item.get("revisionName") != revision:
                raise LifecycleError("Candidate URL tag points to another revision")
            return str(item["url"])
    raise LifecycleError("Cloud Run did not return a URL for the candidate tag")


def candidate(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
    confirm_remote_effects: bool,
    execution_control: LifecycleControlSession | None = None,
    control_client: PlatformExecutionControlClient | None = None,
    authorization_token: str = "",
    actor_id: str = "",
    owner_id: str = "",
    local_fixture: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = candidate_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if execution_control is None and control_client is None:
            _require_live_controls(manifest, "candidate")
        _require_remote_identity(manifest)
        _require_digest(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "candidate", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    control = (
        _open_control_or_record(
            manifest,
            "candidate",
            execution,
            execution_path,
            execution_control=execution_control,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=actor_id,
            owner_id=owner_id,
            local_fixture=local_fixture,
        )
        if execute
        else None
    )
    _attach_control(execution, execution_path, control)
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
    try:
        identity = _candidate_identity(manifest)
        runtime = _runtime(manifest)
        prior = runtime.get("candidate_deployment")
        if prior is not None and prior != identity:
            raise LifecycleError("Persisted candidate deployment identity mismatch")
        runtime["candidate_deployment"] = identity
        _write_manifest(manifest_path, manifest)
        deploy_command = plan["commands"][0]["argv"]
        if prior is None:
            _controlled_effect(
                control,
                execution,
                execution_path,
                stage="candidate-deploy",
                intent="deploy exact digest with no traffic",
                effect_key="candidate-deploy",
                command=deploy_command,
                callback=lambda: _run_command(deploy_command, timeout=1800),
            )
        revision = identity["revision"]
        revision_value = _describe_provider_revision(identity)
        _verify_provider_revision(revision_value, identity)
        revision_digest = identity["digest"]
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
        _controlled_effect(
            control,
            execution,
            execution_path,
            stage="candidate-tag",
            intent="tag exact zero-traffic candidate",
            effect_key="candidate-tag",
            command=traffic_command,
            callback=lambda: _run_command(traffic_command, timeout=300),
        )
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
        url = _candidate_url(service_value, candidate_tag, revision)
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
        runtime.setdefault("known_revisions", {})[revision] = identity
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
        _finish_control(execution, execution_path, control, final_status="CONFIRMED")
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        if control is not None and not control.unknown:
            try:
                _finish_control(
                    execution, execution_path, control, final_status="FAILED"
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
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


def _revision_digest(value: dict[str, Any]) -> str:
    return str((value.get("status") or {}).get("imageDigest", ""))


def promote(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
    confirm_remote_effects: bool,
    confirmation: str,
    execution_control: LifecycleControlSession | None = None,
    control_client: PlatformExecutionControlClient | None = None,
    authorization_token: str = "",
    actor_id: str = "",
    owner_id: str = "",
    local_fixture: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = promote_plan(manifest)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if execution_control is None and control_client is None:
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
    control = (
        _open_control_or_record(
            manifest,
            "promote",
            execution,
            execution_path,
            execution_control=execution_control,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=actor_id,
            owner_id=owner_id,
            local_fixture=local_fixture,
        )
        if execute
        else None
    )
    _attach_control(execution, execution_path, control)
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
        identity = _candidate_identity(manifest)
        if revision != identity["revision"]:
            raise LifecycleError("Candidate revision identity mismatch")
        _verify_provider_revision(revision_value, identity)
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
        previous_identity = _known_revision(manifest, previous_revision, state_dir)
        _verify_provider_revision(
            _describe_provider_revision(previous_identity), previous_identity
        )
        _runtime(manifest)["promotion"] = {
            "previous_traffic": traffic,
            "previous_revision": previous_revision,
            "previous_identity": previous_identity,
            "captured_at": local_release.iso(local_release.utc_now()),
        }
        _write_manifest(manifest_path, manifest)
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
        _controlled_effect(
            control,
            execution,
            execution_path,
            stage="promote",
            intent="move production traffic to exact candidate revision",
            effect_key="promote-traffic",
            command=command,
            callback=lambda: _run_command(command, timeout=600),
        )
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
        _finish_control(execution, execution_path, control, final_status="CONFIRMED")
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        if control is not None and not control.unknown:
            try:
                _finish_control(
                    execution, execution_path, control, final_status="FAILED"
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
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
    execution_control: LifecycleControlSession | None = None,
    control_client: PlatformExecutionControlClient | None = None,
    authorization_token: str = "",
    actor_id: str = "",
    owner_id: str = "",
    local_fixture: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    plan = rollback_plan(manifest, target_revision)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if execution_control is None and control_client is None:
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
    control = (
        _open_control_or_record(
            manifest,
            "rollback",
            execution,
            execution_path,
            execution_control=execution_control,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=actor_id,
            owner_id=owner_id,
            local_fixture=local_fixture,
        )
        if execute
        else None
    )
    _attach_control(execution, execution_path, control)
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
        identity = _known_revision(manifest, target, state_dir)
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
        _verify_provider_revision(revision_value, identity)
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
        _controlled_effect(
            control,
            execution,
            execution_path,
            stage="rollback",
            intent="restore traffic to explicit known-good revision",
            effect_key="rollback-traffic",
            command=command,
            callback=lambda: _run_command(command, timeout=600),
        )
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
            "digest": identity["digest"],
            "identity": identity,
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
        _finish_control(execution, execution_path, control, final_status="CONFIRMED")
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        if control is not None and not control.unknown:
            try:
                _finish_control(
                    execution, execution_path, control, final_status="FAILED"
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
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
        "release_group_id": "",
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


def _post_registration(request: urllib.request.Request) -> tuple[str, int]:
    try:
        with authenticated_urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            try:
                value = json.loads(body)
            except json.JSONDecodeError as exc:
                raise LifecycleError(
                    "Engineering Platform returned invalid registration JSON"
                ) from exc
            if not isinstance(value, (dict, list)) or not value:
                raise LifecycleError(
                    "Engineering Platform returned invalid registration data"
                )
            payload = json.loads(request.data or b"{}")
            if isinstance(value, list):
                expected_rows = [
                    {**payload, **service} for service in payload["services"]
                ]
                fields = (
                    "release_id",
                    "release_group_id",
                    "repository",
                    "source_sha",
                    "artifact_digest",
                    "version",
                    "status",
                    "service_name",
                    "revision",
                    "action",
                )
                if len(value) != len(expected_rows) or any(
                    not any(
                        isinstance(row, dict)
                        and all(
                            row.get(key, "") == expected.get(key, "") for key in fields
                        )
                        for row in value
                    )
                    for expected in expected_rows
                ):
                    raise LifecycleError(
                        "Engineering Platform registration identity mismatch"
                    )
            elif value.get("id") != payload.get("release_id"):
                raise LifecycleError(
                    "Engineering Platform registration receipt mismatch"
                )
            return body, int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LifecycleError(
            f"Engineering Platform registration failed with HTTP {exc.code}: {body[:200]}"
        ) from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        raise LifecycleError(
            "Engineering Platform registration response was lost; reconcile before retrying"
        ) from exc


def _get_registration_observation(
    platform_api_url: str, service_name: str
) -> tuple[dict[str, Any], int]:
    endpoint = (
        _require_secure_base_url(platform_api_url)
        + f"/api/releases/{quote(service_name, safe='')}/latest"
    )
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}, 404
        raise LifecycleError(
            f"Engineering Platform reconciliation failed with HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        raise LifecycleError(
            "Engineering Platform reconciliation endpoint was unavailable"
        ) from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "Engineering Platform reconciliation returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise LifecycleError(
            "Engineering Platform reconciliation returned invalid data"
        )
    return value, status


def reconcile_register_release(
    manifest_path: Path,
    *,
    platform_api_url: str,
    control_client: PlatformExecutionControlClient,
    intent_id: str,
    status: str,
    revision: str,
    reconciliation_id: str,
) -> dict[str, Any]:
    """Reconcile a lost register response using the exact release identity."""
    manifest = load_manifest(manifest_path)
    expected = release_payload(manifest, status=status, revision=revision)
    durable = control_client.get_intent(intent_id)
    if durable is None:
        raise LifecycleError("Registration intent does not exist")
    context = lifecycle_execution_context(
        manifest, "register", str(durable.context.get("actor_id", ""))
    )
    endpoint = _require_secure_base_url(platform_api_url) + "/api/releases/"
    expected_effect = effect_digest(
        {
            "context": context.as_payload(),
            "scope_key": durable.scope_key,
            "stage": "register",
            "effect_key": f"platform-registration:{status}",
            "command": ["POST", endpoint, local_release.digest(expected)],
        }
    )
    if (
        durable.context != context.as_payload()
        or durable.effect_digest != expected_effect
    ):
        raise LifecycleError(
            "Registration reconciliation identity differs from durable intent"
        )
    observation, response_status = _get_registration_observation(
        platform_api_url, _service_name(manifest)
    )
    observed_services = observation.get("services") or []
    observed_service = (
        observed_services[0]
        if isinstance(observed_services, list) and observed_services
        else observation
    )
    if not isinstance(observed_service, dict):
        observed_service = {}
    exact = response_status == 200 and all(
        observation_value == expected_value
        for observation_value, expected_value in (
            (observation.get("release_id"), expected["release_id"]),
            (observation.get("repository"), expected["repository"]),
            (observation.get("source_sha"), expected["source_sha"]),
            (observation.get("artifact_digest"), expected["artifact_digest"]),
            (observation.get("version"), expected["version"]),
            (observation.get("status"), expected["status"]),
            (observed_service.get("revision"), expected["services"][0]["revision"]),
        )
    )
    outcome = "CONFIRMED" if exact else "UNKNOWN"
    reconciled = reconcile_control_intent(
        control_client,
        intent_id,
        reconciliation_id=reconciliation_id,
        outcome=outcome,
        observation_digest=local_release.digest(
            {"status": response_status, "body": observation}
        ),
    )
    if exact:
        runtime = _runtime(manifest)
        runtime["platform_registration"] = {
            "status": status,
            "response": observation,
            "registered_at": local_release.iso(local_release.utc_now()),
        }
        _mark_phase(manifest, "phase-3")
        _write_manifest(manifest_path, manifest)
    return {
        "status": outcome,
        "response_status": response_status,
        "observation": observation,
        "intent": reconciled,
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
    execution_control: LifecycleControlSession | None = None,
    control_client: PlatformExecutionControlClient | None = None,
    authorization_token: str = "",
    actor_id: str = "",
    owner_id: str = "",
    local_fixture: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    payload = release_payload(manifest, status=status, revision=revision)
    _require_confirmation(execute, confirm_remote_effects)
    if execute:
        if not platform_api_url:
            raise LifecycleError(
                "Registration requires --platform-api-url or ENG_PLATFORM_API_URL"
            )
        _require_secure_base_url(platform_api_url)
        if execution_control is None and control_client is None:
            _require_live_controls(manifest, "register")
        _require_remote_identity(manifest)
    execution, execution_path = begin_execution(
        manifest, state_dir, "register", dry_run=not execute
    )
    execution["manifest_path"] = str(manifest_path.resolve())
    control = (
        _open_control_or_record(
            manifest,
            "register",
            execution,
            execution_path,
            execution_control=execution_control,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=actor_id,
            owner_id=owner_id,
            local_fixture=local_fixture,
        )
        if execute
        else None
    )
    _attach_control(execution, execution_path, control)
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
    endpoint = _require_secure_base_url(platform_api_url) + "/api/releases/"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if control_client is not None and not local_fixture:
        headers.update(control_client.authenticated_headers(platform_api_url))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    def post_with_intent() -> tuple[str, int]:
        if control is not None:
            request.add_header(
                "X-Release-Intent", execution["control_intents"][-1]["intent_id"]
            )
        return _post_registration(request)

    try:
        body, response_status = _controlled_effect(
            control,
            execution,
            execution_path,
            stage="register",
            intent="register exact release identity in Engineering Platform",
            effect_key=f"platform-registration:{status}",
            command=["POST", endpoint, local_release.digest(payload)],
            callback=post_with_intent,
        )
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
        _finish_control(execution, execution_path, control, final_status="CONFIRMED")
        execution["status"] = "SUCCEEDED"
    except Exception as exc:
        if execution.get("remote_effects"):
            _mark_phase(manifest, "phase-3")
            _write_manifest(manifest_path, manifest)
        if control is not None and not control.unknown:
            try:
                _finish_control(
                    execution, execution_path, control, final_status="FAILED"
                )
            except Exception as control_exc:
                execution.setdefault("control_errors", []).append(str(control_exc))
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
        if value.get("release_id") == release_id or (
            value.get("phase") == "sanplat"
            and release_id in value.get("related_release_ids", [])
        ):
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
            cloud_run = {
                "status": "CONFIRMED",
                "active_revision": _active_revision(traffic),
                "traffic": traffic,
            }
            revision_checks = (
                ("candidate", candidate_state, "revision"),
                ("production", production_state, "revision"),
                ("rollback", runtime.get("rollback") or {}, "target_revision"),
            )
            for name, state, revision_key in revision_checks:
                revision = str(state.get(revision_key, ""))
                if not revision:
                    continue
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
                digest = _revision_digest(revision_value)
                expected_digest = str(state.get("digest", ""))
                cloud_run[f"{name}_revision"] = revision
                cloud_run[f"{name}_digest"] = digest
                cloud_run[f"{name}_revision_status"] = (
                    "CONFIRMED"
                    if _revision_ready(revision_value)
                    and (not expected_digest or digest == expected_digest)
                    else "MISMATCH"
                )
            result["cloud_run"] = cloud_run
        except LifecycleError as exc:
            result["cloud_run"] = {"status": "UNAVAILABLE", "detail": str(exc)}
    return result


def _unknown_operation(execution: dict[str, Any]) -> str:
    """Return the lifecycle operation whose remote effect is uncertain."""
    for effect in reversed(execution.get("unknown_effects") or []):
        stage = str(effect.get("stage", ""))
        for operation in LIVE_REMOTE_OPERATIONS:
            if stage == operation or stage.startswith(f"{operation}-"):
                return operation
    current_stage = str(execution.get("current_stage", ""))
    for operation in LIVE_REMOTE_OPERATIONS:
        if current_stage == operation or current_stage.startswith(f"{operation}-"):
            return operation
    return ""


def _control_state(
    execution: dict[str, Any], control_client: PlatformExecutionControlClient
) -> dict[str, Any]:
    """Read durable lease/intent state for a previously persisted execution."""
    control_metadata = execution.get("control") or {}
    scope_key = str(control_metadata.get("scope_key", ""))
    lease = control_client.get_lease(scope_key) if scope_key else None
    intents: list[dict[str, Any]] = []
    for reference in execution.get("control_intents") or []:
        intent_id = (
            str(reference.get("intent_id", "")) if isinstance(reference, dict) else ""
        )
        if not intent_id:
            continue
        intent = control_client.get_intent(intent_id)
        intents.append(
            {
                "intent_id": intent_id,
                "status": intent.status if intent else "NOT_FOUND",
                "reconciliation_required": (
                    intent.reconciliation_required if intent else True
                ),
            }
        )
    return {
        "lease": (
            {
                "lease_id": lease.lease_id,
                "status": lease.status,
                "generation": lease.generation,
                "version": lease.version,
                "takeover_allowed": lease.takeover_allowed,
            }
            if lease
            else None
        ),
        "intents": intents,
    }


def reconcile_control_intent(
    control_client: PlatformExecutionControlClient,
    intent_id: str,
    *,
    reconciliation_id: str,
    outcome: str,
    observation_digest: str,
):
    """Resolve one durable UNKNOWN intent after independent observation."""
    intent = control_client.get_intent(intent_id)
    if intent is None:
        raise LifecycleError(f"Control intent was not found: {intent_id}")
    lease = control_client.get_lease(intent.scope_key)
    if lease is None:
        raise LifecycleError(f"Control lease was not found: {intent.scope_key}")
    return control_client.reconcile_intent(
        intent,
        reconciliation_id=reconciliation_id,
        outcome=outcome,
        observation_digest=observation_digest,
        lease=lease,
    )


def _reconciliation_proves_unknown_effect(
    manifest: dict[str, Any], operation: str, remote_state: dict[str, Any]
) -> bool:
    """Require evidence for the phase that may have performed the effect."""
    runtime = _runtime(manifest)
    cloud_run = remote_state.get("cloud_run") or {}
    if operation == "publish":
        return True
    if operation == "candidate":
        # Candidate state is written only after the exact revision, digest, tag,
        # and smoke check have been observed. Without it, a lost deploy response
        # must remain blocked even if publication is fully reconciled.
        candidate = runtime.get("candidate") or {}
        return (
            bool(candidate.get("revision") and candidate.get("digest"))
            and cloud_run.get("status") == "CONFIRMED"
            and cloud_run.get("candidate_revision_status") == "CONFIRMED"
            and cloud_run.get("candidate_revision") == candidate.get("revision")
            and cloud_run.get("candidate_digest") == candidate.get("digest")
        )
    if operation == "promote":
        candidate = runtime.get("candidate") or {}
        return (
            cloud_run.get("status") == "CONFIRMED"
            and cloud_run.get("active_revision") == candidate.get("revision")
            and cloud_run.get("candidate_revision_status") == "CONFIRMED"
            and cloud_run.get("candidate_digest") == candidate.get("digest")
        )
    if operation == "rollback":
        rollback = runtime.get("rollback") or {}
        return (
            cloud_run.get("status") == "CONFIRMED"
            and cloud_run.get("active_revision") == rollback.get("target_revision")
            and cloud_run.get("rollback_revision_status") == "CONFIRMED"
        )
    if operation == "register":
        # The local lifecycle has no read-only registration endpoint. A POST
        # response alone is therefore not evidence that a lost response was
        # applied; keep UNKNOWN blocked until the manifest has confirmation.
        return bool(runtime.get("platform_registration"))
    return False


def resume(
    state_dir: Path,
    release_id: str,
    *,
    reconcile: bool = False,
    control_client: PlatformExecutionControlClient | None = None,
) -> dict[str, Any]:
    """Summarize the first safe continuation point without performing mutations."""
    manifest_path, manifest = _manifest_for_release(state_dir, release_id)
    executions = _execution_records(state_dir, release_id)
    latest = executions[-1] if executions else None
    if any(record.get("phase") == "sanplat" for record in executions):
        return {
            "release_id": release_id,
            "manifest_path": str(manifest_path),
            "remote_query_performed": False,
            "latest_execution": latest,
            "first_pending_stage": "manual-review",
            "safe_to_continue": False,
            "unknown_effect_requires_reconciliation": True,
            "note": "Legacy SanPlat lifecycle state is uncertain and remains blocked for manual review.",
        }
    unknown = bool(
        latest and (latest.get("status") == "UNKNOWN" or latest.get("unknown_effects"))
    )
    unknown_operation = _unknown_operation(latest or {}) if unknown else ""
    remote_state = _reconcile_read_only(manifest) if reconcile else {}
    control_state = (
        _control_state(latest, control_client)
        if latest and control_client is not None
        else {}
    )
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
        if (
            unknown
            and reconciliation_status == "CONFIRMED"
            and not _reconciliation_proves_unknown_effect(
                manifest, unknown_operation, remote_state
            )
        ):
            reconciliation_status = "INCONCLUSIVE"
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
        safe_to_continue = (
            reconcile
            and reconciliation_status == "CONFIRMED"
            and _reconciliation_proves_unknown_effect(
                manifest, unknown_operation, remote_state
            )
        )
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
        "control_state": control_state,
        "safe_to_continue": safe_to_continue,
        "note": "resume reports the next safe command; it never mutates remote state automatically",
    }
