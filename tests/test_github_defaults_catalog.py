"""GitHub defaults maintenance and bounded trigger reads preserve production authority."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import google.auth
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport import requests as transport
from google.cloud import storage
import pytest

from scripts import github_defaults_catalog as a

INSPECT_TRIGGERS = a.inspect_triggers


CANDIDATE_SHA = "a" * 64
RAW = b'{"services": []}'
CANDIDATE = b'{"services": [], "approved": true}'
BUCKET = "private-catalog-test-bucket"
KEY = "private-prefix/sha256-" + a.SOURCE_SHA + ".json"


@pytest.fixture(autouse=True)
def restore_shared_modules(monkeypatch):
    for module, names in (
        (a.existing, ("BASE_SHA", "BASE_PATH")),
        (a.rollout, ("CANDIDATE_SHA", "OLD_REVISION", "REVISION", "TAG")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "true")
    monkeypatch.setattr(
        a,
        "inspect_triggers",
        Mock(return_value={"resources": [], "private_push_blocked": True}),
    )


def runtime():
    return {
        "metadata": {"name": a.existing.SERVICE},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": a.existing.BASE_IMAGE,
                            "env": [
                                {
                                    "name": "ENG_PLATFORM_CATALOG_SHA256",
                                    "value": a.SOURCE_SHA,
                                },
                                {
                                    "name": "ENG_PLATFORM_CATALOG_PATH",
                                    "value": a.existing.BASE_PATH,
                                },
                                {"name": a.ROUTING_KEYS[0], "value": "cloud_build"},
                                {"name": a.ROUTING_KEYS[1], "value": "auto"},
                                {
                                    "name": "UNRELATED_PRIVATE_ENV",
                                    "value": "private-value-must-not-leak",
                                },
                                {
                                    "name": "SECRET_REFERENCE",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": "private-secret"}
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "name": "catalog",
                                    "mountPath": "/mnt/eng-platform-catalog",
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "catalog",
                            "gcs": {
                                "bucket": BUCKET,
                                "mountOptions": ["only-dir=private-prefix"],
                            },
                        }
                    ],
                }
            }
        },
    }


def inspect_mocks(monkeypatch, *, metadata=None, account=None, value=None):
    a.configure(CANDIDATE_SHA)
    value = runtime() if value is None else value
    metadata = (
        {"generation": a.SOURCE_GENERATION, "size": len(RAW)}
        if metadata is None
        else metadata
    )
    replies = [
        ((a.existing.ACCOUNT if account is None else account) + "\n").encode(),
        json.dumps(value).encode(),
        json.dumps(metadata).encode(),
        RAW,
    ]
    reader = Mock(side_effect=replies)
    monkeypatch.setattr(a.existing, "read_command", reader)
    prepare = Mock(return_value=(CANDIDATE, {"candidate_sha256": CANDIDATE_SHA}))
    monkeypatch.setattr(a.policy, "prepare", prepare)
    from eng_platform_api.services import log_catalog

    validate = Mock()
    monkeypatch.setattr(log_catalog, "validate_catalog", validate)
    return reader, prepare, validate


@pytest.mark.parametrize(
    "candidate", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"]
)
def test_configure_rejects_unreviewed_candidate(candidate):
    with pytest.raises(ValueError, match="exact inspected"):
        a.configure(candidate)


def test_configure_preserves_exact_original_and_sets_candidate_identity():
    a.configure(CANDIDATE_SHA)
    assert a.existing.BASE_SHA == a.SOURCE_SHA
    assert (
        a.existing.BASE_PATH
        == "/mnt/eng-platform-catalog/sha256-" + a.SOURCE_SHA + ".json"
    )
    assert a.rollout.CANDIDATE_SHA == CANDIDATE_SHA
    assert a.rollout.OLD_REVISION == a.SOURCE_REVISION
    assert a.rollout.REVISION == "eng-platform-api-cfg-" + CANDIDATE_SHA[:12]
    assert a.rollout.TAG == "catalog-" + CANDIDATE_SHA[:12]


def test_inspect_reads_exact_generation_and_emits_only_allowed_environment(monkeypatch):
    reader, prepare, validate = inspect_mocks(monkeypatch)
    candidate, proof, bucket, key = a.inspect()
    assert (candidate, bucket, key) == (CANDIDATE, BUCKET, KEY)
    assert reader.call_args_list[2].args == (
        "gcloud",
        "storage",
        "objects",
        "describe",
        "gs://" + BUCKET + "/" + KEY,
        "--format=json",
    )
    assert reader.call_args_list[3].args == (
        "gcloud",
        "storage",
        "cat",
        "gs://" + BUCKET + "/" + KEY + "#" + a.SOURCE_GENERATION,
    )
    assert [call.args[:3] for call in reader.call_args_list] == [
        ("gcloud", "auth", "list"),
        ("gcloud", "run", "services"),
        ("gcloud", "storage", "objects"),
        ("gcloud", "storage", "cat"),
    ]
    prepare.assert_called_once_with(RAW)
    assert validate.call_count == 2
    assert validate.call_args_list[0].args == (json.loads(RAW),)
    assert validate.call_args_list[1].args == (json.loads(CANDIDATE),)
    assert (
        proof["bucket_identity_sha256"] == hashlib.sha256(BUCKET.encode()).hexdigest()
    )
    assert proof["existing_account"] == a.existing.ACCOUNT
    assert proof["source_generation"] == a.SOURCE_GENERATION
    assert proof["schema_valid"] is True
    assert proof["mutation_performed"] is False
    assert proof["routing_environment_changed"] is False
    assert set(proof["routing_environment"]) == set(a.ROUTING_KEYS)
    assert proof["routing_environment"][a.ROUTING_KEYS[0]] == "cloud_build"
    assert proof["routing_environment"][a.ROUTING_KEYS[-1]] is None
    serialized = json.dumps(proof)
    for private in [
        BUCKET,
        KEY,
        "private-value-must-not-leak",
        "private-secret",
        "SECRET_REFERENCE",
    ]:
        assert private not in serialized


def test_inspect_wrong_existing_principal_stops_before_cloud_metadata(monkeypatch):
    reader, prepare, validate = inspect_mocks(
        monkeypatch, account="other@example.invalid"
    )
    with pytest.raises(ValueError, match="reviewed identity"):
        a.inspect()
    assert reader.call_count == 1
    prepare.assert_not_called()
    validate.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        {"generation": "stale", "size": 1},
        {"size": 1},
        {"generation": a.SOURCE_GENERATION, "size": 0},
        {"generation": a.SOURCE_GENERATION, "size": -1},
        {"generation": a.SOURCE_GENERATION, "size": 32 * 1024 * 1024 + 1},
        {"generation": a.SOURCE_GENERATION},
    ],
)
def test_inspect_unbounded_or_changed_source_stops_before_bytes(monkeypatch, metadata):
    reader, prepare, validate = inspect_mocks(monkeypatch, metadata=metadata)
    with pytest.raises(ValueError, match="bounded immutable"):
        a.inspect()
    assert reader.call_count == 3
    prepare.assert_not_called()
    validate.assert_not_called()


def test_inspect_runtime_image_change_stops_before_bucket_read(monkeypatch):
    a.configure(CANDIDATE_SHA)
    value = runtime()
    value["spec"]["template"]["spec"]["containers"][0]["image"] = "unreviewed:latest"
    reader, prepare, _ = inspect_mocks(monkeypatch, value=value)
    with pytest.raises(ValueError, match="runtime image"):
        a.inspect()
    assert reader.call_count == 2
    prepare.assert_not_called()


def test_inspect_schema_failure_never_publishes(monkeypatch):
    _, _, validate = inspect_mocks(monkeypatch)
    validate.side_effect = ValueError("private catalog validation details")
    publisher = Mock()
    monkeypatch.setattr(a, "publish", publisher)
    with pytest.raises(ValueError):
        a.inspect()
    publisher.assert_not_called()


def publish_mocks(monkeypatch, *, already_exists=False, downloaded=CANDIDATE):
    credentials = object()
    default = Mock(return_value=(credentials, "ignored-project"))
    monkeypatch.setattr(google.auth, "default", default)
    session = object()
    authorize = Mock(return_value=session)
    monkeypatch.setattr(transport, "AuthorizedSession", authorize)
    blob = Mock(generation="123456")
    blob.download_as_bytes.return_value = downloaded
    if already_exists:
        blob.upload_from_string.side_effect = PreconditionFailed(
            "private object already exists"
        )
    bucket = Mock()
    bucket.blob.return_value = blob
    client = Mock()
    client.bucket.return_value = bucket
    create = Mock(return_value=client)
    monkeypatch.setattr(storage, "Client", create)
    cloud = object()
    monkeypatch.setattr(a.rollout, "ExistingCloud", Mock(return_value=cloud))
    activate = Mock(side_effect=lambda actual, upload, proof: upload())
    monkeypatch.setattr(a.rollout, "activate", activate)
    return SimpleNamespace(
        default=default,
        authorize=authorize,
        blob=blob,
        bucket=bucket,
        client=client,
        create=create,
        activate=activate,
        cloud=cloud,
        credentials=credentials,
        session=session,
    )


@pytest.mark.parametrize("already_exists", [False, True])
def test_publish_creates_only_exact_object_and_reuses_qualified_rollout(
    monkeypatch, already_exists
):
    mocks = publish_mocks(monkeypatch, already_exists=already_exists)
    proof = {"candidate_sha256": CANDIDATE_SHA, "mutation_performed": False}
    a.publish(CANDIDATE, proof, BUCKET, KEY)
    mocks.default.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    mocks.create.assert_called_once_with(
        project=a.existing.PROJECT, credentials=mocks.credentials
    )
    mocks.client.bucket.assert_called_once_with(BUCKET)
    mocks.bucket.blob.assert_called_once_with(
        "private-prefix/sha256-" + CANDIDATE_SHA + ".json"
    )
    mocks.blob.upload_from_string.assert_called_once_with(
        CANDIDATE,
        content_type="application/json",
        if_generation_match=0,
        timeout=30,
    )
    mocks.blob.reload.assert_called_once_with(timeout=20)
    mocks.blob.download_as_bytes.assert_called_once_with(
        if_generation_match="123456", timeout=30
    )
    assert mocks.activate.call_args.args[0] is mocks.cloud
    assert mocks.activate.call_args.args[2] is proof
    assert proof["candidate_object_generation"] == "123456"
    assert proof["mutation_performed"] is True


def test_publish_existing_different_bytes_stops_before_rollout_can_promote(monkeypatch):
    mocks = publish_mocks(
        monkeypatch, already_exists=True, downloaded=b"different private bytes"
    )
    proof = {"candidate_sha256": CANDIDATE_SHA, "mutation_performed": False}
    with pytest.raises(ValueError, match="object differs"):
        a.publish(CANDIDATE, proof, BUCKET, KEY)
    assert proof["production_write_attempted"] is True
    assert proof["mutation_performed"] is None
    assert "candidate_object_generation" not in proof
    assert mocks.activate.call_count == 1


def cli(monkeypatch, tmp_path, mode="inspect", sha=""):
    report = tmp_path / "audit.json"
    monkeypatch.setenv("CGM_ACTIVATION_REPORT", str(report))
    args = ["artemis_release_catalog", "--mode", mode]
    if sha:
        args += ["--candidate-sha256", sha]
    monkeypatch.setattr("sys.argv", args)
    return report


def test_main_inspect_is_read_only_and_writes_bounded_report(monkeypatch, tmp_path):
    report = cli(monkeypatch, tmp_path)
    inspector = Mock(
        return_value=(
            CANDIDATE,
            {"candidate_sha256": CANDIDATE_SHA, "mutation_performed": False},
            BUCKET,
            KEY,
        )
    )
    publisher = Mock()
    monkeypatch.setattr(a, "inspect", inspector)
    monkeypatch.setattr(a, "publish", publisher)
    a.main()
    publisher.assert_not_called()
    audit = json.loads(report.read_text())
    assert audit["status"] == "read_only_preflight_passed"
    assert audit["mutation_performed"] is False
    assert a.rollout.CANDIDATE_SHA == "0" * 64


def test_main_activate_uses_exact_inspected_candidate(monkeypatch, tmp_path):
    report = cli(monkeypatch, tmp_path, "activate", CANDIDATE_SHA)
    proof = {"candidate_sha256": CANDIDATE_SHA, "mutation_performed": False}
    monkeypatch.setattr(
        a, "inspect", Mock(return_value=(CANDIDATE, proof, BUCKET, KEY))
    )
    publisher = Mock(
        side_effect=lambda *_: proof.update(
            status="promoted_verified", mutation_performed=True
        )
    )
    monkeypatch.setattr(a, "publish", publisher)
    a.main()
    publisher.assert_called_once_with(CANDIDATE, proof, BUCKET, KEY)
    assert json.loads(report.read_text())["status"] == "promoted_verified"


def test_main_candidate_hash_mismatch_never_publishes(monkeypatch, tmp_path):
    report = cli(monkeypatch, tmp_path, "activate", CANDIDATE_SHA)
    monkeypatch.setattr(
        a,
        "inspect",
        Mock(
            return_value=(
                CANDIDATE,
                {"candidate_sha256": "b" * 64, "mutation_performed": False},
                BUCKET,
                KEY,
            )
        ),
    )
    publisher = Mock()
    monkeypatch.setattr(a, "publish", publisher)
    with pytest.raises(RuntimeError, match="stopped"):
        a.main()
    publisher.assert_not_called()
    audit = json.loads(report.read_text())
    assert audit["error_type"] == "ValueError"
    assert audit["mutation_performed"] is False
    assert audit.get("status") == "blocked"


@pytest.mark.parametrize(
    "error",
    [
        a.existing.ExistingReadError(("gcloud", "storage", "cat", "private-path"), 403),
        a.rollout.CloudError(403),
        ValueError("private-secret-must-not-appear"),
    ],
)
def test_main_inspection_errors_are_sanitized_in_blocked_audit(
    monkeypatch, tmp_path, error
):
    report = cli(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "inspect", Mock(side_effect=error))
    publisher = Mock()
    monkeypatch.setattr(a, "publish", publisher)
    with pytest.raises(RuntimeError, match="bounded audit") as raised:
        a.main()
    publisher.assert_not_called()
    audit = json.loads(report.read_text())
    assert audit["status"] == "blocked"
    assert audit["error_type"] == type(error).__name__
    assert audit.get("http_status") == (
        403 if not isinstance(error, ValueError) else None
    )
    assert "private" not in report.read_text()
    assert "private" not in str(raised.value)


def test_main_cloud_failure_after_inspection_preserves_bounded_failure(
    monkeypatch, tmp_path
):
    report = cli(monkeypatch, tmp_path, "activate", CANDIDATE_SHA)
    monkeypatch.setattr(
        a,
        "inspect",
        Mock(
            return_value=(
                CANDIDATE,
                {"candidate_sha256": CANDIDATE_SHA, "mutation_performed": False},
                BUCKET,
                KEY,
            )
        ),
    )
    monkeypatch.setattr(a, "publish", Mock(side_effect=a.rollout.CloudError(403)))
    with pytest.raises(RuntimeError):
        a.main()
    audit = json.loads(report.read_text())
    assert audit["http_status"] == 403
    assert audit["error_type"] == "CloudError"
    assert audit.get("status") == "blocked"
    assert "private" not in report.read_text()


def test_main_invalid_approved_hash_stops_before_inspection(monkeypatch, tmp_path):
    report = cli(monkeypatch, tmp_path, "activate", "not-sha256")
    inspector = Mock()
    monkeypatch.setattr(a, "inspect", inspector)
    with pytest.raises(RuntimeError):
        a.main()
    inspector.assert_not_called()
    assert json.loads(report.read_text()) == {
        "status": "blocked",
        "mutation_performed": False,
        "error_type": "ValueError",
        "stage": "configure",
        "error_code": "unclassified",
    }


def test_runtime_metadata_emits_only_reviewed_names_and_digest_images():
    images = {
        key: "registry.example/quality@sha256:" + "d" * 64
        for key in a.IMAGE_METADATA_KEYS
    }
    values = {
        **images,
        **dict.fromkeys(a.RELEASE_METADATA_KEYS, "true"),
        "SECRET": "do not expose",
    }
    report = a.runtime_metadata(values)
    assert set(report) == set(a.RELEASE_METADATA_KEYS) | set(a.IMAGE_METADATA_KEYS) | {
        "evidence_bucket_metadata"
    }
    assert report[a.IMAGE_METADATA_KEYS[0]] == images[a.IMAGE_METADATA_KEYS[0]]
    values[a.IMAGE_METADATA_KEYS[0]] = (
        "https://private-user:private-token@registry.example"
    )
    assert a.runtime_metadata(values)[a.IMAGE_METADATA_KEYS[0]] is None
    assert "private-token" not in json.dumps(a.runtime_metadata(values))


REFERENCE = "projects/cgm-assistant-prod/locations/us-central1/connections/existing/repositories/known"


def test_trigger_projection_maps_github_and_existing_connected_refs_without_private_payloads():
    rows = [
        {
            "name": "known github",
            "github": {
                "owner": "diegomad14",
                "name": "cgm-bot-core",
                "push": {"branch": "^main$"},
            },
            "disabled": False,
            "webhookConfig": {"secret": "DO_NOT_EXPOSE"},
            "build": {"steps": ["DO_NOT_EXPOSE"]},
        },
        {
            "name": "known connected",
            "repositoryEventConfig": {"repository": REFERENCE, "push": {"tag": "^v.*"}},
            "disabled": True,
        },
        {
            "name": "UNMAPPED_DO_NOT_EXPOSE",
            "github": {"owner": "private-owner", "name": "unknown"},
        },
        {"name": "UNKNOWN_DISABLED_DO_NOT_EXPOSE", "disabled": True},
    ]
    result = a.summarize_triggers(rows, {"communications-ms": REFERENCE})
    assert result["status"] == "CERTIFIED"
    assert result["active_unmapped_count"] == 1 and result["unmapped_count"] == 2
    assert result["active_mapped_count"] == 1
    assert result["mapped_triggers"][0] == {
        "repositories": ["diegomad14/cgm-bot-core"],
        "name": "known github",
        "branch": "^main$",
        "disabled": False,
    }
    assert result["mapped_triggers"][1]["connected_repository"] == REFERENCE
    assert result["mapped_triggers"][1]["tag"] == "^v.*"
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
    assert "private-owner" not in json.dumps(result)


@pytest.mark.parametrize(
    "rows,connected",
    [
        ({}, {}),
        (["invalid"], {}),
        ([{}] * 202, {}),
        ([], []),
        ([], {"known": 1}),
        ([{"disabled": "false"}], {}),
        ([{"github": []}], {}),
        ([{"repositoryEventConfig": []}], {}),
        ([{"github": {"owner": "diegomad14", "name": "cgm-bot-core", "push": []}}], {}),
        (
            [
                {
                    "github": {
                        "owner": "diegomad14",
                        "name": "cgm-bot-core",
                        "push": {"branch": "one"},
                    },
                    "repositoryEventConfig": {"push": {"branch": "two"}},
                }
            ],
            {},
        ),
        (
            [
                {
                    "github": {"owner": "diegomad14", "name": "cgm-bot-core"},
                    "repositoryEventConfig": {"repository": REFERENCE},
                }
            ],
            {"communications-ms": REFERENCE},
        ),
    ],
)
def test_invalid_or_ambiguous_trigger_metadata_is_not_certified(rows, connected):
    with pytest.raises(ValueError):
        a.summarize_triggers(rows, connected)


def test_bounded_trigger_limit_is_uncertified_and_does_not_leak_overflow():
    result = a.summarize_triggers([{}] * 201, {})
    assert result["status"] == "UNCERTIFIED" and result["limit_reached"]
    assert result["active_unmapped_count"] == 200


@pytest.mark.parametrize("output", [b"[]", b'[{"disabled":true}]'])
def test_trigger_reads_are_list_only_for_the_four_authorized_resources(
    monkeypatch, output
):
    read = Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS({a.REPOSITORIES_ENV: "{}"})
    assert report["private_push_blocked"] is False
    assert len(report["resources"]) == 4
    assert {row["project"] for row in report["resources"]} == {
        "cgm-assistant-prod",
        "cgm-reconnections-prod",
    }
    assert {row["region"] for row in report["resources"]} == {"global", "us-central1"}
    for call in read.call_args_list:
        assert call.args[0][:4] == ["gcloud", "builds", "triggers", "list"]
        assert call.kwargs["timeout"] == 45
        assert "--format=" + a.TRIGGER_FORMAT in call.args[0]
        assert "secret" not in a.TRIGGER_FORMAT


def test_forbidden_trigger_reads_report_403_per_resource_without_blocking_catalog_inspection(
    monkeypatch,
):
    read = Mock(
        return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"403 PRIVATE_PROVIDER_DIAGNOSTIC"
        )
    )
    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS({})
    assert report["private_push_blocked"]
    assert len(report["resources"]) == 4
    assert all(
        row["status"] == "UNCERTIFIED" and row["http_status"] == 403
        for row in report["resources"]
    )
    assert "PRIVATE_PROVIDER_DIAGNOSTIC" not in json.dumps(report)
    inspect_mocks(monkeypatch)
    monkeypatch.setattr(a, "inspect_triggers", lambda _: report)
    _, proof, _, _ = a.inspect()
    assert proof["schema_valid"] is True
    assert proof["cloud_build_triggers"] == report


@pytest.mark.parametrize(
    "output", [b"invalid", b"x" * (1024 * 1024 + 1), b'{"invalid":true}']
)
def test_bad_trigger_response_is_uncertified(monkeypatch, output):
    monkeypatch.setattr(
        a.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b"")),
    )
    report = INSPECT_TRIGGERS({})
    assert report["private_push_blocked"]
    assert all(row["status"] == "UNCERTIFIED" for row in report["resources"])


@pytest.mark.parametrize(
    "mapping", ["invalid", "[]", '{"known":1}', '{"known":"a","known":"b"}']
)
def test_invalid_in_memory_mapping_cannot_be_certified(monkeypatch, mapping):
    read = Mock()
    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS({a.REPOSITORIES_ENV: mapping})
    assert report["mapping_status"] == "UNCERTIFIED" and report["private_push_blocked"]
    read.assert_not_called()


def test_trigger_timeout_is_uncertified_and_active_known_trigger_blocks_push(
    monkeypatch,
):
    monkeypatch.setattr(
        a.subprocess,
        "run",
        Mock(side_effect=a.subprocess.TimeoutExpired(["gcloud"], 45)),
    )
    assert INSPECT_TRIGGERS({})["private_push_blocked"]
    output = json.dumps(
        [
            {
                "github": {
                    "owner": "diegomad14",
                    "name": "cgm-bot-core",
                    "push": {"branch": "main"},
                }
            }
        ]
    ).encode()
    monkeypatch.setattr(
        a.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b"")),
    )
    report = INSPECT_TRIGGERS({})
    assert report["private_push_blocked"]
    assert all(row["active_mapped_count"] == 1 for row in report["resources"])


@pytest.mark.parametrize(
    "reference",
    [
        "projects/cgm-assistant-prod/locations/europe-west1/connections/existing/repositories/bot",
        "projects/unreviewed-project/locations/us-central1/connections/existing/repositories/bot",
        "not-a-reviewed-resource",
    ],
)
@pytest.mark.parametrize("service_name", ["cgm-bot-api", "cgm-sanplat-api"])
def test_managed_mapping_outside_audited_resources_blocks_push_and_preserves_reads(
    monkeypatch, reference, service_name
):
    read = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"[]", stderr=b""))
    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS(
        {a.REPOSITORIES_ENV: json.dumps({service_name: reference})}
    )
    assert report["mapping_status"] == "UNCERTIFIED"
    assert report["private_push_blocked"] is True
    assert len(report["resources"]) == read.call_count == 4
    assert all(row["status"] == "CERTIFIED" for row in report["resources"])
    assert reference not in json.dumps(report)


def test_unrelated_mapping_does_not_expand_audited_resources(monkeypatch):
    read = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"[]", stderr=b""))
    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS(
        {a.REPOSITORIES_ENV: json.dumps({"not-managed": "private-unrelated-mapping"})}
    )
    assert report["mapping_status"] == "CERTIFIED"
    assert report["private_push_blocked"] is False
    assert read.call_count == 4
    assert "private-unrelated-mapping" not in json.dumps(report)


@pytest.mark.parametrize("family", ["api", "web"])
def test_existing_sanplat_aliases_map_to_the_same_artemis_repository(family):
    reference = REFERENCE
    canonical = "diegomad14/cgm-artemis-" + family
    rows = [
        {
            "github": {"owner": "diegomad14", "name": "cgm-sanplat-" + family},
            "repositoryEventConfig": {"repository": reference},
            "disabled": True,
        }
    ]
    connected = {
        "cgm-sanplat-" + family: reference,
        "cgm-artemis-" + family: reference,
    }
    report = a.summarize_triggers(
        rows, connected, ("cgm-assistant-prod", "us-central1")
    )
    assert report["status"] == "CERTIFIED"
    assert report["mapped_triggers"][0]["repositories"] == [canonical]
    assert report["mapped_count"] == 1


@pytest.mark.parametrize(
    "resource",
    [("cgm-assistant-prod", "global"), ("cgm-reconnections-prod", "us-central1")],
)
def test_known_connected_trigger_cannot_be_certified_in_wrong_resource(resource):
    with pytest.raises(ValueError, match="outside its listed resource"):
        a.summarize_triggers(
            [{"repositoryEventConfig": {"repository": REFERENCE}, "disabled": True}],
            {"communications-ms": REFERENCE},
            resource,
        )


def test_regional_connected_trigger_is_certified_only_in_its_queried_location(
    monkeypatch,
):
    def read(command, **_):
        project = command[command.index("--project") + 1]
        region = command[command.index("--region") + 1]
        rows = (
            [{"repositoryEventConfig": {"repository": REFERENCE}, "disabled": True}]
            if (project, region) == ("cgm-assistant-prod", "us-central1")
            else []
        )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(rows).encode(), stderr=b""
        )

    monkeypatch.setattr(a.subprocess, "run", read)
    report = INSPECT_TRIGGERS(
        {a.REPOSITORIES_ENV: json.dumps({"communications-ms": REFERENCE})}
    )
    assert report["mapping_status"] == "CERTIFIED"
    assert report["private_push_blocked"] is False
    assert sum(row["mapped_count"] for row in report["resources"]) == 1
    regional = next(row for row in report["resources"] if row["mapped_count"])
    assert (regional["project"], regional["region"]) == (
        "cgm-assistant-prod",
        "us-central1",
    )


def test_mislocated_connected_trigger_blocks_push_without_expanding_permissions(
    monkeypatch,
):
    output = json.dumps(
        [{"repositoryEventConfig": {"repository": REFERENCE}, "disabled": True}]
    ).encode()
    reader = Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b""))
    monkeypatch.setattr(a.subprocess, "run", reader)
    report = INSPECT_TRIGGERS(
        {a.REPOSITORIES_ENV: json.dumps({"communications-ms": REFERENCE})}
    )
    assert report["private_push_blocked"] is True
    assert reader.call_count == 4
    assert sum(row["status"] == "UNCERTIFIED" for row in report["resources"]) == 3


@pytest.mark.parametrize(
    "message,code",
    [
        ("reviewed managed service must remain enabled", "managed_mode_or_enabled"),
        ("unreviewed private secret", "unclassified"),
    ],
)
def test_policy_failure_preserves_stage_and_only_safe_error_code(
    monkeypatch, tmp_path, message, code
):
    inspect_mocks(monkeypatch)
    monkeypatch.setattr(a.policy, "prepare", Mock(side_effect=ValueError(message)))
    report = cli(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        a.main()
    result = json.loads(report.read_text())
    assert result["stage"] == "catalog_policy"
    assert result["error_code"] == code
    assert result["mutation_performed"] is False
    assert "private secret" not in report.read_text()


def test_bucket_metadata_contains_only_presence_and_name_hash():
    key = "ENG_PLATFORM_DEPLOYMENT_EVIDENCE_BUCKET"
    result = a.runtime_metadata({key: "private-existing-bucket"})
    assert result["evidence_bucket_metadata"][key] == {
        "present": True,
        "name_sha256": hashlib.sha256(b"private-existing-bucket").hexdigest(),
    }
    assert "private-existing-bucket" not in json.dumps(result)
    assert result["evidence_bucket_metadata"][
        "ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET"
    ] == {"present": False, "name_sha256": None}


@pytest.mark.parametrize(
    "number,project",
    [
        ("546821492326", "cgm-assistant-prod"),
        ("305411509341", "cgm-reconnections-prod"),
    ],
)
def test_verified_project_numbers_are_same_four_audit_scopes(
    monkeypatch, number, project
):
    reference = f"projects/{number}/locations/us-central1/connections/existing/repositories/known"
    assert a.connected_resource(reference) == (project, "us-central1")
    reader = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"[]", stderr=b""))
    monkeypatch.setattr(a.subprocess, "run", reader)
    result = INSPECT_TRIGGERS(
        {a.REPOSITORIES_ENV: json.dumps({"cgm-bot-api": reference})}
    )
    assert result["mapping_status"] == "CERTIFIED"
    assert result["private_push_blocked"] is False
    assert reader.call_count == 4


def test_source_shape_audit_is_known_names_and_booleans_only(monkeypatch):
    raw = json.dumps(
        {"services": [{"service_name": "cgm-bot-api", "private": "NEVER_EMIT"}]}
    ).encode()
    monkeypatch.setattr(a, "SOURCE_SHA", hashlib.sha256(raw).hexdigest())
    reader, _, _ = inspect_mocks(monkeypatch)
    replies = list(reader.side_effect)
    replies[-1] = raw
    reader.side_effect = replies
    audit = {}
    _, proof, _, _ = a.inspect(audit)
    assert audit is proof
    assert audit["source_hash_matches"] is True
    assert audit["managed_target_shapes"] == [
        {
            "service_name": "cgm-bot-api",
            "management_mode_present": False,
            "enabled_present": False,
            "enabled_is_true": True,
        }
    ]
    assert "NEVER_EMIT" not in json.dumps(proof)
    assert audit["stage"] == "preflight_complete"


@pytest.mark.parametrize(
    "reason", ["PERMISSION_DENIED", "UNAUTHENTICATED", "NOT_FOUND", "SERVICE_DISABLED"]
)
def test_trigger_errors_keep_static_reason_without_raw_diagnostics(monkeypatch, reason):
    reader = Mock(
        return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=(reason + " private-secret").encode()
        )
    )
    monkeypatch.setattr(a.subprocess, "run", reader)
    report = INSPECT_TRIGGERS({})
    assert report["private_push_blocked"]
    assert all(
        row["reason"] == reason and row["http_status"] is None
        for row in report["resources"]
    )
    assert "private-secret" not in json.dumps(report)
