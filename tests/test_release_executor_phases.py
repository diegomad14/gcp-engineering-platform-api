"""Artemis staging uses the real engine with only cloud/HTTP transports replaced."""

import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture(params=["cgm-artemis-api", "cgm-artemis-web"])
def staged(request, monkeypatch, tmp_path):
    path = (
        Path(__file__).resolve().parents[1]
        / "docker/release-executor/release_executor.py"
    )
    spec = importlib.util.spec_from_file_location("release_executor_phases", path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    service = request.param
    sha, old_sha, second_sha, digest = "a" * 40, "c" * 40, "e" * 40, "d" * 64
    image_tag = f"us-central1-docker.pkg.dev/test-project/artemis/{service}:v1.2.3"
    image = image_tag.rsplit(":", 1)[0] + "@sha256:" + digest
    old, second, candidate = (
        service + "-old-main",
        service + "-old-second",
        service + "-ep-ffffffffff",
    )
    production = f"https://{service}-123456789012.us-central1.run.app"
    url = f"https://c-ffffffffffff---{service}-123456789012.us-central1.run.app"
    for key, value in {
        "CGM_RELEASE_PHASE": "candidate",
        "CGM_OPERATION": "deploy",
        "CGM_DEPLOYMENT_ID": "123",
        "CGM_SERVICE": service,
        "CGM_PROFILE": service,
        "CGM_PROFILE_SHA256": engine.profile_fingerprint(service),
        "CGM_RELEASE_SHA": sha,
        "CGM_RELEASE_TAG": "v1.2.3",
        "CGM_REQUEST_FINGERPRINT": "f" * 64,
        "CGM_PROJECT_ID": "test-project",
        "CGM_REGION": "us-central1",
        "CGM_REPOSITORY": "diegomad14/" + service,
        "CGM_RUNTIME_KIND": "cloud_run_service",
        "CGM_PRIVATE_RUNTIME": "true",
        "CGM_IMAGE": image_tag,
        "CGM_BUILD_CONTEXT": ".",
        "CGM_DOCKERFILE_PATH": "Dockerfile",
        "CGM_HEALTH_PATH": "/health" if service.endswith("api") else "/",
        "CGM_PAIRED_WEB_SHA": "b" * 40,
        "CGM_EVIDENCE_BUCKET": "offline-evidence",
        "CGM_PLATFORM_API_URL": "https://platform.example",
        "CGM_QUALITY_EVIDENCE_SERVICES": service,
        "CGM_REPOSITORY_ALIASES": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUILD_ID", raising=False)
    monkeypatch.delenv("CGM_CACHE_IMAGE", raising=False)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    def revision(name, release_sha, artifact):
        rollout = []
        if service.endswith("api"):
            rollout = [
                {"name": "APP_RELEASE_SHA", "value": release_sha},
                {"name": "APP_RELEASE_SCOPE", "value": "runtime-v1"},
                {"name": "APP_RELEASE_RESOURCE", "value": service},
            ]
        return {
            "metadata": {
                "name": name,
                "labels": {
                    "commit-sha": release_sha,
                    "serving.knative.dev/service": service,
                },
            },
            "spec": {
                "containers": [
                    {
                        "image": artifact,
                        "env": [
                            *rollout,
                            {
                                "name": "SYNTHETIC_PLAIN_VALUE",
                                "value": "DO_NOT_PERSIST",
                            },
                            {
                                "name": "SYNTHETIC_REF",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "synthetic-secret-ref",
                                        "key": "latest",
                                    }
                                },
                            },
                        ],
                    }
                ]
            },
            "status": {
                "imageDigest": artifact,
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }

    state = {
        "engine": engine,
        "service": service,
        "candidate": candidate,
        "candidate_url": url,
        "production_url": production,
        "image": image,
        "image_tag": image_tag,
        "traffic": {old: 70, second: 30},
        "previous": {old: 70, second: 30},
        "documents": {
            old: revision(old, old_sha, "repo/old@sha256:" + "1" * 64),
            second: revision(second, second_sha, "repo/old@sha256:" + "2" * 64),
        },
        "calls": [],
        "http": [],
        "hooks": [],
        "grants": [],
        "tagged": False,
        "active": False,
        "anonymous_status": 403,
        "invoker_disabled": False,
        "annotations": {},
        "bindings": [],
        "production_failure": False,
        "quality_passed": True,
        "readiness_sha": sha,
        "traffic_timeout": False,
        "grant_timeout": False,
    }

    def transport(*args, **kwargs):
        state["calls"].append(args)
        if args[0] == "git":
            return sha
        if args[:2] == ("docker", "pull"):
            raise subprocess.CalledProcessError(1, args)
        if args[:2] in {("docker", "build"), ("docker", "push")}:
            return ""
        if args[:4] == ("gcloud", "artifacts", "docker", "images"):
            return "sha256:" + digest
        if args[:3] in {
            ("gcloud", "auth", "configure-docker"),
            ("gcloud", "storage", "cp"),
        }:
            return ""
        if args[:3] == ("gcloud", "auth", "print-identity-token"):
            return "offline-token"
        if args[:4] == ("gcloud", "run", "revisions", "describe"):
            if args[-1] == "--format=value(metadata.labels.commit-sha)":
                return "b" * 40
            return json.dumps(state["documents"][args[4]])
        if args[:4] == ("gcloud", "run", "jobs", "execute"):
            assert engine.os.getenv("CGM_RELEASE_PHASE") == "promote"
            parts = next(value for value in args if value.startswith("--args=")).split(
                ","
            )
            action = next(
                value for value in parts if value in {"authorize", "retire", "revoke"}
            )
            grant_sha = parts[parts.index("--sha") + 1]
            state["grants"].append((action, grant_sha))
            if grant_sha == sha:
                state["active"] = action == "authorize"
            if state["grant_timeout"] and action == "authorize" and grant_sha == sha:
                raise RuntimeError("grant committed before timeout")
            return ""
        if args[:4] == ("gcloud", "run", "services", "get-iam-policy"):
            return json.dumps({"bindings": state["bindings"]})
        if args[:4] == ("gcloud", "run", "services", "update"):
            assert "--no-traffic" in args
            state["documents"][candidate] = revision(candidate, sha, image)
            return ""
        if args[:4] == ("gcloud", "run", "services", "update-traffic"):
            if "--update-tags" in args:
                state["tagged"] = True
            else:
                values = args[args.index("--to-revisions") + 1].split(",")
                state["traffic"] = {
                    row.split("=")[0]: int(row.split("=")[1]) for row in values
                }
                if state["traffic_timeout"] and candidate in state["traffic"]:
                    raise RuntimeError("traffic committed before timeout")
            return ""
        if args[:4] == ("gcloud", "run", "services", "describe"):
            if args[-1] == "--format=value(status.url)":
                return production
            if args[-1] == "--format=value(status.latestCreatedRevisionName)":
                return candidate
            if args[-1] == "--format=value(spec.template.spec.containers[0].image)":
                return ""
            if args[-1] == "--format=json(status.traffic)":
                if args[4] == "cgm-artemis-web" and service != "cgm-artemis-web":
                    return json.dumps(
                        {
                            "status": {
                                "traffic": [
                                    {
                                        "revisionName": "cgm-artemis-web-paired",
                                        "percent": 100,
                                    }
                                ]
                            }
                        }
                    )
                rows = [
                    {"revisionName": revision_name, "percent": weight}
                    for revision_name, weight in state["traffic"].items()
                ]
                if state["tagged"]:
                    rows.append(
                        {
                            "revisionName": candidate,
                            "tag": "c-ffffffffffff",
                            "url": state["candidate_url"],
                        }
                    )
                return json.dumps({"status": {"traffic": rows}})
            return json.dumps(
                {
                    "metadata": {"annotations": state["annotations"]},
                    "invokerIamDisabled": state["invoker_disabled"],
                }
            )
        pytest.fail(f"Unexpected transport: {args}")

    class Response(io.BytesIO):
        status = 200

    def http(request, **kwargs):
        endpoint = request if isinstance(request, str) else request.full_url
        headers = {} if isinstance(request, str) else dict(request.header_items())
        state["http"].append((endpoint, headers))
        if "/api/quality/" in endpoint:
            report = {
                "service_name": service,
                "repository": "diegomad14/" + service,
                "commit_sha": sha,
                "policy_version": "oss-v2",
                "quality_gate_status": "PASSED"
                if state["quality_passed"]
                else "FAILED",
                "checks": [{"status": "PASSED"}],
            }
            return Response(json.dumps(report).encode())
        if not headers.get("Authorization"):
            if state["anonymous_status"] != 200:
                raise urllib.error.HTTPError(
                    endpoint, state["anonymous_status"], "private", {}, io.BytesIO()
                )
            return Response(b"public")
        if state["production_failure"] and endpoint.startswith(production):
            raise urllib.error.HTTPError(
                endpoint, 500, "failed smoke", {}, io.BytesIO()
            )
        if endpoint.endswith("/ready"):
            payload = {
                "resource": service,
                "release_sha": state["readiness_sha"],
                "scope": "runtime-v1",
                "status": "ready" if state["active"] else "blocked",
                "reason": "active" if state["active"] else "release_not_activated",
            }
            if not state["active"]:
                raise urllib.error.HTTPError(
                    endpoint,
                    503,
                    "inactive",
                    {},
                    io.BytesIO(json.dumps(payload).encode()),
                )
            return Response(json.dumps(payload).encode())
        return Response(b"healthy")

    monkeypatch.setattr(engine, "run", transport)
    monkeypatch.setattr(engine.urllib.request, "urlopen", http)
    monkeypatch.setattr(engine, "run_hooks", lambda phase: state["hooks"].append(phase))
    monkeypatch.setattr(engine.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    return state


def _candidate(staged, monkeypatch):
    engine = staged["engine"]
    engine.main()
    path = engine.ROOT / engine._CANDIDATE_FILE
    payload = path.read_bytes()
    monkeypatch.setenv("CGM_CANDIDATE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setenv("CGM_RELEASE_PHASE", "promote")
    staged["calls"].clear()
    staged["http"].clear()
    return path, json.loads(payload)


def _rewrite(path, payload, monkeypatch, *, trusted_checksum=True):
    path.chmod(0o600)
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    path.write_bytes(body)
    if trusted_checksum:
        monkeypatch.setenv("CGM_CANDIDATE_SHA256", hashlib.sha256(body).hexdigest())


def _no_promote(staged):
    assert staged["traffic"] == staged["previous"]
    assert not staged["grants"]
    assert not any(
        call[:4] == ("gcloud", "run", "services", "update-traffic")
        and "--to-revisions" in call
        for call in staged["calls"]
    )


def test_candidate_builds_once_and_never_activates_or_promotes(staged):
    engine = staged["engine"]
    engine.main()
    _no_promote(staged)
    assert (
        len([call for call in staged["calls"] if call[:2] == ("docker", "build")]) == 1
    )
    assert (
        len([call for call in staged["calls"] if call[:2] == ("docker", "push")]) == 1
    )
    assert "pre_promote_hooks" not in staged["hooks"]
    assert "post_promote_hooks" not in staged["hooks"]
    path = engine.ROOT / engine._CANDIDATE_FILE
    assert path.stat().st_mode & 0o777 == 0o444
    assert b"DO_NOT_PERSIST" not in path.read_bytes()
    assert b"synthetic-secret-ref" not in path.read_bytes()
    result = json.loads((engine.ROOT / ".eng-platform-release-result.json").read_text())
    assert result["phase"] == "candidate" and "production_revision" not in result
    assert result["candidate_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_promote_whole_main_never_rebuilds_or_redeploys(staged, monkeypatch):
    _candidate(staged, monkeypatch)
    engine = staged["engine"]
    with (
        mock.patch.object(
            engine, "image_for_tag", side_effect=AssertionError("promote cannot build")
        ) as build,
        mock.patch.object(
            engine,
            "_deploy_candidate",
            side_effect=AssertionError("promote cannot redeploy"),
        ) as redeploy,
    ):
        engine.main()
    build.assert_not_called()
    redeploy.assert_not_called()
    assert not any(call[0] == "docker" for call in staged["calls"])
    assert staged["traffic"] == {staged["candidate"]: 100}
    result = json.loads((engine.ROOT / ".eng-platform-release-result.json").read_text())
    assert result["production_revision"] == staged["candidate"]
    assert result["image_digest"] == staged["image"]
    if staged["service"].endswith("api"):
        assert staged["grants"][0] == ("authorize", "a" * 40)
        assert ("retire", "c" * 40) in staged["grants"]
    else:
        assert not staged["grants"]


@pytest.mark.parametrize(
    "field",
    [
        "CGM_RELEASE_SHA",
        "CGM_REPOSITORY",
        "CGM_SERVICE",
        "CGM_DEPLOYMENT_ID",
        "CGM_RELEASE_TAG",
        "CGM_REQUEST_FINGERPRINT",
        "CGM_PROFILE",
        "CGM_PROFILE_SHA256",
    ],
)
def test_tampered_continuation_identity_fails_even_with_matching_checksum(
    staged, monkeypatch, field
):
    path, payload = _candidate(staged, monkeypatch)
    payload["identity"][field] = "tampered"
    _rewrite(path, payload, monkeypatch)
    with pytest.raises(RuntimeError, match="identity or configuration"):
        staged["engine"].main()
    _no_promote(staged)


@pytest.mark.parametrize(
    "field",
    [
        "CGM_PROJECT_ID",
        "CGM_REGION",
        "CGM_PRIVATE_RUNTIME",
        "CGM_HEALTH_PATH",
        "CGM_PAIRED_WEB_SHA",
        "CGM_EVIDENCE_BUCKET",
    ],
)
def test_tampered_continuation_configuration_fails(staged, monkeypatch, field):
    path, payload = _candidate(staged, monkeypatch)
    payload["configuration"][field] = "tampered"
    _rewrite(path, payload, monkeypatch)
    with pytest.raises(RuntimeError, match="identity or configuration"):
        staged["engine"].main()
    _no_promote(staged)


@pytest.mark.parametrize(
    "kind",
    [
        "checksum",
        "size",
        "duplicate",
        "schema",
        "extra",
        "traffic",
        "runtime",
        "image",
        "url",
        "revision",
        "spec",
    ],
)
def test_malformed_or_forged_continuation_stops_before_mutations(
    staged, monkeypatch, kind
):
    path, payload = _candidate(staged, monkeypatch)
    trusted = True
    if kind == "checksum":
        payload["identity"]["CGM_RELEASE_SHA"] = "b" * 40
        trusted = False
    elif kind == "size":
        payload = b"x" * (staged["engine"]._CANDIDATE_LIMIT + 1)
    elif kind == "duplicate":
        payload = path.read_bytes().replace(
            b'"schema_version":1', b'"schema_version":1,"schema_version":1'
        )
    elif kind == "schema":
        payload["schema_version"] = True
    elif kind == "extra":
        payload["unknown"] = "field"
    elif kind == "traffic":
        payload["previous_traffic"][next(iter(payload["previous_traffic"]))] = True
    elif kind == "runtime":
        payload["previous_runtime"] = []
    elif kind == "image":
        payload["image_digest"] = staged["image"].replace("d" * 64, "b" * 64)
    elif kind == "url":
        payload["candidate_url"] = "https://evil.example"
    elif kind == "revision":
        payload["candidate_revision"] = "other-service-revision"
    elif kind == "spec":
        payload["candidate_spec_sha256"] = "b" * 64
    _rewrite(path, payload, monkeypatch, trusted_checksum=trusted)
    with pytest.raises(RuntimeError):
        staged["engine"].main()
    _no_promote(staged)


@pytest.mark.parametrize(
    "kind",
    [
        "traffic",
        "old_spec",
        "candidate_spec",
        "candidate_sha",
        "candidate_image",
        "candidate_digest",
        "candidate_readiness",
        "candidate_url",
    ],
)
def test_live_drift_stops_before_grant_or_promotion(staged, monkeypatch, kind):
    _candidate(staged, monkeypatch)
    document = staged["documents"][staged["candidate"]]
    if kind == "traffic":
        staged["traffic"] = {next(iter(staged["previous"])): 100}
    elif kind == "old_spec":
        staged["documents"][next(iter(staged["previous"]))]["spec"]["containers"][0][
            "env"
        ].append({"name": "DRIFT", "value": "changed"})
    elif kind == "candidate_spec":
        document["spec"]["containers"][0]["env"].append(
            {"name": "DRIFT", "value": "changed"}
        )
    elif kind == "candidate_sha":
        document["metadata"]["labels"]["commit-sha"] = "b" * 40
    elif kind == "candidate_image":
        document["spec"]["containers"][0]["image"] = "repo/unverified:latest"
    elif kind == "candidate_digest":
        document["status"]["imageDigest"] = "sha256:" + "b" * 64
    elif kind == "candidate_readiness":
        document["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    else:
        staged["candidate_url"] = "https://changed-tag.run.app"
    before = copy.deepcopy(staged["traffic"])
    with pytest.raises(RuntimeError):
        staged["engine"].main()
    assert staged["traffic"] == before
    assert not staged["grants"]


@pytest.mark.parametrize(
    "kind",
    [
        "allUsers",
        "allAuthenticatedUsers",
        "invoker_disabled",
        "annotation_disabled",
        "anonymous_public",
    ],
)
@pytest.mark.parametrize("phase", ["candidate", "promote"])
def test_effective_private_boundary_is_required_in_both_phases(
    staged, monkeypatch, kind, phase
):
    if phase == "promote":
        _candidate(staged, monkeypatch)
    if kind in {"allUsers", "allAuthenticatedUsers"}:
        staged["bindings"] = [{"role": "roles/run.invoker", "members": [kind]}]
    elif kind == "invoker_disabled":
        staged["invoker_disabled"] = True
    elif kind == "annotation_disabled":
        staged["annotations"] = {"run.googleapis.com/invoker-iam-disabled": "true"}
    else:
        staged["anonymous_status"] = 200
    with pytest.raises(RuntimeError, match="private runtime"):
        staged["engine"].main()
    _no_promote(staged)


@pytest.mark.parametrize("failure", ["production_smoke", "traffic_timeout"])
def test_failure_after_traffic_change_restores_exact_prior_weights(
    staged, monkeypatch, failure
):
    _candidate(staged, monkeypatch)
    staged["production_failure"] = failure == "production_smoke"
    staged["traffic_timeout"] = failure == "traffic_timeout"
    with pytest.raises(staged["engine"].AutomaticRollback):
        staged["engine"].main()
    assert staged["traffic"] == staged["previous"]
    changes = [
        call
        for call in staged["calls"]
        if call[:4] == ("gcloud", "run", "services", "update-traffic")
        and "--to-revisions" in call
    ]
    assert len(changes) == 2
    if staged["service"].endswith("api"):
        assert ("authorize", "c" * 40) in staged["grants"]
        assert ("authorize", "e" * 40) in staged["grants"]
        assert staged["grants"][-1] == ("revoke", "a" * 40)


def test_exact_quality_failure_stops_before_build_or_candidate(staged):
    staged["quality_passed"] = False
    with pytest.raises(RuntimeError, match="passed oss-v2"):
        staged["engine"].main()
    assert not any(
        call[0] == "docker" or call[:4] == ("gcloud", "run", "services", "update")
        for call in staged["calls"]
    )
    _no_promote(staged)


@pytest.mark.parametrize(
    "changed",
    [
        {"CGM_RUNTIME_KIND": "cloud_run_job"},
        {"CGM_SERVICE": "cgm-artemis-wm-sweep-worker"},
        {"CGM_OPERATION": "rollback"},
        {"BUILD_ID": "forbidden-cloud-build"},
        {"CGM_RELEASE_PHASE": "unknown"},
    ],
)
def test_unsupported_staging_fails_before_any_transport(staged, monkeypatch, changed):
    for key, value in changed.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="release phase|Artemis GitHub"):
        staged["engine"].main()
    assert staged["calls"] == [] and staged["http"] == []


def test_continuation_is_not_overwritten_on_repeated_candidate(staged):
    engine = staged["engine"]
    engine.main()
    path = engine.ROOT / engine._CANDIDATE_FILE
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        engine.main()
    assert path.read_bytes() == original
    assert (
        len([call for call in staged["calls"] if call[:2] == ("docker", "build")]) == 1
    )
    assert (
        len(
            [
                call
                for call in staged["calls"]
                if call[:4] == ("gcloud", "run", "services", "update")
            ]
        )
        == 1
    )
    _no_promote(staged)


@pytest.mark.parametrize("phase", [None, "full"])
@pytest.mark.parametrize("cloud_build", [False, True])
@pytest.mark.parametrize("runtime", ["cloud_run_service", "cloud_run_job"])
def test_default_full_main_retains_legacy_dispatch_for_services_jobs_and_cloud_build(
    staged, monkeypatch, phase, cloud_build, runtime
):
    engine = staged["engine"]
    if phase is None:
        monkeypatch.delenv("CGM_RELEASE_PHASE")
    else:
        monkeypatch.setenv("CGM_RELEASE_PHASE", phase)
    if cloud_build:
        monkeypatch.setenv("BUILD_ID", "offline-legacy-build")
    monkeypatch.setenv("CGM_RUNTIME_KIND", runtime)
    result = {"production_revision": "legacy-revision", "image_digest": staged["image"]}
    with (
        mock.patch.object(engine, "configure_runtime_home"),
        mock.patch.object(engine, "emit"),
        mock.patch.object(
            engine, "image_for_tag", return_value=staged["image"]
        ) as image,
        mock.patch.object(engine, "deploy", return_value=result) as service,
        mock.patch.object(engine, "deploy_job", return_value=result) as job,
        mock.patch.object(
            engine, "stage_candidate", side_effect=AssertionError("full must not stage")
        ) as stage,
        mock.patch.object(
            engine,
            "promote_candidate",
            side_effect=AssertionError("full must not resume"),
        ) as promote,
    ):
        engine.main()
    image.assert_called_once()
    stage.assert_not_called()
    promote.assert_not_called()
    if runtime == "cloud_run_job":
        job.assert_called_once_with(staged["image"])
        service.assert_not_called()
    else:
        service.assert_called_once_with(staged["image"])
        job.assert_not_called()
