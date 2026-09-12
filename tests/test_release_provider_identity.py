"""Provider identity regressions; all external effects are local fixtures."""

import copy
import json
from types import SimpleNamespace

import pytest

from scripts.release import release_lifecycle as lifecycle
from tests.test_release_lifecycle import manifest, write_manifest
from tests.test_release_lifecycle_local_integration import (
    _authorized_context,
    _controlled_client,
    controlled_api as controlled_api,
    health_server as health_server,
    register_server as register_server,
)


def revision_fixture(identity):
    return {
        "metadata": {
            "name": identity["revision"],
            "labels": {
                **lifecycle._provider_labels(identity),
                "serving.knative.dev/service": identity["service"],
            },
        },
        "status": {
            "imageDigest": identity["digest"],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


@pytest.fixture
def provider(monkeypatch, tmp_path):
    value = manifest(tmp_path)
    identity = lifecycle._candidate_identity(value)
    value["runtime"]["candidate"]["revision"] = identity["revision"]
    old = {
        **identity,
        "revision": "eng-platform-api-old",
        "source_sha": "e" * 40,
        "release_id": "old-release",
        "digest": "sha256:" + "f" * 64,
    }
    value["runtime"]["known_revisions"] = {old["revision"]: old}
    value["runtime"]["promotion"]["previous_revision"] = old["revision"]
    path = write_manifest(tmp_path, value)
    effects = []
    observations = {
        identity["revision"]: revision_fixture(identity),
        old["revision"]: revision_fixture(old),
    }

    def read(command, **kwargs):
        if "revisions" in command:
            return observations[command[4]]
        return {
            "status": {
                "latestCreatedRevisionName": "foreign-revision",
                "url": "https://fixture",
                "traffic": [{"revisionName": old["revision"], "percent": 100}],
            }
        }

    monkeypatch.setattr(lifecycle, "_json_command", read)
    monkeypatch.setattr(
        lifecycle, "_run_command", lambda command, **kwargs: effects.append(command)
    )
    monkeypatch.setattr(lifecycle, "_require_live_controls", lambda *args: None)
    return SimpleNamespace(
        value=value,
        identity=identity,
        old=old,
        path=path,
        effects=effects,
        observations=observations,
    )


@pytest.mark.parametrize("operation", ["candidate", "promote", "rollback"])
@pytest.mark.parametrize(
    "field",
    [
        "revision",
        "digest",
        "cgm-repository",
        "cgm-sha",
        "cgm-config",
        "cgm-release",
        "service",
        "ready",
    ],
)
def test_identity_mismatch_rejected_before_traffic(
    provider, tmp_path, operation, field
):
    identity = provider.old if operation == "rollback" else provider.identity
    observation = provider.observations[identity["revision"]]
    if field == "revision":
        observation["metadata"]["name"] = "foreign-same-digest"
    elif field == "digest":
        observation["status"]["imageDigest"] = "sha256:" + "0" * 64
    elif field == "ready":
        observation["status"]["conditions"] = []
    else:
        key = "serving.knative.dev/service" if field == "service" else field
        observation["metadata"]["labels"][key] = "foreign"
    kwargs = {}
    if operation != "candidate":
        kwargs["confirmation"] = (
            "PROMOTE_PROD" if operation == "promote" else "ROLLBACK_PROD"
        )
    if operation == "rollback":
        kwargs["target_revision"] = identity["revision"]
    with pytest.raises(lifecycle.LifecycleError):
        getattr(lifecycle, operation)(
            provider.path,
            state_dir=tmp_path / "state",
            execute=True,
            confirm_remote_effects=True,
            **kwargs,
        )
    assert not any("update-traffic" in command for command in provider.effects)


def test_rollback_rejects_unrecorded_ready_revision(provider, tmp_path):
    provider.value["runtime"].pop("known_revisions")
    write_manifest(tmp_path, provider.value)
    with pytest.raises(lifecycle.LifecycleError, match="prior known"):
        lifecycle.rollback(
            provider.path,
            state_dir=tmp_path / "state",
            target_revision=provider.old["revision"],
            execute=True,
            confirm_remote_effects=True,
            confirmation="ROLLBACK_PROD",
        )
    assert provider.effects == []


def test_publish_rejects_changed_digest_after_push(provider, monkeypatch, tmp_path):
    digests = iter([None, "sha256:" + "0" * 64])
    monkeypatch.setattr(
        lifecycle, "_remote_artifact_digest", lambda image: next(digests)
    )
    with pytest.raises(lifecycle.LifecycleError, match="expected digest"):
        lifecycle.publish(
            provider.path,
            state_dir=tmp_path / "state",
            execute=True,
            confirm_remote_effects=True,
        )
    assert [command[:2] for command in provider.effects] == [
        ["docker", "tag"],
        ["docker", "push"],
    ]
    assert (
        lifecycle.load_manifest(provider.path)["artifact"]["digest"]
        == provider.identity["digest"]
    )


def test_lost_deploy_response_reconciles_exact_revision_without_second_deploy(
    provider, controlled_api, health_server, monkeypatch, tmp_path
):
    context, token = _authorized_context(provider.value, "candidate", "diegomad14")
    client = _controlled_client(controlled_api)

    def lost_response(command, **kwargs):
        saved = lifecycle.load_manifest(provider.path)
        assert saved["runtime"]["candidate_deployment"] == provider.identity
        provider.effects.append(command)
        raise TimeoutError("response lost after deploy")

    monkeypatch.setattr(lifecycle, "_run_command", lost_response)
    with pytest.raises(TimeoutError):
        lifecycle.candidate(
            provider.path,
            state_dir=tmp_path / "state",
            execute=True,
            confirm_remote_effects=True,
            control_client=client,
            authorization_token=token,
            actor_id=context.actor_id,
            owner_id="provider-worker",
            local_fixture=True,
        )
    execution_path = next((tmp_path / "state" / "executions").glob("*.json"))
    execution = json.loads(execution_path.read_text())
    intent_id = execution["control_intents"][0]["intent_id"]
    assert client.get_intent(intent_id).status == "UNKNOWN"
    observation = provider.observations[provider.identity["revision"]]
    observation["metadata"]["name"] = "foreign-same-digest"
    with pytest.raises(lifecycle.LifecycleError, match="revision identity"):
        lifecycle.reconcile_candidate_deploy(
            provider.path,
            execution_path=execution_path,
            control_client=client,
            intent_id=intent_id,
            reconciliation_id="foreign-observation",
        )
    assert client.get_intent(intent_id).status == "UNKNOWN"
    observation["metadata"]["name"] = provider.identity["revision"]
    # Reload from disk and a new client, with a concurrent foreign latest revision.
    result = lifecycle.reconcile_candidate_deploy(
        provider.path,
        execution_path=execution_path,
        control_client=_controlled_client(controlled_api),
        intent_id=intent_id,
        reconciliation_id="exact-deploy-observation",
    )
    assert result["status"] == "CONFIRMED"
    assert client.get_intent(intent_id).status == "CONFIRMED"
    assert len(provider.effects) == 1
    assert result["identity"]["revision"] == provider.identity["revision"]
    assert (
        lifecycle.load_manifest(provider.path)["runtime"]["candidate_deploy_reconciled"]
        == provider.identity
    )
    # A fresh authorized session completes candidate tagging/probing, no redeploy.
    monkeypatch.setattr(
        lifecycle, "_run_command", lambda argv, **kwargs: provider.effects.append(argv)
    )
    monkeypatch.setattr(
        lifecycle,
        "_json_command",
        lambda argv, **kwargs: (
            observation
            if "revisions" in argv
            else {
                "status": {
                    "latestCreatedRevisionName": "foreign-same-digest",
                    "traffic": [
                        {
                            "tag": "candidate-v1-2-3",
                            "revisionName": provider.identity["revision"],
                            "url": health_server,
                        }
                    ],
                }
            }
        ),
    )
    current = lifecycle.load_manifest(provider.path)
    context, token = _authorized_context(current, "candidate", "diegomad14")
    completed = lifecycle.candidate(
        provider.path,
        state_dir=tmp_path / "state",
        execute=True,
        confirm_remote_effects=True,
        control_client=_controlled_client(controlled_api),
        authorization_token=token,
        actor_id=context.actor_id,
        owner_id="recovery-worker",
        local_fixture=True,
    )
    assert completed["execution"]["status"] == "SUCCEEDED"
    assert (
        sum(argv[:3] == ["gcloud", "run", "deploy"] for argv in provider.effects) == 1
    )


def test_candidate_url_rejects_foreign_tag_revision():
    with pytest.raises(lifecycle.LifecycleError, match="another revision"):
        lifecycle._candidate_url(
            {
                "status": {
                    "traffic": [
                        {
                            "tag": "candidate",
                            "revisionName": "foreign",
                            "url": "https://fixture",
                        }
                    ]
                }
            },
            "candidate",
            "owned",
        )


def test_deterministic_revision_changes_with_release_configuration_or_sha(provider):
    for key in ("release_id", "source", "catalog"):
        changed = copy.deepcopy(provider.value)
        if key == "source":
            changed[key]["sha"] = "1" * 40
        elif key == "catalog":
            changed[key]["deployment"]["health_path"] = "/other"
        else:
            changed[key] = "other-release"
        assert (
            lifecycle._candidate_identity(changed)["revision"]
            != provider.identity["revision"]
        )


@pytest.mark.parametrize(
    "service_name", ["eng-platform-api", "cgm-sanplat-api", "cgm-sanplat-web"]
)
def test_full_lifecycle_with_durable_control_and_synthetic_provider(
    controlled_api, register_server, health_server, monkeypatch, tmp_path, service_name
):
    """Actual lifecycle/control API, synthetic command provider and loopback HTTP."""
    value = manifest(tmp_path, service_name)
    identity = lifecycle._candidate_identity(value)
    old = {
        **identity,
        "revision": f"{service_name}-known-good",
        "source_sha": "e" * 40,
        "release_id": "prior-release",
        "digest": "sha256:" + "f" * 64,
    }
    value["runtime"] = {}
    value["artifact"]["remote_published"] = False
    path = write_manifest(tmp_path, value)
    state_dir = tmp_path / "state"
    previous = copy.deepcopy(value)
    previous["release_id"] = old["release_id"]
    previous["source"]["sha"] = old["source_sha"]
    previous["artifact"]["digest"] = old["digest"]
    previous["runtime"] = {
        "production": {"revision": old["revision"], "digest": old["digest"]}
    }
    lifecycle.local_release.write_json(
        lifecycle.local_release.state_paths(state_dir)["manifests"] / "previous.json",
        previous,
    )
    client = _controlled_client(controlled_api)
    remote = {
        "digest": None,
        "tag": None,
        "release": None,
        "active": old["revision"],
        "candidate": None,
    }
    revisions = {old["revision"]: revision_fixture(old)}
    effects = []

    def command(argv, **kwargs):
        # Every synthetic mutation observes an already persisted real intent.
        intents = [
            item
            for record in (state_dir / "executions").glob("*.json")
            for item in json.loads(record.read_text()).get("control_intents", [])
        ]
        assert any(
            client.get_intent(item["intent_id"]).status == "INTENDED"
            for item in intents
        )
        effects.append(argv)
        if argv[:2] == ["docker", "push"]:
            remote["digest"] = identity["digest"]
        elif argv[:2] == ["git", "push"]:
            remote["tag"] = identity["source_sha"]
        elif argv[:3] == ["gh", "release", "create"]:
            remote["release"] = {"url": "https://fixture.invalid/release"}
        elif argv[:3] == ["gcloud", "run", "deploy"]:
            assert argv[3] == service_name
            saved = lifecycle.load_manifest(path)["runtime"]["candidate_deployment"]
            assert saved == identity
            revisions[identity["revision"]] = revision_fixture(identity)
        elif "--set-tags" in argv:
            assert argv[4] == service_name
            remote["candidate"] = argv[argv.index("--set-tags") + 1].split("=", 1)[1]
        elif "--to-revisions" in argv:
            assert argv[4] == service_name
            remote["active"] = argv[argv.index("--to-revisions") + 1].split("=", 1)[0]
        elif argv[:2] not in (["docker", "tag"], ["git", "tag"]):
            raise AssertionError(f"Unexpected synthetic command: {argv}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def describe(argv, **kwargs):
        if "revisions" in argv:
            return revisions[argv[4]]
        traffic = [{"revisionName": remote["active"], "percent": 100}]
        if remote["candidate"]:
            traffic.append(
                {
                    "revisionName": remote["candidate"],
                    "tag": "candidate-v1-2-3",
                    "url": health_server,
                }
            )
        return {
            "status": {
                "latestCreatedRevisionName": "foreign-same-digest",
                "traffic": traffic,
                "url": health_server,
            }
        }

    monkeypatch.setattr(lifecycle, "_run_command", command)
    monkeypatch.setattr(lifecycle, "_json_command", describe)
    monkeypatch.setattr(
        lifecycle, "_remote_artifact_digest", lambda image: remote["digest"]
    )
    monkeypatch.setattr(lifecycle, "_remote_tag_target", lambda *args: remote["tag"])
    monkeypatch.setattr(lifecycle, "_local_tag_target", lambda *args: None)
    monkeypatch.setattr(lifecycle, "_release_view", lambda *args: remote["release"])
    results = []
    for operation in ("publish", "candidate", "register", "promote", "rollback"):
        current = lifecycle.load_manifest(path)
        context, token = _authorized_context(current, operation, "diegomad14")
        kwargs = dict(
            state_dir=state_dir,
            execute=True,
            confirm_remote_effects=True,
            control_client=client,
            authorization_token=token,
            actor_id=context.actor_id,
            owner_id=f"worker-{operation}",
            local_fixture=True,
        )
        if operation == "register":
            kwargs.update(
                status="candidate",
                revision=identity["revision"],
                platform_api_url=register_server,
                token="fixture-token",
            )
        elif operation == "promote":
            kwargs.update(confirmation="PROMOTE_PROD")
        elif operation == "rollback":
            kwargs.update(confirmation="ROLLBACK_PROD", target_revision=old["revision"])
        fn = (
            lifecycle.register_release
            if operation == "register"
            else getattr(lifecycle, operation)
        )
        result = fn(path, **kwargs)
        assert result["execution"]["status"] == "SUCCEEDED"
        for item in result["execution"]["control_intents"]:
            assert client.get_intent(item["intent_id"]).status == "CONFIRMED"
        results.append(result)
    assert len(results) == 5
    assert sum(argv[:3] == ["gcloud", "run", "deploy"] for argv in effects) == 1
    assert remote["active"] == old["revision"]
    assert (
        lifecycle.load_manifest(path)["runtime"]["rollback"]["digest"] == old["digest"]
    )
    assert not hasattr(lifecycle, "REMOTE_ACTIVATION_ENABLED")
