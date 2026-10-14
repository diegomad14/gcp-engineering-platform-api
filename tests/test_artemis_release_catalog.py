"""Artemis catalog maintenance preserves provenance and stops before unsafe writes."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import google.auth
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport import requests as transport
from google.cloud import storage
import pytest

from scripts import artemis_release_catalog as a


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
    }
