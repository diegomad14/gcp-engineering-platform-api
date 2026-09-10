import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release import release_lifecycle


def manifest(tmp_path: Path, service_name: str = "eng-platform-api") -> dict:
    source_sha = "a" * 40
    digest = "sha256:" + "b" * 64
    return {
        "schema_version": 1,
        "release_id": "11111111-1111-4111-8111-111111111111",
        "created_at": "2026-09-09T12:00:00+00:00",
        "service_name": service_name,
        "repository": f"diegomad14/{service_name}",
        "catalog": {
            "project_id": "cgm-assistant-prod",
            "region": "us-central1",
            "deployment": {
                "image_name": service_name,
                "artifact_repository": "cgm-sanplat-repo",
                "build_context": ".",
                "health_path": "/health",
            },
        },
        "source": {
            "path": str(tmp_path),
            "sha": source_sha,
            "reviewed_sha": source_sha,
            "base_sha": "c" * 40,
            "branch": "main",
            "dirty": False,
            "dirty_paths": [],
            "remote_origin": "https://github.com/diegomad14/example.git",
            "repository": f"diegomad14/{service_name}",
            "publishable": True,
        },
        "version": {
            "tag": "v1.2.3",
            "semver": "1.2.3",
            "notes": "# v1.2.3\n",
            "local_tag": {"status": "same-sha", "sha": source_sha},
        },
        "quality": {"policy_id": "oss-v2", "status": "PASSED"},
        "artifact": {
            "local_image": f"cgm-local/{service_name}:1.2.3-{source_sha[:12]}",
            "image_reference": f"us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/{service_name}:v1.2.3",
            "digest": digest,
            "remote_published": True,
            "reuse_key": "d" * 64,
        },
        "dependencies": {
            "workflow_inventory": {
                "files": [
                    {
                        "path": ".github/workflows/semantic-release.yml",
                        "release": True,
                        "triggers": ["push"],
                    }
                ]
            }
        },
        "runtime": {
            "candidate": {
                "revision": f"{service_name}-00012-abc",
                "digest": digest,
                "tag": "candidate-v1-2-3",
                "url": "https://candidate.example",
            },
            "promotion": {"previous_revision": f"{service_name}-00011-old"},
        },
    }


def write_manifest(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / f"{value['service_name']}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_publish_dry_run_records_no_remote_effects(tmp_path):
    path = write_manifest(tmp_path, manifest(tmp_path))

    result = release_lifecycle.publish(
        path,
        state_dir=tmp_path / "state",
        execute=False,
        confirm_remote_effects=False,
    )

    assert result["plan"]["publisher_count"] == 1
    assert result["plan"]["gates"]["single_publisher"] is True
    assert result["plan"]["audit"]["events"] == [
        "push",
        "create",
        "release",
        "deployment",
        "deployment_status",
        "workflow_run",
    ]
    assert result["execution"]["status"] == "PLANNED"
    assert result["execution"]["remote_effects"] == []
    assert result["execution_path"]
    assert result["plan"]["execution_control"]["status"] == "BLOCKED"
    assert (
        result["plan"]["execution_control"]["shared_exclusion"]["cli_cli"]["configured"]
        is False
    )


def test_candidate_promote_and_rollback_plans_are_digest_and_revision_bound(tmp_path):
    value = manifest(tmp_path)
    value["runtime"]["candidate"]["digest"] = value["artifact"]["digest"]

    candidate = release_lifecycle.candidate_plan(value)
    promote = release_lifecycle.promote_plan(value)
    rollback = release_lifecycle.rollback_plan(value)

    assert "@sha256:" in candidate["image"]
    assert candidate["gates"]["no_source_deploy"] is True
    assert promote["candidate_revision"] == "eng-platform-api-00012-abc"
    assert promote["gates"]["candidate_digest_matches_manifest"] is True
    assert rollback["target_revision"] == "eng-platform-api-00011-old"
    assert rollback["gates"]["does_not_revert_migrations"] is True


def test_register_payload_carries_release_identity_and_digest(tmp_path):
    value = manifest(tmp_path)

    payload = release_lifecycle.release_payload(value, status="candidate")

    assert payload["release_id"] == value["release_id"]
    assert payload["source_sha"] == "a" * 40
    assert payload["artifact_digest"] == "sha256:" + "b" * 64
    assert payload["triggered_by"] == "local-release"

    value["runtime"]["rollback"] = {"target_revision": "eng-platform-api-00010-old"}
    rolled_back = release_lifecycle.release_payload(value, status="rolled_back")
    assert rolled_back["services"][0]["revision"] == "eng-platform-api-00010-old"


def test_sanplat_plan_preserves_pair_and_stops_execution_without_adapter(tmp_path):
    api = manifest(tmp_path, "cgm-sanplat-api")
    web = manifest(tmp_path, "cgm-sanplat-web")
    web["release_id"] = "22222222-2222-4222-8222-222222222222"
    web["repository"] = "diegomad14/cgm-sanplat-web"
    web["source"]["repository"] = web["repository"]

    plan = release_lifecycle.sanplat_plan(
        api,
        web,
        release_group_id="sanplat-window-20260909",
        auxiliary_services=["cgm-bot-api"],
    )

    assert plan["release_group_id"] == "sanplat-window-20260909"
    assert plan["services"]["unchanged_auxiliary"] == ["cgm-bot-api"]
    assert [step["name"] for step in plan["ordered_steps"]] == [
        "prepare",
        "authorize",
        "capture-state",
        "maintenance",
        "pause-deliveries",
        "drain",
        "migrations",
        "promote-pair",
        "validate-functional",
        "resume",
    ]
    assert plan["gates"]["adapter_configured"] is False
    assert plan["gates"]["frontend_api_base_url_declared"] is False
    assert "web API_BASE_URL" in plan["gates"]["missing_candidate_evidence"]
    assert plan["gates"]["candidate_identity_exact"] is True
    assert "API_BASE_URL" in plan["frontend_config"]["source"]

    api_path = write_manifest(tmp_path, api)
    web_path = write_manifest(tmp_path, web)
    with pytest.raises(
        release_lifecycle.LifecycleError,
        match="SanPlat execution is intentionally gated",
    ):
        release_lifecycle.sanplat(
            api_path,
            web_path,
            state_dir=tmp_path / "state",
            release_group_id="sanplat-window-20260909",
            auxiliary_services=[],
            execute=True,
            confirm_remote_effects=True,
        )


def test_live_mutations_fail_closed_without_shared_exclusion_or_authorization(tmp_path):
    value = manifest(tmp_path)
    path = write_manifest(tmp_path, value)

    with pytest.raises(release_lifecycle.LifecycleError, match="shared CLI/CLI"):
        release_lifecycle.publish(
            path,
            state_dir=tmp_path / "state",
            execute=True,
            confirm_remote_effects=True,
        )

    sanplat_value = manifest(tmp_path, "cgm-sanplat-api")
    sanplat_path = tmp_path / "sanplat-generic.json"
    sanplat_path.write_text(json.dumps(sanplat_value), encoding="utf-8")
    with pytest.raises(
        release_lifecycle.LifecycleError, match="SanPlat generic lifecycle commands"
    ):
        release_lifecycle.candidate(
            sanplat_path,
            state_dir=tmp_path / "state",
            execute=True,
            confirm_remote_effects=True,
        )


def test_resume_blocks_unknown_until_read_only_reconciliation(tmp_path):
    value = manifest(tmp_path)
    state_dir = tmp_path / "state"
    manifest_path = state_dir / "manifests" / f"{value['release_id']}.json"
    release_lifecycle.local_release.write_json(manifest_path, value)
    execution, execution_path = release_lifecycle.begin_execution(
        value, state_dir, "publish", dry_run=False
    )
    execution["status"] = "UNKNOWN"
    execution["unknown_effects"] = [{"stage": "publish-github", "result": "UNKNOWN"}]
    release_lifecycle.save_execution(execution_path, execution)

    result = release_lifecycle.resume(state_dir, value["release_id"])

    assert result["first_pending_stage"] == "register"
    assert result["unknown_effect_requires_reconciliation"] is True
    assert result["remote_query_performed"] is False
    assert result["safe_to_continue"] is False
    assert str(manifest_path) in result["next_command"]


def test_resume_reconciles_unknown_without_retrying_a_mutation(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    state_dir = tmp_path / "state"
    manifest_path = state_dir / "manifests" / f"{value['release_id']}.json"
    release_lifecycle.local_release.write_json(manifest_path, value)
    execution, execution_path = release_lifecycle.begin_execution(
        value, state_dir, "publish", dry_run=False
    )
    execution["status"] = "UNKNOWN"
    execution["unknown_effects"] = [{"stage": "publish-github", "result": "UNKNOWN"}]
    release_lifecycle.save_execution(execution_path, execution)
    monkeypatch.setattr(
        release_lifecycle,
        "_reconcile_read_only",
        lambda _manifest: {
            "artifact_registry": {"status": "CONFIRMED"},
            "git_tag": {"status": "CONFIRMED"},
            "github_release": {"status": "CONFIRMED"},
            "cloud_run": {"status": "NOT_CHECKED"},
        },
    )

    result = release_lifecycle.resume(state_dir, value["release_id"], reconcile=True)

    assert result["reconciliation_status"] == "CONFIRMED"
    assert result["safe_to_continue"] is True
    assert "--execute" not in result["next_command"]
    assert result["note"].endswith("never mutates remote state automatically")


def test_resume_keeps_unknown_candidate_blocked_without_candidate_evidence(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    value["runtime"].pop("candidate")
    state_dir = tmp_path / "state"
    manifest_path = state_dir / "manifests" / f"{value['release_id']}.json"
    release_lifecycle.local_release.write_json(manifest_path, value)
    execution, execution_path = release_lifecycle.begin_execution(
        value, state_dir, "candidate", dry_run=False
    )
    execution["status"] = "UNKNOWN"
    execution["current_stage"] = "candidate-deploy"
    execution["unknown_effects"] = [{"stage": "candidate-deploy", "result": "UNKNOWN"}]
    release_lifecycle.save_execution(execution_path, execution)
    monkeypatch.setattr(
        release_lifecycle,
        "_reconcile_read_only",
        lambda _manifest: {
            "artifact_registry": {"status": "CONFIRMED"},
            "git_tag": {"status": "CONFIRMED"},
            "github_release": {"status": "CONFIRMED"},
            "cloud_run": {"status": "NOT_CHECKED"},
        },
    )

    result = release_lifecycle.resume(state_dir, value["release_id"], reconcile=True)

    assert result["reconciliation_status"] == "INCONCLUSIVE"
    assert result["unknown_effect_requires_reconciliation"] is True
    assert result["safe_to_continue"] is False


def test_resume_keeps_unknown_promote_blocked_on_stale_active_revision(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    state_dir = tmp_path / "state"
    manifest_path = state_dir / "manifests" / f"{value['release_id']}.json"
    release_lifecycle.local_release.write_json(manifest_path, value)
    execution, execution_path = release_lifecycle.begin_execution(
        value, state_dir, "promote", dry_run=False
    )
    execution["status"] = "UNKNOWN"
    execution["current_stage"] = "promote-traffic"
    execution["unknown_effects"] = [{"stage": "promote-traffic", "result": "UNKNOWN"}]
    release_lifecycle.save_execution(execution_path, execution)
    common = {
        "artifact_registry": {"status": "CONFIRMED"},
        "git_tag": {"status": "CONFIRMED"},
        "github_release": {"status": "CONFIRMED"},
    }
    monkeypatch.setattr(
        release_lifecycle,
        "_reconcile_read_only",
        lambda _manifest: {
            **common,
            "cloud_run": {
                "status": "CONFIRMED",
                "active_revision": "eng-platform-api-00011-old",
                "candidate_revision_status": "CONFIRMED",
                "candidate_revision": value["runtime"]["candidate"]["revision"],
                "candidate_digest": value["runtime"]["candidate"]["digest"],
            },
        },
    )

    result = release_lifecycle.resume(state_dir, value["release_id"], reconcile=True)

    assert result["reconciliation_status"] == "INCONCLUSIVE"
    assert result["safe_to_continue"] is False


def test_revision_ready_accepts_cloud_run_condition_succeeded():
    assert release_lifecycle._revision_ready(
        {"status": {"conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}]}}
    )


def test_lifecycle_identity_helpers_and_local_command_doubles(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    assert release_lifecycle._service_name(value) == "eng-platform-api"
    assert release_lifecycle._project_region(value) == (
        "cgm-assistant-prod",
        "us-central1",
    )
    assert release_lifecycle._candidate_tag(value) == "candidate-v1-2-3"
    assert release_lifecycle._image_with_digest(value).endswith(
        "@" + value["artifact"]["digest"]
    )
    assert release_lifecycle._traffic_snapshot(
        {"spec": {"traffic": [{"revisionName": "old"}]}}
    ) == [{"revisionName": "old"}]
    assert (
        release_lifecycle._active_revision(
            [{"percent": "bad"}, {"percent": 50, "revisionName": "old"}]
        )
        == "old"
    )
    assert release_lifecycle._revision_ready({"status": {"service": "READY"}}) is True
    assert (
        release_lifecycle._candidate_url(
            {
                "status": {
                    "traffic": [{"tag": "candidate-v1-2-3", "url": "https://candidate"}]
                }
            },
            "candidate-v1-2-3",
        )
        == "https://candidate"
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="URL"):
        release_lifecycle._candidate_url({"status": {"traffic": []}}, "candidate")
    with pytest.raises(release_lifecycle.LifecycleError, match="digest"):
        release_lifecycle._require_digest({**value, "artifact": {"digest": "invalid"}})
    with pytest.raises(release_lifecycle.LifecycleError, match="Unsupported"):
        release_lifecycle._require_live_controls(value, "unknown")
    with pytest.raises(release_lifecycle.LifecycleError, match="confirm"):
        release_lifecycle._require_confirmation(True, False)
    release_lifecycle._require_confirmation(False, False)

    monkeypatch.setattr(release_lifecycle.shutil, "which", lambda _name: None)
    with pytest.raises(release_lifecycle.LifecycleError, match="unavailable"):
        release_lifecycle._run_command(["gcloud", "version"])
    monkeypatch.setattr(
        release_lifecycle.shutil, "which", lambda _name: "/usr/bin/tool"
    )
    monkeypatch.setattr(
        release_lifecycle.local_release,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="failed"
        ),
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="Command failed"):
        release_lifecycle._run_command(["tool", "fail"])
    with pytest.raises(release_lifecycle.LifecycleError, match="empty"):
        release_lifecycle._run_command([])

    command_result = SimpleNamespace(
        returncode=0, stdout=json.dumps({"ok": True}), stderr=""
    )
    monkeypatch.setattr(
        release_lifecycle, "_run_command", lambda *_args, **_kwargs: command_result
    )
    assert release_lifecycle._json_command(["gcloud", "describe"])["ok"] is True
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="JSON object"):
        release_lifecycle._json_command(["gcloud", "describe"])
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="not-json", stderr=""
        ),
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="invalid JSON"):
        release_lifecycle._json_command(["gcloud", "describe"])


def test_lifecycle_read_only_reconciliation_helpers(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    tag = value["version"]["tag"]
    digest = value["artifact"]["digest"]

    def command(argv, **_kwargs):
        if argv[0] == "git":
            return SimpleNamespace(
                returncode=0,
                stdout=f"{'a' * 40} refs/tags/{tag}\n{'a' * 40} refs/tags/{tag}^{{}}\n",
                stderr="",
            )
        if argv[0] == "gcloud":
            return SimpleNamespace(returncode=0, stdout=digest, stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"tagName": tag, "url": "https://release"}),
            stderr="",
        )

    monkeypatch.setattr(release_lifecycle, "_run_command", command)
    assert release_lifecycle._remote_tag_target(tmp_path, tag) == "a" * 40
    assert release_lifecycle._remote_artifact_digest("image:tag") == digest
    assert release_lifecycle._release_view(value["repository"], tag)["tagName"] == tag

    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda argv, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not found"
        ),
    )
    assert release_lifecycle._local_tag_target(tmp_path, tag) is None
    assert release_lifecycle._remote_artifact_digest("image:tag") is None
    assert release_lifecycle._release_view(value["repository"], tag) is None

    value["runtime"]["production"] = {"revision": "eng-platform-api-00011-old"}
    monkeypatch.setattr(
        release_lifecycle, "_remote_artifact_digest", lambda _image: digest
    )
    monkeypatch.setattr(
        release_lifecycle, "_remote_tag_target", lambda _repo, _tag: "a" * 40
    )
    monkeypatch.setattr(
        release_lifecycle, "_release_view", lambda _repo, _tag: {"tagName": tag}
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_json_command",
        lambda _argv, **_kwargs: {
            "status": {
                "traffic": [
                    {"percent": 100, "revisionName": "eng-platform-api-00011-old"}
                ]
            }
        },
    )
    reconciled = release_lifecycle._reconcile_read_only(value)
    assert reconciled["artifact_registry"]["status"] == "CONFIRMED"
    assert reconciled["git_tag"]["status"] == "CONFIRMED"
    assert reconciled["github_release"]["status"] == "CONFIRMED"
    assert reconciled["cloud_run"]["active_revision"] == "eng-platform-api-00011-old"


def test_read_only_reconciliation_binds_candidate_to_revision_and_digest(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    tag = value["version"]["tag"]
    digest = value["artifact"]["digest"]

    monkeypatch.setattr(
        release_lifecycle, "_remote_artifact_digest", lambda _image: digest
    )
    monkeypatch.setattr(
        release_lifecycle, "_remote_tag_target", lambda _repo, _tag: "a" * 40
    )
    monkeypatch.setattr(
        release_lifecycle, "_release_view", lambda _repo, _tag: {"tagName": tag}
    )

    def command(argv, **_kwargs):
        if argv[0] == "gcloud" and "revisions" in argv:
            return {
                "status": {
                    "imageDigest": digest,
                    "conditions": [{"type": "Ready", "state": "True"}],
                }
            }
        return {
            "status": {
                "traffic": [
                    {"percent": 100, "revisionName": "eng-platform-api-00011-old"}
                ]
            }
        }

    monkeypatch.setattr(release_lifecycle, "_json_command", command)

    reconciled = release_lifecycle._reconcile_read_only(value)

    cloud_run = reconciled["cloud_run"]
    assert cloud_run["candidate_revision_status"] == "CONFIRMED"
    assert cloud_run["candidate_revision"] == value["runtime"]["candidate"]["revision"]
    assert cloud_run["candidate_digest"] == digest


def test_publish_execute_reconciles_existing_remote_state_with_local_doubles(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    path = write_manifest(tmp_path, value)
    digest = value["artifact"]["digest"]
    monkeypatch.setattr(
        release_lifecycle, "_require_live_controls", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_require_remote_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_remote_artifact_digest", lambda _image: digest
    )
    monkeypatch.setattr(
        release_lifecycle, "_remote_tag_target", lambda _repo, _tag: "a" * 40
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_release_view",
        lambda _repository, _tag: {"tagName": "v1.2.3", "url": "https://release"},
    )
    result = release_lifecycle.publish(
        path,
        state_dir=tmp_path / "state",
        execute=True,
        confirm_remote_effects=True,
    )
    assert result["execution"]["status"] == "SUCCEEDED"
    assert result["manifest"]["transition"]["phase"] == "phase-2"
    assert result["manifest"]["artifact"]["digest"] == digest


def test_publish_execute_records_local_double_effects_and_detects_digest_conflict(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    path = write_manifest(tmp_path, value)
    digest = value["artifact"]["digest"]
    monkeypatch.setattr(
        release_lifecycle, "_require_live_controls", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_require_remote_identity", lambda *_args: None
    )
    digest_values = iter([None, digest])
    tag_values = iter([None, "a" * 40])
    release_values = iter([None, {"tagName": "v1.2.3", "url": "https://release"}])
    monkeypatch.setattr(
        release_lifecycle, "_remote_artifact_digest", lambda _image: next(digest_values)
    )
    monkeypatch.setattr(
        release_lifecycle, "_remote_tag_target", lambda _repo, _tag: next(tag_values)
    )
    monkeypatch.setattr(
        release_lifecycle, "_local_tag_target", lambda _repo, _tag: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_release_view", lambda _repo, _tag: next(release_values)
    )
    commands = []
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda argv, **_kwargs: (
            commands.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    result = release_lifecycle.publish(
        path,
        state_dir=tmp_path / "state",
        execute=True,
        confirm_remote_effects=True,
    )
    assert result["execution"]["status"] == "SUCCEEDED"
    assert any(command[:2] == ["docker", "push"] for command in commands)
    assert any(command[:2] == ["git", "push"] for command in commands)
    assert any(command[:3] == ["gh", "release", "create"] for command in commands)

    conflict = manifest(tmp_path)
    conflict["artifact"]["digest"] = "sha256:" + "c" * 64
    conflict_path = write_manifest(tmp_path, conflict)
    monkeypatch.setattr(
        release_lifecycle, "_remote_artifact_digest", lambda _image: digest
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="another digest"):
        release_lifecycle.publish(
            conflict_path,
            state_dir=tmp_path / "conflict-state",
            execute=True,
            confirm_remote_effects=True,
        )


def test_candidate_promote_and_rollback_execute_with_simulated_external_services(
    monkeypatch, tmp_path
):
    value = manifest(tmp_path)
    value["runtime"]["candidate"]["digest"] = value["artifact"]["digest"]
    path = write_manifest(tmp_path, value)
    digest = value["artifact"]["digest"]
    monkeypatch.setattr(
        release_lifecycle, "_require_live_controls", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_require_remote_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    candidate_revision = "eng-platform-api-00013-new"
    candidate_json = iter(
        [
            {"status": {"latestCreatedRevisionName": candidate_revision}},
            {"status": {"imageDigest": digest}},
            {
                "status": {
                    "traffic": [{"tag": "candidate-v1-2-3", "url": "https://candidate"}]
                }
            },
        ]
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_json_command",
        lambda *_args, **_kwargs: next(candidate_json),
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_probe",
        lambda url, path, **_kwargs: {"url": url + path, "status": 200},
    )
    result = release_lifecycle.candidate(
        path,
        state_dir=tmp_path / "candidate-state",
        execute=True,
        confirm_remote_effects=True,
    )
    assert result["execution"]["status"] == "SUCCEEDED"
    assert result["manifest"]["runtime"]["candidate"]["revision"] == candidate_revision

    traffic = {
        "status": {
            "traffic": [{"percent": 100, "revisionName": "eng-platform-api-00011-old"}],
            "url": "https://prod",
        }
    }
    promote_json = iter(
        [
            {
                "status": {
                    "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}]
                }
            },
            traffic,
            {
                "status": {
                    "traffic": [{"percent": 100, "revisionName": candidate_revision}],
                    "url": "https://prod",
                }
            },
        ]
    )
    monkeypatch.setattr(
        release_lifecycle, "_json_command", lambda *_args, **_kwargs: next(promote_json)
    )
    promoted = release_lifecycle.promote(
        path,
        state_dir=tmp_path / "promote-state",
        execute=True,
        confirm_remote_effects=True,
        confirmation="PROMOTE_PROD",
    )
    assert promoted["execution"]["status"] == "SUCCEEDED"
    assert (
        promoted["manifest"]["runtime"]["production"]["revision"] == candidate_revision
    )

    rollback_json = iter(
        [
            {"status": {"service": "READY"}},
            {
                "status": {
                    "traffic": [
                        {"percent": 100, "revisionName": "eng-platform-api-00011-old"}
                    ],
                    "url": "https://prod",
                }
            },
        ]
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_json_command",
        lambda *_args, **_kwargs: next(rollback_json),
    )
    rolled_back = release_lifecycle.rollback(
        path,
        state_dir=tmp_path / "rollback-state",
        target_revision="eng-platform-api-00011-old",
        execute=True,
        confirm_remote_effects=True,
        confirmation="ROLLBACK_PROD",
    )
    assert rolled_back["execution"]["status"] == "SUCCEEDED"
    assert (
        rolled_back["manifest"]["runtime"]["rollback"]["target_revision"]
        == "eng-platform-api-00011-old"
    )


def test_lifecycle_unknown_and_register_local_http_double(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    path = write_manifest(tmp_path, value)
    monkeypatch.setattr(
        release_lifecycle, "_require_live_controls", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle, "_require_remote_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            release_lifecycle.LifecycleError("simulated interruption")
        ),
    )
    with pytest.raises(release_lifecycle.LifecycleError, match="interruption"):
        release_lifecycle.candidate(
            path,
            state_dir=tmp_path / "unknown-state",
            execute=True,
            confirm_remote_effects=True,
        )
    records = list((tmp_path / "unknown-state" / "executions").glob("*.json"))
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "UNKNOWN"

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"id": value["release_id"]}).encode("utf-8")

    monkeypatch.setattr(
        release_lifecycle.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    registered = release_lifecycle.register_release(
        path,
        state_dir=tmp_path / "register-state",
        status="candidate",
        revision="eng-platform-api-00013-new",
        platform_api_url="https://local-double.invalid",
        token="simulated-token",
        execute=True,
        confirm_remote_effects=True,
    )
    assert registered["execution"]["status"] == "SUCCEEDED"
    assert (
        registered["manifest"]["runtime"]["platform_registration"]["response"]["id"]
        == value["release_id"]
    )

    with pytest.raises(release_lifecycle.LifecycleError, match="Unsupported platform"):
        release_lifecycle.release_payload(value, status="invalid")


def test_lifecycle_sanplat_dry_run_adoption_and_stage_progression(tmp_path):
    api = manifest(tmp_path, "cgm-sanplat-api")
    web = manifest(tmp_path, "cgm-sanplat-web")
    web["release_id"] = "22222222-2222-4222-8222-222222222222"
    web["repository"] = "diegomad14/cgm-sanplat-web"
    api_path = write_manifest(tmp_path, api)
    web_path = write_manifest(tmp_path, web)
    planned = release_lifecycle.sanplat(
        api_path,
        web_path,
        state_dir=tmp_path / "sanplat-state",
        release_group_id="",
        auxiliary_services=[],
        execute=False,
        confirm_remote_effects=False,
    )
    assert planned["execution"]["status"] == "PLANNED"
    assert planned["execution"]["remote_effects"] == []

    value = manifest(tmp_path)
    stages = [
        ("quality", {"quality": {"status": "PENDING"}}),
        (
            "build",
            {
                "quality": {"status": "PASSED"},
                "artifact": {"status": "PENDING", "digest": ""},
            },
        ),
        (
            "publish",
            {
                "quality": {"status": "PASSED"},
                "artifact": {
                    "status": "AVAILABLE",
                    "digest": "x",
                    "remote_published": False,
                },
                "runtime": {},
            },
        ),
        (
            "candidate",
            {
                "quality": {"status": "PASSED"},
                "artifact": {
                    "status": "AVAILABLE",
                    "digest": "x",
                    "remote_published": True,
                },
                "runtime": {},
            },
        ),
        (
            "register",
            {
                "quality": {"status": "PASSED"},
                "artifact": {
                    "status": "AVAILABLE",
                    "digest": "x",
                    "remote_published": True,
                },
                "runtime": {"candidate": {"revision": "r"}},
            },
        ),
        (
            "promote",
            {
                "quality": {"status": "PASSED"},
                "artifact": {
                    "status": "AVAILABLE",
                    "digest": "x",
                    "remote_published": True,
                },
                "runtime": {
                    "candidate": {"revision": "r"},
                    "platform_registration": {"status": "candidate"},
                },
            },
        ),
        (
            "close",
            {
                "quality": {"status": "PASSED"},
                "artifact": {
                    "status": "AVAILABLE",
                    "digest": "x",
                    "remote_published": True,
                },
                "runtime": {
                    "candidate": {"revision": "r"},
                    "platform_registration": {"status": "candidate"},
                    "production": {"revision": "r"},
                },
            },
        ),
    ]
    for expected, changes in stages:
        current = json.loads(json.dumps(value))
        for key, item in changes.items():
            current[key] = item
        assert release_lifecycle._next_lifecycle_stage(current) == expected
        assert release_lifecycle._next_command(api_path, current, expected)


@pytest.mark.parametrize(
    "operation", ["publish", "candidate", "promote", "rollback", "register"]
)
def test_every_lifecycle_dry_run_has_no_remote_effects(
    monkeypatch, tmp_path, operation
):
    value = manifest(tmp_path)
    path = write_manifest(tmp_path, value)
    monkeypatch.setattr(
        release_lifecycle,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run attempted an external command")
        ),
    )
    kwargs = {
        "state_dir": tmp_path / f"{operation}-state",
        "execute": False,
        "confirm_remote_effects": False,
    }
    if operation == "promote":
        kwargs["confirmation"] = ""
    elif operation == "rollback":
        kwargs["target_revision"] = ""
        kwargs["confirmation"] = ""
    elif operation == "register":
        kwargs.update(
            {"status": "candidate", "revision": "", "platform_api_url": "", "token": ""}
        )
    target = (
        release_lifecycle.register_release
        if operation == "register"
        else getattr(release_lifecycle, operation)
    )
    result = target(path, **kwargs)
    assert result["execution"]["status"] == "PLANNED"
    assert result["execution"]["remote_effects"] == []


@pytest.mark.parametrize("operation", ["candidate", "promote", "rollback"])
def test_sanplat_generic_routes_are_all_blocked(monkeypatch, tmp_path, operation):
    value = manifest(tmp_path, "cgm-sanplat-api")
    path = write_manifest(tmp_path, value)
    with pytest.raises(
        release_lifecycle.LifecycleError, match="SanPlat generic lifecycle commands"
    ):
        kwargs = {
            "state_dir": tmp_path / f"{operation}-state",
            "execute": True,
            "confirm_remote_effects": True,
        }
        if operation == "promote":
            kwargs["confirmation"] = "PROMOTE_PROD"
        elif operation == "rollback":
            kwargs.update(
                {"target_revision": "known-good", "confirmation": "ROLLBACK_PROD"}
            )
        getattr(release_lifecycle, operation)(path, **kwargs)
