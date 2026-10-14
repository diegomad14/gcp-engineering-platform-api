"""The reviewed GitHub migration preserves every field outside twenty two pins."""

import copy
import hashlib
import json
import sys
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from scripts import pin_github_defaults_catalog as policy
from eng_platform_api.config import config
from eng_platform_api.models import CatalogService
from eng_platform_api.services import (
    deployment_commands,
    log_catalog,
    release_executions,
    release_orchestrator,
)


def document(release_shape="null"):
    rows = []
    for name, repository in sorted(policy.SERVICE_REPOSITORIES.items()):
        row = {
            "service_name": name,
            "repository": repository,
            "management_mode": "managed",
            "owner": "cgm",
            "project_id": "test-project",
            "region": "us-central1",
            "quality": {"enabled": True},
            "private_marker": "lector ñ Ω",
            "deployment": {
                "enabled": True,
                "executor": "github_actions"
                if name in policy.EXISTING_GITHUB_DEPLOYMENTS
                else "cloud_build"
                if name in policy.artemis.SERVICES
                else "auto",
            },
        }
        if name in policy.EXISTING_GITHUB_RELEASES:
            row["release"] = {"executor": "github_actions"}
        elif name == "cgm-artemis-web":
            row["release"] = {"executor": "cloud_build"}
        elif release_shape == "null":
            row["release"] = {"executor": None}
        elif release_shape == "empty":
            row["release"] = {}
        rows.append(row)
    for index in range(32):
        rows.insert(
            index,
            {
                "service_name": "observation-" + str(index),
                "management_mode": "observability_only",
                "repository": None,
                "deployment": {"enabled": False, "executor": "auto"},
            },
        )
    rows.insert(
        7,
        {
            "service_name": "disabled-extra",
            "management_mode": "managed",
            "repository": "owner/disabled",
            "deployment": {"enabled": False, "executor": "cloud_build"},
            "release": {"executor": "cloud_build"},
        },
    )
    return {"services": rows, "unchanged": "á"}


def encoded(value, compact=False):
    return json.dumps(value, ensure_ascii=False, indent=None if compact else 2).encode()


def reviewed(monkeypatch, raw):
    monkeypatch.setattr(policy, "EXPECTED_SHA", hashlib.sha256(raw).hexdigest())
    return raw


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("shape", ["null", "empty", "absent"])
def test_replay_twenty_two_edits_preserves_observations_disabled_and_existing_pins(
    monkeypatch, compact, shape
):
    raw = reviewed(monkeypatch, encoded(document(shape), compact))
    candidate, report = policy.prepare(raw)
    assert report["edit_counts"] == {"release": 7, "deployment": 15}
    assert len(report["exact_byte_edits"]) == 22
    replay = raw
    for edit in report["exact_byte_edits"]:
        offset, old, new = (
            edit["offset"],
            edit["old_utf8"].encode(),
            edit["new_utf8"].encode(),
        )
        assert replay[offset : offset + len(old)] == old
        replay = replay[:offset] + new + replay[offset + len(old) :]
    assert replay == candidate
    assert hashlib.sha256(replay).hexdigest() == report["candidate_sha256"]
    before, after = json.loads(raw), json.loads(candidate)
    for original, changed in zip(before["services"], after["services"], strict=True):
        name = original["service_name"]
        if name in policy.SERVICE_REPOSITORIES:
            expected = copy.deepcopy(original)
            for field in ["release", "deployment"]:
                expected.setdefault(field, {})["executor"] = "github_actions"
            assert changed == expected
            if name in policy.EXISTING_GITHUB_RELEASES:
                assert original["release"] == changed["release"]
        else:
            assert original == changed
    assert "lector" not in json.dumps(report)
    assert report["bytes_outside_target_entries_identical"]
    assert report["network_or_production_action"] is False


def target(document):
    return next(
        row for row in document["services"] if row["service_name"] == "cgm-bot-api"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["services"].remove(target(d)),
        lambda d: d["services"].append(copy.deepcopy(target(d))),
        lambda d: target(d).update(repository="owner/unreviewed"),
        lambda d: target(d).update(management_mode="observability_only"),
        lambda d: target(d)["deployment"].update(enabled=False),
        lambda d: target(d)["deployment"].pop("enabled"),
        lambda d: target(d)["deployment"].update(enabled=1),
        lambda d: target(d)["deployment"].update(executor="cloud_build"),
        lambda d: target(d).update(release={"executor": "cloud_build"}),
        lambda d: target(d).update(release=None),
        lambda d: target(d).update(deployment=None),
        lambda d: d["services"].append(
            {
                "service_name": "unreviewed",
                "management_mode": "managed",
                "deployment": {"enabled": True},
            }
        ),
        lambda d: d["services"].append({"service_name": "invalid", "deployment": None}),
        lambda d: target(d).update(service_name=None),
        lambda d: d["services"].append("invalid"),
        lambda d: d.update(services={}),
    ],
)
def test_snapshot_drift_or_scope_broadening_is_rejected(monkeypatch, mutation):
    value = document()
    mutation(value)
    with pytest.raises(ValueError):
        policy.prepare(reviewed(monkeypatch, encoded(value)))


@pytest.mark.parametrize(
    "raw", [b"[]", b'{"services":[],"services":[]}', b'{"services":[],"bad":NaN}']
)
def test_invalid_json_is_rejected(monkeypatch, raw):
    with pytest.raises(ValueError):
        policy.prepare(reviewed(monkeypatch, raw))


def test_baseline_hash_is_fixed():
    assert (
        policy.EXPECTED_SHA
        == "fcfe13a685cd607004b6f132d476b66f550cf8b8b72ae22d0825375abb9a1c17"
    )
    with pytest.raises(ValueError, match="SHA256"):
        policy.prepare(encoded(document()))


def test_cli_never_overwrites_source_or_candidate(monkeypatch, tmp_path, capsys):
    source, candidate = tmp_path / "source.json", tmp_path / "candidate.json"
    raw = reviewed(monkeypatch, encoded(document()))
    source.write_bytes(raw)
    monkeypatch.setattr(
        sys, "argv", ["policy", "--source", str(source), "--candidate", str(candidate)]
    )
    policy.main()
    assert source.read_bytes() == raw
    assert candidate.read_bytes() == policy.prepare(raw)[0]
    assert json.loads(capsys.readouterr().out)["edit_counts"] == {
        "release": 7,
        "deployment": 15,
    }
    with pytest.raises(FileExistsError):
        policy.main()
    monkeypatch.setattr(
        sys, "argv", ["policy", "--source", str(source), "--candidate", str(source)]
    )
    with pytest.raises(ValueError, match="separate"):
        policy.main()


@pytest.mark.parametrize("legacy", [False, True])
def test_current_release_admission_ignores_runtime_cloud_build_fallback_and_keeps_lock(
    monkeypatch, legacy
):
    service = CatalogService(
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        owner="platform",
        project_id="test-project",
        region="us-central1",
    )
    if legacy:
        service.release.executor = None
        service.deployment.executor = "auto"
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(log_catalog, "load_catalog", lambda: [service.model_dump()])
    monkeypatch.setattr(release_orchestrator.catalog, "get_service", lambda _: service)
    monkeypatch.setattr(release_executions, "_collection", lambda: None)
    monkeypatch.setattr(config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(
        config.release_orchestrator, "enabled_services", (service.service_name,)
    )
    monkeypatch.setattr(
        config.release_orchestrator, "private_executor_mode", "cloud_build"
    )
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        config.cloud_build,
        "cloud_build_only_services",
        tuple(policy.SERVICE_REPOSITORIES),
    )
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", True)
    monkeypatch.setattr(
        config.release_orchestrator,
        "quality_python_image",
        "quality@sha256:" + "d" * 64,
    )
    monkeypatch.setattr(
        config.release_orchestrator,
        "release_planner_image",
        "planner@sha256:" + "e" * 64,
    )
    control = release_orchestrator.github_release_control
    monkeypatch.setattr(control, "current_default_sha", lambda _: "a" * 40)
    monkeypatch.setattr(control, "is_fast_forward", lambda *_: True)
    monkeypatch.setattr(release_orchestrator, "_start_checks", Mock())
    github = Mock()
    forbidden = Mock(
        side_effect=AssertionError(
            "No implicit quota, visibility or Cloud Build admission"
        )
    )
    monkeypatch.setattr(control, "dispatch_release", github)
    monkeypatch.setattr(control, "repository_is_private", forbidden)
    monkeypatch.setattr(
        deployment_commands.github_actions_quota, "should_use_cloud_build", forbidden
    )
    monkeypatch.setattr(release_orchestrator, "_submit_if_managed", forbidden)
    assert deployment_commands._selection(service, None).effective == "github_actions"
    kwargs = dict(
        service_name=service.service_name,
        head_sha="a" * 40,
        base_sha="b" * 40,
        requested_by="operator",
    )
    record = release_orchestrator.start_release(**kwargs)
    assert record["provider"] == "github_actions"
    assert record["provider_selection"]["locked"] is True
    assert (
        release_orchestrator.start_release(**kwargs)["execution_id"]
        == record["execution_id"]
    )
    with pytest.raises(HTTPException) as caught:
        release_orchestrator.start_release(**kwargs, provider="cloud_build")
    assert caught.value.status_code == 409
    github.assert_called_once()
    forbidden.assert_not_called()
