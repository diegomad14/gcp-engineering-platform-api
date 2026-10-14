"""Bounded maintenance stays read-only and emits a reproducible scoped diff."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import google.auth
from google.auth.transport import requests as google_requests
import pytest
import yaml

from scripts import pin_reconnections_catalog as pin
from scripts import reconnections_catalog_preflight as pre


ROOT = Path(__file__).resolve().parents[1]


def catalog_bytes(release="absent", deployment="auto", compact=False):
    target = yaml.safe_load(
        (ROOT / "catalog/services/cgm-reconnections-api.yaml").read_text()
    )
    if release != "absent":
        target["release"] = release
    if deployment is None:
        target["deployment"].pop("executor")
    else:
        target["deployment"]["executor"] = deployment
    other = copy.deepcopy(target)
    other["service_name"] = "demo-private-other"
    other["repository"] = "demo/other"
    other["logs"] = {"enabled": False, "allowed_logins": ["demo-private-reader"]}
    other["infrastructure_resources"][0]["description"] += " ñ Ω"
    return json.dumps(
        {"services": [other, target]},
        ensure_ascii=False,
        indent=None if compact else 2,
    ).encode()


@pytest.mark.parametrize("release", ["absent", {}, {"executor": None}])
@pytest.mark.parametrize("deployment", [None, "auto"])
@pytest.mark.parametrize("compact", [True, False])
def test_exact_byte_patch_preserves_other_entry_and_private_data(
    release, deployment, compact
):
    raw = catalog_bytes(release, deployment, compact)
    candidate, proof = pin.prepare(raw, hashlib.sha256(raw).hexdigest())
    replay = raw
    for edit in proof["exact_byte_edits"]:
        old, new = edit["old_utf8"].encode(), edit["new_utf8"].encode()
        offset = edit["offset"]
        assert replay[offset : offset + len(old)] == old
        replay = replay[:offset] + new + replay[offset + len(old) :]
    assert replay == candidate
    assert hashlib.sha256(replay).hexdigest() == proof["candidate_sha256"]
    before, after = json.loads(raw), json.loads(candidate)
    assert before["services"][0] == after["services"][0]
    assert "demo-private-reader" not in json.dumps(proof)
    assert proof["bytes_outside_target_entry_identical"]
    assert after["services"][1]["release"]["executor"] == "github_actions"
    assert after["services"][1]["deployment"]["executor"] == "github_actions"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["services"][1].update(project_id="demo-wrong"),
        lambda d: d["services"][1].update(release={"executor": "cloud_build"}),
        lambda d: d["services"][1].update(deployment={"executor": "cloud_build"}),
        lambda d: d["services"].append(copy.deepcopy(d["services"][1])),
        lambda d: d["services"].pop(),
        lambda d: d["services"][0].update(
            repository="diegomad14/cgm-reconnections-api"
        ),
        lambda d: d.update(services="invalid"),
    ],
)
def test_stale_or_broadened_catalog_scope_rejected(change):
    document = json.loads(catalog_bytes())
    change(document)
    raw = json.dumps(document).encode()
    with pytest.raises(ValueError):
        pin.prepare(raw, hashlib.sha256(raw).hexdigest())


def test_integrity_and_duplicate_json_fail_closed():
    raw = catalog_bytes()
    with pytest.raises(ValueError, match="SHA256"):
        pin.prepare(raw, "0" * 64)
    raw = b'{"services": [], "services": []}'
    with pytest.raises(ValueError, match="duplicate"):
        pin.prepare(raw, hashlib.sha256(raw).hexdigest())
    raw = b'{"services": [], "invalid": NaN}'
    with pytest.raises(ValueError, match="constant"):
        pin.prepare(raw, hashlib.sha256(raw).hexdigest())


def test_offline_cli_never_overwrites_original_or_existing_candidate(
    tmp_path, monkeypatch
):
    raw = catalog_bytes()
    original, candidate = tmp_path / "original.json", tmp_path / "candidate.json"
    original.write_bytes(raw)
    monkeypatch.setattr(pin, "EXPECTED_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(
        "sys.argv", ["pin", "--source", str(original), "--candidate", str(candidate)]
    )
    pin.main()
    assert original.read_bytes() == raw
    with pytest.raises(FileExistsError):
        pin.main()
    monkeypatch.setattr(
        "sys.argv", ["pin", "--source", str(original), "--candidate", str(original)]
    )
    with pytest.raises(ValueError, match="separate"):
        pin.main()


def runtime():
    return {
        "metadata": {"name": pre.SERVICE},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": pre.BASE_IMAGE,
                            "env": [
                                {
                                    "name": "ENG_PLATFORM_CATALOG_SHA256",
                                    "value": pre.BASE_SHA,
                                },
                                {
                                    "name": "ENG_PLATFORM_CATALOG_PATH",
                                    "value": pre.BASE_PATH,
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
                            "csi": {
                                "driver": "gcsfuse.run.googleapis.com",
                                "volumeAttributes": {
                                    "bucketName": "demo-private-bucket"
                                },
                            },
                        }
                    ],
                }
            }
        },
    }


def test_mount_provenance_and_existing_directory_prefix():
    value = runtime()
    assert pre.source_identity(value)[0] == "demo-private-bucket"
    volume = value["spec"]["template"]["spec"]["volumes"][0]
    volume["csi"]["volumeAttributes"]["mountOptions"] = (
        "implicit-dirs,only-dir=private/catalog"
    )
    assert pre.source_identity(value)[1].startswith("private/catalog/")
    volume.pop("csi")
    volume["gcs"] = {"bucket": "demo-private-bucket", "mountOptions": []}
    assert pre.source_identity(value)[1].startswith("sha256-")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["metadata"].update(name="demo-other"),
        lambda d: d["spec"]["template"]["spec"]["containers"][0].update(
            image="demo-other"
        ),
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "ENG_PLATFORM_CATALOG_SHA256", "value": "0" * 64}
        ),
        lambda d: d["spec"]["template"]["spec"]["containers"][0].update(env=[]),
        lambda d: d["spec"]["template"]["spec"]["containers"][0].update(
            volumeMounts=[]
        ),
        lambda d: d["spec"]["template"]["spec"].update(volumes=[]),
        lambda d: d["spec"]["template"]["spec"]["volumes"][0]["csi"].update(
            driver="unknown"
        ),
        lambda d: d["spec"]["template"]["spec"]["volumes"][0]["csi"][
            "volumeAttributes"
        ].update(bucketName="other/bucket"),
        lambda d: d["spec"]["template"]["spec"]["volumes"][0]["csi"][
            "volumeAttributes"
        ].update(mountOptions="only-dir=../private"),
    ],
)
def test_unknown_or_changed_runtime_cannot_select_a_source(change):
    value = runtime()
    change(value)
    with pytest.raises(ValueError):
        pre.source_identity(value)


@pytest.mark.parametrize(
    "permission_response",
    [
        (200, ["storage.objects.get", "storage.objects.create"]),
        (200, ["storage.objects.get"]),
        (403, []),
    ],
)
def test_main_reads_generation_without_writes_and_audit_leaks(
    permission_response, tmp_path, monkeypatch
):
    raw = catalog_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(pre, "BASE_SHA", sha)
    monkeypatch.setattr(
        pre, "BASE_PATH", "/mnt/eng-platform-catalog/sha256-" + sha + ".json"
    )
    report = tmp_path / "report.json"
    monkeypatch.setenv("CGM_PREFLIGHT_REPORT", str(report))
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "true")
    commands = []

    def read(*args):
        commands.append(args)
        if args[:3] == ("gcloud", "auth", "list"):
            return pre.ACCOUNT.encode()
        if args[:4] == ("gcloud", "run", "services", "describe"):
            return json.dumps(runtime()).encode()
        if args[:4] == ("gcloud", "storage", "objects", "describe"):
            return b'{"generation": "17", "size": 12345}'
        assert args[:3] == ("gcloud", "storage", "cat")
        assert args[3].endswith("#17")
        return raw

    monkeypatch.setattr(pre, "read_command", read)
    monkeypatch.setattr(google.auth, "default", lambda **_kwargs: (object(), "demo"))
    status, permissions = permission_response
    gets = []

    class Session:
        def get(self, url, **kwargs):
            gets.append((url, kwargs))
            return SimpleNamespace(
                status_code=status, json=lambda: {"permissions": permissions}
            )

    monkeypatch.setattr(google_requests, "AuthorizedSession", lambda _creds: Session())
    if status == 403:
        with pytest.raises(RuntimeError, match="no permissions"):
            pre.main()
    else:
        pre.main()
    audit = json.loads(report.read_text())
    assert audit["mutation_performed"] is False
    assert "demo-private-reader" not in report.read_text()
    assert "demo-private-bucket" not in report.read_text()
    assert len(gets) == 1 and gets[0][0].endswith("/iam/testPermissions")
    assert len(commands) == 4
    if status == 200:
        assert len(audit["exact_byte_edits"]) == 2
        assert audit["schema_valid"] is True
        assert audit["bucket_test_reports_object_create"] == (len(permissions) == 2)


def test_failed_read_never_exposes_provider_stderr(monkeypatch):
    monkeypatch.setattr(
        pre.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"sensitive-private-provider-detail"
        ),
    )
    with pytest.raises(RuntimeError) as caught:
        pre.read_command("gcloud", "auth", "list")
    assert "sensitive" not in str(caught.value)


def test_read_error_retains_status_without_bucket_or_provider_payload(monkeypatch):
    monkeypatch.setattr(
        pre.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"ERROR 403 secret-provider-detail"
        ),
    )
    with pytest.raises(pre.ExistingReadError) as caught:
        pre.read_command("gcloud", "storage", "cat", "gs://demo-private-bucket/object")
    assert caught.value.status == 403
    assert caught.value.command == ["gcloud", "storage", "cat"]
    assert "private" not in str(caught.value)
    monkeypatch.setattr(
        pre.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"ok"),
    )
    assert pre.read_command("gcloud", "auth", "list") == b"ok"


@pytest.mark.parametrize("failure", ["identity", "metadata", "generation", "object"])
def test_main_fails_closed_with_precise_read_stage(failure, tmp_path, monkeypatch):
    report = tmp_path / "blocked.json"
    monkeypatch.setenv("CGM_PREFLIGHT_REPORT", str(report))

    def read(*args):
        if args[:3] == ("gcloud", "auth", "list"):
            return (
                b"wrong-existing-account"
                if failure == "identity"
                else pre.ACCOUNT.encode()
            )
        if args[:4] == ("gcloud", "run", "services", "describe"):
            return json.dumps(runtime()).encode()
        if args[:4] == ("gcloud", "storage", "objects", "describe"):
            if failure == "metadata":
                raise pre.ExistingReadError(args, 403)
            return json.dumps(
                {
                    "generation": "invalid" if failure == "generation" else "17",
                    "size": 120,
                }
            ).encode()
        raise pre.ExistingReadError(args, 403)

    monkeypatch.setattr(pre, "read_command", read)
    with pytest.raises(RuntimeError, match="no permissions"):
        pre.main()
    audit = json.loads(report.read_text())
    assert audit["mutation_performed"] is False
    assert (
        audit["stage"]
        == {
            "identity": "verify_existing_identity",
            "metadata": "read_original_object_metadata",
            "generation": "read_original_object_metadata",
            "object": "read_exact_original_generation",
        }[failure]
    )
    if failure in {"metadata", "object"}:
        assert audit["http_status"] == 403
    assert "demo-private-bucket" not in report.read_text()


def test_workflow_requires_owner_main_exact_quality_and_existing_identity():
    value = yaml.load(
        (ROOT / ".github/workflows/reconnections-catalog-preflight.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(value["on"]) == {"workflow_dispatch"}
    job = value["jobs"]["inspect"]
    assert job["runs-on"] == "ubuntu-latest"
    assert "97854332" in job["if"] and "refs/heads/main" in job["if"]
    gate = next(
        s for s in job["steps"] if s.get("uses") == "./.github/actions/verify-quality"
    )
    assert gate["with"]["commit-sha"] == "${{ github.sha }}"
    assert gate["with"]["wait-seconds"] == "0"
    assert not any("secrets." in json.dumps(s) for s in job["steps"])
