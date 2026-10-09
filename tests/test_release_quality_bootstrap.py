"""Local bootstrap tests: all Google/GitHub transport is a recording double."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from eng_platform_api.models import (
    CatalogService,
    QualityCheck,
    QualityReportCreate,
    ReleaseExecutionEvent,
    ServiceQualityConfig,
)
from eng_platform_api.services import release_cloud_build as transport
from eng_platform_api.services import release_executions as store
from eng_platform_api.services import release_quality_bootstrap as bootstrap
from eng_platform_api.services import release_reconciler
from eng_platform_api.routers import release_execution_events as events

IMAGE = "quality-python@sha256:" + "c" * 64
REPOSITORY_RESOURCE = (
    "projects/test/locations/us-central1/connections/github/repositories/artemis"
)
KEY = "reviewed-bootstrap-key-172"
_OBSERVED_METADATA = (
    Path(__file__).parent
    / "fixtures/cloud_build/canonical_artemis_response_metadata.json"
)


class Response:
    def __init__(self, payload=None, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def configured(monkeypatch):
    service = CatalogService(
        service_name=bootstrap.SERVICE_NAME,
        repository=bootstrap.REPOSITORY,
        owner="platform",
        project_id="test",
        region="us-central1",
        quality=ServiceQualityConfig(
            enabled=True, profile="python", coverage_threshold=70
        ),
    )
    # The catalog profile has the canonical Artemis threshold (70).
    monkeypatch.setattr(bootstrap.catalog, "get_service", lambda _: service)
    settings = bootstrap.config.release_orchestrator
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "enabled_services", (service.service_name,))
    monkeypatch.setattr(settings, "quality_python_image", IMAGE)
    monkeypatch.setattr(settings, "release_planner_image", "planner@sha256:" + "d" * 64)
    monkeypatch.setattr(settings, "postgres_image", "postgres@sha256:" + "e" * 64)
    monkeypatch.setattr(
        settings,
        "service_account",
        "projects/test/serviceAccounts/quality@test.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(settings, "build_minute_price_usd", 0.006)
    monkeypatch.setattr(bootstrap.config.cloud_build, "project_id", "test")
    monkeypatch.setattr(
        bootstrap.config.cloud_build,
        "repositories",
        {service.service_name: REPOSITORY_RESOURCE},
    )
    monkeypatch.setattr(
        bootstrap,
        "_github_conditions",
        Mock(
            return_value={
                "workflow_id": 123,
                "workflow_path": ".github/workflows/eng-platform-quality.yml",
            }
        ),
    )
    profile = bootstrap.profile_for(service)
    policy_hash = bootstrap.canonical_hash(
        {
            "policy_version": service.release_policy,
            "profile": service.quality.profile,
            "coverage_threshold": service.quality.coverage_threshold,
            "differential_threshold": service.quality.differential_threshold,
        }
    )
    execution = {
        "repository": service.repository,
        "service_name": service.service_name,
        "operation": "pr_quality",
        "head_sha": bootstrap.APPROVED_HEAD_SHA,
        "base_sha": bootstrap.APPROVED_BASE_SHA,
        "profile_hash": profile.fingerprint(),
        "executor_digest": IMAGE,
        "policy_hash": policy_hash,
        "planner_hash": "",
        "provider": "github_actions",
        "status": "waiting_github",
        "pull_request_number": 172,
    }
    fingerprint = store.fingerprint(
        **{
            key: execution[key]
            for key in (
                "repository",
                "service_name",
                "operation",
                "head_sha",
                "base_sha",
                "profile_hash",
                "executor_digest",
                "policy_hash",
            )
        }
    )
    execution.update(execution_id=fingerprint, fingerprint=fingerprint)
    store._memory[fingerprint] = deepcopy(execution)
    session = Mock()

    def successful_post(_url, *, json, **_kwargs):
        return Response(
            {
                "metadata": {
                    "build": {
                        **deepcopy(json),
                        "id": "build-one",
                        "status": "QUEUED",
                        "logUrl": "https://console.cloud.google.com/cloud-build/builds/build-one?project=test",
                    }
                }
            }
        )

    session.post.side_effect = successful_post
    monkeypatch.setattr(transport, "_bootstrap_session", lambda: session)
    return SimpleNamespace(service=service, execution=execution, session=session)


def request(context, *, key=KEY, reauthorize=lambda: None):
    return bootstrap.request_quality_bootstrap(
        context.execution["execution_id"],
        idempotency_key=key,
        actor="diegomad14",
        reauthorize=reauthorize,
    )


def consumed(context, monkeypatch):
    context.session.post.side_effect = TimeoutError("private request nonce")
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed"):
        request(context)
    return store.get(context.execution["execution_id"])


def exact_build(execution, *, build_id="recovered"):
    return {
        **deepcopy(execution["bootstrap_build_request"]),
        "id": build_id,
        "status": "SUCCESS",
    }


def test_single_post_and_repeated_key_only_public_status(configured):
    result = request(configured)
    assert result["status"] == "running_quality"
    assert result["build_id"] == "build-one"
    assert set(result) == {
        "execution_id",
        "status",
        "provider",
        "build_id",
        "provider_run_id",
        "logs_url",
    }
    assert request(configured) == result
    assert configured.session.post.call_count == 1
    kwargs = configured.session.post.call_args.kwargs
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 30
    assert kwargs["json"]["timeout"] == "3600s"
    assert kwargs["json"]["options"] == {
        "logging": "CLOUD_LOGGING_ONLY",
        "substitutionOption": "ALLOW_LOOSE",
    }
    ticket = store.get_quality_bootstrap_ticket()
    assert ticket["max_compute_usd"] == "0.36"
    assert KEY not in str(ticket)
    assert "dispatch_token" not in ticket


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/repo"),
        ("service_name", "other"),
        ("pull_request_number", 173),
        ("head_sha", "a" * 40),
        ("base_sha", "b" * 40),
        ("fingerprint", "f" * 64),
        ("profile_hash", "f" * 64),
        ("policy_hash", "f" * 64),
        ("executor_digest", "other@sha256:" + "e" * 64),
        ("status", "failed"),
        ("provider", "cloud_build"),
    ],
)
def test_identity_and_terminal_drift_rejected_before_consuming(
    configured, field, value
):
    store._memory[configured.execution["execution_id"]][field] = value
    with pytest.raises(bootstrap.QualityBootstrapError):
        request(configured)
    assert store.get_quality_bootstrap_ticket() is None
    configured.session.post.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_run_id", "100"),
        ("event_token_hash", "hash"),
        ("pending_report_hash", "hash"),
        ("source_token_issued_at", "now"),
        ("event_sequence", 1),
        ("build_id", "prior"),
    ],
)
def test_admitted_evidence_or_tokens_cannot_be_reopened(configured, field, value):
    store._memory[configured.execution["execution_id"]][field] = value
    with pytest.raises(ValueError, match="fresh"):
        request(configured)
    assert store.get_quality_bootstrap_ticket() is None
    configured.session.post.assert_not_called()


def test_different_keys_have_one_global_winner(configured):
    def try_request(index):
        try:
            return request(configured, key=f"unique-reviewed-key-{index}")
        except (bootstrap.QualityBootstrapError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_request, range(8)))
    assert sum(result is not None for result in results) == 1
    assert configured.session.post.call_count == 1


@pytest.mark.parametrize(
    "authorization_number,consumes",
    [(1, False), (2, False), (3, True), (4, True), (5, True)],
)
def test_live_revocation_never_posts(configured, authorization_number, consumes):
    counter = 0

    def authorize():
        nonlocal counter
        counter += 1
        return counter != authorization_number

    with pytest.raises(bootstrap.QualityBootstrapError):
        request(configured, reauthorize=authorize)
    assert (store.get_quality_bootstrap_ticket() is not None) == consumes
    configured.session.post.assert_not_called()


def test_failed_reservation_never_posts(configured, monkeypatch):
    monkeypatch.setattr(
        store,
        "reserve_quality_bootstrap",
        Mock(side_effect=RuntimeError("persistent store unavailable")),
    )
    with pytest.raises(RuntimeError):
        request(configured)
    configured.session.post.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        Response(status_code=401),
        Response(status_code=302),
        Response(status_code=500),
        Response(ValueError("private payload")),
        Response({"metadata": {"build": {}}}),
        Response(["unexpected"]),
    ],
)
def test_transport_failures_are_consumed_without_replay(configured, response):
    configured.session.post.side_effect = None
    configured.session.post.return_value = response
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed") as error:
        request(configured)
    assert "private" not in str(error.value)
    assert request(configured)["status"] == "unknown"
    assert configured.session.post.call_count == 1
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed"):
        request(configured, key="different-reviewed-key")
    assert configured.session.post.call_count == 1


def test_timeout_and_failed_binding_persistence_are_not_replayed(
    configured, monkeypatch
):
    original = store.update_quality_bootstrap

    def failing_bind(execution_id, *, attempt_nonce, changes):
        if "build_id" in changes:
            raise RuntimeError("private persistence failure")
        return original(execution_id, attempt_nonce=attempt_nonce, changes=changes)

    monkeypatch.setattr(store, "update_quality_bootstrap", failing_bind)
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed"):
        request(configured)
    assert request(configured)["status"] == "unknown"
    assert configured.session.post.call_count == 1


def test_bound_commit_then_lost_persistence_response_recovers_read_only(
    configured, monkeypatch
):
    original = store.update_quality_bootstrap
    build = {}

    def committed_then_lost(execution_id, *, attempt_nonce, changes):
        result = original(execution_id, attempt_nonce=attempt_nonce, changes=changes)
        if "build_id" in changes:
            build.update(exact_build(result, build_id=changes["build_id"]))
            raise TimeoutError("lost durable response")
        return result

    monkeypatch.setattr(store, "update_quality_bootstrap", committed_then_lost)
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed"):
        request(configured)
    latest = store.get(configured.execution["execution_id"])
    assert latest["status"] == "unknown" and latest["build_id"] == "build-one"
    monkeypatch.setattr(store, "update_quality_bootstrap", original)
    monkeypatch.setattr(transport, "get_build", lambda _: build)
    result = transport.reconcile_quality_bootstrap(latest)
    assert result["status"] == "running_quality"
    assert configured.session.post.call_count == 1


def test_recovery_paginates_and_binds_exactly_one_build(configured, monkeypatch):
    execution = consumed(configured, monkeypatch)
    session = Mock()
    session.get.side_effect = [
        Response({"builds": [], "nextPageToken": "second"}),
        Response({"builds": [exact_build(execution)]}),
    ]
    monkeypatch.setattr(transport, "_session", lambda: session)
    result = transport.reconcile_quality_bootstrap(execution)
    assert result["build_id"] == "recovered"
    assert result["bootstrap_build_verified"] is True
    assert session.get.call_args.kwargs["params"]["pageToken"] == "second"
    assert configured.session.post.call_count == 1


def test_ambiguous_recovery_never_binds_or_posts(configured, monkeypatch):
    execution = consumed(configured, monkeypatch)
    session = Mock()
    session.get.return_value = Response(
        {"builds": [exact_build(execution), exact_build(execution, build_id="second")]}
    )
    monkeypatch.setattr(transport, "_session", lambda: session)
    with pytest.raises(transport.ReleaseCloudBuildError, match="ambiguous"):
        transport.reconcile_quality_bootstrap(execution)
    assert not store.get(execution["execution_id"]).get("build_id")
    assert configured.session.post.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda build: build["source"]["connectedRepository"].update(
            repository="different"
        ),
        lambda build: build["source"]["connectedRepository"].update(revision="a" * 40),
        lambda build: build.update(serviceAccount="other@test"),
        lambda build: build.update(timeout="1800s"),
        lambda build: build["options"].update(machineType="E2_STANDARD_2"),
        lambda build: build["options"].update(pool={"name": "unapproved-pool"}),
        lambda build: build["options"].update(workerPool="custom"),
        lambda build: build["options"].update(diskSizeGb=100),
        lambda build: build["options"].update(logging="LEGACY"),
        lambda build: build["steps"][0].update(name="unapproved:image"),
        lambda build: build["steps"][0]["args"].append("unapproved"),
        lambda build: build["substitutions"].update(_PROFILE_SHA256="f" * 64),
        lambda build: build.update(images=["unexpected"]),
        lambda build: build.update(availableSecrets={"secretManager": []}),
    ],
)
def test_changed_recovery_request_is_rejected(configured, monkeypatch, change):
    execution = consumed(configured, monkeypatch)
    build = exact_build(execution)
    change(build)
    session = Mock()
    session.get.return_value = Response({"builds": [build]})
    monkeypatch.setattr(transport, "_session", lambda: session)
    with pytest.raises((transport.ReleaseCloudBuildError, ValueError)):
        transport.reconcile_quality_bootstrap(execution)
    assert not store.get(execution["execution_id"]).get("build_id")


def test_absent_recovery_does_not_reset_or_resubmit(configured, monkeypatch):
    execution = consumed(configured, monkeypatch)
    session = Mock()
    session.get.return_value = Response({"builds": []})
    monkeypatch.setattr(transport, "_session", lambda: session)
    reset = Mock()
    monkeypatch.setattr(store, "reconcile_submission_absent", reset)
    assert (
        release_reconciler.reconcile(execution["execution_id"])["status"] == "unknown"
    )
    reset.assert_not_called()
    assert configured.session.post.call_count == 1
    with pytest.raises(transport.ReleaseCloudBuildError, match="cannot be retried"):
        transport.submit(execution["execution_id"], configured.service)


def test_dispatch_token_is_one_use_and_cannot_be_recovered_from_state(
    configured, monkeypatch
):
    token = "private-winning-dispatch-token"
    monkeypatch.setattr(
        bootstrap.secrets, "token_hex", Mock(side_effect=["nonce", token])
    )
    execution = consumed(configured, monkeypatch)
    execution["status"] = "submitting"
    with pytest.raises(transport.ReleaseCloudBuildError, match="already consumed"):
        transport.submit_quality_bootstrap(
            execution,
            request=execution["bootstrap_build_request"],
            dispatch_token=token,
            reauthorize=lambda: None,
        )
    assert configured.session.post.call_count == 1
    assert token not in str(store.get_quality_bootstrap_ticket())


def test_bootstrap_session_disables_all_refresh_and_transport_retries(monkeypatch):
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(transport, "default", lambda **_: ("credential", "test"))
    monkeypatch.setattr(transport, "AuthorizedSession", constructor)
    session = transport._bootstrap_session()
    constructor.assert_called_once_with("credential", max_refresh_attempts=0)
    calls = session.mount.call_args_list
    assert [call.args[0] for call in calls] == ["https://", "http://"]
    assert all(call.args[1].max_retries.total == 0 for call in calls)


def test_workflow_drift_after_reservation_consumes_without_post(
    configured, monkeypatch
):
    guard = Mock(
        side_effect=[{"workflow_id": 123}, bootstrap.QualityBootstrapError("active")]
    )
    monkeypatch.setattr(bootstrap, "_github_conditions", guard)
    with pytest.raises(bootstrap.QualityBootstrapError, match="consumed"):
        request(configured)
    assert store.get_quality_bootstrap_ticket() is not None
    configured.session.post.assert_not_called()


def test_budget_and_empty_allowlist_fail_closed(configured, monkeypatch):
    monkeypatch.setattr(
        bootstrap.config.release_orchestrator, "build_minute_price_usd", 0.00601
    )
    with pytest.raises(bootstrap.QualityBootstrapError, match="budget"):
        request(configured)
    monkeypatch.setattr(bootstrap.config.auth, "allowed_logins", ())
    with pytest.raises(bootstrap.QualityBootstrapError, match="allowlisted"):
        request(configured)
    configured.session.post.assert_not_called()


def test_known_broken_executor_cannot_consume_the_bootstrap(configured, monkeypatch):
    monkeypatch.setattr(
        bootstrap.config.release_orchestrator,
        "quality_python_image",
        "quality-python@sha256:" + bootstrap._BROKEN_EXECUTOR_DIGEST,
    )
    with pytest.raises(bootstrap.QualityBootstrapError, match="corrected executor"):
        request(configured)
    assert store.get_quality_bootstrap_ticket() is None
    configured.session.post.assert_not_called()


def test_incomplete_executor_sha_cannot_consume_the_bootstrap(configured, monkeypatch):
    monkeypatch.setattr(
        bootstrap.config.release_orchestrator,
        "quality_python_image",
        "quality-python@sha256:123",
    )
    with pytest.raises(bootstrap.QualityBootstrapError, match="complete pinned SHA256"):
        request(configured)
    assert store.get_quality_bootstrap_ticket() is None
    configured.session.post.assert_not_called()


@pytest.mark.parametrize("passed", [True, False])
def test_bootstrap_uses_canonical_truthful_pass_fail_evidence(
    configured, monkeypatch, tmp_path, passed
):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_BUCKET", "")
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    execution_id = configured.execution["execution_id"]
    store._memory[execution_id]["check_ids"] = {"quality": 10, "workflows": 11}
    request(configured)
    execution = store.get(execution_id)
    report = QualityReportCreate(
        service_name=bootstrap.SERVICE_NAME,
        repository=bootstrap.REPOSITORY,
        commit_sha=bootstrap.APPROVED_HEAD_SHA,
        base_sha=bootstrap.APPROVED_BASE_SHA,
        branch="reviewed-pr",
        profile="python",
        policy_version="oss-v2",
        generated_at=datetime.now(timezone.utc).isoformat(),
        coverage=90,
        coverage_threshold=70,
        differential_coverage=90,
        differential_threshold=80,
        changed_lines=10,
        covered_changed_lines=9,
        checks=[
            QualityCheck(
                name=name,
                category=name,
                status="FAILED" if name == "sast" and not passed else "PASSED",
            )
            for name in (
                "setup",
                "tests",
                "lint",
                "typecheck",
                "format",
                "sast",
                "dependencies",
                "secrets",
                "misconfiguration",
                "differential_coverage",
            )
        ],
    )
    quality = release_reconciler.quality_store
    report_hash = quality.save_pending_report(execution_id, report)
    store.save(
        execution_id,
        pending_report_hash=report_hash,
        engine_event_status="quality_passed" if passed else "quality_failed",
    )
    build = exact_build(execution, build_id="build-one")
    build["status"] = "SUCCESS" if passed else "FAILURE"
    monkeypatch.setattr(transport, "get_build", lambda _: build)
    checks = Mock()
    monkeypatch.setattr(
        release_reconciler.github_release_control, "upsert_check", checks
    )
    result = release_reconciler.reconcile(execution_id)
    assert result["status"] == ("quality_passed" if passed else "quality_failed")
    assert result["report_hash"] == report_hash
    assert all(
        call.kwargs["conclusion"] == ("success" if passed else "failure")
        for call in checks.call_args_list
    )
    assert len(checks.call_args_list) == 2
    assert configured.session.post.call_count == 1
    if not passed:
        assert "Required check not passed: sast" in result["quality_errors"]
        assert release_reconciler.reconcile(execution_id)["status"] == "quality_failed"


def test_public_status_removes_unapproved_query_and_sensitive_url(configured):
    state = configured.execution | {
        "logs_url": "https://console.cloud.google.com/cloud-build/builds/build-one?project=test&nonce=private&token=secret",
    }
    assert (
        bootstrap.public_status(state)["logs_url"]
        == "https://console.cloud.google.com/cloud-build/builds/build-one?project=test"
    )
    for url in (
        "https://console.cloud.google.com/credentials/secret?project=test",
        "https://secret@console.cloud.google.com/cloud-build/builds/build-one?project=test",
        "https://other.example/cloud-build/builds/build-one?project=test",
    ):
        assert bootstrap.public_status(state | {"logs_url": url})["logs_url"] == ""


def _observed_response_metadata():
    return json.loads(_OBSERVED_METADATA.read_text(encoding="utf-8"))["response_fields"]


def test_observed_canonical_response_fixture_normalizes_only_known_defaults():
    observed = _observed_response_metadata()
    original = deepcopy(observed)
    normalized = transport._normalize_quality_bootstrap_response(observed)
    assert observed == original
    assert normalized == {
        key: (
            {name: value for name, value in fields.items() if name != "pool"}
            if key == "options"
            else fields
        )
        for key, fields in observed.items()
        if key != "queueTtl"
    }
    transport.validate_submission(normalized)
    assert (
        normalized["source"]["connectedRepository"]["revision"]
        == "d5c38323cd4cf0c28f7a35657ab1b3d9c8d5a01f"
    )


def test_submission_accepts_observed_response_defaults_but_never_requests_them(
    configured,
):
    metadata = _observed_response_metadata()

    def observed_post(_url, *, json, **_kwargs):
        # Reuse only the observed provider metadata. The approved request's
        # full current source, account, steps, images and nonce stay exact.
        return Response(
            {
                "metadata": {
                    "build": {
                        **deepcopy(json),
                        "id": metadata["id"],
                        "status": "QUEUED",
                        "options": metadata["options"],
                        "queueTtl": metadata["queueTtl"],
                        "sourceProvenance": metadata["sourceProvenance"],
                    }
                }
            }
        )

    configured.session.post.side_effect = observed_post
    result = request(configured)
    assert result["status"] == "running_quality"
    assert result["build_id"] == metadata["id"]
    sent = configured.session.post.call_args.kwargs["json"]
    assert "pool" not in sent["options"]
    assert "queueTtl" not in sent
    assert sent["timeout"] == "3600s"


def test_read_only_recovery_accepts_observed_response_defaults(configured, monkeypatch):
    execution = consumed(configured, monkeypatch)
    metadata = _observed_response_metadata()
    build = exact_build(execution, build_id=metadata["id"])
    build.update(
        options=metadata["options"],
        queueTtl=metadata["queueTtl"],
        sourceProvenance=metadata["sourceProvenance"],
    )
    session = Mock()
    session.get.return_value = Response({"builds": [build]})
    monkeypatch.setattr(transport, "_session", lambda: session)
    assert (
        transport.reconcile_quality_bootstrap(execution)["build_id"] == metadata["id"]
    )
    assert configured.session.post.call_count == 1


@pytest.mark.parametrize("queue_ttl", [None, 3600, "3599s", "7200s", {}, True])
def test_response_queue_ttl_accepts_only_observed_literal(
    configured, monkeypatch, queue_ttl
):
    execution = consumed(configured, monkeypatch)
    build = exact_build(execution)
    build.update(queueTtl=queue_ttl)
    build["options"]["pool"] = {}
    with pytest.raises(transport.ReleaseCloudBuildError, match="queue TTL"):
        transport.verify_quality_bootstrap_build(execution, build)


@pytest.mark.parametrize(
    "pool", [None, "", [], {"name": "custom"}, {"workerPool": "custom"}]
)
def test_response_pool_accepts_only_empty_object(configured, monkeypatch, pool):
    execution = consumed(configured, monkeypatch)
    build = exact_build(execution)
    build["options"]["pool"] = pool
    build["queueTtl"] = "3600s"
    with pytest.raises(ValueError, match="default machine"):
        transport.verify_quality_bootstrap_build(execution, build)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["options"].update(pool={}),
        lambda value: value["options"].update(pool={"name": "custom"}),
        lambda value: value.update(queueTtl="3600s"),
        lambda value: value.update(queueTtl="7200s"),
        lambda value: value.update(queueTtl=None),
    ],
)
def test_request_cannot_include_pool_or_queue_ttl_even_if_matching_response_defaults(
    configured, mutation
):
    execution = deepcopy(configured.execution)
    execution.update(
        status="submitting",
        provider="cloud_build",
        bootstrap_ticket_id=bootstrap.POLICY_ID,
        bootstrap_nonce="private-test-nonce",
    )
    payload = transport.build_request(execution, configured.service)
    payload["substitutions"]["_BOOTSTRAP_NONCE"] = execution["bootstrap_nonce"]
    mutation(payload)
    execution.update(
        bootstrap_build_request=payload,
        bootstrap_build_request_hash=transport._request_hash(payload),
    )
    with pytest.raises((ValueError, transport.ReleaseCloudBuildError)):
        transport.submit_quality_bootstrap(
            execution,
            request=payload,
            dispatch_token="private-test-dispatch",
            reauthorize=lambda: None,
        )
    configured.session.post.assert_not_called()
    with pytest.raises((ValueError, transport.ReleaseCloudBuildError)):
        transport.verify_quality_bootstrap_build(execution, exact_build(execution))


@pytest.mark.parametrize(
    "setting,value",
    [
        ("machineType", "E2_STANDARD_2"),
        ("diskSizeGb", 100),
        ("workerPool", "custom"),
    ],
)
def test_observed_defaults_do_not_mask_other_response_machine_overrides(
    configured, monkeypatch, setting, value
):
    execution = consumed(configured, monkeypatch)
    build = exact_build(execution)
    build["options"].update(pool={}, **{setting: value})
    build["queueTtl"] = "3600s"
    with pytest.raises(ValueError, match="default machine"):
        transport.verify_quality_bootstrap_build(execution, build)


@pytest.mark.parametrize("bound_field", ["build_id", "provider_run_id"])
def test_verifier_and_binding_reject_mismatch_with_any_linked_build_id(
    configured, monkeypatch, bound_field
):
    execution = consumed(configured, monkeypatch)
    execution[bound_field] = "requested-build"
    build = exact_build(execution, build_id="different-build")
    update = Mock()
    monkeypatch.setattr(store, "update_quality_bootstrap", update)
    with pytest.raises(transport.ReleaseCloudBuildError, match="build ID"):
        transport.verify_quality_bootstrap_build(execution, build)
    with pytest.raises(transport.ReleaseCloudBuildError, match="build ID"):
        transport._bind_quality_bootstrap(execution, build)
    update.assert_not_called()


def test_verifier_rejects_mismatch_with_requested_unbound_build_id(
    configured, monkeypatch
):
    execution = consumed(configured, monkeypatch)
    with pytest.raises(transport.ReleaseCloudBuildError, match="build ID"):
        transport.verify_quality_bootstrap_build(
            execution, exact_build(execution), expected_build_id="requested-build"
        )


def test_bound_recovery_rejects_get_response_with_a_different_id(
    configured, monkeypatch
):
    request(configured)
    execution = store.get(configured.execution["execution_id"])
    store.update_quality_bootstrap(
        execution["execution_id"],
        attempt_nonce=execution["bootstrap_nonce"],
        changes={"status": "unknown"},
    )
    execution = store.get(execution["execution_id"])
    get = Mock(return_value=exact_build(execution, build_id="different-build"))
    monkeypatch.setattr(transport, "get_build", get)
    with pytest.raises(transport.ReleaseCloudBuildError, match="bound build identity"):
        transport.reconcile_quality_bootstrap(execution)
    get.assert_called_once_with("build-one")
    assert store.get(execution["execution_id"])["build_id"] == "build-one"


def test_unbound_recovery_rejects_list_result_with_different_requested_id_before_binding(
    configured, monkeypatch
):
    execution = consumed(configured, monkeypatch)
    session = Mock()
    session.get.return_value = Response(
        {"builds": [exact_build(execution, build_id="different-build")]}
    )
    monkeypatch.setattr(transport, "_session", lambda: session)
    with pytest.raises(transport.ReleaseCloudBuildError, match="build ID"):
        transport.reconcile_quality_bootstrap(
            execution, expected_build_id="requested-build"
        )
    assert not store.get(execution["execution_id"]).get("build_id")


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("callback", ["source", "event", "evidence"])
def test_callback_different_get_id_cannot_bind_issue_tokens_or_stage_evidence(
    configured, monkeypatch, bound, callback
):
    if bound:
        request(configured)
        execution = store.get(configured.execution["execution_id"])
        requested_id = "build-one"
    else:
        execution = consumed(configured, monkeypatch)
        requested_id = "requested-build"
    monkeypatch.setattr(events, "_verify_google", lambda _: None)
    monkeypatch.setattr(
        transport,
        "get_build",
        Mock(return_value=exact_build(execution, build_id="different-build")),
    )
    session = Mock()
    monkeypatch.setattr(transport, "_session", lambda: session)
    mint = Mock()
    monkeypatch.setattr(events.github_release_control, "installation_read_token", mint)
    stage_report = Mock()
    monkeypatch.setattr(events, "_verify_report", stage_report)
    with pytest.raises(HTTPException) as error:
        if callback == "evidence":
            events._accept_event(
                execution["execution_id"],
                ReleaseExecutionEvent(
                    execution_id=execution["execution_id"],
                    fingerprint=execution["fingerprint"],
                    provider_run_id=requested_id,
                    sequence=1,
                    status="running_quality",
                ),
                "Bearer offline-google-identity",
                "offline-event-token",
            )
        else:
            endpoint = (
                events.issue_source_token
                if callback == "source"
                else events.issue_event_token
            )
            endpoint(
                execution["execution_id"],
                events.SourceTokenRequest(
                    fingerprint=execution["fingerprint"],
                    provider_run_id=requested_id,
                ),
                authorization="Bearer offline-google-identity",
            )
    assert error.value.status_code == 403
    session.get.assert_not_called()
    mint.assert_not_called()
    stage_report.assert_not_called()
    current = store.get(execution["execution_id"])
    assert current.get("build_id") == ("build-one" if bound else None)
    assert not current.get("source_token_issued_at")
    assert not current.get("event_token_hash")
    assert not current.get("pending_report_hash")
    assert not current.get("event_sequence")


@pytest.mark.parametrize("callback", ["source", "event"])
def test_unbound_callback_rejects_different_list_id_before_binding_or_tokens(
    configured, monkeypatch, callback
):
    execution = consumed(configured, monkeypatch)
    monkeypatch.setattr(events, "_verify_google", lambda _: None)
    monkeypatch.setattr(
        transport,
        "get_build",
        lambda _: exact_build(execution, build_id="requested-build"),
    )
    session = Mock()
    session.get.return_value = Response(
        {"builds": [exact_build(execution, build_id="different-build")]}
    )
    monkeypatch.setattr(transport, "_session", lambda: session)
    mint = Mock()
    monkeypatch.setattr(events.github_release_control, "installation_read_token", mint)
    endpoint = (
        events.issue_source_token if callback == "source" else events.issue_event_token
    )
    with pytest.raises(HTTPException) as error:
        endpoint(
            execution["execution_id"],
            events.SourceTokenRequest(
                fingerprint=execution["fingerprint"],
                provider_run_id="requested-build",
            ),
            authorization="Bearer offline-google-identity",
        )
    assert error.value.status_code == 403
    mint.assert_not_called()
    current = store.get(execution["execution_id"])
    assert not current.get("build_id")
    assert not current.get("source_token_issued_at")
    assert not current.get("event_token_hash")
