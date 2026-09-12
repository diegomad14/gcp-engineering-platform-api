#!/usr/bin/env python3
"""Process-level Firestore emulator integration for release execution control.

This command is intentionally outside the normal release path.  It starts only
the official local Firestore emulator, two loopback API processes and two
independent adapter clients.  It never falls back to the JSON backend and it
never calls a provider, Actions, Cloud Build or gcloud services.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import importlib.metadata
import json
import multiprocessing as mp
import os
import queue
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from itsdangerous import TimestampSigner

from scripts.release.execution_control import (
    ExecutionContext,
    IntentHandle,
    LeaseHandle,
    PlatformExecutionControlClient,
)
from scripts.release.lifecycle_control import (
    LifecycleControlSession,
    LifecycleControlError,
)


BASE_SHA = "6f502ca8ca65cf7de019ac17c22ceadde57e7542"
START_SHA = "81f011b9e1bbfcb01d530b72b73f90677a04ea3c"
LOCAL_ACTORS = {"cli-integration", "actions-contract-local"}
LOCALHOSTS = {"127.0.0.1", "localhost", "::1"}
EFFECT_DIGEST = "e" * 64
OBSERVATION_DIGEST = "f" * 64


class IntegrationFailure(RuntimeError):
    """The dedicated integration cannot provide valid emulator evidence."""


class _LifecycleRegistrationFixture(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    entered = threading.Event()
    proceed = threading.Event()
    disconnect = False

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append({"path": self.path, "body": body})
        type(self).entered.set()
        if not type(self).proceed.wait(30):
            raise IntegrationFailure("Registration fixture was not released")
        if type(self).disconnect:
            self.connection.shutdown(1)
            self.connection.close()
            return
        response = json.dumps(
            {"id": body.get("release_id", ""), "fixture": True}
        ).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802
        body = json.dumps(type(self).requests[-1]["body"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _current_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_repo_root(), text=True
    ).strip()


def _working_tree_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=_repo_root(), text=True
    ).strip()


def _is_ancestor(ancestor: str, revision: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, revision],
            cwd=_repo_root(),
            check=False,
        ).returncode
        == 0
    )


def _loopback_host(host_port: str) -> bool:
    host, separator, port = host_port.rpartition(":")
    if not separator or not port or host not in LOCALHOSTS:
        return False
    try:
        return 1 <= int(port) <= 65535
    except ValueError:
        return False


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(
    host: str, port: int, process: subprocess.Popen[Any], timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IntegrationFailure(
                f"El proceso local terminó antes de abrir {host}:{port} "
                f"(exit={process.returncode})"
            )
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.1)
    raise IntegrationFailure(f"Timeout esperando el endpoint local {host}:{port}")


def _wait_api(
    base_url: str, process: subprocess.Popen[Any], timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(f"{base_url}/openapi.json", method="GET")
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IntegrationFailure(
                f"La API local terminó durante el arranque (exit={process.returncode})"
            )
        try:
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.HTTPError):
            time.sleep(0.1)
    raise IntegrationFailure(f"Timeout esperando {base_url}")


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _safe_child_environment(
    base_env: dict[str, str], host_port: str, project: str
) -> dict[str, str]:
    env = dict(base_env)
    env["FIRESTORE_EMULATOR_HOST"] = host_port
    env["GOOGLE_CLOUD_PROJECT"] = project
    env["ENG_PLATFORM_GCP_PROJECT_ID"] = project
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("ALL_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("all_proxy", None)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env.pop("GOOGLE_AUTH_CREDENTIAL_FILE_OVERRIDE", None)
    return env


def _context(
    release_id: str, target: str, actor_id: str, *, operation: str = "candidate"
) -> ExecutionContext:
    seed = f"{release_id}:{target}:{actor_id}"
    source_sha = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    digest = "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
    configuration_hash = hashlib.sha256(
        json.dumps({"target": target, "namespace": release_id}, sort_keys=True).encode()
    ).hexdigest()
    return ExecutionContext(
        release_id=release_id,
        repository="local-integration/synthetic-release",
        service_name="eng-platform-api",
        release_group_id="",
        source_sha=source_sha,
        tag=f"v0.0.0-{release_id[-12:]}",
        operation=operation,
        actor_id=actor_id,
        artifact_digest=digest,
        target=target,
        configuration_hash=configuration_hash,
    )


def _issue(context: ExecutionContext, issuer: Any) -> tuple[ExecutionContext, str]:
    token, claims = issuer.issue(
        repository=context.repository,
        service_name=context.service_name,
        tag=context.tag,
        sha=context.source_sha,
        github_deployment_id=0,
        requested_by=context.actor_id,
        kind="deploy",
        release_id=context.release_id,
        artifact_digest=context.artifact_digest,
        target=context.target,
        operation=context.operation,
        audience=issuer.LOCAL_AUDIENCE,
        execution_mode=issuer.LOCAL_EXECUTION_MODE,
        configuration_hash=context.configuration_hash,
        capability_issued=True,
    )
    return dataclasses.replace(
        context, configuration_hash=str(claims["configuration_hash"])
    ), token


def _session_headers(session_secret: str, actor_id: str) -> dict[str, str]:
    """Create an in-memory, emulator-only signed OAuth-session equivalent.

    The random signing key never enters an argument, log, evidence JSON, or
    repository file.  It is valid only for the loopback child API processes.
    """
    encoded = base64.b64encode(
        json.dumps({"github_login": actor_id}, separators=(",", ":")).encode()
    )
    cookie = TimestampSigner(session_secret).sign(encoded).decode("ascii")
    return {"Cookie": f"session={cookie}"}


def _fixture_http_transport(base_url: str, headers: dict[str, str]) -> Any:
    """Return a fixture transport that still uses authenticated loopback HTTP."""

    def send(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read().decode())
        if not isinstance(value, dict):
            raise IntegrationFailure("Fixture HTTP transport returned non-object JSON")
        return value

    return send


def _worker(
    action: str,
    base_url: str,
    context_payload: dict[str, Any],
    token: str,
    options: dict[str, Any],
    barrier: Any,
    result_queue: Any,
) -> None:
    """Run one real adapter client in an independent process."""
    try:
        context = ExecutionContext(**context_payload)
        # The official emulator can spend >20s resolving contended locks.
        # Keep the bounded client wait inside the parent's 45s result budget.
        client = PlatformExecutionControlClient(
            base_url,
            timeout=40.0,
            # Kept solely in the spawned process memory; never persisted in
            # the scenario result or evidence.
            auth_headers=options.get("auth_headers")
            or _session_headers(
                os.environ["ENG_PLATFORM_SESSION_SECRET"], context.actor_id
            ),
        )

        def wait_barrier() -> None:
            if barrier is not None:
                barrier.wait(timeout=30)

        def consume() -> dict[str, Any]:
            return client.consume_authorization(token, context)

        def acquire(consumed: dict[str, Any]) -> LeaseHandle:
            return client.acquire_lease(
                context,
                scope="deployment",
                scope_key=str(options["scope_key"]),
                owner_id=context.actor_id,
                authorization_jti=str(consumed["jti"]),
                ttl_seconds=int(options.get("ttl_seconds", 900)),
                reconciliation_id=str(options.get("reconciliation_id", "")),
            )

        if action == "consume_then_acquire":
            wait_barrier()
            consumed = consume()
            lease = acquire(consumed)
            result_queue.put(
                {
                    "ok": True,
                    "jti": str(consumed["jti"]),
                    "actor_id": context.actor_id,
                    "lease": dataclasses.asdict(lease),
                }
            )
            return
        if action == "consume_sync_acquire":
            wait_barrier()
            consumed = consume()
            wait_barrier()
            lease = acquire(consumed)
            result_queue.put(
                {
                    "ok": True,
                    "jti": str(consumed["jti"]),
                    "actor_id": context.actor_id,
                    "lease": dataclasses.asdict(lease),
                }
            )
            return
        if action == "consume":
            wait_barrier()
            consumed = consume()
            result_queue.put({"ok": True, "jti": str(consumed["jti"])})
            return
        if action == "setup":
            consumed = consume()
            lease = acquire(consumed)
            intent = client.create_intent(
                context,
                idempotency_key=str(options["idempotency_key"]),
                effect_digest=EFFECT_DIGEST,
                lease=lease,
                authorization_jti=str(consumed["jti"]),
            )
            result_queue.put(
                {
                    "ok": True,
                    "jti": str(consumed["jti"]),
                    "lease": dataclasses.asdict(lease),
                    "intent": dataclasses.asdict(intent),
                }
            )
            return
        if action == "commit_discard":
            lease = LeaseHandle(**options["lease"])
            intent = IntentHandle(**options["intent"])
            client.record_result(
                intent, lease=lease, status="CONFIRMED", result_digest=EFFECT_DIGEST
            )
            result_queue.put({"ok": True, "response": "discarded_after_commit"})
            return
        if action == "replay_intent":
            lease = LeaseHandle(**options["lease"])
            intent = IntentHandle(**options["intent"])
            replayed = client.create_intent(
                context,
                idempotency_key=intent.intent_id,
                effect_digest=EFFECT_DIGEST,
                lease=lease,
                authorization_jti=str(options["authorization_jti"]),
            )
            result_queue.put({"ok": True, "intent": dataclasses.asdict(replayed)})
            return
        if action == "renew":
            renewed = client.renew_lease(
                LeaseHandle(**options["lease"]), ttl_seconds=60
            )
            result_queue.put({"ok": True, "lease": dataclasses.asdict(renewed)})
            return
        if action == "record_unknown":
            lease = LeaseHandle(**options["lease"])
            intent = IntentHandle(**options["intent"])
            recorded = client.record_result(
                intent, lease=lease, status="UNKNOWN", error_code="response_lost"
            )
            result_queue.put({"ok": True, "intent": dataclasses.asdict(recorded)})
            return
        if action == "reconcile_intent_unknown":
            intent = IntentHandle(**options["intent"])
            reconciled = client.reconcile_intent(
                intent,
                reconciliation_id=str(options["reconciliation_id"]),
                outcome="UNKNOWN",
                observation_digest=OBSERVATION_DIGEST,
            )
            result_queue.put({"ok": True, "intent": dataclasses.asdict(reconciled)})
            return
        if action == "reconcile_lease":
            lease = client.reconcile_lease(
                LeaseHandle(**options["lease"]),
                reconciliation_id=str(options["reconciliation_id"]),
                observation=str(options["observation"]),
                observation_digest=OBSERVATION_DIGEST,
            )
            result_queue.put({"ok": True, "lease": dataclasses.asdict(lease)})
            return
        if action == "release":
            lease = client.release_lease(
                LeaseHandle(**options["lease"]), final_status="FAILED"
            )
            result_queue.put({"ok": True, "lease": dataclasses.asdict(lease)})
            return
        raise IntegrationFailure(f"Acción de cliente desconocida: {action}")
    except Exception as exc:  # The parent asserts expected rejection statuses.
        result_queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _run_client(
    action: str,
    base_url: str,
    context: ExecutionContext,
    token: str,
    options: dict[str, Any] | None = None,
    *,
    concurrent: int = 1,
) -> list[dict[str, Any]]:
    context_mp = mp.get_context("spawn")
    result_queue = context_mp.Queue()
    barrier = context_mp.Barrier(concurrent) if concurrent > 1 else None
    processes = [
        context_mp.Process(
            target=_worker,
            args=(
                action,
                base_url,
                context.as_payload(),
                token,
                options or {},
                barrier,
                result_queue,
            ),
            name=f"release-control-client-{index + 1}",
        )
        for index in range(concurrent)
    ]
    for process in processes:
        process.start()
    results: list[dict[str, Any]] = []
    try:
        for _ in processes:
            results.append(result_queue.get(timeout=45))
    except queue.Empty as exc:
        raise IntegrationFailure(
            f"Un cliente independiente no reportó resultado: {action}"
        ) from exc
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
    return results


def _one(result: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if len(result) != 1:
        raise IntegrationFailure(f"{label}: resultado ambiguo")
    return result[0]


def _assert_ok(result: dict[str, Any], label: str) -> dict[str, Any]:
    if not result.get("ok"):
        raise IntegrationFailure(f"{label}: {result.get('error', 'fallo desconocido')}")
    return result


def _assert_rejected(result: dict[str, Any], label: str, status: str = "409") -> None:
    if result.get("ok") or status not in str(result.get("error", "")):
        raise IntegrationFailure(f"{label}: rechazo esperado no observado ({result})")


def _scenario_record(name: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": "PASS", "details": details}


def _start_registration_fixture() -> tuple[ThreadingHTTPServer, str, threading.Thread]:
    _LifecycleRegistrationFixture.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LifecycleRegistrationFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}", thread


def _sanitized_environment() -> dict[str, str]:
    env = dict(os.environ)
    configured_host = env.get("FIRESTORE_EMULATOR_HOST", "")
    if configured_host and not _loopback_host(configured_host):
        raise IntegrationFailure(
            "FIRESTORE_EMULATOR_HOST existente no es loopback; se detiene antes de escribir"
        )
    return env


def _component_versions(gcloud: str, env: dict[str, str]) -> dict[str, str]:
    result = {"gcloud": "unknown", "firestore_emulator": "unknown", "java": "unknown"}
    try:
        result["gcloud"] = subprocess.check_output(
            [gcloud, "--version"], text=True, stderr=subprocess.STDOUT, env=env
        ).splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        pass
    try:
        output = subprocess.run(
            [
                gcloud,
                "components",
                "list",
                "--filter=id:cloud-firestore-emulator",
                "--format=json",
                "--quiet",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        components = json.loads(output.stdout)
        if components:
            result["firestore_emulator"] = str(
                components[0].get("current_version_string", "unknown")
            )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        pass
    try:
        result["java"] = subprocess.run(
            ["java", "-version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        pass
    return result


def _start_local_processes(
    evidence_dir: Path,
    env: dict[str, str],
    project: str,
    auth_collection: str,
    control_collection: str,
    signing_private: str,
    signing_public: str,
) -> tuple[subprocess.Popen[Any], subprocess.Popen[Any], str, str, str, dict[str, str]]:
    gcloud = shutil.which("gcloud")
    if not gcloud:
        raise IntegrationFailure(
            "gcloud no está instalado; no se puede iniciar el emulador oficial y no hay fallback"
        )
    emulator_port = _pick_port()
    emulator_host = f"127.0.0.1:{emulator_port}"
    if not _loopback_host(emulator_host):
        raise IntegrationFailure(
            "La dirección del emulador seleccionada no es loopback"
        )
    emulator_log = (evidence_dir / "emulator.log").open("w", encoding="utf-8")
    emulator_env = _safe_child_environment(env, emulator_host, project)
    emulator_env["CLOUDSDK_CONFIG"] = str(evidence_dir / "gcloud-config")
    emulator_env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    emulator_env["CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK"] = "1"
    emulator_env["CLOUDSDK_CORE_PROJECT"] = project
    emulator = subprocess.Popen(
        [
            gcloud,
            "emulators",
            "firestore",
            "start",
            f"--host-port={emulator_host}",
            "--quiet",
        ],
        env=emulator_env,
        stdout=emulator_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _wait_port("127.0.0.1", emulator_port, emulator, 60.0)
    except Exception:
        _stop_process(emulator)
        emulator_log.close()
        raise
    emulator_log.close()

    api_env = _safe_child_environment(env, emulator_host, project)
    api_env.update(
        {
            "ENG_PLATFORM_MOCK_MODE": "false",
            "ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION": auth_collection,
            "ENG_PLATFORM_RELEASE_CONTROL_FIRESTORE_COLLECTION": control_collection,
            "ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY": signing_private,
            "ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY": signing_public,
            "ENG_PLATFORM_ALLOWED_GITHUB_LOGINS": ",".join(sorted(LOCAL_ACTORS)),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[1] / "src"),
                    str(Path(__file__).resolve().parents[1]),
                ]
            ),
        }
    )
    api_ports = (_pick_port(), _pick_port())
    api_urls = [f"http://127.0.0.1:{port}" for port in api_ports]
    api_processes: list[subprocess.Popen[Any]] = []
    api_logs = []
    try:
        for index, (port, url) in enumerate(
            zip(api_ports, api_urls, strict=True), start=1
        ):
            log_handle = (evidence_dir / f"api-{index}.log").open("w", encoding="utf-8")
            api_logs.append(log_handle)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "eng_platform_api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "info",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=api_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            api_processes.append(process)
            _wait_api(url, process)
    except Exception:
        for process in api_processes:
            _stop_process(process)
        for handle in api_logs:
            handle.close()
        _stop_process(emulator)
        raise
    for handle in api_logs:
        handle.close()
    return (
        emulator,
        api_processes[0],
        api_processes[1],
        emulator_host,
        api_urls[0],
        {"api2": api_urls[1], **_component_versions(gcloud, emulator_env)},
    )


def _wait_for_expiry(expires_at: str) -> None:
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
    delay = max(0.0, expiry - time.time() + 1.0)
    if delay:
        time.sleep(delay)


def _emulator_warning_summary(log_path: Path) -> dict[str, int]:
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {
        "transaction_lock_timeout_lines": text.count(
            "WARNING: Operation failed: Transaction lock timeout."
        ),
        "already_exists_lines": text.count(
            "WARNING: Operation failed: entity already exists:"
        ),
        "jdk_unsafe_warning_lines": text.count("Unsafe::allocateMemory"),
    }


def _run_suite(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    head_sha = _current_head()
    if not _working_tree_clean():
        raise IntegrationFailure(
            "El árbol de la propuesta está sucio; fija el SHA final antes de generar evidencia"
        )
    if not _is_ancestor(START_SHA, head_sha):
        raise IntegrationFailure(
            f"El HEAD {head_sha} no desciende de la propuesta inicial {START_SHA}"
        )
    evidence_dir = Path(
        args.evidence_dir
        or tempfile.mkdtemp(prefix="eng-platform-firestore-integration-")
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    env = _sanitized_environment()
    namespace = args.namespace or f"local-{uuid.uuid4().hex[:12]}"
    project = f"eng-platform-local-{namespace[-20:]}"
    auth_collection = f"release-auth-{namespace}"
    control_collection = f"release-control-{namespace}"
    key = Ed25519PrivateKey.generate()
    session_secret = secrets.token_urlsafe(32)
    session_headers = _session_headers(session_secret, "cli-integration")
    header_file = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="eng-platform-loopback-session-",
        delete=False,
        encoding="utf-8",
    )
    try:
        header_file.write(json.dumps(session_headers))
        header_file.flush()
        os.fchmod(header_file.fileno(), 0o600)
    finally:
        header_file.close()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )

    env["FIRESTORE_EMULATOR_HOST"] = (
        "127.0.0.1:1"  # overwritten after startup; proves no pre-write path
    )
    env.update(
        {
            "ENG_PLATFORM_MOCK_MODE": "false",
            "ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION": auth_collection,
            "ENG_PLATFORM_RELEASE_CONTROL_FIRESTORE_COLLECTION": control_collection,
            "ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY": private_pem,
            "ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY": public_pem,
            "ENG_PLATFORM_ALLOWED_GITHUB_LOGINS": ",".join(sorted(LOCAL_ACTORS)),
            "ENG_PLATFORM_SESSION_SECRET": session_secret,
            "ENG_PLATFORM_AUTH_HEADERS_FILE": header_file.name,
            "ENG_PLATFORM_LOCAL_RELEASE_ENABLED": "true",
            "ENG_PLATFORM_LOCAL_RELEASE_SERVICES": "eng-platform-api",
        }
    )
    os.environ.update(_safe_child_environment(env, "127.0.0.1:1", project))

    # Import the existing issuer only after the child configuration is defined.
    from eng_platform_api.services import release_authorization
    from scripts.release import release_lifecycle

    process_start = time.perf_counter()
    emulator, api1, api2, emulator_host, api1_url, runtime = _start_local_processes(
        evidence_dir,
        env,
        project,
        auth_collection,
        control_collection,
        private_pem,
        public_pem,
    )
    process_start_seconds = time.perf_counter() - process_start
    api2_url = str(runtime.pop("api2"))
    os.environ.update(_safe_child_environment(env, emulator_host, project))
    scenarios: list[dict[str, Any]] = []
    external_effects = 0
    scenario_start = time.perf_counter()
    summary: dict[str, Any] | None = None
    registration_fixture, registration_url, registration_thread = (
        _start_registration_fixture()
    )

    try:
        shared_context, shared_token = _issue(
            _context(f"{namespace}-auth", "service-shared/qa", "cli-integration"),
            release_authorization,
        )
        shared_results = _run_client(
            "consume_then_acquire",
            api1_url,
            shared_context,
            shared_token,
            {"scope_key": "deployment:service-shared:qa", "ttl_seconds": 60},
            concurrent=2,
        )
        accepted = [item for item in shared_results if item.get("ok")]
        rejected = [item for item in shared_results if not item.get("ok")]
        if (
            len(accepted) != 1
            or len(rejected) != 1
            or "409" not in rejected[0]["error"]
        ):
            raise IntegrationFailure(
                f"Consumo concurrente inesperado: {shared_results}"
            )
        scenarios.append(
            _scenario_record(
                "same_ticket_concurrent_consumption",
                {"accepted": 1, "rejected": 1, "one_lease": True},
            )
        )
        _assert_ok(
            _one(
                _run_client(
                    "release",
                    api1_url,
                    shared_context,
                    "",
                    {"lease": accepted[0]["lease"]},
                ),
                "release shared",
            ),
            "release shared",
        )

        compete_a, token_a = _issue(
            _context(f"{namespace}-release-a", "service-compete/qa", "cli-integration"),
            release_authorization,
        )
        compete_b, token_b = _issue(
            _context(
                f"{namespace}-release-b", "service-compete/qa", "actions-contract-local"
            ),
            release_authorization,
        )
        compete_results = []
        context_mp = mp.get_context("spawn")
        result_queue = context_mp.Queue()
        barrier = context_mp.Barrier(2)
        processes = [
            context_mp.Process(
                target=_worker,
                args=(
                    "consume_sync_acquire",
                    api2_url,
                    context.as_payload(),
                    token,
                    {
                        "scope_key": "deployment:service-compete:qa",
                        "ttl_seconds": 60,
                        "auth_headers": _session_headers(
                            session_secret, context.actor_id
                        ),
                    },
                    barrier,
                    result_queue,
                ),
                name=f"{role}-client",
            )
            for role, context, token in (
                ("cli", compete_a, token_a),
                ("actions-contract-local", compete_b, token_b),
            )
        ]
        for process in processes:
            process.start()
        for _ in processes:
            compete_results.append(result_queue.get(timeout=45))
        for process in processes:
            process.join(timeout=5)
        winners = [item for item in compete_results if item.get("ok")]
        losers = [item for item in compete_results if not item.get("ok")]
        if len(winners) != 1 or len(losers) != 1 or "409" not in losers[0]["error"]:
            raise IntegrationFailure(
                f"Competencia de releases inesperada: {compete_results}"
            )
        scenarios.append(
            _scenario_record(
                "same_service_environment_competition",
                {"valid_owners": 1, "different_release_ids": True},
            )
        )
        independent, independent_token = _issue(
            _context(
                f"{namespace}-independent", "service-independent/qa", "cli-integration"
            ),
            release_authorization,
        )
        independent_result = _assert_ok(
            _one(
                _run_client(
                    "consume_then_acquire",
                    api1_url,
                    independent,
                    independent_token,
                    {
                        "scope_key": "deployment:service-independent:qa",
                        "ttl_seconds": 60,
                    },
                ),
                "independent resource",
            ),
            "independent resource",
        )
        scenarios.append(
            _scenario_record(
                "independent_resource", {"blocked_by_shared_competition": False}
            )
        )
        _assert_ok(
            _one(
                _run_client(
                    "release",
                    api1_url,
                    independent,
                    "",
                    {"lease": independent_result["lease"]},
                ),
                "release independent",
            ),
            "release independent",
        )
        winner_context = (
            compete_a if winners[0]["actor_id"] == compete_a.actor_id else compete_b
        )
        _assert_ok(
            _one(
                _run_client(
                    "release",
                    api2_url,
                    winner_context,
                    "",
                    {
                        "lease": winners[0]["lease"],
                        "auth_headers": _session_headers(
                            session_secret, winner_context.actor_id
                        ),
                    },
                ),
                "release compete",
            ),
            "release compete",
        )

        lifecycle_target = "synthetic-project/synthetic-region/local-integration"
        lifecycle_context = _context(
            f"{namespace}-lifecycle",
            lifecycle_target,
            "cli-integration",
            operation="register",
        )
        lifecycle_context, lifecycle_token = _issue(
            lifecycle_context, release_authorization
        )
        lifecycle_manifest = {
            "schema_version": 1,
            "release_id": lifecycle_context.release_id,
            "service_name": lifecycle_context.service_name,
            "repository": lifecycle_context.repository,
            "catalog": {
                "project_id": "synthetic-project",
                "region": "synthetic-region",
                "deployment": {"health_path": "/health"},
            },
            "source": {
                "path": str(_repo_root()),
                "sha": lifecycle_context.source_sha,
                "reviewed_sha": lifecycle_context.source_sha,
                "base_sha": BASE_SHA,
                "dirty": False,
                "publishable": True,
                "repository": lifecycle_context.repository,
            },
            "version": {"tag": lifecycle_context.tag},
            "quality": {"policy_id": "oss-v2", "status": "PASSED"},
            "artifact": {"digest": lifecycle_context.artifact_digest},
            "execution_control": {
                "target": lifecycle_target,
                "configuration_hash": lifecycle_context.configuration_hash,
            },
            "runtime": {},
        }
        lifecycle_manifest_path = evidence_dir / "lifecycle-manifest.json"
        lifecycle_manifest_path.write_text(
            json.dumps(lifecycle_manifest, indent=2) + "\n", encoding="utf-8"
        )
        lifecycle_state_dir = evidence_dir / "lifecycle-state"
        lifecycle_state_dir.mkdir(parents=True, exist_ok=True)
        lifecycle_state_manifest = (
            lifecycle_state_dir / "manifests" / f"{lifecycle_context.release_id}.json"
        )
        lifecycle_state_manifest.parent.mkdir(parents=True, exist_ok=True)
        lifecycle_state_manifest.write_text(
            json.dumps(lifecycle_manifest, indent=2) + "\n", encoding="utf-8"
        )
        lifecycle_session = LifecycleControlSession.open(
            PlatformExecutionControlClient(
                api1_url,
                timeout=20.0,
                # A fixture capability is allowed only behind a fixture
                # transport.  This transport still reaches the authenticated
                # loopback API; it is not an in-memory control substitute.
                transport=_fixture_http_transport(api1_url, session_headers),
            ),
            lifecycle_context,
            token=lifecycle_token,
            owner_id="lifecycle-fixture-owner",
            scope="deployment",
            scope_key=f"deployment:{lifecycle_target}",
        )
        competitor = dataclasses.replace(
            lifecycle_session,
            client=PlatformExecutionControlClient(api2_url, timeout=20.0),
        )

        def register_with(session, state):
            return release_lifecycle.register_release(
                lifecycle_manifest_path,
                state_dir=state,
                status="candidate",
                revision="local-integration-00001-abc",
                platform_api_url=registration_url,
                token="",
                execute=True,
                confirm_remote_effects=True,
                execution_control=session,
                local_fixture=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_register = pool.submit(
                register_with, lifecycle_session, lifecycle_state_dir
            )
            try:
                if not _LifecycleRegistrationFixture.entered.wait(20):
                    raise IntegrationFailure(
                        "Lifecycle did not reach registration fixture"
                    )
                try:
                    register_with(
                        competitor, evidence_dir / "competing-lifecycle-state"
                    )
                except LifecycleControlError as exc:
                    if "already exists" not in str(exc):
                        raise
                else:
                    raise IntegrationFailure(
                        "Replayed intent granted a duplicate effect"
                    )
            finally:
                _LifecycleRegistrationFixture.proceed.set()
            lifecycle_result = pending_register.result(timeout=30)
        if len(_LifecycleRegistrationFixture.requests) != 1:
            raise IntegrationFailure(
                "Competing lifecycle clients executed duplicate POSTs"
            )
        scenarios.append(
            _scenario_record(
                "competing_lifecycle_same_owner_one_effect",
                {"api_processes": 2, "effects": 1, "replayed_intent_rejected": True},
            )
        )
        if lifecycle_result["execution"]["status"] != "SUCCEEDED":
            raise IntegrationFailure(
                f"Lifecycle register no terminó confirmado: {lifecycle_result}"
            )
        lifecycle_intent = lifecycle_result["execution"]["control_intents"][0]
        lifecycle_client = PlatformExecutionControlClient(api1_url, timeout=20.0)
        durable_intent = lifecycle_client.get_intent(lifecycle_intent["intent_id"])
        if durable_intent is None or durable_intent.status != "CONFIRMED":
            raise IntegrationFailure(
                "El lifecycle no dejó un intent CONFIRMED en Firestore Emulator"
            )
        resumed = release_lifecycle.resume(
            lifecycle_state_dir,
            lifecycle_context.release_id,
            control_client=lifecycle_client,
        )
        if resumed["control_state"]["intents"][0]["status"] != "CONFIRMED":
            raise IntegrationFailure(
                "resume no consultó el intent durable del lifecycle"
            )
        scenarios.append(
            _scenario_record(
                "lifecycle_register_through_firestore_control",
                {
                    "intent_status": durable_intent.status,
                    "lease_status": resumed["control_state"]["lease"]["status"],
                    "provider": "loopback-registration-fixture",
                    "effects": 1,
                },
            )
        )

        tamper_context, tamper_token = _issue(
            _context(f"{namespace}-tamper", "service-tamper/qa", "cli-integration"),
            release_authorization,
        )
        tampered = dataclasses.replace(tamper_context, target="service-other/qa")
        tamper_result = _one(
            _run_client("consume", api1_url, tampered, tamper_token), "tampered context"
        )
        _assert_rejected(tamper_result, "tampered context", "401")
        scenarios.append(
            _scenario_record("tampered_context", {"rejected": True, "status": 401})
        )

        expired_context = _context(
            f"{namespace}-expired", "service-expired/qa", "cli-integration"
        )
        original_time = release_authorization.time.time
        try:
            release_authorization.time.time = lambda: (
                original_time() - release_authorization.TOKEN_TTL_SECONDS - 5
            )
            expired_context, expired_token = _issue(
                expired_context, release_authorization
            )
        finally:
            release_authorization.time.time = original_time
        expired_result = _one(
            _run_client("consume", api2_url, expired_context, expired_token),
            "expired authorization",
        )
        _assert_rejected(expired_result, "expired authorization", "401")
        scenarios.append(
            _scenario_record("expired_authorization", {"rejected": True, "status": 401})
        )

        restart_context, restart_token = _issue(
            _context(f"{namespace}-restart", "service-restart/qa", "cli-integration"),
            release_authorization,
        )
        restart_setup = _assert_ok(
            _one(
                _run_client(
                    "setup",
                    api1_url,
                    restart_context,
                    restart_token,
                    {
                        "scope_key": "deployment:service-restart:qa",
                        "idempotency_key": f"{namespace}:restart",
                    },
                ),
                "restart setup",
            ),
            "restart setup",
        )
        _assert_ok(
            _one(
                _run_client(
                    "commit_discard",
                    api1_url,
                    restart_context,
                    "",
                    {
                        "lease": restart_setup["lease"],
                        "intent": restart_setup["intent"],
                    },
                ),
                "lost response commit",
            ),
            "lost response commit",
        )
        # Lose a real lifecycle POST response, then restart the actual API
        # process before observing/reconciling through a new client.
        _, lost_token = _issue(lifecycle_context, release_authorization)
        _LifecycleRegistrationFixture.disconnect = True
        try:
            release_lifecycle.register_release(
                lifecycle_manifest_path,
                state_dir=evidence_dir / "lost-registration",
                status="promoted",
                revision="local-integration-00001-abc",
                platform_api_url=registration_url,
                token="",
                execute=True,
                confirm_remote_effects=True,
                local_fixture=True,
                control_client=PlatformExecutionControlClient(
                    api1_url,
                    transport=_fixture_http_transport(api1_url, session_headers),
                ),
                authorization_token=lost_token,
                actor_id=lifecycle_context.actor_id,
                owner_id="lost-register-owner",
            )
        except release_lifecycle.LifecycleError:
            pass
        else:
            raise IntegrationFailure("Lost POST response was reported as success")
        lost_intent_id = (
            f"{lifecycle_context.release_id}:register:platform-registration:promoted"
        )
        unknown = PlatformExecutionControlClient(api2_url).get_intent(lost_intent_id)
        if unknown is None or unknown.status != "UNKNOWN":
            raise IntegrationFailure("Lost lifecycle POST did not persist UNKNOWN")
        requests_before_reconcile = len(_LifecycleRegistrationFixture.requests)
        _stop_process(api1)
        restart_env = _safe_child_environment(env, emulator_host, project)
        restart_env.update(
            {
                "ENG_PLATFORM_MOCK_MODE": "false",
                "ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION": auth_collection,
                "ENG_PLATFORM_RELEASE_CONTROL_FIRESTORE_COLLECTION": control_collection,
                "ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY": private_pem,
                "ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY": public_pem,
                "ENG_PLATFORM_ALLOWED_GITHUB_LOGINS": ",".join(sorted(LOCAL_ACTORS)),
                "PYTHONPATH": os.pathsep.join(
                    [str(_repo_root() / "src"), str(_repo_root())]
                ),
            }
        )
        with (evidence_dir / "api-1-restarted.log").open("w") as restart_log:
            api1 = subprocess.Popen(
                api1.args,
                cwd=_repo_root(),
                env=restart_env,
                stdout=restart_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _wait_api(api1_url, api1)
        reconciled_register = release_lifecycle.reconcile_register_release(
            lifecycle_manifest_path,
            platform_api_url=registration_url,
            control_client=PlatformExecutionControlClient(api1_url),
            intent_id=lost_intent_id,
            status="promoted",
            revision="local-integration-00001-abc",
            reconciliation_id="observed-register-after-restart",
        )
        if (
            reconciled_register["status"] != "CONFIRMED"
            or len(_LifecycleRegistrationFixture.requests) != requests_before_reconcile
        ):
            raise IntegrationFailure(
                "Lifecycle reconciliation repeated POST or failed identity"
            )
        scenarios.append(
            _scenario_record(
                "lifecycle_lost_post_restart_and_exact_get_reconciliation",
                {
                    "unknown_before_restart": True,
                    "reconciled": "CONFIRMED",
                    "repeated_posts": 0,
                    "api_process_restarted": True,
                },
            )
        )
        replay = _assert_ok(
            _one(
                _run_client(
                    "replay_intent",
                    api2_url,
                    restart_context,
                    "",
                    {
                        "lease": restart_setup["lease"],
                        "intent": restart_setup["intent"],
                        "authorization_jti": restart_setup["jti"],
                    },
                ),
                "API restart replay",
            ),
            "API restart replay",
        )
        if replay["intent"]["status"] != "CONFIRMED":
            raise IntegrationFailure(
                f"La reconciliación tras reinicio no recuperó CONFIRMED: {replay}"
            )
        _assert_ok(
            _one(
                _run_client(
                    "release",
                    api2_url,
                    restart_context,
                    "",
                    {"lease": restart_setup["lease"]},
                ),
                "release restarted lease",
            ),
            "release restarted lease",
        )
        scenarios.append(
            _scenario_record(
                "lost_response_and_api_client_restart",
                {
                    "intent_reused": True,
                    "status": "CONFIRMED",
                    "api_process_restarted": True,
                },
            )
        )

        expiry_context, expiry_token = _issue(
            _context(f"{namespace}-unknown", "service-unknown/qa", "cli-integration"),
            release_authorization,
        )
        expiry_setup = _assert_ok(
            _one(
                _run_client(
                    "setup",
                    api2_url,
                    expiry_context,
                    expiry_token,
                    {
                        "scope_key": "deployment:service-unknown:qa",
                        "idempotency_key": f"{namespace}:unknown",
                        "ttl_seconds": 30,
                    },
                ),
                "expiry setup",
            ),
            "expiry setup",
        )
        _wait_for_expiry(expiry_setup["lease"]["expires_at"])
        stale_renew = _one(
            _run_client(
                "renew", api2_url, expiry_context, "", {"lease": expiry_setup["lease"]}
            ),
            "stale renew",
        )
        _assert_rejected(stale_renew, "stale renew")
        unknown = _assert_ok(
            _one(
                _run_client(
                    "record_unknown",
                    api2_url,
                    expiry_context,
                    "",
                    {"lease": expiry_setup["lease"], "intent": expiry_setup["intent"]},
                ),
                "record UNKNOWN",
            ),
            "record UNKNOWN",
        )
        if unknown["intent"]["status"] != "UNKNOWN":
            raise IntegrationFailure(f"UNKNOWN no persistido: {unknown}")
        unknown_reconciled = _assert_ok(
            _one(
                _run_client(
                    "reconcile_intent_unknown",
                    api2_url,
                    expiry_context,
                    "",
                    {
                        "intent": expiry_setup["intent"],
                        "reconciliation_id": f"{namespace}:unknown-observation",
                    },
                ),
                "reconcile UNKNOWN",
            ),
            "reconcile UNKNOWN",
        )
        if unknown_reconciled["intent"]["status"] != "UNKNOWN":
            raise IntegrationFailure(
                "Una observación UNKNOWN fue transformada indebidamente"
            )
        indeterminate = _assert_ok(
            _one(
                _run_client(
                    "reconcile_lease",
                    api2_url,
                    expiry_context,
                    "",
                    {
                        "lease": expiry_setup["lease"],
                        "reconciliation_id": f"{namespace}:indeterminate",
                        "observation": "INDETERMINATE",
                    },
                ),
                "indeterminate reconciliation",
            ),
            "indeterminate reconciliation",
        )
        if indeterminate["lease"]["takeover_allowed"]:
            raise IntegrationFailure("INDETERMINATE desbloqueó takeover")
        takeover_context, takeover_token = _issue(
            _context(
                f"{namespace}-takeover-blocked",
                "service-unknown/qa",
                "actions-contract-local",
            ),
            release_authorization,
        )
        blocked_takeover = _one(
            _run_client(
                "consume_then_acquire",
                api2_url,
                takeover_context,
                takeover_token,
                {
                    "scope_key": "deployment:service-unknown:qa",
                    "ttl_seconds": 60,
                    "reconciliation_id": f"{namespace}:indeterminate",
                },
            ),
            "blocked takeover",
        )
        _assert_rejected(blocked_takeover, "blocked takeover")
        safe = _assert_ok(
            _one(
                _run_client(
                    "reconcile_lease",
                    api2_url,
                    expiry_context,
                    "",
                    {
                        "lease": expiry_setup["lease"],
                        "reconciliation_id": f"{namespace}:not-started",
                        "observation": "NOT_STARTED",
                    },
                ),
                "NOT_STARTED reconciliation",
            ),
            "NOT_STARTED reconciliation",
        )
        if not safe["lease"]["takeover_allowed"]:
            raise IntegrationFailure("NOT_STARTED no habilitó la transición prevista")
        takeover_context, takeover_token = _issue(
            _context(
                f"{namespace}-takeover-safe",
                "service-unknown/qa",
                "actions-contract-local",
            ),
            release_authorization,
        )
        takeover = _assert_ok(
            _one(
                _run_client(
                    "consume_then_acquire",
                    api2_url,
                    takeover_context,
                    takeover_token,
                    {
                        "scope_key": "deployment:service-unknown:qa",
                        "ttl_seconds": 60,
                        "reconciliation_id": f"{namespace}:not-started",
                    },
                ),
                "safe takeover",
            ),
            "safe takeover",
        )
        if takeover["lease"]["generation"] <= expiry_setup["lease"]["generation"]:
            raise IntegrationFailure("El takeover seguro no incrementó generation")
        stale_release = _one(
            _run_client(
                "release",
                api2_url,
                expiry_context,
                "",
                {"lease": expiry_setup["lease"]},
            ),
            "old owner release",
        )
        # The previous actor no longer owns the destination, so the router
        # rejects it before the stale-generation check in the store.
        _assert_rejected(stale_release, "old owner release", "403")
        _assert_ok(
            _one(
                _run_client(
                    "release",
                    api2_url,
                    takeover_context,
                    "",
                    {"lease": takeover["lease"]},
                ),
                "release takeover",
            ),
            "release takeover",
        )
        scenarios.append(
            _scenario_record(
                "expiry_unknown_reconciliation_and_fencing",
                {
                    "stale_owner_rejected": True,
                    "unknown_preserved": True,
                    "indeterminate_takeover": False,
                    "not_started_takeover": True,
                },
            )
        )

        scenarios.append(
            _scenario_record(
                "explicit_loopback_activation_only",
                {
                    "service": "eng-platform-api",
                    "activation_env": "local-integration-only",
                    "external_effects": 0,
                },
            )
        )

        from google.cloud import firestore

        firestore_client = firestore.Client(project=project)
        control_docs = [
            snapshot.to_dict() or {}
            for snapshot in firestore_client.collection(control_collection).stream()
        ]
        auth_docs = list(firestore_client.collection(auth_collection).stream())
        final_state = {
            "control_documents": len(control_docs),
            "authorization_documents": len(auth_docs),
            "control_status_counts": dict(
                Counter(str(item.get("status", "")) for item in control_docs)
            ),
            "emulator_host": emulator_host,
            "external_effects": external_effects,
        }
        summary = {
            "status": "INTEGRACIÓN LOCAL DEL CONTROL VERIFICADA",
            "generated_at_utc": _now_utc(),
            "repository": "diegomad14/gcp-engineering-platform-api",
            "head_sha": head_sha,
            "base_sha": BASE_SHA,
            "namespace": namespace,
            "synthetic_project": project,
            "firestore": {
                "emulator_host": emulator_host,
                "auth_collection": auth_collection,
                "control_collection": control_collection,
            },
            "runtime": {
                "python": sys.version.split()[0],
                "google_cloud_firestore": importlib.metadata.version(
                    "google-cloud-firestore"
                ),
                **runtime,
            },
            "durations_seconds": {
                "emulator_and_api_start": round(process_start_seconds, 3),
                "scenarios": round(time.perf_counter() - scenario_start, 3),
                "total": round(time.perf_counter() - started, 3),
            },
            "scenarios": scenarios,
            "final_emulator_state": final_state,
            "remote_effects": {
                "count": external_effects,
                "provider_calls": False,
                "activation": False,
            },
        }
        (evidence_dir / "integration-summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return summary
    finally:
        _LifecycleRegistrationFixture.proceed.set()
        registration_fixture.shutdown()
        registration_thread.join(timeout=5)
        registration_fixture.server_close()
        _stop_process(api1)
        _stop_process(api2)
        _stop_process(emulator)
        Path(header_file.name).unlink(missing_ok=True)
        if summary is not None:
            summary["emulator_warnings"] = _emulator_warning_summary(
                evidence_dir / "emulator.log"
            )
            (evidence_dir / "integration-summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real API/Firestore emulator release-control integration"
    )
    parser.add_argument(
        "--evidence-dir", help="Directory for sanitized logs and JSON evidence"
    )
    parser.add_argument(
        "--namespace", help="Synthetic namespace; defaults to a random local value"
    )
    args = parser.parse_args()
    evidence_dir = Path(
        args.evidence_dir
        or tempfile.mkdtemp(prefix="eng-platform-firestore-integration-")
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = _run_suite(
            argparse.Namespace(evidence_dir=str(evidence_dir), namespace=args.namespace)
        )
    except Exception as exc:
        failure = {
            "status": "BLOCKED",
            "generated_at_utc": _now_utc(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "remote_effects": 0,
        }
        (evidence_dir / "integration-failure.json").write_text(
            json.dumps(failure, indent=2) + "\n", encoding="utf-8"
        )
        print(f"INTEGRACIÓN LOCAL BLOQUEADA: {exc}", file=sys.stderr)
        print(f"Evidencia: {evidence_dir}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary["status"],
                "evidence_dir": str(evidence_dir),
                "scenarios": len(summary["scenarios"]),
                "external_effects": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
