#!/usr/bin/env python3
"""Deterministic, local-first release preparation engine.

Phase 1 commands intentionally have no publication or deployment effect.  They
prepare an immutable release manifest, record each local execution attempt
separately, and reuse only quality evidence and local artifact evidence whose
complete input identity matches the requested commit.  Later lifecycle
commands are guarded by explicit execution and confirmation flags.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
POLICY_ID = "oss-v2"
QUALITY_TTL_HOURS = 168
SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[A-Za-z][A-Za-z0-9-]*)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s+(?P<subject>.+)$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REMOTE_RE = re.compile(
    r"(?:github\.com[/:])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "quality-reports",
    "test-results",
    "playwright-report",
    "__pycache__",
}
RELEASE_STAGES = (
    "plan",
    "quality",
    "build",
    "publish-artifact",
    "publish-github",
    "register",
    "candidate",
    "promote",
    "validate",
    "close",
)
TOOL_COMMANDS = {
    "git": ("git", "--version"),
    "python": (sys.executable, "--version"),
    "docker": ("docker", "--version"),
    "docker-buildx": ("docker", "buildx", "version"),
    "gh": ("gh", "--version"),
    "gcloud": ("gcloud", "--version"),
    "node": ("node", "--version"),
    "npm": ("npm", "--version"),
}
REMOTE_EFFECT_MARKERS = (
    "gh release create",
    "git push",
    "gcloud run",
    "gcloud builds",
    "docker push",
    "workflow_dispatch",
    "create_dispatch",
)


class ReleaseError(RuntimeError):
    """A release precondition or local operation failed."""


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ReleaseError(f"Invalid SemVer: {value!r}")
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            match.group("pre") or "",
            match.group("build") or "",
        )

    def tag(self) -> str:
        value = f"v{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += f"-{self.prerelease}"
        if self.build:
            value += f"+{self.build}"
        return value

    def bump(self, level: str) -> "SemVer":
        if level == "major":
            return SemVer(self.major + 1, 0, 0)
        if level == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if level == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise ReleaseError(f"Unsupported release level: {level}")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    authored_at: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(mode)
    temporary.replace(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Cannot read JSON file: {path}") from exc


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={**os.environ, "GH_PAGER": "cat"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"Unable to execute {args[0]}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise ReleaseError(f"Command failed ({args[0]}){suffix}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=repo, check=check).stdout.strip()


def require_sha(value: str, label: str = "SHA") -> str:
    normalized = value.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise ReleaseError(f"{label} must be a full 40-character SHA-1")
    return normalized


def repo_snapshot(repo_path: Path) -> dict[str, Any]:
    repo = repo_path.expanduser().resolve()
    if not repo.is_dir():
        raise ReleaseError(f"Repository path does not exist: {repo}")
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    head = require_sha(git(root, "rev-parse", "HEAD"), "HEAD")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not branch:
        branch = "DETACHED"
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    remote = git(root, "config", "--get", "remote.origin.url", check=False)
    repository = parse_repository(remote)
    return {
        "path": str(root),
        "sha": head,
        "branch": branch,
        "dirty": bool(status),
        "dirty_paths": [line[3:] for line in status.splitlines() if len(line) >= 4],
        "remote_origin": remote,
        "repository": repository,
    }


def parse_repository(remote: str) -> str:
    if not remote:
        return ""
    match = REMOTE_RE.search(remote.strip())
    if not match:
        return ""
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_base_sha(repo: Path, head: str, requested: str = "") -> str:
    if requested:
        base = require_sha(
            git(repo, "rev-parse", f"{requested}^{{commit}}"), "base SHA"
        )
    else:
        base = ""
        for candidate in ("origin/main", "main", "origin/master", "master"):
            result = git(repo, "merge-base", head, candidate, check=False)
            if result:
                base = require_sha(result, "base SHA")
                break
    if base == head:
        raise ReleaseError("Comparison base must differ from the release SHA")
    return base


def load_catalog(platform_root: Path, service_name: str) -> dict[str, Any]:
    path = platform_root / "src/eng_platform_api/static_examples/mock_catalog.json"
    if not path.exists():
        path = platform_root / "catalog/services.json"
    data = read_json(path)
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, list):
        raise ReleaseError(f"Catalog has no services list: {path}")
    service = next(
        (item for item in services if item.get("service_name") == service_name), None
    )
    if not isinstance(service, dict):
        raise ReleaseError(
            f"Service is not registered in the platform catalog: {service_name}"
        )
    return service


def validate_catalog_repository(
    snapshot: dict[str, Any], service: dict[str, Any]
) -> None:
    expected = str(service.get("repository", ""))
    if not REPOSITORY_RE.fullmatch(expected):
        raise ReleaseError("Catalog repository is invalid")
    actual = snapshot.get("repository", "")
    if actual and actual != expected:
        raise ReleaseError(
            f"Local origin {actual} does not match catalog repository {expected}"
        )


def list_release_tags(repo: Path, head: str) -> list[dict[str, str]]:
    tags = git(repo, "tag", "--merged", head, "--list", "v*", check=True).splitlines()
    result: list[dict[str, str]] = []
    for name in tags:
        try:
            version = SemVer.parse(name)
        except ReleaseError:
            continue
        commit = git(repo, "rev-parse", f"{name}^{{commit}}", check=False)
        if SHA_RE.fullmatch(commit):
            result.append({"name": name, "sha": commit, "version": version.tag()})
    return result


def latest_release(repo: Path, head: str) -> dict[str, str] | None:
    tags = list_release_tags(repo, head)
    if not tags:
        return None
    return max(tags, key=lambda item: SemVer.parse(item["name"]))


def commit_log(repo: Path, previous_tag: str | None) -> list[Commit]:
    revision = f"{previous_tag}..HEAD" if previous_tag else "HEAD"
    raw = git(
        repo,
        "log",
        "--no-merges",
        "--date=iso-strict",
        "--pretty=format:%H%x00%s%x00%b%x00%ad%x00%x1e",
        revision,
    )
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        fields = record.lstrip("\n").split("\x00")
        if len(fields) < 4 or not fields[0].strip():
            continue
        commits.append(
            Commit(
                sha=require_sha(fields[0], "commit SHA"),
                subject=fields[1].strip(),
                body=fields[2].strip(),
                authored_at=fields[3].strip(),
            )
        )
    return commits


def commit_release_level(commit: Commit) -> str | None:
    match = CONVENTIONAL_RE.fullmatch(commit.subject)
    breaking = bool(match and match.group("breaking")) or bool(
        re.search(
            r"^BREAKING(?: CHANGE)?\s*:", commit.body, re.IGNORECASE | re.MULTILINE
        )
    )
    if breaking:
        return "major"
    if not match:
        return None
    kind = match.group("type").lower()
    if kind == "feat":
        return "minor"
    if kind in {"fix", "perf"}:
        return "patch"
    return None


def strongest_level(commits: list[Commit]) -> str | None:
    levels = {commit_release_level(commit) for commit in commits}
    if "major" in levels:
        return "major"
    if "minor" in levels:
        return "minor"
    if "patch" in levels:
        return "patch"
    return None


def release_notes(commits: list[Commit], version: str) -> str:
    groups: list[tuple[str, set[str]]] = [
        ("Breaking Changes", {"major"}),
        ("Features", {"minor"}),
        ("Bug Fixes", {"patch"}),
    ]
    sections: list[str] = [f"# {version}"]
    for title, levels in groups:
        entries = [
            f"- {commit.subject} ({commit.sha[:7]})"
            for commit in commits
            if commit_release_level(commit) in levels
        ]
        if entries:
            sections.extend(["", f"## {title}", "", *entries])
    return "\n".join(sections).rstrip() + "\n"


def local_tag_state(repo: Path, tag: str, head: str) -> dict[str, Any]:
    result = run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=repo,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"status": "absent", "sha": ""}
    tag_sha = require_sha(result.stdout, "tag SHA")
    if tag_sha == head:
        return {"status": "same-sha", "sha": tag_sha}
    return {"status": "conflict", "sha": tag_sha}


def version_plan(
    repo: Path,
    service: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    base_sha: str = "",
    requested_version: str = "",
) -> dict[str, Any]:
    validate_catalog_repository(snapshot, service)
    previous = latest_release(repo, snapshot["sha"])
    commits = commit_log(repo, previous["name"] if previous else None)
    level = strongest_level(commits)
    if requested_version:
        selected = SemVer.parse(requested_version)
        if previous and selected <= SemVer.parse(previous["name"]):
            raise ReleaseError(
                "Requested version must be greater than the previous release"
            )
    else:
        if not previous:
            raise ReleaseError(
                "No reachable SemVer tag; provide --version for an explicitly reviewed initial release"
            )
        if not level:
            raise ReleaseError(
                "No release-worthy Conventional Commit since the previous tag"
            )
        selected = SemVer.parse(previous["name"]).bump(level)
    tag = selected.tag()
    tag_state = local_tag_state(repo, tag, snapshot["sha"])
    if tag_state["status"] == "conflict":
        raise ReleaseError(
            f"Tag {tag} exists locally on another SHA; refusing to move it"
        )
    notes = release_notes(commits, tag)
    return {
        "tag": tag,
        "semver": tag.removeprefix("v"),
        "change_level": level or "explicit",
        "previous_tag": previous["name"] if previous else "",
        "previous_sha": previous["sha"] if previous else "",
        "commits": [
            {
                "sha": item.sha,
                "subject": item.subject,
                "release_level": commit_release_level(item) or "none",
                "authored_at": item.authored_at,
            }
            for item in commits
        ],
        "notes": notes,
        "notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
        "local_tag": tag_state,
        "base_sha": base_sha,
        "policy": {
            "id": POLICY_ID,
            "semantic_release_versions": {
                "semantic-release": "25.0.7",
                "@semantic-release/commit-analyzer": "13.0.1",
                "@semantic-release/release-notes-generator": "14.1.1",
            },
        },
    }


def workflow_inventory(repo: Path) -> dict[str, Any]:
    workflow_dir = repo / ".github/workflows"
    files: list[dict[str, Any]] = []
    if not workflow_dir.exists():
        return {
            "actions_dependency": False,
            "cloud_build_references": [],
            "pre_merge_checks": [],
            "files": [],
        }
    for path in sorted(workflow_dir.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        triggers = [
            name
            for name in ("push", "pull_request", "workflow_dispatch", "workflow_call")
            if re.search(rf"^\s*{re.escape(name)}\s*:", text, re.MULTILINE)
        ]
        limits: list[dict[str, Any]] = []
        for match in re.finditer(
            r"(?P<label>timeout(?:-minutes)?|--task-timeout|timeout)\s*(?:=|:)\s*['\"]?(?P<seconds>\d+)",
            text,
            re.IGNORECASE,
        ):
            value = int(match.group("seconds"))
            if match.group("label").lower() == "timeout-minutes":
                value *= 60
            limits.append(
                {
                    "setting": match.group("label"),
                    "configured_seconds": value,
                    "observed_duration_seconds": None,
                    "interpretation": "configured limit, not an observed duration",
                }
            )
        files.append(
            {
                "path": str(path.relative_to(repo)),
                "name": next(
                    (
                        line.partition(":")[2].strip()
                        for line in text.splitlines()
                        if line.startswith("name:")
                    ),
                    path.stem,
                ),
                "triggers": triggers,
                "uses_actions": bool(
                    re.search(r"^\s*(?:-\s*)?uses:\s*", text, re.MULTILINE)
                ),
                "quality": bool(
                    re.search(r"quality|test|lint|sonar|trivy|semgrep", text, re.I)
                ),
                "release": bool(
                    re.search(
                        r"semantic-release|release|deploy|promote|rollback", text, re.I
                    )
                ),
                "cloud_build": bool(
                    re.search(r"gcloud\s+builds|cloudbuild|--source", text, re.I)
                ),
                "remote_effects": sorted(
                    marker
                    for marker in REMOTE_EFFECT_MARKERS
                    if marker.lower() in text.lower()
                ),
                "configured_limits": limits,
            }
        )
    return {
        "actions_dependency": any(item["uses_actions"] for item in files),
        "cloud_build_references": [
            {"path": item["path"], "effects": item["remote_effects"]}
            for item in files
            if item["cloud_build"]
        ],
        "pre_merge_checks": [
            {
                "path": item["path"],
                "name": item["name"],
                "actions_dependency": item["uses_actions"],
            }
            for item in files
            if "pull_request" in item["triggers"]
        ],
        "files": files,
    }


def iter_context_files(context: Path) -> Iterator[Path]:
    for path in sorted(context.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = set(path.relative_to(context).parts)
        if relative_parts & DEFAULT_EXCLUDED_DIRS:
            continue
        if path.name in {".DS_Store", "release-manifest.json"}:
            continue
        yield path


def context_fingerprint(context: Path) -> str:
    if not context.is_dir():
        raise ReleaseError(f"Docker build context does not exist: {context}")
    entries: list[dict[str, Any]] = []
    for path in iter_context_files(context):
        relative = path.relative_to(context).as_posix()
        entries.append(
            {"path": relative, "size": path.stat().st_size, "sha256": file_digest(path)}
        )
    return digest(entries)


def build_inputs(
    repo: Path, service: dict[str, Any], version: dict[str, Any]
) -> dict[str, Any]:
    deployment = service.get("deployment", {})
    context = (repo / str(deployment.get("build_context", "."))).resolve()
    if not context.is_relative_to(repo.resolve()):
        raise ReleaseError("Catalog build context escapes the repository")
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise ReleaseError(f"Dockerfile is missing from build context: {dockerfile}")
    recipe_files = [dockerfile]
    dockerignore = context / ".dockerignore"
    if dockerignore.exists():
        recipe_files.append(dockerignore)
    recipe = digest(
        [
            {"path": str(item.relative_to(context)), "sha256": file_digest(item)}
            for item in recipe_files
        ]
    )
    dependency_names = {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "go.mod",
        "go.sum",
    }
    dependencies = [
        {"path": path.relative_to(context).as_posix(), "sha256": file_digest(path)}
        for path in iter_context_files(context)
        if path.name in dependency_names
    ]
    base_images = [
        match.group(1)
        for match in re.finditer(
            r"^\s*FROM\s+(\S+)",
            dockerfile.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
    ]
    inputs = {
        "repository": service["repository"],
        "sha": version["source_sha"],
        "recipe_sha256": recipe,
        "context_sha256": context_fingerprint(context),
        "dependencies": dependencies,
        "build_args": {"APP_VERSION": version["tag"]},
        "architecture": platform.machine(),
        "platform": "linux/amd64",
        "base_images": base_images,
    }
    return {
        "context": str(context),
        "dockerfile": str(dockerfile),
        "inputs": inputs,
        "reuse_key": digest(inputs),
        "image_reference": (
            f"{service['region']}-docker.pkg.dev/{service['project_id']}/"
            f"{deployment.get('artifact_repository', '')}/{deployment.get('image_name') or service['service_name']}:{version['tag']}"
        ),
        "local_image": f"cgm-local/{service['service_name']}:{version['semver']}-{version['source_sha'][:12]}",
    }


def default_state_dir() -> Path:
    configured = os.environ.get("CGM_RELEASE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "state" / "cgm-release").resolve()


def state_paths(state_dir: Path) -> dict[str, Path]:
    return {
        "root": state_dir,
        "manifests": state_dir / "manifests",
        "executions": state_dir / "executions",
        "evidence": state_dir / "evidence",
        "artifacts": state_dir / "artifacts",
        "lock": state_dir / "release.lock",
    }


@contextlib.contextmanager
def local_lock(state_dir: Path) -> Iterator[None]:
    paths = state_paths(state_dir)
    paths["root"].mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = paths["lock"].open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        stream.close()


def tool_info(name: str) -> dict[str, Any]:
    command = TOOL_COMMANDS[name]
    executable = shutil.which(command[0])
    if not executable:
        return {"name": name, "available": False, "version": "unavailable"}
    result = run(list(command), check=False, timeout=20)
    output = (result.stdout or result.stderr).strip().splitlines()
    return {
        "name": name,
        "available": result.returncode == 0,
        "version": output[0][:200] if output else "available",
    }


def toolchain_fingerprint(repo: Path) -> dict[str, Any]:
    values = {name: tool_info(name)["version"] for name in TOOL_COMMANDS}
    values["host_architecture"] = platform.machine()
    values["python_executable"] = sys.executable
    return {"values": values, "sha256": digest(values)}


def doctor(repo_path: Path, service: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = repo_snapshot(repo_path)
    profile = ((service or {}).get("quality") or {}).get("profile")
    required = {"git", "python"}
    if profile == "node":
        required |= {"node", "npm"}
    checks = []
    for name in TOOL_COMMANDS:
        info = tool_info(name)
        checks.append({**info, "required": name in required})
    inventory = workflow_inventory(Path(snapshot["path"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "local-phase-1",
        "repository": snapshot,
        "tools": checks,
        "workflow_inventory": inventory,
        "remote_mutations": [],
        "cloud_build_allowed": False,
        "quality_policy": {
            "id": POLICY_ID,
            "requires_exact_sha": True,
            "requires_base_sha": profile in {"python", "node"},
            "requires_differential_evidence": profile in {"python", "node"},
            "differential_threshold": 80.0,
            "evidence_ttl_hours": QUALITY_TTL_HOURS,
        },
        "pre_merge_independent": not any(
            item["actions_dependency"] for item in inventory["pre_merge_checks"]
        ),
    }


def quality_status(report: dict[str, Any]) -> tuple[str, list[str]]:
    checks = report.get("checks")
    if not isinstance(checks, list):
        return "FAILED", ["Quality report has no checks"]
    failed = [
        str(item.get("name", "unknown"))
        for item in checks
        if item.get("status") == "FAILED"
    ]
    return (
        ("PASSED", [])
        if not failed
        else ("FAILED", [f"Failed checks: {', '.join(failed)}"])
    )


def differential_status(
    path: Path | None,
    *,
    base_sha: str,
    profile: str,
) -> tuple[str, list[str], dict[str, Any] | None]:
    if profile == "static":
        return "PASSED", [], None
    if not path:
        return "FAILED", ["Missing differential coverage evidence for oss-v2"], None
    value = read_json(path)
    if not isinstance(value, dict):
        return "FAILED", ["Differential coverage report is not an object"], None
    errors: list[str] = []
    if value.get("policy_version") != POLICY_ID:
        errors.append("Differential report is not for oss-v2")
    if value.get("base_sha") != base_sha:
        errors.append("Differential report base SHA does not match")
    threshold = float(value.get("differential_threshold", 0) or 0)
    coverage = value.get("differential_coverage")
    if threshold < 80:
        errors.append("Differential threshold cannot be lowered below 80%")
    if coverage is not None and float(coverage) < threshold:
        errors.append(f"Changed-line coverage {coverage}% is below {threshold}%")
    if coverage is None and int(value.get("changed_lines", 0) or 0) != 0:
        errors.append(
            "Differential report has no coverage for changed executable lines"
        )
    return ("PASSED" if not errors else "FAILED", errors, value)


def validate_evidence(
    evidence_path: Path,
    *,
    service: dict[str, Any],
    snapshot: dict[str, Any],
    base_sha: str,
    current_toolchain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = read_json(evidence_path)
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != SCHEMA_VERSION
    ):
        raise ReleaseError("Evidence is not a local release evidence record")
    expected = {
        "service_name": service["service_name"],
        "repository": service["repository"],
        "commit_sha": snapshot["sha"],
        "base_sha": base_sha,
        "policy_id": POLICY_ID,
    }
    labels = {"base_sha": "base SHA", "commit_sha": "commit SHA", "policy_id": "policy"}
    mismatches = [
        f"{labels.get(key, key)} mismatch"
        for key, value in expected.items()
        if evidence.get(key) != value
    ]
    if mismatches:
        raise ReleaseError("Evidence cannot be reused: " + ", ".join(mismatches))
    if (
        evidence.get("quality_gate_status") != "PASSED"
        or evidence.get("policy_status") != "PASSED"
    ):
        raise ReleaseError("Evidence is not a passed oss-v2 gate")
    try:
        expires = datetime.fromisoformat(
            str(evidence["expires_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("Evidence expiry is invalid") from exc
    if utc_now() > expires:
        raise ReleaseError("Evidence is stale")
    if current_toolchain and evidence.get("toolchain", {}).get(
        "sha256"
    ) != current_toolchain.get("sha256"):
        raise ReleaseError("Evidence toolchain fingerprint differs")
    return evidence


def find_reusable_evidence(
    state_dir: Path,
    *,
    service: dict[str, Any],
    snapshot: dict[str, Any],
    base_sha: str,
    current_toolchain: dict[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted(state_paths(state_dir)["evidence"].glob("*.json")):
        try:
            evidence = validate_evidence(
                path,
                service=service,
                snapshot=snapshot,
                base_sha=base_sha,
                current_toolchain=current_toolchain,
            )
        except ReleaseError:
            continue
        return path, evidence
    return None


def find_manifest(
    state_dir: Path, service: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted(state_paths(state_dir)["manifests"].glob("*.json")):
        try:
            manifest = read_json(path)
        except ReleaseError:
            continue
        source = manifest.get("source", {})
        if (
            manifest.get("service_name") == service.get("service_name")
            and manifest.get("repository") == service.get("repository")
            and source.get("sha") == snapshot.get("sha")
        ):
            return path, manifest
    return None


def build_manifest(
    platform_root: Path,
    service_name: str,
    repo_path: Path,
    *,
    state_dir: Path,
    base_sha_arg: str = "",
    requested_version: str = "",
    allow_dirty: bool = False,
    quality_evidence_path: Path | None = None,
) -> dict[str, Any]:
    service = load_catalog(platform_root, service_name)
    snapshot = repo_snapshot(repo_path)
    validate_catalog_repository(snapshot, service)
    if snapshot["dirty"] and not allow_dirty:
        raise ReleaseError(
            "Working tree is dirty; review and commit the exact SHA or pass --allow-dirty for a non-publishable plan"
        )
    base_sha = resolve_base_sha(Path(snapshot["path"]), snapshot["sha"], base_sha_arg)
    version = version_plan(
        Path(snapshot["path"]),
        service,
        snapshot,
        base_sha=base_sha,
        requested_version=requested_version,
    )
    version["source_sha"] = snapshot["sha"]
    artifact = build_inputs(Path(snapshot["path"]), service, version)
    toolchain = toolchain_fingerprint(Path(snapshot["path"]))
    evidence = None
    quality_errors: list[str] = []
    if quality_evidence_path:
        evidence = validate_evidence(
            quality_evidence_path,
            service=service,
            snapshot=snapshot,
            base_sha=base_sha,
            current_toolchain=toolchain,
        )
    else:
        reusable = find_reusable_evidence(
            state_dir,
            service=service,
            snapshot=snapshot,
            base_sha=base_sha,
            current_toolchain=toolchain,
        )
        if reusable:
            quality_evidence_path, evidence = reusable
        else:
            quality_errors.append("No reusable exact-SHA oss-v2 evidence")
    inventory = workflow_inventory(Path(snapshot["path"]))
    release_id = str(uuid.uuid4())
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "created_at": iso(utc_now()),
        "service_name": service["service_name"],
        "repository": service["repository"],
        "catalog": {
            "project_id": service.get("project_id", ""),
            "region": service.get("region", ""),
            "deployment": service.get("deployment", {}),
            "quality": service.get("quality", {}),
        },
        "source": {
            **snapshot,
            "reviewed_sha": snapshot["sha"],
            "base_sha": base_sha,
            "publishable": not snapshot["dirty"],
        },
        "version": version,
        "quality": {
            "policy_id": POLICY_ID,
            "status": "PASSED" if evidence else "PENDING",
            "evidence_path": str(quality_evidence_path)
            if quality_evidence_path
            else "",
            "errors": quality_errors,
            "ttl_hours": QUALITY_TTL_HOURS,
        },
        "artifact": {
            **artifact,
            "status": "REUSE_CANDIDATE" if artifact.get("reuse_key") else "NOT_BUILT",
            "digest": "",
            "remote_published": False,
        },
        "dependencies": {
            "workflow_inventory": inventory,
            "pre_merge_checks_depend_on_actions": bool(
                any(
                    item["actions_dependency"] for item in inventory["pre_merge_checks"]
                )
            ),
            "cloud_build_in_new_path": False,
        },
        "stages": [
            {
                "name": stage,
                "status": "succeeded" if stage == "plan" else "pending",
                "effect": "none",
            }
            for stage in RELEASE_STAGES
        ],
        "remote_effects": [],
        "transition": {
            "phase": "phase-1",
            "remote_mutations_allowed": False,
            "actions_dispatch": False,
            "cloud_build": False,
            "note": "This manifest is preparatory and cannot publish or deploy.",
        },
    }


def next_attempt(state_dir: Path, release_id: str) -> int:
    attempts = []
    for path in state_paths(state_dir)["executions"].glob("*.json"):
        try:
            record = read_json(path)
        except ReleaseError:
            continue
        if record.get("release_id") == release_id:
            attempts.append(int(record.get("attempt", 0)))
    return max(attempts, default=0) + 1


def prepare(
    platform_root: Path,
    service_name: str,
    repo_path: Path,
    *,
    state_dir: Path,
    base_sha: str = "",
    requested_version: str = "",
    allow_dirty: bool = False,
    quality_evidence: Path | None = None,
) -> dict[str, Any]:
    paths = state_paths(state_dir)
    with local_lock(state_dir):
        service = load_catalog(platform_root, service_name)
        snapshot = repo_snapshot(repo_path)
        existing = find_manifest(state_dir, service, snapshot)
        if existing:
            manifest_path, manifest = existing
            if (
                requested_version
                and manifest.get("version", {}).get("tag")
                != SemVer.parse(requested_version).tag()
            ):
                raise ReleaseError(
                    "An existing release identity already uses another version"
                )
        else:
            manifest = build_manifest(
                platform_root,
                service_name,
                repo_path,
                state_dir=state_dir,
                base_sha_arg=base_sha,
                requested_version=requested_version,
                allow_dirty=allow_dirty,
                quality_evidence_path=quality_evidence,
            )
            manifest_path = paths["manifests"] / f"{manifest['release_id']}.json"
            write_json(manifest_path, manifest)
        execution_id = str(uuid.uuid4())
        attempt = next_attempt(state_dir, manifest["release_id"])
        execution = {
            "schema_version": SCHEMA_VERSION,
            "execution_id": execution_id,
            "release_id": manifest["release_id"],
            "attempt": attempt,
            "created_at": iso(utc_now()),
            "status": "PREPARED",
            "current_stage": "plan",
            "manifest_path": str(manifest_path),
            "events": [
                {
                    "stage": "plan",
                    "intent": "persist local release manifest",
                    "result": "CONFIRMED",
                    "remote_effect": False,
                }
            ],
            "unknown_effects": [],
            "remote_effects": [],
            "reused_release_identity": bool(existing),
        }
        execution_path = paths["executions"] / f"{execution_id}.json"
        write_json(execution_path, execution)
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "execution": execution,
        "execution_path": str(execution_path),
    }


def run_quality(
    platform_root: Path,
    service_name: str,
    repo_path: Path,
    *,
    state_dir: Path,
    base_sha_arg: str = "",
    differential_report: Path | None = None,
    command_overrides: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    service = load_catalog(platform_root, service_name)
    snapshot = repo_snapshot(repo_path)
    validate_catalog_repository(snapshot, service)
    if snapshot["dirty"]:
        raise ReleaseError(
            "Working tree is dirty; quality evidence must be produced from the exact reviewed SHA"
        )
    profile = str((service.get("quality") or {}).get("profile") or "")
    if profile not in {"python", "node", "static"}:
        raise ReleaseError("Catalog quality profile is not configured")
    base_sha = resolve_base_sha(Path(snapshot["path"]), snapshot["sha"], base_sha_arg)
    evidence_id = str(uuid.uuid4())
    evidence_dir = state_paths(state_dir)["evidence"] / evidence_id
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_report_path = evidence_dir / "quality-report.json"
    report_dir = evidence_dir / "quality-reports"
    args = [
        sys.executable,
        str(platform_root / "scripts/quality/quality_gate.py"),
        "--service-name",
        service_name,
        "--repository",
        service["repository"],
        "--commit-sha",
        snapshot["sha"],
        "--branch",
        snapshot["branch"],
        "--profile",
        profile,
        "--working-directory",
        snapshot["path"],
        "--coverage-threshold",
        str((service.get("quality") or {}).get("coverage_threshold", 70)),
        "--output",
        str(raw_report_path),
        "--report-directory",
        str(report_dir),
    ]
    for name, option in (
        ("install", "--install-command"),
        ("tests", "--test-command"),
        ("build", "--build-command"),
        ("lint", "--lint-command"),
        ("format", "--format-command"),
        ("typecheck", "--typecheck-command"),
    ):
        value = (command_overrides or {}).get(name)
        if value is not None:
            args.extend([option, value])
    started = time.monotonic()
    completed = run(args, check=False)
    (evidence_dir / "quality-command.log").write_text(
        (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
    )
    if not raw_report_path.exists():
        raise ReleaseError("Quality runner did not emit its normalized report")
    report = read_json(raw_report_path)
    gate_status, gate_errors = quality_status(report)
    diff_status, diff_errors, diff_value = differential_status(
        differential_report,
        base_sha=base_sha,
        profile=profile,
    )
    errors = gate_errors + diff_errors
    policy_status = (
        "PASSED" if gate_status == "PASSED" and diff_status == "PASSED" else "FAILED"
    )
    generated = utc_now()
    toolchain = toolchain_fingerprint(Path(snapshot["path"]))
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "service_name": service_name,
        "repository": service["repository"],
        "commit_sha": snapshot["sha"],
        "base_sha": base_sha,
        "policy_id": POLICY_ID,
        "profile": profile,
        "generated_at": iso(generated),
        "expires_at": iso(generated + timedelta(hours=QUALITY_TTL_HOURS)),
        "toolchain": toolchain,
        "quality_gate_status": gate_status,
        "policy_status": policy_status,
        "errors": errors,
        "differential": diff_value,
        "report_sha256": file_digest(raw_report_path),
        "report_path": str(raw_report_path),
        "duration_seconds": round(time.monotonic() - started, 3),
        "source": "local",
    }
    evidence_path = state_paths(state_dir)["evidence"] / f"{evidence_id}.json"
    write_json(evidence_path, evidence)
    return (
        0 if completed.returncode == 0 and policy_status == "PASSED" else 1,
        {"evidence": evidence, "evidence_path": str(evidence_path)},
    )


def build_local(
    manifest_path: Path,
    *,
    state_dir: Path,
    execute: bool,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ReleaseError("Invalid release manifest")
    artifact = manifest.get("artifact", {})
    key = artifact.get("reuse_key")
    for path in sorted(state_paths(state_dir)["artifacts"].glob("*.json")):
        value = read_json(path)
        if value.get("reuse_key") == key and value.get("status") == "AVAILABLE":
            return {
                "action": "reused",
                "artifact": value,
                "manifest_path": str(manifest_path),
            }
    if not execute:
        return {
            "action": "planned",
            "manifest_path": str(manifest_path),
            "reuse_key": key,
            "local_image": artifact.get("local_image", ""),
            "remote_push": False,
        }
    if manifest.get("quality", {}).get("status") != "PASSED":
        raise ReleaseError(
            "Local build execution requires a passed oss-v2 quality record"
        )
    context = Path(artifact["context"])
    image = artifact["local_image"]
    completed = run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "--platform",
            "linux/amd64",
            "--tag",
            image,
            "--build-arg",
            f"APP_VERSION={manifest['version']['tag']}",
            "--label",
            f"org.cgm.release.reuse-key={key}",
            "--label",
            f"org.opencontainers.image.revision={manifest['source']['sha']}",
            "--label",
            f"org.opencontainers.image.version={manifest['version']['tag']}",
            str(context),
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseError("Local Docker/BuildKit build failed")
    inspected = run(
        ["docker", "image", "inspect", image, "--format={{.Id}}"], check=False
    )
    if inspected.returncode != 0 or not inspected.stdout.strip():
        raise ReleaseError("Local image was not found after build")
    artifact_record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": str(uuid.uuid4()),
        "status": "AVAILABLE",
        "reuse_key": key,
        "image": image,
        "image_id": inspected.stdout.strip(),
        "repository": manifest["repository"],
        "service_name": manifest["service_name"],
        "source_sha": manifest["source"]["sha"],
        "version": manifest["version"]["tag"],
        "created_at": iso(utc_now()),
        "remote_published": False,
    }
    path = (
        state_paths(state_dir)["artifacts"] / f"{artifact_record['artifact_id']}.json"
    )
    write_json(path, artifact_record)
    return {"action": "built", "artifact": artifact_record, "artifact_path": str(path)}


def resume(state_dir: Path, release_id: str) -> dict[str, Any]:
    manifest_matches = []
    for path in state_paths(state_dir)["manifests"].glob("*.json"):
        manifest = read_json(path)
        if manifest.get("release_id") == release_id:
            manifest_matches.append((path, manifest))
    if len(manifest_matches) != 1:
        raise ReleaseError("Release identity is missing or not unique")
    executions = []
    for path in state_paths(state_dir)["executions"].glob("*.json"):
        record = read_json(path)
        if record.get("release_id") == release_id:
            executions.append(record)
    latest = max(executions, key=lambda item: item.get("attempt", 0), default=None)
    pending = next(
        (
            stage["name"]
            for stage in manifest_matches[0][1].get("stages", [])
            if stage.get("status") == "pending"
        ),
        "complete",
    )
    return {
        "release_id": release_id,
        "manifest_path": str(manifest_matches[0][0]),
        "latest_execution": latest,
        "first_pending_stage": pending,
        "remote_query_performed": False,
        "safe_to_continue": pending in {"quality", "build"},
    }


def output(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                print(f"{key}: {item}")
    else:
        print(value)


def common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service", dest="service_name", required=True)
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).parents[2])
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--json", action="store_true", dest="as_json")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser(
        "doctor", help="inspect tools and workflow dependencies"
    )
    doctor_parser.add_argument("--repo-path", type=Path, required=True)
    doctor_parser.add_argument("--service", dest="service_name", default="")
    doctor_parser.add_argument(
        "--platform-root", type=Path, default=Path(__file__).parents[2]
    )
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    for name, help_text in (
        ("plan", "calculate exact SHA, version and local notes"),
        ("version", "calculate the proposed SemVer without writing state"),
        ("notes", "generate deterministic release notes without writing state"),
        ("prepare", "persist a manifest and a separate execution attempt"),
        ("quality", "run or validate local quality evidence"),
    ):
        command = commands.add_parser(name, help=help_text)
        common_arguments(command)
        command.add_argument("--base-sha", default="")
        command.add_argument("--version", dest="requested_version", default="")
        if name in {"plan", "prepare"}:
            command.add_argument("--allow-dirty", action="store_true")
            command.add_argument("--quality-evidence", type=Path)
        if name == "quality":
            command.add_argument("--differential-report", type=Path)
            for option in ("install", "tests", "build", "lint", "format", "typecheck"):
                command.add_argument(f"--{option}-command")

    for name, help_text in (
        ("publish", "publish one artifact, exact tag and GitHub Release idempotently"),
        (
            "candidate",
            "deploy one published digest as a zero-traffic Cloud Run candidate",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--state-dir", type=Path, default=default_state_dir())
        command.add_argument("--execute", action="store_true")
        command.add_argument("--confirm-remote-effects", action="store_true")
        command.add_argument("--json", action="store_true", dest="as_json")

    for name, help_text, confirmation in (
        (
            "promote",
            "promote an exact candidate revision to production",
            "PROMOTE_PROD",
        ),
        (
            "rollback",
            "restore traffic to an explicit known-good revision",
            "ROLLBACK_PROD",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--state-dir", type=Path, default=default_state_dir())
        command.add_argument("--target-revision", default="")
        command.add_argument("--execute", action="store_true")
        command.add_argument(
            "--confirm", default="", help=f"type {confirmation} for a live effect"
        )
        command.add_argument("--confirm-remote-effects", action="store_true")
        command.add_argument("--json", action="store_true", dest="as_json")

    register_parser = commands.add_parser(
        "register", help="register one exact release state in Engineering Platform"
    )
    register_parser.add_argument("--manifest", type=Path, required=True)
    register_parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    register_parser.add_argument(
        "--status",
        choices=("candidate", "promoted", "rolled_back"),
        default="candidate",
    )
    register_parser.add_argument("--revision", default="")
    register_parser.add_argument(
        "--platform-api-url", default=os.environ.get("ENG_PLATFORM_API_URL", "")
    )
    register_parser.add_argument("--execute", action="store_true")
    register_parser.add_argument("--confirm-remote-effects", action="store_true")
    register_parser.add_argument("--json", action="store_true", dest="as_json")

    sanplat_parser = commands.add_parser(
        "sanplat", help="plan a coordinated SanPlat API/Web corporate window"
    )
    sanplat_parser.add_argument("--api-manifest", type=Path, required=True)
    sanplat_parser.add_argument("--web-manifest", type=Path, required=True)
    sanplat_parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    sanplat_parser.add_argument("--release-group-id", default="")
    sanplat_parser.add_argument("--auxiliary-service", action="append", default=[])
    sanplat_parser.add_argument("--execute", action="store_true")
    sanplat_parser.add_argument("--confirm-remote-effects", action="store_true")
    sanplat_parser.add_argument("--json", action="store_true", dest="as_json")

    adoption_parser = commands.add_parser(
        "adopt", help="generate a phase-5 service adoption plan"
    )
    adoption_parser.add_argument("--service", dest="service_name", required=True)
    adoption_parser.add_argument("--repo-path", type=Path, required=True)
    adoption_parser.add_argument(
        "--platform-root", type=Path, default=Path(__file__).parents[2]
    )
    adoption_parser.add_argument("--json", action="store_true", dest="as_json")

    build_parser = commands.add_parser(
        "build", help="plan or execute one local Docker/BuildKit build"
    )
    build_parser.add_argument("--manifest", type=Path, required=True)
    build_parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    build_parser.add_argument("--execute", action="store_true")
    build_parser.add_argument("--json", action="store_true", dest="as_json")

    resume_parser = commands.add_parser(
        "resume", help="read the first safe local pending stage"
    )
    resume_parser.add_argument("--release-id", required=True)
    resume_parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    resume_parser.add_argument(
        "--reconcile",
        action="store_true",
        help="perform read-only remote reconciliation before reporting the next stage",
    )
    resume_parser.add_argument("--json", action="store_true", dest="as_json")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        platform_root = (
            Path(getattr(args, "platform_root", Path(__file__).parents[2]))
            .expanduser()
            .resolve()
        )
        state_dir = (
            Path(getattr(args, "state_dir", default_state_dir())).expanduser().resolve()
        )
        lifecycle = None
        if args.command in {
            "publish",
            "candidate",
            "promote",
            "rollback",
            "register",
            "sanplat",
            "adopt",
            "resume",
        }:
            try:
                from . import release_lifecycle as lifecycle
            except ImportError:
                import release_lifecycle as lifecycle  # type: ignore[no-redef]
        if args.command == "doctor":
            service = (
                load_catalog(platform_root, args.service_name)
                if args.service_name
                else None
            )
            value = doctor(args.repo_path, service)
        elif args.command in {"plan", "version", "notes"}:
            service = load_catalog(platform_root, args.service_name)
            snapshot = repo_snapshot(args.repo_path)
            if snapshot["dirty"] and not getattr(args, "allow_dirty", False):
                raise ReleaseError(
                    "Working tree is dirty; use --allow-dirty only for a non-publishable plan"
                )
            base_sha = resolve_base_sha(
                Path(snapshot["path"]), snapshot["sha"], args.base_sha
            )
            plan = version_plan(
                Path(snapshot["path"]),
                service,
                snapshot,
                base_sha=base_sha,
                requested_version=args.requested_version,
            )
            plan["source_sha"] = snapshot["sha"]
            plan["repository"] = service["repository"]
            plan["service_name"] = args.service_name
            if args.command == "version":
                value = {
                    key: plan[key]
                    for key in (
                        "tag",
                        "semver",
                        "change_level",
                        "previous_tag",
                        "previous_sha",
                        "local_tag",
                    )
                }
            elif args.command == "notes":
                value = {
                    "tag": plan["tag"],
                    "notes": plan["notes"],
                    "sha256": plan["notes_sha256"],
                    "commit_count": len(plan["commits"]),
                }
            else:
                value = {
                    "source": snapshot,
                    "base_sha": base_sha,
                    "service_name": args.service_name,
                    "repository": service["repository"],
                    "version": plan,
                    "workflow_inventory": workflow_inventory(Path(snapshot["path"])),
                    "remote_mutations": [],
                }
        elif args.command == "prepare":
            value = prepare(
                platform_root,
                args.service_name,
                args.repo_path,
                state_dir=state_dir,
                base_sha=args.base_sha,
                requested_version=args.requested_version,
                allow_dirty=args.allow_dirty,
                quality_evidence=args.quality_evidence,
            )
        elif args.command == "quality":
            overrides = {
                key: getattr(args, f"{key}_command")
                for key in ("install", "tests", "build", "lint", "format", "typecheck")
                if getattr(args, f"{key}_command") is not None
            }
            status, value = run_quality(
                platform_root,
                args.service_name,
                args.repo_path,
                state_dir=state_dir,
                base_sha_arg=args.base_sha,
                differential_report=args.differential_report,
                command_overrides=overrides,
            )
            output(value, args.as_json)
            return status
        elif args.command == "publish":
            value = lifecycle.publish(
                args.manifest,
                state_dir=state_dir,
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
            )
        elif args.command == "candidate":
            value = lifecycle.candidate(
                args.manifest,
                state_dir=state_dir,
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
            )
        elif args.command == "promote":
            value = lifecycle.promote(
                args.manifest,
                state_dir=state_dir,
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
                confirmation=args.confirm,
            )
        elif args.command == "rollback":
            value = lifecycle.rollback(
                args.manifest,
                state_dir=state_dir,
                target_revision=args.target_revision,
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
                confirmation=args.confirm,
            )
        elif args.command == "register":
            value = lifecycle.register_release(
                args.manifest,
                state_dir=state_dir,
                status=args.status,
                revision=args.revision,
                platform_api_url=args.platform_api_url,
                token=os.environ.get("ENG_PLATFORM_RELEASE_TOKEN", ""),
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
            )
        elif args.command == "sanplat":
            value = lifecycle.sanplat(
                args.api_manifest,
                args.web_manifest,
                state_dir=state_dir,
                release_group_id=args.release_group_id,
                auxiliary_services=args.auxiliary_service,
                execute=args.execute,
                confirm_remote_effects=args.confirm_remote_effects,
            )
        elif args.command == "adopt":
            value = lifecycle.adoption_plan(
                platform_root, args.repo_path, args.service_name
            )
        elif args.command == "resume":
            value = lifecycle.resume(
                state_dir, args.release_id, reconcile=args.reconcile
            )
        elif args.command == "build":
            value = build_local(
                args.manifest.resolve(), state_dir=state_dir, execute=args.execute
            )
        output(value, args.as_json)
        return 0
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
