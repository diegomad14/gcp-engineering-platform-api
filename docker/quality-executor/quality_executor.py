#!/usr/bin/env python3
"""Run quality without control-plane credentials and publish it separately.

``run`` executes repository-owned commands and only writes a local manifest.
``start``/``prepare`` and ``publish`` are trusted control-plane operations and
must run in different containers which do not mount the repository (except for
the fixed history preparation operation).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from quality_profiles import (
    QualityProfileError,
    available_services,
    verify_profile_hash,
)


_UNTRUSTED_UID = 65532
_UNTRUSTED_GID = 65532
_RESULT_NAME = "quality-result.json"
_RESULT_KIND = "eng-platform-quality-result"
_EVENT_TOKEN_NAME = "event-token"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_SENSITIVE_ENV_NAMES = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GHA_CREDS_PATH",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


class QualityExecutorError(RuntimeError):
    """An executor invariant was not satisfied."""


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise QualityExecutorError(f"Missing required engine identity: {name}")
    return value


def _identity() -> dict[str, str]:
    head = _required("ENG_PLATFORM_RELEASE_HEAD_SHA").lower()
    base = _required("ENG_PLATFORM_RELEASE_BASE_SHA").lower()
    if not _SHA.fullmatch(head) or not _SHA.fullmatch(base) or head == base:
        raise QualityExecutorError("Invalid release source identity")
    operation = _required("ENG_PLATFORM_RELEASE_OPERATION")
    if operation not in {"pr_quality", "main_release"}:
        raise QualityExecutorError("Unsupported release operation")
    fingerprint = _required("ENG_PLATFORM_RELEASE_FINGERPRINT")
    profile_hash = _required("ENG_PLATFORM_RELEASE_PROFILE_SHA256")
    provider_run_id = os.environ.get("ENG_PLATFORM_PROVIDER_RUN_ID") or os.environ.get(
        "GITHUB_RUN_ID", ""
    )
    if not _HASH.fullmatch(fingerprint) or not _HASH.fullmatch(profile_hash):
        raise QualityExecutorError("Invalid release execution hash")
    if not provider_run_id:
        raise QualityExecutorError("Missing provider run identity")
    repository = _required("ENG_PLATFORM_RELEASE_REPOSITORY")
    if not _REPOSITORY.fullmatch(repository):
        raise QualityExecutorError("Invalid GitHub repository identity")
    return {
        "execution_id": _required("ENG_PLATFORM_RELEASE_EXECUTION_ID"),
        "fingerprint": fingerprint,
        "service_name": _required("ENG_PLATFORM_RELEASE_SERVICE"),
        "repository": repository,
        "head_sha": head,
        "base_sha": base,
        "operation": operation,
        "profile_hash": profile_hash,
        "provider_run_id": provider_run_id,
    }


def _json_request(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = (
        json.dumps(data, separators=(",", ":")).encode() if data is not None else None
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        value = json.load(response)
    if not isinstance(value, dict):
        raise QualityExecutorError("Control-plane response was not an object")
    return value


def _identity_token(audience: str) -> str:
    actions_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    actions_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if actions_url and actions_token:
        url = (
            actions_url
            + ("&" if "?" in actions_url else "?")
            + urllib.parse.urlencode(
                {"audience": "engineering-platform-release-orchestrator"}
            )
        )
        value = _json_request(url, headers={"Authorization": f"Bearer {actions_token}"})
        token = value.get("value")
        if not isinstance(token, str) or not token:
            raise QualityExecutorError("GitHub did not issue a workflow identity")
        return token
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?"
        + urllib.parse.urlencode({"audience": audience})
    )
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
        return response.read().decode()


def _event_token_path(control_dir: Path) -> Path:
    return control_dir / _EVENT_TOKEN_NAME


def _read_event_token(control_dir: Path) -> str:
    path = _event_token_path(control_dir)
    try:
        stat = path.stat(follow_symlinks=False)
        if not path.is_file() or stat.st_mode & 0o077:
            raise QualityExecutorError("Execution event token permissions are unsafe")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise QualityExecutorError("Execution event token is unavailable") from exc
    if not 32 <= len(token) <= 512 or any(character.isspace() for character in token):
        raise QualityExecutorError("Execution event token is invalid")
    return token


def _claim_event_token(control_dir: Path) -> str:
    path = _event_token_path(control_dir)
    if path.exists():
        return _read_event_token(control_dir)
    api = _required("ENG_PLATFORM_API_URL").rstrip("/")
    identity = _identity()
    oidc_token = _identity_token(api)
    value = _json_request(
        f"{api}/api/internal/release-executions/{identity['execution_id']}/event-token",
        data={
            "fingerprint": identity["fingerprint"],
            "provider_run_id": identity["provider_run_id"],
        },
        headers={"Authorization": f"Bearer {oidc_token}"},
    )
    event_token = value.get("event_token")
    if (
        not isinstance(event_token, str)
        or not 32 <= len(event_token) <= 512
        or any(character.isspace() for character in event_token)
    ):
        raise QualityExecutorError("Control plane did not issue an event token")
    control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(control_dir, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".event-token.", dir=control_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(event_token)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return event_token


def _event(
    sequence: int, status: str, control_dir: Path, **changes: Any
) -> dict[str, Any]:
    api = _required("ENG_PLATFORM_API_URL").rstrip("/")
    identity = _identity()
    if not identity["provider_run_id"]:
        raise QualityExecutorError("Missing provider run identity")
    payload = {
        "execution_id": identity["execution_id"],
        "provider_run_id": identity["provider_run_id"],
        "fingerprint": identity["fingerprint"],
        "sequence": sequence,
        "status": status,
        **changes,
    }
    token = _identity_token(api)
    return _json_request(
        f"{api}/api/internal/release-executions/{identity['execution_id']}/events",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Eng-Platform-Event-Token": _read_event_token(control_dir),
        },
    )


def _source_token(identity: dict[str, str]) -> str:
    api = _required("ENG_PLATFORM_API_URL").rstrip("/")
    token = _identity_token(api)
    value = _json_request(
        f"{api}/api/internal/release-executions/{identity['execution_id']}/source-token",
        data={
            "fingerprint": identity["fingerprint"],
            "provider_run_id": identity["provider_run_id"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    source_token = value.get("token")
    if not isinstance(source_token, str) or not source_token:
        raise QualityExecutorError("Control plane did not issue a source token")
    return source_token


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _verify_checkout(source: Path, identity: dict[str, str]) -> None:
    if not source.is_dir() or not (source / ".git").exists():
        raise QualityExecutorError("Source checkout is not a Git repository")
    try:
        actual = _git(
            source, "-c", f"safe.directory={source}", "rev-parse", "HEAD"
        ).lower()
    except subprocess.CalledProcessError as exc:
        raise QualityExecutorError("Unable to read source checkout") from exc
    if actual != identity["head_sha"]:
        raise QualityExecutorError("Checkout does not match the authorized head SHA")


def _prepare_history(source: Path, identity: dict[str, str]) -> None:
    """Fetch exact history with a one-use token; never run repository code."""
    if not source.is_dir():
        raise QualityExecutorError("Source volume is unavailable")
    bootstrap = not (source / ".git").exists()
    if bootstrap and any(source.iterdir()):
        raise QualityExecutorError("Source volume is not empty")
    if not bootstrap:
        _verify_checkout(source, identity)
    repository = identity.get("repository", "")
    if not _REPOSITORY.fullmatch(repository):
        raise QualityExecutorError("Invalid GitHub repository identity")
    repository_url = f"https://github.com/{repository}.git"
    token = _source_token(identity)
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.mkdtemp(prefix="eng-platform-git-home-"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
        "GIT_CONFIG_KEY_1": "http.followRedirects",
        "GIT_CONFIG_VALUE_1": "false",
    }
    try:
        if bootstrap:
            _git(source, "init", "--quiet", env=environment)
        _git(
            source,
            "-c",
            f"safe.directory={source}",
            "fetch",
            "--force",
            "--no-recurse-submodules",
            repository_url,
            *(
                (f"+{identity['head_sha']}:refs/heads/eng-platform-quality-head",)
                if bootstrap
                else ()
            ),
            f"+{identity['base_sha']}:refs/heads/eng-platform-quality-base",
            "+refs/tags/*:refs/tags/*",
            env=environment,
        )
        if bootstrap:
            _git(
                source,
                "-c",
                f"safe.directory={source}",
                "checkout",
                "--detach",
                identity["head_sha"],
                env=environment,
            )
            _verify_checkout(source, identity)
        resolved = _git(
            source,
            "-c",
            f"safe.directory={source}",
            "rev-parse",
            f"{identity['base_sha']}^{{commit}}",
        )
        if resolved.lower() != identity["base_sha"]:
            raise QualityExecutorError("Fetched base SHA does not match authorization")
    finally:
        environment["GIT_CONFIG_VALUE_0"] = ""
        authorization = ""
        token = ""
        shutil.rmtree(environment["HOME"], ignore_errors=True)


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    os.chown(root, uid, gid, follow_symlinks=False)
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)


def _isolated_checkout(
    source: Path, scratch: Path, identity: dict[str, str]
) -> tuple[Path, Path, Path]:
    _verify_checkout(source, identity)
    runtime = scratch / "runtime"
    trusted = scratch / "trusted"
    destination = runtime / "repository"
    git_directory = trusted / "repository.git"
    for path in (runtime, trusted):
        if path.exists():
            shutil.rmtree(path)
    runtime.mkdir(parents=True, exist_ok=True)
    trusted.mkdir(parents=True, exist_ok=True)
    try:
        _git(
            scratch,
            "-c",
            f"safe.directory={source}",
            "clone",
            "--local",
            "--no-single-branch",
            "--no-hardlinks",
            "--no-checkout",
            "--no-recurse-submodules",
            "--separate-git-dir",
            str(git_directory),
            str(source),
            str(destination),
        )
        _git(destination, "checkout", "--detach", identity["head_sha"])
        if _git(destination, "rev-parse", "HEAD").lower() != identity["head_sha"]:
            raise QualityExecutorError("Isolated checkout changed the authorized SHA")
        if (
            _git(destination, "rev-parse", f"{identity['base_sha']}^{{commit}}").lower()
            != identity["base_sha"]
        ):
            raise QualityExecutorError("Authorized base SHA is missing")
        _git(destination, "remote", "remove", "origin")
        _git(destination, "config", "core.hooksPath", "/dev/null")
    except subprocess.CalledProcessError as exc:
        raise QualityExecutorError("Unable to create isolated source checkout") from exc
    for directory, names, files in os.walk(trusted, followlinks=False):
        os.chown(directory, 0, 0, follow_symlinks=False)
        os.chmod(directory, os.stat(directory, follow_symlinks=False).st_mode & ~0o022)
        for name in [*names, *files]:
            path = Path(directory) / name
            os.chown(path, 0, 0, follow_symlinks=False)
            if not path.is_symlink():
                os.chmod(path, os.stat(path, follow_symlinks=False).st_mode & ~0o022)
    return destination, git_directory, runtime


def _child_environment(
    runtime: Path,
    identity: dict[str, str],
    profile: dict[str, Any],
    checkout: Path | None = None,
    git_directory: Path | None = None,
) -> dict[str, str]:
    home = runtime / "home"
    home.mkdir(parents=True, exist_ok=True)
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if profile["runtime"] == "python":
        virtualenv = runtime / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(virtualenv)],
            check=True,
        )
        path = f"{virtualenv / 'bin'}:{path}"
    environment = {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(runtime / "tmp"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "NPM_CONFIG_CACHE": str(home / ".npm"),
        "PIP_CACHE_DIR": str(home / ".cache" / "pip"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "CI": "true",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ENG_PLATFORM_RELEASE_HEAD_SHA": identity["head_sha"],
        "ENG_PLATFORM_RELEASE_BASE_SHA": identity["base_sha"],
    }
    if checkout is not None and git_directory is not None:
        environment["GIT_DIR"] = str(git_directory)
        environment["GIT_WORK_TREE"] = str(checkout)
    if profile.get("postgres"):
        environment.update(_postgres_environment())
    if any(name in environment for name in _SENSITIVE_ENV_NAMES):
        raise QualityExecutorError("Credential environment leaked into quality runtime")
    return environment


def _postgres_environment() -> dict[str, str]:
    """Return only disposable loopback DSNs for the isolated test database.

    PostgreSQL clients allow query parameters such as ``hostaddr`` and
    ``service`` to override the URL hostname. Rejecting all query/fragment
    fields keeps repository tests bound to the local socat proxy established
    by the trusted orchestration step.
    """

    result: dict[str, str] = {}
    for name in ("FND_TEST_POSTGRES_DSN", "WM_TEST_POSTGRES_DSN"):
        dsn = os.environ.get(name, "")
        try:
            parsed = urllib.parse.urlsplit(dsn)
            port = parsed.port
        except ValueError as exc:
            raise QualityExecutorError(
                f"Isolated PostgreSQL test DSN {name} is invalid"
            ) from exc
        if (
            parsed.scheme not in {"postgresql", "postgresql+psycopg"}
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or port not in {None, 5432}
            or not parsed.path.removeprefix("/")
            or parsed.query
            or parsed.fragment
        ):
            raise QualityExecutorError(
                f"Isolated PostgreSQL test DSN {name} must use loopback port 5432"
            )
        result[name] = dsn
    return result


def _working_directory(checkout: Path, configured: str) -> Path:
    directory = (checkout / configured).resolve()
    try:
        directory.relative_to(checkout.resolve())
    except ValueError as exc:
        raise QualityExecutorError(
            "Profile working directory escapes the checkout"
        ) from exc
    if not directory.is_dir():
        raise QualityExecutorError("Profile working directory does not exist")
    return directory


def _extra_check(
    extra: dict[str, Any],
    cwd: Path,
    environment_path: Path,
    report_directory: Path,
    deadline: float,
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "/opt/eng-platform/untrusted_command.py",
            "--environment",
            str(environment_path),
            "--report-directory",
            str(report_directory),
            "--deadline",
            f"{deadline:.6f}",
            "--command",
            base64.b64encode(extra["command"].encode()).decode("ascii"),
        ],
        cwd=cwd,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    tail = completed.stdout.strip().splitlines()
    failed = completed.returncode != 0
    blocking = bool(extra["blocking"])
    detail = tail[-1][:500] if tail else ""
    if failed and not blocking:
        detail = f"Advisory check failed: {detail}"[:500]
    return {
        "name": extra["name"],
        "category": extra["category"],
        "status": "FAILED" if failed and blocking else "PASSED",
        "findings": int(failed),
        "blocking_findings": int(failed and blocking),
        "duration_seconds": round(time.monotonic() - started, 3),
        "details": detail,
        "report_path": "",
    }


def _external_check(external_dir: Path) -> dict[str, Any]:
    path = external_dir / "api-container-smoke.json"
    try:
        directory_metadata = external_dir.stat(follow_symlinks=False)
        file_metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != 0
            or directory_metadata.st_gid != 0
            or directory_metadata.st_mode & 0o022
            or not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_uid != 0
            or file_metadata.st_gid != 0
            or file_metadata.st_mode & 0o222
            or file_metadata.st_nlink != 1
        ):
            raise QualityExecutorError("API container smoke ownership is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityExecutorError(
            "Required API container smoke result is missing"
        ) from exc
    required = {
        "schema_version",
        "name",
        "category",
        "status",
        "findings",
        "blocking_findings",
        "duration_seconds",
        "details",
        "report_path",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("category") != "container_smoke"
        or value.get("status") not in {"PASSED", "FAILED"}
        or not isinstance(value.get("findings"), int)
        or not isinstance(value.get("blocking_findings"), int)
        or value.get("blocking_findings") != int(value.get("status") == "FAILED")
    ):
        raise QualityExecutorError("API container smoke result is invalid")
    return {key: value[key] for key in required if key != "schema_version"}


def _normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(report, ensure_ascii=False))
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks:
        raise QualityExecutorError("Quality report does not contain checks")
    for check in checks:
        if not isinstance(check, dict):
            raise QualityExecutorError("Quality report contains an invalid check")
        check.setdefault("findings", 0)
        check.setdefault("blocking_findings", 0)
        check.setdefault("duration_seconds", 0.0)
        check.setdefault("details", "")
        check.setdefault("report_path", "")
    return value


def _report_hash(report: dict[str, Any]) -> str:
    normalized = _normalize_report(report)
    return hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _validate_report(
    report: dict[str, Any], identity: dict[str, str], profile: dict[str, Any]
) -> str:
    normalized = _normalize_report(report)
    expected = {
        "service_name": identity["service_name"],
        "repository": identity["repository"],
        "commit_sha": identity["head_sha"],
        "base_sha": identity["base_sha"],
        "policy_version": "oss-v2",
        "profile": profile["runtime"],
    }
    if any(str(normalized.get(key, "")) != value for key, value in expected.items()):
        raise QualityExecutorError("Quality report identity does not match execution")
    smoke_checks = [
        check
        for check in normalized["checks"]
        if check.get("category") == "container_smoke"
    ]
    if profile.get("container_smoke") and len(smoke_checks) != 1:
        raise QualityExecutorError("Required API container smoke was not recorded")
    if not profile.get("container_smoke") and smoke_checks:
        raise QualityExecutorError("Unexpected API container smoke result")
    return _report_hash(normalized)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        path.unlink(missing_ok=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_private_json(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _supervised_command(
    command: str,
    environment_path: Path,
    report_directory: Path,
    deadline: float,
) -> str:
    if not command.strip():
        return ""
    encoded = base64.b64encode(command.encode()).decode("ascii")
    return " ".join(
        (
            shlex.quote(sys.executable),
            "/opt/eng-platform/untrusted_command.py",
            "--environment",
            shlex.quote(str(environment_path)),
            "--report-directory",
            shlex.quote(str(report_directory)),
            "--deadline",
            shlex.quote(f"{deadline:.6f}"),
            "--command",
            shlex.quote(encoded),
        )
    )


def _trusted_gate_environment(
    trusted_runtime: Path, checkout: Path, git_directory: Path
) -> dict[str, str]:
    home = trusted_runtime / "home"
    temporary = trusted_runtime / "tmp"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "PATH": (
            "/opt/eng-platform/trusted-bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_DIR": str(git_directory),
        "GIT_WORK_TREE": str(checkout),
        "CI": "true",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _anchor_report_exchange(
    runtime: Path,
    checkout: Path,
    working_directory: Path,
    report_directory: Path,
) -> None:
    """Make every exchange path component root-owned and sticky.

    Repository files remain owned by UID 65532, but it cannot rename the
    working directory or the report exchange through a writable parent.
    """

    checkout = checkout.resolve()
    working_directory = working_directory.resolve()
    relative = working_directory.relative_to(checkout)
    anchors = [runtime.resolve(), checkout]
    current = checkout
    for part in relative.parts:
        current /= part
        anchors.append(current)
    anchors.append(report_directory)
    for anchor in anchors:
        if anchor.is_symlink() or not anchor.is_dir():
            raise QualityExecutorError("Quality report exchange path is unsafe")
        os.chown(anchor, 0, 0, follow_symlinks=False)
        os.chmod(anchor, 0o1777)


def _run_trusted_gate(
    gate: list[str], cwd: Path, environment: dict[str, str], timeout: float
) -> tuple[int, bool]:
    process = subprocess.Popen(
        gate,
        cwd=cwd,
        env=environment,
        text=True,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=max(0.1, timeout)), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait()
        _terminate_untrusted_processes()
        return 124, True


def _untrusted_processes() -> list[int]:
    processes: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        real_uid = state = ""
        for line in status.splitlines():
            if line.startswith("Uid:"):
                real_uid = line.split()[1]
            elif line.startswith("State:"):
                state = line.split()[1]
        if real_uid == str(_UNTRUSTED_UID) and state != "Z":
            processes.append(int(entry.name))
    return processes


def _terminate_untrusted_processes() -> None:
    for _ in range(20):
        processes = _untrusted_processes()
        if not processes:
            return
        for pid in processes:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
        while True:
            try:
                reaped, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped == 0:
                break
    if _untrusted_processes():
        raise QualityExecutorError("Unable to terminate timed-out repository code")


def _timeout_report(
    identity: dict[str, str], profile: dict[str, Any]
) -> dict[str, Any]:
    return {
        "policy_version": "oss-v2",
        "service_name": identity["service_name"],
        "repository": identity["repository"],
        "commit_sha": identity["head_sha"],
        "base_sha": identity["base_sha"],
        "branch": "main" if identity["operation"] == "main_release" else "pull-request",
        "profile": profile["runtime"],
        "workflow_run_url": "",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "coverage": None,
        "coverage_threshold": profile["coverage_threshold"],
        "differential_coverage": None,
        "differential_threshold": 80.0,
        "changed_lines": None,
        "covered_changed_lines": None,
        "tool_versions": {},
        "checks": [
            {
                "name": "Quality execution deadline",
                "category": "engine",
                "status": "FAILED",
                "findings": 1,
                "blocking_findings": 1,
                "duration_seconds": float(profile["timeout_seconds"]),
                "details": "Quality profile exceeded its server-owned deadline.",
                "report_path": "",
            }
        ],
    }


def _run_quality(
    service: str,
    source: Path,
    scratch: Path,
    output_dir: Path,
    external_dir: Path,
) -> int:
    identity = _identity()
    if service != identity["service_name"]:
        raise QualityExecutorError("Service argument does not match execution identity")
    profile = verify_profile_hash(service, identity["profile_hash"])
    checkout, git_directory, runtime = _isolated_checkout(
        source.resolve(), scratch.resolve(), identity
    )
    cwd = _working_directory(checkout, profile["working_directory"])
    environment = _child_environment(
        runtime, identity, profile, checkout, git_directory
    )
    (runtime / "tmp").mkdir(parents=True, exist_ok=True)
    report_directory = cwd / "quality-reports"
    if report_directory.is_symlink():
        report_directory.unlink()
    elif report_directory.exists():
        shutil.rmtree(report_directory)
    report_directory.mkdir(parents=True, exist_ok=True)
    _chown_tree(runtime, _UNTRUSTED_UID, _UNTRUSTED_GID)
    # The trusted gate owns this sticky exchange directory. Repository commands
    # can create their report artifacts but cannot replace root-owned logs, and
    # the trusted gate does not need DAC_OVERRIDE to consume normal 0644 output.
    _anchor_report_exchange(runtime, checkout, cwd, report_directory)
    trusted_runtime = scratch / "trusted-runtime"
    if trusted_runtime.exists():
        shutil.rmtree(trusted_runtime)
    trusted_runtime.mkdir(parents=True, mode=0o700)
    environment_path = trusted_runtime / "untrusted-environment.json"
    _write_private_json(environment_path, environment)
    report_path = trusted_runtime / "quality-report.json"
    scanner_staging = trusted_runtime / "scanner-staging"
    scanner_staging.mkdir(mode=0o700)
    deadline = time.monotonic() + float(profile["timeout_seconds"])
    gate_environment = _trusted_gate_environment(
        trusted_runtime, checkout, git_directory
    )
    gate_environment.update(
        {
            "ENG_PLATFORM_TRUSTED_REPORT_DIRECTORY": str(report_directory),
            "ENG_PLATFORM_SCANNER_STAGING_DIRECTORY": str(scanner_staging),
            "ENG_PLATFORM_SCANNER_DEADLINE": f"{deadline:.6f}",
        }
    )
    repository_deadline = deadline - 10.0
    commands = profile["commands"]
    gate = [
        sys.executable,
        "/opt/eng-platform/quality_gate.py",
        "--service-name",
        service,
        "--repository",
        identity["repository"],
        "--commit-sha",
        identity["head_sha"],
        "--base-sha",
        identity["base_sha"],
        "--branch",
        "main" if identity["operation"] == "main_release" else "pull-request",
        "--profile",
        profile["runtime"],
        "--trusted-scanner-policy",
        "--working-directory",
        str(cwd),
        "--coverage-threshold",
        str(profile["coverage_threshold"]),
        "--output",
        str(report_path),
        "--install-command",
        _supervised_command(
            commands["install"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
        "--test-command",
        _supervised_command(
            commands["tests"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
        "--build-command",
        _supervised_command(
            commands["build"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
        "--lint-command",
        _supervised_command(
            commands["lint"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
        "--format-command",
        _supervised_command(
            commands["format"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
        "--typecheck-command",
        _supervised_command(
            commands["typecheck"],
            environment_path,
            report_directory,
            repository_deadline,
        ),
    ]
    gate_returncode, timed_out = _run_trusted_gate(
        gate,
        checkout,
        gate_environment,
        deadline - time.monotonic(),
    )
    if timed_out:
        report = _timeout_report(identity, profile)
    else:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QualityExecutorError(
                "Quality gate did not produce a valid report"
            ) from exc
    if not isinstance(report, dict):
        raise QualityExecutorError("Quality gate report must be an object")
    for extra in profile["extra"]:
        report["checks"].append(
            _extra_check(
                extra,
                cwd,
                environment_path,
                report_directory,
                repository_deadline,
            )
        )
    if profile.get("container_smoke"):
        report["checks"].append(_external_check(external_dir))
    integrity = subprocess.run(
        [
            "git",
            f"--git-dir={git_directory}",
            f"--work-tree={checkout}",
            "diff",
            "--quiet",
            identity["head_sha"],
            "--",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    report["checks"].append(
        {
            "name": "Source integrity",
            "category": "identity",
            "status": "PASSED" if integrity.returncode == 0 else "FAILED",
            "findings": int(integrity.returncode != 0),
            "blocking_findings": int(integrity.returncode != 0),
            "duration_seconds": 0.0,
            "details": (
                "Tracked source remained unchanged during quality."
                if integrity.returncode == 0
                else "Quality commands modified tracked source files."
            ),
            "report_path": "",
        }
    )
    report_hash = _validate_report(report, identity, profile)
    failed = gate_returncode != 0 or any(
        check.get("status") == "FAILED" for check in report["checks"]
    )
    manifest = {
        "schema_version": 1,
        "kind": _RESULT_KIND,
        **identity,
        "status": "quality_failed" if failed else "quality_passed",
        "report_hash": report_hash,
        "report": _normalize_report(report),
    }
    _write_json(output_dir / _RESULT_NAME, manifest)
    print(f"Quality manifest: {output_dir / _RESULT_NAME}")
    # A quality failure is reported by the credentialed publisher. Returning zero
    # here ensures GitHub/Cloud Build do not skip that trusted final step.
    return 0


def _read_manifest(path: Path, service: str) -> tuple[dict[str, Any], str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityExecutorError("Quality result manifest is unavailable") from exc
    if not isinstance(manifest, dict):
        raise QualityExecutorError("Quality result manifest must be an object")
    identity = _identity()
    profile = verify_profile_hash(service, identity["profile_hash"])
    immutable = {
        "kind": _RESULT_KIND,
        "execution_id": identity["execution_id"],
        "fingerprint": identity["fingerprint"],
        "service_name": identity["service_name"],
        "repository": identity["repository"],
        "head_sha": identity["head_sha"],
        "base_sha": identity["base_sha"],
        "operation": identity["operation"],
        "profile_hash": identity["profile_hash"],
    }
    if manifest.get("schema_version") != 1 or any(
        manifest.get(key) != value for key, value in immutable.items()
    ):
        raise QualityExecutorError("Quality result manifest identity mismatch")
    if manifest.get("provider_run_id") != identity["provider_run_id"]:
        raise QualityExecutorError("Quality result provider identity mismatch")
    status = manifest.get("status")
    if status not in {"quality_passed", "quality_failed"}:
        raise QualityExecutorError("Quality result status is invalid")
    report = manifest.get("report")
    if not isinstance(report, dict):
        raise QualityExecutorError("Quality result does not contain a report")
    calculated = _validate_report(report, identity, profile)
    if not hmac.compare_digest(calculated, str(manifest.get("report_hash", ""))):
        raise QualityExecutorError("Quality result hash mismatch")
    failed = any(check.get("status") == "FAILED" for check in report["checks"])
    if (status == "quality_failed") != failed:
        raise QualityExecutorError("Quality result status does not match its checks")
    return manifest, status


def _publish(service: str, manifest_path: Path, control_dir: Path) -> int:
    try:
        manifest, status = _read_manifest(manifest_path, service)
    except (QualityExecutorError, QualityProfileError) as exc:
        # A malformed/missing manifest is an engine failure, not a code-quality
        # result. The publisher is still able to terminate the execution.
        _event(2, "failed", control_dir, error=str(exc)[:1000])
        raise
    _event(
        2,
        status,
        control_dir,
        report=manifest["report"],
        report_hash=manifest["report_hash"],
    )
    return int(status == "quality_failed")


def _start(service: str, control_dir: Path) -> int:
    identity = _identity()
    if service != identity["service_name"]:
        raise QualityExecutorError("Service argument does not match execution identity")
    verify_profile_hash(service, identity["profile_hash"])
    _claim_event_token(control_dir)
    _event(1, "running_quality", control_dir)
    return 0


def _prepare(service: str, source: Path, control_dir: Path) -> int:
    identity = _identity()
    if service != identity["service_name"]:
        raise QualityExecutorError("Service argument does not match execution identity")
    verify_profile_hash(service, identity["profile_hash"])
    _claim_event_token(control_dir)
    _prepare_history(source.resolve(), identity)
    _event(1, "running_quality", control_dir)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("run", "start", "prepare", "publish"), default="run"
    )
    parser.add_argument("--service", required=True, choices=available_services())
    parser.add_argument("--source", type=Path, default=Path("/workspace"))
    parser.add_argument(
        "--scratch", type=Path, default=Path("/tmp/eng-platform-quality")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/eng-platform-output"))
    parser.add_argument(
        "--external-dir", type=Path, default=Path("/eng-platform-external")
    )
    parser.add_argument(
        "--control-dir", type=Path, default=Path("/eng-platform-control")
    )
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.mode == "run":
        return _run_quality(
            args.service,
            args.source,
            args.scratch,
            args.output_dir,
            args.external_dir,
        )
    if args.mode == "start":
        return _start(args.service, args.control_dir)
    if args.mode == "prepare":
        return _prepare(args.service, args.source, args.control_dir)
    return _publish(
        args.service,
        args.manifest or args.output_dir / _RESULT_NAME,
        args.control_dir,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualityExecutorError, QualityProfileError) as exc:
        print(f"quality executor: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
