from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

from eng_platform_api.models import CatalogService, QualityReportCreate
from eng_platform_api.services.quality_policy import policy_errors

spec = importlib.util.spec_from_file_location(
    "differential",
    Path(__file__).parents[1] / "scripts/quality/differential_coverage.py",
)
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


def payload(**overrides):
    checks = [
        {"name": c, "category": c, "status": "PASSED"}
        for c in (
            "setup",
            "tests",
            "format",
            "lint",
            "typecheck",
            "sast",
            "dependencies",
            "secrets",
            "misconfiguration",
            "differential_coverage",
        )
    ]
    return QualityReportCreate(
        **{
            "service_name": "test-api",
            "repository": "test/api",
            "commit_sha": "a" * 40,
            "base_sha": "b" * 40,
            "profile": "python",
            "policy_version": "oss-v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coverage": 70,
            "coverage_threshold": 70,
            "differential_coverage": 80,
            "differential_threshold": 80,
            "changed_lines": 5,
            "covered_changed_lines": 4,
            "checks": checks,
            **overrides,
        }
    )


def service():
    return CatalogService(
        service_name="test-api",
        repository="test/api",
        owner="test",
        project_id="test",
        region="test",
        quality={"enabled": True, "profile": "python", "coverage_threshold": 70},
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"coverage": 69.99},
        {"coverage_threshold": 50},
        {"differential_threshold": 79},
        {"covered_changed_lines": 3, "differential_coverage": 60},
        {"covered_changed_lines": 6},
        {"changed_lines": None},
        {"differential_coverage": None},
        {"differential_coverage": 81},
        {"repository": "other/repo"},
        {"profile": "node"},
        {"policy_version": "oss-v1"},
        {"base_sha": ""},
        {"base_sha": "a" * 40},
        {"commit_sha": "a" * 7},
        {"checks": [{"name": "tests", "category": "tests", "status": "PASSED"}]},
        {"generated_at": "not a date"},
        {"generated_at": datetime.now().isoformat()},
        {"generated_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    ],
)
def test_policy_rejects_incomplete_or_weaker_evidence(updates):
    assert policy_errors(payload(**updates), service())


def test_policy_accepts_boundary_and_higher_coverage():
    assert not policy_errors(payload(), service())
    assert not policy_errors(
        payload(covered_changed_lines=5, differential_coverage=100), service()
    )
    assert policy_errors(payload(), None)


def test_no_changed_lines_requires_explicit_not_applicable():
    report = payload(
        changed_lines=0, covered_changed_lines=0, differential_coverage=None
    )
    assert policy_errors(report, service())
    report.checks[-1].status = "SKIPPED"
    assert not policy_errors(report, service())


def test_required_checks_cannot_be_skipped_or_duplicated():
    report = payload()
    report.checks[0].status = "SKIPPED"
    assert policy_errors(report, service())
    report = payload()
    report.checks.append(report.checks[0])
    assert policy_errors(report, service())
    report = payload()
    report.checks[0].blocking_findings = 1
    assert policy_errors(report, service())


def commit(root, message):
    diff.git(root, "add", ".")
    diff.git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )
    return diff.git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    diff.git(tmp_path, "init", "-b", "main")
    (tmp_path / "README.md").write_text("initial\n")
    base = commit(tmp_path, "initial")
    return tmp_path, base


def test_multi_commit_push_new_files_and_restart(repository):
    root, base = repository
    (root / "a.py").write_text("a=1\nb=2\n")
    commit(root, "first")
    (root / "file with space.py").write_text("c=3\n")
    head = commit(root, "second")
    assert diff.resolve_base(root, head, {"before": base}) == base
    assert diff.changed_lines(root, base, head) == {
        "a.py": {1, 2},
        "file with space.py": {1},
    }
    assert diff.resolve_base(root, head, {}, base) == base
    assert (
        diff.resolve_base(root, head, {"pull_request": {"base": {"sha": base}}}) == base
    )
    with pytest.raises(ValueError):
        diff.resolve_base(root, base, {}, base)
    with pytest.raises(ValueError):
        diff.resolve_base(root, head, {})
    with pytest.raises(ValueError):
        diff.resolve_base(root, head, {}, head)


def test_differential_counts_executable_lines_and_rejects_missing_files(repository):
    import json

    root, base = repository
    app = root / "services/app"
    app.mkdir(parents=True)
    (app / "code.py").write_text("a=1\nb=2\n# comment\n")
    (app / ".quality-sources.json").write_text(json.dumps({"roots": ["code.py"]}))
    head = commit(root, "app")
    reports = app / "reports"
    reports.mkdir()
    coverage = reports / "coverage.json"
    coverage.write_text(
        json.dumps(
            {"files": {"code.py": {"executed_lines": [1], "missing_lines": [2]}}}
        )
    )
    result = diff.differential(app, reports, "python", base, head)
    assert result["changed_lines"] == 2
    assert result["covered_changed_lines"] == 1
    assert result["differential_coverage"] == 50
    coverage.write_text(
        json.dumps(
            {"files": {"other.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )
    with pytest.raises(ValueError, match="missing from coverage"):
        diff.differential(app, reports, "python", base, head)


def test_lcov_and_documentation_only(repository):
    import json

    root, base = repository
    (root / ".quality-sources.json").write_text(
        json.dumps({"roots": ["src"], "exclude": ["*.test.ts"]})
    )
    head = commit(root, "configuration")
    reports = root / "reports"
    reports.mkdir()
    p = reports / "lcov.info"
    p.write_text("SF:src/a.ts\nDA:1,0\nDA:2,1\nLF:2\nLH:1\nend_of_record\n")
    assert diff.lcov_coverage(p, root, root) == {"src/a.ts": {1: False, 2: True}}
    result = diff.differential(root, reports, "node", base, head)
    assert result["changed_lines"] == 0 and result["differential_coverage"] is None
    p.write_text("SF:src/a.ts\nDA:1,0\n")
    with pytest.raises(ValueError):
        diff.lcov_coverage(p, root, root)


def test_renames_and_deletions(repository):
    root, _ = repository
    (root / "old.py").write_text("a=1\nb=2\nc=3\nd=4\n")
    base = commit(root, "source")
    diff.git(root, "mv", "old.py", "new.py")
    (root / "README.md").unlink()
    head = commit(root, "rename")
    assert diff.changed_lines(root, base, head) == {"new.py": set()}


def test_exact_release_query_rejects_legacy_and_wrong_repository(monkeypatch):
    from fastapi.testclient import TestClient
    from eng_platform_api.main import app
    from eng_platform_api.models import QualityReport
    from eng_platform_api.routers import quality

    report = QualityReport(
        **payload().model_dump(),
        quality_gate_status="PASSED",
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(quality.catalog, "get_service", lambda _: service())
    monkeypatch.setattr(quality.quality_store, "get_report", lambda *_: report)
    client = TestClient(app)
    endpoint = f"/api/quality/services/test-api/commits/{'a' * 40}"
    assert client.get(endpoint + "?for_release=true").status_code == 200
    report.policy_version = "oss-v1"
    assert client.get(endpoint).status_code == 200
    assert client.get(endpoint + "?for_release=true").status_code == 409
    report.policy_version = "oss-v2"
    report.repository = "other/repo"
    assert client.get(endpoint + "?for_release=true").status_code == 409


def test_rollback_uses_original_evidence_and_requires_prior_promotion(monkeypatch):
    from fastapi.testclient import TestClient
    from eng_platform_api.main import app
    from eng_platform_api.models import DeploymentItem, QualityReport
    from eng_platform_api.routers import quality
    from eng_platform_api.services import deployment_store

    original = payload(policy_version="oss-v1", generated_at="2025-01-01T00:00:00Z")
    report = QualityReport(
        **original.model_dump(),
        quality_gate_status="PASSED",
        received_at="2025-01-01T00:00:00Z",
    )
    target = DeploymentItem(
        id="release",
        service_name="test-api",
        repository="test/api",
        tag="v1.0.0",
        sha="a" * 40,
        status="SUCCEEDED",
        production_revision="test-api-00001",
    )
    monkeypatch.setattr(quality.catalog, "get_service", lambda _: service())
    monkeypatch.setattr(deployment_store, "list_for_service", lambda *a, **k: [target])
    monkeypatch.setattr(quality.quality_store, "get_report", lambda *a: report)
    client = TestClient(app)
    endpoint = "/api/quality/services/test-api/rollback-targets/test-api-00001"
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.json()["commit_sha"] == "a" * 40
    assert response.json()["policy_version"] == "oss-v1"
    assert response.json()["image"].endswith(":v1.0.0")
    target.status = "FAILED"
    assert client.get(endpoint).status_code == 409
    target.status = "SUCCEEDED"
    report.quality_gate_status = "FAILED"
    assert client.get(endpoint).status_code == 409
    monkeypatch.setattr(quality.catalog, "get_service", lambda _: None)
    assert client.get(endpoint).status_code == 404


def test_server_recalculates_v2_status_from_catalog(monkeypatch, tmp_path):
    from eng_platform_api.services import catalog, quality_store

    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setattr(catalog, "get_service", lambda _: service())
    assert quality_store.save_report(payload()).quality_gate_status == "PASSED"
    assert (
        quality_store.save_report(payload(coverage_threshold=50)).quality_gate_status
        == "FAILED"
    )


def test_release_verifier_rejects_wrong_sha_failed_old_and_stale_evidence():
    spec = importlib.util.spec_from_file_location(
        "verify_quality",
        Path(__file__).parents[1] / ".github/actions/verify-quality/verify.py",
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    report = payload().model_dump() | {"quality_gate_status": "PASSED"}
    verifier.validate(report, "test-api", "test/api", "a" * 40)
    for override in [
        {"commit_sha": "b" * 40},
        {"repository": "other/repo"},
        {"policy_version": "oss-v1"},
        {"quality_gate_status": "FAILED"},
        {"generated_at": "2025-01-01T00:00:00Z"},
        {"checks": []},
    ]:
        with pytest.raises(ValueError):
            verifier.validate(report | override, "test-api", "test/api", "a" * 40)


def test_factory_emits_complete_oss_gate_and_sources():
    import json
    import yaml
    from eng_platform_api.models import ServiceFactoryRequest
    from eng_platform_api.services.service_factory import generate_plan

    plan = generate_plan(
        ServiceFactoryRequest(
            repository="test/api",
            service_name="test-api",
            service_type="api",
            runtime="python",
            gcp_project="test",
            owner="test",
        )
    )
    assert json.loads(plan.quality_sources)["roots"] == ["src"]
    assert "Require exact OSS quality evidence" in plan.semantic_release_workflow
    assert "service-name: test-api" in plan.semantic_release_workflow
    assert (
        yaml.safe_load(plan.yaml_contract)["quality"]["quality_gate"]["policy_version"]
        == "oss-v2"
    )
    assert plan.sonar_properties == ""


def test_runner_emits_complete_differential_check_for_ci_summary(tmp_path, monkeypatch):
    import json
    import sys

    scripts = Path(__file__).parents[1] / "scripts/quality"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("runner", scripts / "quality_gate.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quality_gate.py",
            "--service-name",
            "test-api",
            "--repository",
            "test/api",
            "--commit-sha",
            "a" * 40,
            "--profile",
            "python",
            "--working-directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "report.json"),
        ],
    )
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *a: {
            "returncode": 0,
            "duration": 0,
            "output": "passed",
            "skipped": False,
        },
    )
    monkeypatch.setattr(runner, "_version", lambda *a: "test-version")
    monkeypatch.setattr(runner, "_coverage", lambda *a: 100)
    monkeypatch.setattr(runner, "resolve_base", lambda *a: "b" * 40)
    monkeypatch.setattr(
        runner,
        "differential",
        lambda *a: {
            "policy_version": "oss-v2",
            "base_sha": "b" * 40,
            "changed_lines": 5,
            "covered_changed_lines": 4,
            "differential_coverage": 80,
            "differential_threshold": 80,
        },
    )
    assert runner.main() == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert all(isinstance(check["findings"], int) for check in report["checks"])
    assert report["checks"][-1]["category"] == "differential_coverage"
    assert not policy_errors(QualityReportCreate(**report), service())
