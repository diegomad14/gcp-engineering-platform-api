"""Artemis release policy is an exact, offline twelve-field catalog edit."""

import copy
import hashlib
import json
import sys
from unittest import mock

import pytest

from scripts import pin_artemis_release_catalog as policy
from eng_platform_api.config import config
from eng_platform_api.models import CatalogService
from eng_platform_api.services import release_orchestrator


def document():
    rows = []
    for name in sorted(policy.SERVICES):
        rows.append(
            {
                "service_name": name,
                "repository": policy.REPOSITORY,
                "management_mode": "managed",
                "owner": "cgm",
                "project_id": "test-project",
                "region": "us-central1",
                "quality": {"enabled": True},
                "release": {"executor": "cloud_build"},
                "deployment": {"executor": "cloud_build", "enabled": True},
            }
        )
    rows.insert(
        3,
        {
            "service_name": "cgm-sanplat-api",
            "repository": "diegomad14/cgm-sanplat-api",
            "release": {"executor": None},
            "private_marker": "lector ñ Ω",
        },
    )
    rows.insert(
        8,
        {
            "service_name": "cgm-artemis-web",
            "repository": "diegomad14/cgm-artemis-web",
            "release": {"executor": "cloud_build"},
        },
    )
    rows.append(
        {
            "service_name": "observation",
            "management_mode": "observability_only",
            "repository": None,
            "release": {"executor": None},
        }
    )
    return {"services": rows, "unchanged": "á"}


def encode(value, compact=False):
    return json.dumps(value, ensure_ascii=False, indent=None if compact else 2).encode()


def reviewed(monkeypatch, raw):
    monkeypatch.setattr(policy, "EXPECTED_SHA", hashlib.sha256(raw).hexdigest())
    return raw


@pytest.mark.parametrize("compact", [False, True])
def test_exact_twelve_changes_replay_preserves_all_other_bytes(monkeypatch, compact):
    raw = reviewed(monkeypatch, encode(document(), compact))
    candidate, report = policy.prepare(raw)
    replay = raw
    assert len(report["exact_byte_edits"]) == 12
    for edit in report["exact_byte_edits"]:
        offset, old, new = (
            edit["offset"],
            edit["old_utf8"].encode(),
            edit["new_utf8"].encode(),
        )
        assert old == b'"cloud_build"' and new == b'"github_actions"'
        assert replay[offset : offset + len(old)] == old
        replay = replay[:offset] + new + replay[offset + len(old) :]
    assert replay == candidate
    assert hashlib.sha256(candidate).hexdigest() == report["candidate_sha256"]
    before, after = json.loads(raw), json.loads(candidate)
    for original, changed in zip(before["services"], after["services"], strict=True):
        if original["service_name"] in policy.SERVICES:
            expected = copy.deepcopy(original)
            expected["release"]["executor"] = "github_actions"
            assert changed == expected
            assert changed["deployment"] == original["deployment"]
        else:
            assert changed == original
    assert "lector" not in json.dumps(report)
    assert report["bytes_outside_target_entries_identical"]
    assert report["network_or_production_action"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["services"].pop(0),
        lambda d: d["services"].append(copy.deepcopy(d["services"][0])),
        lambda d: d["services"][0].update(repository="diegomad14/cgm-sanplat-api"),
        lambda d: d["services"][0].update(repository=policy.REPOSITORY.upper()),
        lambda d: d["services"][0].update(management_mode="observability_only"),
        lambda d: d["services"][0].pop("management_mode"),
        lambda d: d["services"][0].update(quality={"enabled": False}),
        lambda d: d["services"][0].update(quality={"enabled": 1}),
        lambda d: d["services"][0].update(quality=[]),
        lambda d: d["services"][0].update(release={"executor": "github_actions"}),
        lambda d: d["services"][0].update(release={"executor": None}),
        lambda d: d["services"][0].update(release=[]),
        lambda d: d["services"].append(
            {"service_name": "unreviewed-worker", "repository": policy.REPOSITORY}
        ),
        lambda d: d["services"].append(
            {
                "service_name": "unreviewed-worker",
                "repository": policy.REPOSITORY.upper(),
            }
        ),
        lambda d: d["services"][0].update(service_name=None),
        lambda d: d["services"].append("invalid"),
        lambda d: d.update(services={}),
    ],
)
def test_identity_scope_quality_and_baseline_changes_fail_closed(monkeypatch, mutation):
    value = document()
    mutation(value)
    with pytest.raises(ValueError):
        policy.prepare(reviewed(monkeypatch, encode(value)))


@pytest.mark.parametrize(
    "raw", [b'{"services":[],"services":[]}', b'{"services":[],"bad":NaN}', b"[]"]
)
def test_noncanonical_json_is_rejected(monkeypatch, raw):
    with pytest.raises(ValueError):
        policy.prepare(reviewed(monkeypatch, raw))


def test_source_pin_is_fixed_and_stale_bytes_are_rejected():
    assert (
        policy.EXPECTED_SHA
        == "b82cdad9d13a881b25b777d5f9f0cb76f909c17e9cb11e823805b13d88da3c1d"
    )
    with pytest.raises(ValueError, match="SHA256"):
        policy.prepare(encode(document()))


@pytest.mark.parametrize("name", sorted(policy.SERVICES))
def test_explicit_release_pin_precedes_all_cloud_build_overrides(monkeypatch, name):
    raw = reviewed(monkeypatch, encode(document()))
    candidate, _ = policy.prepare(raw)
    row = next(
        row for row in json.loads(candidate)["services"] if row["service_name"] == name
    )
    service = CatalogService(**row)
    monkeypatch.setattr(
        config.release_orchestrator, "private_executor_mode", "cloud_build"
    )
    monkeypatch.setattr(
        config.cloud_build, "cloud_build_only_services", tuple(policy.SERVICES)
    )
    forbidden = mock.Mock(
        side_effect=AssertionError(
            "Explicit release must not inspect GitHub visibility or quota"
        )
    )
    monkeypatch.setattr(
        release_orchestrator.github_release_control, "repository_is_private", forbidden
    )
    monkeypatch.setattr(
        release_orchestrator.github_actions_quota, "explicit_private_policy", forbidden
    )
    monkeypatch.setattr(release_orchestrator.executor_circuits, "get", forbidden)
    assert service.deployment.executor == "cloud_build"
    assert release_orchestrator._provider(service) == "github_actions"
    forbidden.assert_not_called()


def test_cli_writes_separate_new_candidate_and_bounded_report(
    monkeypatch, tmp_path, capsys
):
    source, candidate = tmp_path / "source.json", tmp_path / "candidate.json"
    raw = reviewed(monkeypatch, encode(document()))
    source.write_bytes(raw)
    monkeypatch.setattr(
        sys, "argv", ["policy", "--source", str(source), "--candidate", str(candidate)]
    )
    policy.main()
    assert source.read_bytes() == raw
    assert candidate.read_bytes() == policy.prepare(raw)[0]
    assert json.loads(capsys.readouterr().out)["status"] == "prepared_not_applied"
    with pytest.raises(FileExistsError):
        policy.main()
    monkeypatch.setattr(
        sys, "argv", ["policy", "--source", str(source), "--candidate", str(source)]
    )
    with pytest.raises(ValueError, match="separate"):
        policy.main()
