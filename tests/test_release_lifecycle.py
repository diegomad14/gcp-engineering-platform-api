import json
from pathlib import Path

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
    assert plan["gates"]["frontend_api_base_url_declared"] is True
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


def test_revision_ready_accepts_cloud_run_condition_succeeded():
    assert release_lifecycle._revision_ready(
        {"status": {"conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}]}}
    )
