from eng_platform_api.services import catalog, release_executions
from eng_platform_api.services.repository_identity import (
    aliases,
    repository_id,
    same_repository,
    verify_webhook_identity,
)


def test_artemis_worker_reuses_only_allowlisted_repo_evidence(monkeypatch):
    from eng_platform_api.routers import quality
    from eng_platform_api.services import catalog

    worker = catalog.get_service("cgm-artemis-sync-worker")
    assert worker is not None
    assert worker.quality.evidence_services == ["cgm-artemis-api", "cgm-sanplat-api"]
    requested = []

    class Report:
        service_name = "cgm-sanplat-api"
        quality_gate_status = "PASSED"

        def model_copy(self, **kwargs):
            return self

    def get_report(owner, sha):
        requested.append(owner)
        return Report() if owner == "cgm-sanplat-api" else None

    monkeypatch.setattr(quality.quality_store, "get_report", get_report)
    monkeypatch.setattr(quality, "policy_errors", lambda report, service: [])
    monkeypatch.setattr(quality, "_is_stale", lambda report: False)
    report = quality.get_quality_report(worker.service_name, "a" * 40, for_release=True)
    assert report.service_name == "cgm-sanplat-api"
    assert requested == [worker.service_name, "cgm-artemis-api", "cgm-sanplat-api"]


def test_rename_keeps_one_quality_execution_per_repo(monkeypatch):
    from eng_platform_api.services import quality_profiles, release_orchestrator

    old = catalog.get_service("cgm-sanplat-api")
    new = catalog.get_service("cgm-artemis-api")
    assert (
        quality_profiles.profile_for(old).fingerprint()
        == quality_profiles.profile_for(new).fingerprint()
    )
    monkeypatch.setattr(release_orchestrator, "_service_enabled", lambda service: True)
    chosen = release_orchestrator._services("diegomad14/cgm-artemis-api")
    assert [service.service_name for service in chosen] == ["cgm-artemis-api"]


def test_rename_aliases_keep_stable_github_identity():
    old = "diegomad14/cgm-sanplat-api"
    new = "diegomad14/cgm-artemis-api"
    assert repository_id(old) == repository_id(new) == 1306114845
    assert aliases(new) == (old, new)
    assert same_repository(old, new)
    assert not same_repository(old, "diegomad14/cgm-artemis-web")
    assert verify_webhook_identity(new, 1306114845)
    assert not verify_webhook_identity(new, 1306114872)
    assert not verify_webhook_identity(new, "1306114845")


def test_catalog_finds_old_service_by_new_repository_name():
    services = catalog.get_services_by_repository("diegomad14/cgm-artemis-api")
    assert any(service.service_name == "cgm-sanplat-api" for service in services)


def test_historical_execution_can_be_found_after_rename(monkeypatch):
    old = "diegomad14/cgm-sanplat-web"
    new = "diegomad14/cgm-artemis-web"
    sha = "a" * 40
    monkeypatch.setattr(release_executions, "_collection", lambda: None)
    monkeypatch.setattr(
        release_executions,
        "_memory",
        {
            "historical": {
                "repository": old,
                "head_sha": sha,
                "operation": "main_release",
                "status": "released",
                "created_at": "2026-09-22T00:00:00Z",
            }
        },
    )
    assert release_executions.find(new, sha, "main_release")["repository"] == old
