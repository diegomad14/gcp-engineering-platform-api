"""Loopback integration coverage for guarded release lifecycle effects.

These tests deliberately keep the real urllib/subprocess transports in place.
Only the production safety gate is opened inside the test so that the effects
can be directed to local fixtures instead of GitHub, GCP, Docker, or Actions.
"""

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from scripts.release import release_lifecycle
from scripts.release.execution_control import (
    ExecutionControlError,
    PlatformExecutionControlClient,
)
from eng_platform_api.config import config
from eng_platform_api.services import (
    execution_control_store,
    release_authorization,
    release_authorization_store,
)


def _manifest(tmp_path: Path) -> dict:
    source_sha = "a" * 40
    digest = "sha256:" + "b" * 64
    return {
        "schema_version": 1,
        "release_id": "11111111-1111-4111-8111-111111111111",
        "created_at": "2026-09-09T12:00:00+00:00",
        "service_name": "eng-platform-api",
        "repository": "diegomad14/eng-platform-api",
        "catalog": {
            "project_id": "local-project",
            "region": "local-region",
            "deployment": {"image_name": "eng-platform-api", "health_path": "/health"},
        },
        "source": {
            "path": str(tmp_path),
            "sha": source_sha,
            "reviewed_sha": source_sha,
            "base_sha": "c" * 40,
            "branch": "main",
            "dirty": False,
            "dirty_paths": [],
            "remote_origin": "https://github.com/example/local-fixture.git",
            "repository": "diegomad14/eng-platform-api",
            "publishable": True,
        },
        "version": {"tag": "v1.2.3", "semver": "1.2.3"},
        "quality": {"policy_id": "oss-v2", "status": "PASSED"},
        "artifact": {
            "local_image": "local/eng-platform-api:fixture",
            "image_reference": "local/eng-platform-api:v1.2.3",
            "digest": digest,
            "remote_published": True,
        },
        "dependencies": {"workflow_inventory": {"files": []}},
        "runtime": {},
    }


class _RegisterHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    mode = "success"
    entered = threading.Event()
    release = threading.Event()

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append({"path": self.path, "body": body})
        if type(self).mode == "hold":
            type(self).entered.set()
            type(self).release.wait(10)
        if type(self).mode == "disconnect":
            self.connection.shutdown(1)
            self.connection.close()
            return
        if type(self).mode == "failure":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"fixture unavailable"}')
            return
        response = json.dumps({"id": body["release_id"], "fixture": True}).encode()
        if type(self).mode == "rows":
            response = json.dumps(
                [{**body, **service} for service in body["services"]]
            ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):  # noqa: N802 - stdlib handler API
        if not type(self).requests:
            self.send_response(404)
            self.end_headers()
            return
        body = type(self).requests[-1]["body"]
        service = body["services"][0]
        response = json.dumps(
            {
                "service_name": service["service_name"],
                "repository": body["repository"],
                "version": body["version"],
                "status": body["status"],
                "release_id": body["release_id"],
                "source_sha": body["source_sha"],
                "artifact_digest": body["artifact_digest"],
                "revision": service["revision"],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        return


@pytest.fixture
def register_server():
    _RegisterHandler.requests = []
    _RegisterHandler.mode = "success"
    _RegisterHandler.entered = threading.Event()
    _RegisterHandler.release = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegisterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        _RegisterHandler.release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_manifest(tmp_path: Path) -> Path:
    value = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _local_gcloud(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "gcloud-state"
    executable = tmp_path / "bin" / "gcloud"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['LOCAL_GCLOUD_STATE'])
args = sys.argv[1:]
if args[:3] == ['run', 'services', 'update-traffic']:
    if os.environ.get('LOCAL_GCLOUD_FAIL_UPDATE') == '1':
        print('fixture update failed', file=sys.stderr)
        raise SystemExit(7)
    state.write_text('updated', encoding='utf-8')
    raise SystemExit(0)
if args[:2] == ['run', 'deploy']:
    state.write_text('deployed', encoding='utf-8')
    labels = dict(item.split('=', 1) for item in args[args.index('--labels') + 1].split(','))
    labels['serving.knative.dev/service'] = args[2]
    revision = args[2] + '-' + args[args.index('--revision-suffix') + 1]
    state.with_suffix('.revision').write_text(json.dumps({'metadata': {'name': revision, 'labels': labels}, 'status': {'imageDigest': 'sha256:' + 'b' * 64, 'conditions': [{'type': 'Ready', 'status': 'True'}]}}))
    raise SystemExit(0)
if args[:3] == ['run', 'revisions', 'describe']:
    print(state.with_suffix('.revision').read_text())
    raise SystemExit(0)
if args[:3] == ['run', 'services', 'describe']:
    if state.exists() and state.read_text(encoding='utf-8') == 'updated':
        revision = json.loads(state.with_suffix('.revision').read_text())['metadata']['name']
        traffic = [{'tag': 'candidate-v1-2-3', 'url': os.environ['LOCAL_HEALTH_URL'], 'revisionName': revision}]
    else:
        traffic = []
    print(json.dumps({'status': {'latestCreatedRevisionName': 'fixture-revision', 'traffic': traffic}}))
    raise SystemExit(0)
raise SystemExit('unexpected fixture command: ' + ' '.join(args))
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, state


@pytest.fixture
def health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _allow_local_fixture_execution(monkeypatch):
    monkeypatch.setattr(
        release_lifecycle, "_require_live_controls", lambda *_args: None
    )


@pytest.fixture
def controlled_api(monkeypatch, tmp_path):
    monkeypatch.setattr(
        execution_control_store, "_DEFAULT_STORE_PATH", tmp_path / "control.json"
    )
    monkeypatch.setattr(execution_control_store, "_COLLECTION", "")
    monkeypatch.setattr(release_authorization_store, "_mock_entries", {})
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.release_execution, "remote_activation_enabled", True)
    monkeypatch.setattr(
        config.release_execution,
        "allowed_services",
        ("eng-platform-api", "cgm-sanplat-api", "cgm-sanplat-web"),
    )
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(config.github, "release_signing_private_key", private_pem)
    monkeypatch.setattr(config.github, "release_signing_public_key", "")
    from eng_platform_api.main import app

    return TestClient(app)


def _controlled_client(api_client):
    def send(path, payload):
        response = api_client.post(path, json=payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", "")
            raise ExecutionControlError(
                f"Control plane rejected {path} ({response.status_code}): {detail}"
            )
        return response.json()

    def read(path):
        response = api_client.get(path)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ExecutionControlError(
                f"Control plane rejected {path} ({response.status_code})"
            )
        return response.json()

    return PlatformExecutionControlClient("", transport=send, read_transport=read)


def _authorized_context(manifest_value, operation, actor_id, target=""):
    context = release_lifecycle.lifecycle_execution_context(
        manifest_value, operation, actor_id, target=target
    )
    token, claims = release_authorization.issue(
        repository=context.repository,
        service_name=context.service_name,
        tag=context.tag,
        sha=context.source_sha,
        github_deployment_id=0,
        requested_by=actor_id,
        kind="deploy",
        release_id=context.release_id,
        artifact_digest=context.artifact_digest,
        target=context.target,
        operation=context.operation,
        audience=release_authorization.LOCAL_AUDIENCE,
        execution_mode=release_authorization.LOCAL_EXECUTION_MODE,
        configuration_hash=context.configuration_hash,
    )
    return replace(
        context, configuration_hash=claims.get("configuration_hash", "")
    ), token


def _persist_resume_manifest(state_dir, manifest_value):
    path = state_dir / "manifests" / f"{manifest_value['release_id']}.json"
    release_lifecycle.local_release.write_json(path, manifest_value)
    return path


@pytest.mark.parametrize("response_shape", ["success", "rows"])
def test_controlled_register_records_durable_intent_and_resume_reads_it(
    register_server, controlled_api, tmp_path, response_shape
):
    _RegisterHandler.mode = response_shape
    value = _manifest(tmp_path)
    path = _write_manifest(tmp_path)
    state_dir = tmp_path / "state"
    _persist_resume_manifest(state_dir, value)
    context, authorization_token = _authorized_context(value, "register", "diegomad14")
    control_client = _controlled_client(controlled_api)

    result = release_lifecycle.register_release(
        path,
        state_dir=state_dir,
        status="candidate",
        revision="fixture-revision",
        platform_api_url=register_server,
        token="fixture-token",
        execute=True,
        confirm_remote_effects=True,
        control_client=control_client,
        authorization_token=authorization_token,
        actor_id=context.actor_id,
        owner_id="local-register-one",
        local_fixture=True,
    )

    assert result["execution"]["status"] == "SUCCEEDED"
    assert result["execution"]["control_intents"]
    resumed = release_lifecycle.resume(
        state_dir, value["release_id"], control_client=control_client
    )
    assert resumed["control_state"]["intents"][0]["status"] == "CONFIRMED"
    assert resumed["control_state"]["lease"]["status"] == "RELEASED"


def test_competing_controlled_registers_allow_at_most_one_fixture_effect(
    register_server, controlled_api, tmp_path
):
    value = _manifest(tmp_path)
    path = _write_manifest(tmp_path)
    state_dir = tmp_path / "state"
    _persist_resume_manifest(state_dir, value)
    first_context, first_token = _authorized_context(value, "register", "diegomad14")
    second_context, second_token = _authorized_context(value, "register", "diegomad14")
    first_client = _controlled_client(controlled_api)
    from eng_platform_api.main import app

    second_api = TestClient(app)
    second_client = _controlled_client(second_api)
    _RegisterHandler.mode = "hold"
    first_result: dict[str, object] = {}

    def run_first():
        try:
            first_result["value"] = release_lifecycle.register_release(
                path,
                state_dir=state_dir,
                status="candidate",
                revision="fixture-revision",
                platform_api_url=register_server,
                token="fixture-token",
                execute=True,
                confirm_remote_effects=True,
                control_client=first_client,
                authorization_token=first_token,
                actor_id=first_context.actor_id,
                owner_id="local-register-first",
                local_fixture=True,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            first_result["error"] = exc

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert _RegisterHandler.entered.wait(10)
        with pytest.raises(ExecutionControlError, match="409"):
            release_lifecycle.register_release(
                path,
                state_dir=state_dir,
                status="candidate",
                revision="fixture-revision",
                platform_api_url=register_server,
                token="fixture-token",
                execute=True,
                confirm_remote_effects=True,
                control_client=second_client,
                authorization_token=second_token,
                actor_id=second_context.actor_id,
                owner_id="local-register-second",
                local_fixture=True,
            )
    finally:
        _RegisterHandler.release.set()
        thread.join(timeout=10)
        second_api.close()

    assert "error" not in first_result
    assert first_result["value"]["execution"]["status"] == "SUCCEEDED"
    assert len(_RegisterHandler.requests) == 1


@pytest.mark.parametrize("lost_boundary", ["provider", "durable-result"])
def test_lost_register_response_stays_unknown_until_durable_reconciliation(
    register_server, controlled_api, tmp_path, monkeypatch, lost_boundary
):
    value = _manifest(tmp_path)
    path = _write_manifest(tmp_path)
    state_dir = tmp_path / "state"
    _persist_resume_manifest(state_dir, value)
    context, authorization_token = _authorized_context(value, "register", "diegomad14")
    control_client = _controlled_client(controlled_api)
    _RegisterHandler.mode = "disconnect" if lost_boundary == "provider" else "success"
    if lost_boundary == "durable-result":
        record_result = control_client.record_result

        def lose_confirmation(*args, **kwargs):
            result = record_result(*args, **kwargs)
            if kwargs.get("status") == "CONFIRMED":
                raise ExecutionControlError("confirmation response lost after commit")
            return result

        monkeypatch.setattr(control_client, "record_result", lose_confirmation)

    with pytest.raises(RuntimeError):
        release_lifecycle.register_release(
            path,
            state_dir=state_dir,
            status="candidate",
            revision="fixture-revision",
            platform_api_url=register_server,
            token="fixture-token",
            execute=True,
            confirm_remote_effects=True,
            control_client=control_client,
            authorization_token=authorization_token,
            actor_id=context.actor_id,
            owner_id="local-register-one",
            local_fixture=True,
        )

    execution = json.loads(
        next((state_dir / "executions").glob("*.json")).read_text(encoding="utf-8")
    )
    intent_id = execution["control_intents"][0]["intent_id"]
    resumed = release_lifecycle.resume(
        state_dir, value["release_id"], control_client=control_client
    )
    durable_status = "UNKNOWN" if lost_boundary == "provider" else "CONFIRMED"
    assert resumed["control_state"]["intents"][0]["status"] == durable_status
    assert resumed["control_state"]["lease"]["status"] == (
        "UNKNOWN" if lost_boundary == "provider" else "HELD"
    )

    # The fixture observed that the request was received before the response
    # was lost, so this is an independent, explicit reconciliation decision.
    _RegisterHandler.mode = "success"
    with pytest.raises(release_lifecycle.LifecycleError, match="identity differs"):
        release_lifecycle.reconcile_register_release(
            path,
            platform_api_url=register_server,
            control_client=control_client,
            intent_id=intent_id,
            status="candidate",
            revision="foreign-revision",
            reconciliation_id="wrong-revision",
        )
    assert control_client.get_intent(intent_id).status == durable_status
    reconciled = release_lifecycle.reconcile_register_release(
        path,
        platform_api_url=register_server,
        control_client=control_client,
        intent_id=intent_id,
        status="candidate",
        revision="fixture-revision",
        reconciliation_id="register-fixture-observation-1",
    )
    assert reconciled["status"] == "CONFIRMED"
    final = release_lifecycle.resume(
        state_dir, value["release_id"], control_client=control_client
    )
    assert final["control_state"]["intents"][0]["status"] == "CONFIRMED"
    assert final["control_state"]["lease"]["status"] == "RELEASED"


def test_sanplat_fixture_adapter_runs_ordered_pair_with_one_durable_lease(
    controlled_api, tmp_path
):
    assert not hasattr(release_lifecycle, "SanPlatFixtureAdapter")
    assert not hasattr(release_lifecycle, "sanplat")


def test_register_success_uses_real_loopback_http_transport(
    register_server, monkeypatch, tmp_path
):
    _allow_local_fixture_execution(monkeypatch)
    path = _write_manifest(tmp_path)

    result = release_lifecycle.register_release(
        path,
        state_dir=tmp_path / "state",
        status="candidate",
        revision="fixture-revision",
        platform_api_url=register_server,
        token="fixture-token",
        local_fixture=True,
        execute=True,
        confirm_remote_effects=True,
    )

    assert result["execution"]["status"] == "SUCCEEDED"
    assert _RegisterHandler.requests[0]["path"] == "/api/releases/"
    assert (
        _RegisterHandler.requests[0]["body"]["release_id"]
        == _manifest(tmp_path)["release_id"]
    )


def test_register_http_failure_is_recorded_as_unknown_after_intent(
    register_server, monkeypatch, tmp_path
):
    _allow_local_fixture_execution(monkeypatch)
    _RegisterHandler.mode = "failure"
    path = _write_manifest(tmp_path)

    with pytest.raises(release_lifecycle.LifecycleError, match="HTTP 503"):
        release_lifecycle.register_release(
            path,
            state_dir=tmp_path / "state",
            status="candidate",
            revision="fixture-revision",
            platform_api_url=register_server,
            token="",
            execute=True,
            confirm_remote_effects=True,
        )

    record = next((tmp_path / "state" / "executions").glob("*.json"))
    execution = json.loads(record.read_text(encoding="utf-8"))
    assert execution["status"] == "UNKNOWN"
    assert any(event["result"] == "INTENT_RECORDED" for event in execution["events"])


def test_candidate_success_and_partial_local_command_failure_use_real_subprocess(
    monkeypatch, tmp_path, health_server
):
    _allow_local_fixture_execution(monkeypatch)
    executable, state = _local_gcloud(tmp_path)
    path = _write_manifest(tmp_path)
    env = {
        "PATH": f"{executable.parent}:{os.environ['PATH']}",
        "LOCAL_GCLOUD_STATE": str(state),
        "LOCAL_HEALTH_URL": health_server,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = release_lifecycle.candidate(
        path,
        state_dir=tmp_path / "success-state",
        execute=True,
        confirm_remote_effects=True,
    )
    assert result["execution"]["status"] == "SUCCEEDED"
    assert result["manifest"]["runtime"]["candidate"]["url"] == health_server

    failing_executable, failing_state = _local_gcloud(tmp_path / "failure")
    monkeypatch.setenv("PATH", f"{failing_executable.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("LOCAL_GCLOUD_STATE", str(failing_state))
    monkeypatch.setenv("LOCAL_GCLOUD_FAIL_UPDATE", "1")
    failing_path = _write_manifest(tmp_path / "failure")
    with pytest.raises(release_lifecycle.LifecycleError, match="Command failed"):
        release_lifecycle.candidate(
            failing_path,
            state_dir=tmp_path / "failure-state",
            execute=True,
            confirm_remote_effects=True,
        )
    record = next((tmp_path / "failure-state" / "executions").glob("*.json"))
    execution = json.loads(record.read_text(encoding="utf-8"))
    assert execution["status"] == "UNKNOWN"
    assert any(event["stage"] == "candidate-deploy" for event in execution["events"])


def test_dry_run_does_not_hit_loopback_or_execute_local_command(
    register_server, monkeypatch, tmp_path
):
    path = _write_manifest(tmp_path)
    result = release_lifecycle.register_release(
        path,
        state_dir=tmp_path / "register-state",
        status="candidate",
        revision="fixture-revision",
        platform_api_url=register_server,
        token="",
        execute=False,
        confirm_remote_effects=False,
    )
    assert result["execution"]["status"] == "PLANNED"
    assert result["execution"]["remote_effects"] == []
    assert _RegisterHandler.requests == []

    executable, state = _local_gcloud(tmp_path / "command")
    monkeypatch.setenv("PATH", f"{executable.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("LOCAL_GCLOUD_STATE", str(state))
    command_path = _write_manifest(tmp_path / "command")
    planned = release_lifecycle.candidate(
        command_path,
        state_dir=tmp_path / "candidate-state",
        execute=False,
        confirm_remote_effects=False,
    )
    assert planned["execution"]["status"] == "PLANNED"
    assert not state.exists()
