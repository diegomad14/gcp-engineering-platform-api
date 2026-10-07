"""Fake-only fixtures exercise the fail-closed shadow trust boundary."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from eng_platform_api import quality_evidence as evidence
from eng_platform_api.services import quality_observation_store as store
from eng_platform_api.services import quality_store

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def observation():
    identifiers = ["a" * 64]
    return {
        "schema_version": 1,
        "state": "observed",
        "coverage": {
            "src/example.py": {"executed": [1, 2], "missing": [3], "excluded": []}
        },
        "tests": {
            "items": [[identifiers[0], 1, 1, 1]],
            "collection_sha256": evidence.digest(identifiers),
            "collected": 1,
            "deselected": 0,
            "collection_errors": 0,
            "exit_code": 0,
            "complete": True,
        },
        "dependencies": {"sha256": "b" * 64, "files": 4, "bytes": 40, "complete": True},
        "runtime": {
            "implementation": "cpython",
            "version": "3.12.1",
            "os": "linux",
            "machine": "x86_64",
        },
        "inputs": {
            "source_tree": "c" * 40,
            "config_sha256": "d" * 64,
            "fixture_sha256": "e" * 64,
            "command_sha256": "f" * 64,
        },
        "measurement_ms": 1,
    }


def execution():
    return {
        "execution_id": "fake-execution",
        "repository": "example/test-repo",
        "service_name": "example-api",
        "head_sha": "1" * 40,
        "base_sha": "2" * 40,
        "fingerprint": "3" * 64,
        "provider": "github_actions",
        "provider_run_id": "123",
        "executor_digest": "registry.example/executor@sha256:" + "4" * 64,
        "profile_hash": "5" * 64,
        "policy_hash": "6" * 64,
        "operation": "pr_quality",
        "pending_report_hash": "7" * 64,
        "engine_event_status": "quality_passed",
    }


def receipt(obs=None, context=None):
    return evidence.seal_observation(
        obs or observation(), context or execution(), "7" * 64, NOW.isoformat()
    )


def pair():
    left = receipt()
    right = receipt(
        context={**execution(), "fingerprint": "8" * 64, "provider_run_id": "124"}
    )
    return left, right


def test_equal_materials_never_authorize_reuse():
    left, right = pair()
    before = copy.deepcopy((left, right))
    result = evidence.compare_shadow(left, right, now=NOW)
    assert result == {
        "mode": "shadow",
        "eligible": False,
        "reuse_allowed": False,
        "reasons": [
            "hermeticity_unproven",
            "stage_a_shadow_only",
            "untrusted_measurements",
        ],
    }
    assert (left, right) == before


@pytest.mark.parametrize(
    "key",
    [
        "repository",
        "service_name",
        "head_sha",
        "base_sha",
        "executor_digest",
        "profile_hash",
        "policy_hash",
    ],
)
def test_context_mismatch_is_explained(key):
    left, right = pair()
    replacements = {
        "repository": "other/test-repo",
        "service_name": "other-api",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "executor_digest": "registry.example/executor@sha256:" + "c" * 64,
        "profile_hash": "d" * 64,
        "policy_hash": "e" * 64,
    }
    right["context"][key] = replacements[key]
    assert f"{key}_mismatch" in evidence.compare_shadow(left, right, now=NOW)["reasons"]


@pytest.mark.parametrize(
    "key", ["source_tree", "config_sha256", "fixture_sha256", "command_sha256"]
)
def test_input_mismatch_is_explained(key):
    left, right = pair()
    right["observation"]["inputs"][key] = "9" * (40 if key == "source_tree" else 64)
    right["observation_sha256"] = evidence.digest(right["observation"])
    assert f"{key}_mismatch" in evidence.compare_shadow(left, right, now=NOW)["reasons"]


@pytest.mark.parametrize(
    "key,value",
    [
        (
            "dependencies",
            {"sha256": "9" * 64, "files": 4, "bytes": 40, "complete": True},
        ),
        (
            "runtime",
            {
                "implementation": "cpython",
                "version": "3.13.1",
                "os": "linux",
                "machine": "x86_64",
            },
        ),
        (
            "coverage",
            {"src/example.py": {"executed": [1], "missing": [2, 3], "excluded": []}},
        ),
    ],
)
def test_measurement_mismatch_is_explained(key, value):
    left, right = pair()
    right["observation"][key] = value
    right["observation_sha256"] = evidence.digest(right["observation"])
    assert f"{key}_mismatch" in evidence.compare_shadow(left, right, now=NOW)["reasons"]


def test_missing_tampered_stale_revoked_and_replayed_evidence():
    left, right = pair()
    assert (
        "previous_evidence_missing"
        in evidence.compare_shadow(None, right, now=NOW)["reasons"]
    )
    right["observation"]["coverage"]["src/example.py"]["executed"] = [1]
    assert (
        "current_evidence_invalid"
        in evidence.compare_shadow(left, right, now=NOW)["reasons"]
    )
    result = evidence.compare_shadow(
        left,
        left,
        now=NOW + timedelta(days=2),
        revoked_fingerprints=frozenset({"3" * 64}),
    )
    assert {
        "same_execution_replay",
        "previous_evidence_stale",
        "current_evidence_revoked",
    } <= set(result["reasons"])
    # Expanding TTL cannot enable reuse, including byte-identical materials.
    assert (
        evidence.compare_shadow(*pair(), now=NOW, max_age_seconds=10**9)["eligible"]
        is False
    )


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/private.py",
        "../secret.py",
        "src/../secret.py",
        "src//a.py",
        "src/./a.py",
        "src/a\\b.py",
        "src/a\n.py",
        ".git/config.py",
        "src/a.py?token=secret",
        "C:/private.py",
    ],
)
def test_malicious_paths_are_rejected(path):
    obs = observation()
    obs["coverage"] = {path: {"executed": [1], "missing": [], "excluded": []}}
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(obs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(provenance={"trusted": True}),
        lambda value: value.update(environment={"TOKEN": "never-export"}),
        lambda value: value["tests"].update(items=[["a" * 64, 1, 9, 1]]),
        lambda value: value["tests"].update(collection_sha256="0" * 64),
        lambda value: value["coverage"]["src/example.py"].update(missing=[2]),
        lambda value: value["coverage"]["src/example.py"].update(executed=[True]),
        lambda value: value.update(measurement_ms=float("nan")),
    ],
)
def test_untrusted_claims_and_invalid_shapes_are_rejected(mutate):
    obs = observation()
    mutate(obs)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(obs)


def test_byte_limit_and_incomplete_measurements_fail_closed():
    with pytest.raises(evidence.EvidenceError, match="byte limit"):
        evidence.validate_observation({"data": "x" * evidence.MAX_BYTES})
    obs = observation()
    obs["tests"]["items"][0][2] = 3
    obs["dependencies"]["complete"] = False
    result = evidence.compare_shadow(receipt(obs), receipt(), now=NOW)
    assert {
        "previous_tests_skipped_or_deselected",
        "previous_dependency_content_incomplete",
    } <= set(result["reasons"])
    missing = receipt(evidence.unavailable("missing_coverage"))
    assert (
        "previous_missing_coverage"
        in evidence.compare_shadow(missing, receipt(), now=NOW)["reasons"]
    )


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_BUCKET", "")
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setattr(store, "require_managed_service_name", lambda _: None)

    class FakeReport:
        repository = "example/test-repo"
        service_name = "example-api"
        commit_sha = "1" * 40
        base_sha = "2" * 40

    monkeypatch.setattr(quality_store, "get_pending_report", lambda *_: FakeReport())
    monkeypatch.setattr(quality_store, "_report_hash", lambda _: "7" * 64)
    return tmp_path


def test_immutable_store_is_idempotent_and_not_a_quality_report(local_store):
    first = store.save_observation(observation(), execution(), "7" * 64)
    assert store.save_observation(observation(), execution(), "7" * 64) == first
    assert store.get_observation("3" * 64) == first
    assert not (local_store / "quality/reports").exists()
    assert not (local_store / "quality/latest").exists()
    changed = observation()
    changed["measurement_ms"] = 2
    with pytest.raises(
        quality_store.QualityEvidenceConflict, match="execution conflicts"
    ):
        store.save_observation(changed, execution(), "7" * 64)
    assert store.get_observation("3" * 64) == first


def test_unaccepted_or_mismatched_report_cannot_be_sealed(local_store, monkeypatch):
    with pytest.raises(evidence.EvidenceError, match="accepted report"):
        store.save_observation(observation(), execution(), "8" * 64)
    monkeypatch.setattr(quality_store, "get_pending_report", lambda *_: None)
    with pytest.raises(evidence.EvidenceError, match="missing or mismatched"):
        store.save_observation(observation(), execution(), "7" * 64)
    assert list(local_store.iterdir()) == []


def test_blob_tamper_and_index_tamper_are_rejected(local_store):
    first = store.save_observation(observation(), execution(), "7" * 64)
    blob = (
        local_store
        / f"quality/test-observations/blobs/{first['observation_sha256']}.json"
    )
    blob.write_text("{}")
    with pytest.raises(quality_store.QualityEvidenceConflict, match="blob conflicts"):
        store.save_observation(observation(), execution(), "7" * 64)
    index = local_store / f"quality/test-observations/by-execution/{'3' * 64}.json"
    index.write_text("{}")
    with pytest.raises(evidence.EvidenceError):
        store.get_observation("3" * 64)


def test_gcs_write_once_uses_generation_zero(monkeypatch):
    calls = []

    class Blob:
        def upload_from_string(self, payload, **kwargs):
            calls.append(kwargs)

    class Client:
        def bucket(self, name):
            return self

        def blob(self, name):
            return Blob()

    monkeypatch.setenv("ENG_PLATFORM_QUALITY_BUCKET", "fake-bucket")
    monkeypatch.setattr(quality_store, "_storage_client", lambda: Client())
    assert quality_store._write_object(
        "fake-observation", {"schema_version": 1}, if_absent=True
    )
    assert calls == [{"content_type": "application/json", "if_generation_match": 0}]


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_schema_version_requires_exact_integer(value):
    obs = observation()
    obs["schema_version"] = value
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(obs)
    unavailable = evidence.unavailable("missing_coverage")
    unavailable["schema_version"] = value
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(unavailable)
    sealed = receipt()
    sealed["schema_version"] = value
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_receipt(sealed)


@pytest.mark.parametrize(
    "path", [("runtime", "implementation"), ("runtime", "os"), ("runtime", "machine")]
)
def test_unhashable_enums_fail_closed(path):
    obs = observation()
    obs[path[0]][path[1]] = []
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(obs)
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_observation(
            {"schema_version": 1, "state": "unavailable", "reason": []}
        )
    left, right = pair()
    right["context"]["provider"] = []
    assert (
        "current_evidence_invalid"
        in evidence.compare_shadow(left, right, now=NOW)["reasons"]
    )


@pytest.mark.parametrize("phases", [[1, 0, 1], [0, 0, 0]])
def test_complete_manifest_cannot_omit_execution_phases(phases):
    obs = observation()
    obs["tests"]["items"][0][1:] = phases
    with pytest.raises(evidence.EvidenceError, match="missing test phases"):
        evidence.validate_observation(obs)


@pytest.mark.parametrize("outcome", [2, 5])
def test_failed_or_unexpected_pass_always_explained(outcome):
    obs = observation()
    obs["tests"]["items"][0][2] = outcome
    assert (
        "previous_tests_incomplete_or_failed"
        in evidence.compare_shadow(receipt(obs), receipt(), now=NOW)["reasons"]
    )


def test_rejected_replay_creates_no_orphan_blobs(local_store):
    store.save_observation(observation(), execution(), "7" * 64)
    before = sorted(
        path.relative_to(local_store) for path in local_store.rglob("*.json")
    )
    for index in range(3):
        obs = observation()
        obs["measurement_ms"] = index + 2
        with pytest.raises(quality_store.QualityEvidenceConflict):
            store.save_observation(obs, execution(), "7" * 64)
    assert (
        sorted(path.relative_to(local_store) for path in local_store.rglob("*.json"))
        == before
    )


def test_swapped_receipt_does_not_match_requested_index(local_store):
    store.save_observation(observation(), execution(), "7" * 64)
    index = local_store / f"quality/test-observations/by-execution/{'3' * 64}.json"
    other = receipt(context={**execution(), "fingerprint": "8" * 64})
    index.write_bytes(evidence.canonical(other))
    with pytest.raises(evidence.EvidenceError, match="index identity"):
        store.get_observation("3" * 64)


def test_retry_repairs_interrupted_blob_after_receipt_claim(local_store, monkeypatch):
    original = quality_store._write_object
    first = True

    def interrupted(name, value, **kwargs):
        nonlocal first
        if "/blobs/" in name and first:
            first = False
            raise OSError("fake storage interruption")
        return original(name, value, **kwargs)

    monkeypatch.setattr(quality_store, "_write_object", interrupted)
    with pytest.raises(OSError):
        store.save_observation(observation(), execution(), "7" * 64)
    claimed = store.get_observation("3" * 64)
    assert claimed is not None
    recovered = store.save_observation(observation(), execution(), "7" * 64)
    assert recovered == claimed
    assert len(list(local_store.rglob("*.json"))) == 2


def test_concurrent_retries_have_one_identity_and_no_extra_blobs(local_store):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.save_observation(observation(), execution(), "7" * 64),
                range(16),
            )
        )
    assert all(item == results[0] for item in results)
    assert len(list(local_store.rglob("*.json"))) == 2


@pytest.mark.parametrize("later_event", ["release_planned", "no_release"])
def test_same_accepted_report_remains_idempotent_after_later_phase(
    local_store, later_event
):
    first = store.save_observation(observation(), execution(), "7" * 64)
    later = {**execution(), "engine_event_status": later_event}
    assert store.save_observation(observation(), later, "7" * 64) == first
    blob = (
        local_store
        / f"quality/test-observations/blobs/{first['observation_sha256']}.json"
    )
    blob.unlink()
    assert store.save_observation(observation(), later, "7" * 64) == first
    assert blob.exists()


def test_pending_file_alone_is_not_accepted_callback_authority(local_store):
    # The fixture supplies a readable report, but the server execution has not
    # accepted its callback. A caller-provided hash must not substitute for that.
    server = execution()
    server.pop("pending_report_hash")
    with pytest.raises(evidence.EvidenceError, match="accepted report"):
        store.save_observation(observation(), server, "7" * 64)
    assert list(local_store.iterdir()) == []
