import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def control():
    path = Path(__file__).parents[1] / "scripts/ops/cloud-build-fallback/control.py"
    spec = importlib.util.spec_from_file_location("fallback_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def request_data():
    return dict(
        request_fingerprint="f" * 64,
        project_id="test",
        region="us-central1",
        build_profile="performance",
        commit_sha="a" * 40,
        base_sha="b" * 40,
    )


def test_disconnect_recovers_remote_build_without_resubmission(
    control, request_data, tmp_path, monkeypatch
):
    monkeypatch.setattr(control, "validate_inputs", lambda _: request_data)
    state = tmp_path / "state.json"
    calls = []
    build = {
        "id": "build-1",
        "status": "WORKING",
        "substitutions": {"_REQUEST_FINGERPRINT": request_data["request_fingerprint"]},
    }

    def cloud(*args):
        calls.append(args)
        if args[:2] == ("builds", "submit"):
            raise subprocess.TimeoutExpired("gcloud", 300)
        return [build]

    monkeypatch.setattr(control, "cloud", cloud)
    with pytest.raises(subprocess.TimeoutExpired):
        control.operate(tmp_path / "build", state, submit=True)
    assert json.loads(state.read_text())["status"] == "SUBMISSION_PENDING"
    assert control.operate(tmp_path / "build", state, submit=True)["id"] == "build-1"
    assert sum(call[:2] == ("builds", "submit") for call in calls) == 1


@pytest.mark.parametrize("matches", [[], [{"id": "one"}, {"id": "two"}]])
def test_ambiguous_submission_never_retries(
    control, request_data, tmp_path, monkeypatch, matches
):
    monkeypatch.setattr(control, "matching", lambda *args: True)
    monkeypatch.setattr(control, "cloud", lambda *args: matches)
    with pytest.raises(RuntimeError, match="no automatic resubmission"):
        control.reconcile(request_data, {}, tmp_path / "state.json")


def test_mismatched_existing_state_cannot_launch(
    control, request_data, tmp_path, monkeypatch
):
    monkeypatch.setattr(control, "validate_inputs", lambda _: request_data)
    state = tmp_path / "state.json"
    control.save(state, {"request_fingerprint": "different"})
    with pytest.raises(ValueError, match="another prepared build"):
        control.operate(tmp_path / "build", state, submit=True)


def test_remote_identity_mismatch_preserves_state_without_resubmitting(
    control, request_data, tmp_path, monkeypatch
):
    monkeypatch.setattr(control, "validate_inputs", lambda _: request_data)
    state_file = tmp_path / "state.json"
    original = {
        "request_fingerprint": request_data["request_fingerprint"],
        "build_id": "build-1",
        "status": "WORKING",
    }
    control.save(state_file, original)
    calls = []

    def cloud(*args):
        calls.append(args)
        return {
            "id": "build-1",
            "status": "SUCCESS",
            "substitutions": {"_REQUEST_FINGERPRINT": "e" * 64},
        }

    monkeypatch.setattr(control, "cloud", cloud)
    with pytest.raises(RuntimeError, match="Build identity does not match"):
        control.operate(tmp_path / "build", state_file, submit=True)

    assert calls == [
        ("builds", "describe", "build-1", "--project=test", "--region=us-central1")
    ]
    assert control.read(state_file) == original
    assert control.read(tmp_path / "build.submission.json") == original


def test_experiment_caps_attempts_and_requires_same_source(
    control, request_data, tmp_path
):
    experiment = tmp_path / "experiment.json"
    control.reserve(experiment, "performance", request_data, tmp_path / "p.json")
    with pytest.raises(RuntimeError, match="already consumed"):
        control.reserve(experiment, "performance", request_data, tmp_path / "p2.json")
    changed = dict(
        request_data,
        build_profile="economy",
        commit_sha="c" * 40,
        request_fingerprint="e" * 64,
    )
    with pytest.raises(ValueError, match="same SHA"):
        control.reserve(experiment, "economy", changed, tmp_path / "e.json")
    assert set(control.read(experiment)) == {"performance"}


def test_changing_state_file_cannot_duplicate_submission(
    control, request_data, tmp_path, monkeypatch
):
    monkeypatch.setattr(control, "validate_inputs", lambda _: request_data)
    calls = []
    build = {
        "id": "build-1",
        "status": "WORKING",
        "substitutions": {"_REQUEST_FINGERPRINT": request_data["request_fingerprint"]},
    }

    def cloud(*args):
        calls.append(args)
        return build

    monkeypatch.setattr(control, "cloud", cloud)
    for filename in ("first.json", "second.json"):
        assert (
            control.operate(tmp_path / "build", tmp_path / filename, submit=True)["id"]
            == "build-1"
        )
    assert sum(call[:2] == ("builds", "submit") for call in calls) == 1


def test_confirmation_cannot_reuse_prepared_attempt(control, request_data, tmp_path):
    experiment = tmp_path / "experiment.json"
    control.reserve(experiment, "performance", request_data, tmp_path / "p.json")
    with pytest.raises(RuntimeError, match="fresh preparation"):
        control.reserve(experiment, "confirmation", request_data, tmp_path / "c.json")


def test_prepared_source_rejects_extra_files(control, tmp_path):
    import hashlib

    (tmp_path / "source.bundle").write_bytes(b"source")
    (tmp_path / "input-manifest.sha256").write_text(
        hashlib.sha256(b"source").hexdigest() + "  source.bundle\n"
    )
    (tmp_path / "unexpected").write_text("unreviewed")
    with pytest.raises(ValueError, match="added or removed"):
        control.validate_inputs(tmp_path)


@pytest.mark.parametrize(
    "scenario,rejection",
    [
        ("valid", None),
        ("tampered_hash", "hash mismatch"),
        ("wrong_sha", "Report does not match the prepared release"),
        ("wrong_repository", "Report does not match the prepared release"),
        ("failed_policy", "Quality policy rejected report"),
    ],
)
def test_register_reuses_exact_report_and_rejects_tampering(
    control, request_data, tmp_path, monkeypatch, scenario, rejection
):
    import hashlib
    import io
    from types import SimpleNamespace

    request_data.update(
        repository="test/api",
        service_name="test-api",
        report_uri="gs://test/report",
        summary_uri="gs://test/summary",
    )
    report = {
        **{
            key: request_data[key]
            for key in ("repository", "service_name", "commit_sha", "base_sha")
        },
        "profile": "python",
        "policy_version": "oss-v2",
        "coverage": 90,
        "coverage_threshold": 70,
        "changed_lines": 0,
        "covered_changed_lines": 0,
        "differential_coverage": None,
        "differential_threshold": 80,
        "generated_at": "2026-01-01T00:00:00Z",
        "checks": [
            {"name": name, "category": name, "status": "PASSED"}
            for name in (
                "setup",
                "tests",
                "lint",
                "format",
                "typecheck",
                "sast",
                "dependencies",
                "secrets",
                "misconfiguration",
            )
        ]
        + [
            {
                "name": "differential",
                "category": "differential_coverage",
                "status": "SKIPPED",
            }
        ],
    }
    if scenario == "wrong_sha":
        report["commit_sha"] = "c" * 40
    elif scenario == "wrong_repository":
        report["repository"] = "other/api"
    elif scenario == "failed_policy":
        report["checks"][0]["status"] = "FAILED"
    # Identity and policy failures retain a correct hash and successful summary:
    # the controller must independently reject their otherwise intact reports.
    report_bytes = json.dumps(report).encode()
    summary = {
        "passed": True,
        "build_id": "b1",
        "request_fingerprint": request_data["request_fingerprint"],
        "quality_report_sha256": "wrong"
        if scenario == "tampered_hash"
        else hashlib.sha256(report_bytes).hexdigest(),
    }
    responses = {
        "gs://test/report": report_bytes,
        "gs://test/summary": json.dumps(summary).encode(),
    }
    monkeypatch.setattr(control, "validate_inputs", lambda _: request_data)
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(stdout=responses[args[-1]]),
    )
    monkeypatch.setenv("QUALITY_API_TOKEN", "test-token")
    control.save(
        tmp_path / "catalog-service.json",
        dict(
            service_name="test-api",
            repository="test/api",
            owner="test",
            project_id="test",
            region="test",
            quality=dict(enabled=True, profile="python", coverage_threshold=70),
        ),
    )
    calls = []

    def publish(message, timeout):
        calls.append(message)
        return io.BytesIO(
            json.dumps({**report, "quality_gate_status": "PASSED"}).encode()
        )

    monkeypatch.setattr(control.urllib.request, "urlopen", publish)
    build = {
        "id": "b1",
        "status": "SUCCESS",
        "logUrl": "https://console.cloud.google.com/build/test",
        "substitutions": {"_REQUEST_FINGERPRINT": request_data["request_fingerprint"]},
    }
    if rejection:
        with pytest.raises(ValueError, match=rejection):
            control.register_quality(tmp_path, build, "https://platform.test")
        assert not calls
    else:
        assert (
            control.register_quality(tmp_path, build, "https://platform.test")[
                "quality_gate_status"
            ]
            == "PASSED"
        )
        assert len(calls) == 1
        assert json.loads(calls[0].data)["commit_sha"] == request_data["commit_sha"]
