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

import pytest

from scripts.release import release_lifecycle


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

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append({"path": self.path, "body": body})
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
        self.send_response(201)
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegisterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
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
    raise SystemExit(0)
if args[:3] == ['run', 'revisions', 'describe']:
    print(json.dumps({'status': {'imageDigest': 'sha256:' + 'b' * 64}}))
    raise SystemExit(0)
if args[:3] == ['run', 'services', 'describe']:
    if state.exists() and state.read_text(encoding='utf-8') == 'updated':
        traffic = [{'tag': 'candidate-v1-2-3', 'url': os.environ['LOCAL_HEALTH_URL']}]
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
