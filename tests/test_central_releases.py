"""Central release controls, durable result retries and immutable runtime evidence."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from eng_platform_api.models import DeploymentItem, ReleaseTag
from eng_platform_api.services import central_releases as central, release_plan as plans


def selected_plan():
    return {
        "service_name": "service",
        "repository": "owner/service",
        "kind": "deploy",
        "project_id": "project",
        "region": "region",
        "artifact_repository": "images",
        "image_name": "service",
        "configuration": {
            "generation": 1,
            "secrets": {
                "WM_PASSWORD": "projects/project/secrets/wm-password/versions/2",
            },
        },
        "auxiliary_services": ["worker"],
        "auxiliary_jobs": ["job"],
    }


def result():
    image = "region-docker.pkg.dev/project/images/service@sha256:" + "a" * 64
    runtime = {
        "image": image,
        "secrets": {"WM_PASSWORD": "wm-password:2"},
        "traffic": {},
    }
    return {
        "status": "SUCCEEDED",
        "image_digest": image,
        "production_revision": "service-r123",
        "runtime_snapshot": {
            "services": {
                name: {**deepcopy(runtime), "traffic": {name + "-r123": 100}}
                for name in ("service", "worker")
            },
            "jobs": {"job": runtime},
        },
    }


def test_numeric_configuration_and_all_runtimes_are_accepted():
    central.validate_result(selected_plan(), result())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(image_digest="untrusted@sha256:" + "a" * 64),
        lambda data: data.update(production_revision="other-r123"),
        lambda data: data["runtime_snapshot"]["services"].pop("worker"),
        lambda data: data["runtime_snapshot"]["services"]["service"]["secrets"].update(
            WM_PASSWORD="wm-password:latest"
        ),
        lambda data: data["runtime_snapshot"]["services"]["service"]["secrets"].update(
            WM_PASSWORD="wm-password:3"
        ),
        lambda data: data["runtime_snapshot"]["services"]["service"].update(
            traffic={"service-r123": 50}
        ),
        lambda data: data["runtime_snapshot"]["jobs"]["job"].update(
            traffic={"job": 100}
        ),
        lambda data: data.update(status="UNKNOWN"),
        lambda data: data.update(status="FAILED"),
    ],
)
def test_untrusted_or_incomplete_runtime_evidence_is_rejected(mutation):
    data = result()
    mutation(data)
    with pytest.raises(plans.ReleasePlanError):
        central.validate_result(selected_plan(), data)


@pytest.mark.parametrize("status", ["FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"])
def test_failure_never_applies_configuration(status):
    central.validate_result(selected_plan(), {"status": status})


def check(name, conclusion="success", identifier=1):
    return SimpleNamespace(
        name=name, conclusion=conclusion, status="completed", id=identifier
    )


def require_test_quality(repo, *, missing=False, errors=None, generated=None):
    from eng_platform_api.services.catalog import get_service

    report = SimpleNamespace(
        commit_sha="a" * 40,
        quality_gate_status="PASSED",
        generated_at=generated or datetime.now(timezone.utc).isoformat(),
    )
    with (
        patch.object(
            plans.quality_store, "get_report", return_value=None if missing else report
        ),
        patch.object(plans.quality_policy, "policy_errors", return_value=errors or []),
    ):
        plans.require_quality(repo, "a" * 40, get_service("cgm-sanplat-api"))


def test_exact_sha_quality_uses_latest_check_attempt():
    repo = Mock()
    repo.get_commit.return_value.get_check_runs.return_value = [
        check("quality", "failure"),
        check("quality", identifier=2),
        check("SonarCloud Code Analysis"),
    ]
    require_test_quality(repo)
    repo.get_commit.assert_called_once_with("a" * 40)


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [check("normalized / quality-gate", "failure")],
        [check("quality", "failure"), check("SonarCloud Code Analysis")],
        [
            check("quality"),
            check("SonarCloud Code Analysis"),
            check("workflows", "failure"),
        ],
    ],
)
def test_missing_or_failed_quality_blocks_release(checks):
    repo = Mock()
    repo.get_commit.return_value.get_check_runs.return_value = checks
    with pytest.raises(plans.ReleasePlanError):
        require_test_quality(repo)


def test_catalog_owned_oss_policy_cannot_be_bypassed_with_green_github_check():
    repo = Mock()
    repo.get_commit.return_value.get_check_runs.return_value = [check("quality")]
    for options in (
        {"missing": True},
        {"errors": ["Changed-line coverage must be at least 80%"]},
        {"generated": "2020-01-01T00:00:00+00:00"},
    ):
        with pytest.raises(plans.ReleasePlanError):
            require_test_quality(repo, **options)


def test_retired_vendor_check_does_not_override_valid_oss_evidence():
    repo = Mock()
    repo.get_commit.return_value.get_check_runs.return_value = [
        check("normalized / quality-gate"),
        check("SonarCloud Code Analysis", "cancelled"),
    ]
    require_test_quality(repo)


def test_runner_defaults_to_hosted_without_querying_runners(monkeypatch):
    monkeypatch.delenv("CGM_ACTIONS_RUNNER", raising=False)
    repo = Mock()
    assert plans.runner(repo, "") == "ubuntu-latest"
    repo.get_self_hosted_runners.assert_not_called()


@pytest.mark.parametrize(
    "configured,requested", [("bad", ""), ("", "bad"), ("", "cgm-release-local")]
)
def test_invalid_or_disabled_runner_is_rejected(monkeypatch, configured, requested):
    monkeypatch.setenv("CGM_ACTIONS_RUNNER", configured)
    with pytest.raises(plans.ReleasePlanError):
        plans.runner(Mock(), requested)


def test_fallback_requires_available_exact_label(monkeypatch):
    monkeypatch.setenv("CGM_ACTIONS_RUNNER", "cgm-release-local")
    repo = Mock()
    runner = SimpleNamespace(
        status="online", busy=False, labels=[SimpleNamespace(name="cgm-release-local")]
    )
    repo.get_self_hosted_runners.return_value = [runner]
    assert plans.runner(repo, "cgm-release-local") == "cgm-release-local"
    runner.busy = True
    with pytest.raises(plans.ReleasePlanError):
        plans.runner(repo, "cgm-release-local")


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return Ref(self.db, self.path + "/" + name)

    document = collection

    def get(self, **_):
        return SimpleNamespace(to_dict=lambda: deepcopy(self.db.data.get(self.path)))

    def create(self, value):
        self.db.create(self, value)


class DB:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return Ref(self, name)

    def transaction(self):
        return self

    def set(self, ref, value, merge=False):
        self.data[ref.path] = {
            **(self.data.get(ref.path, {}) if merge else {}),
            **deepcopy(value),
        }

    def update(self, ref, value):
        self.set(ref, value, merge=True)

    create = set

    def delete(self, ref):
        del self.data[ref.path]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION", "deployments")
    monkeypatch.setattr(central.firestore, "transactional", lambda function: function)
    return DB()


def seed(db):
    item = DeploymentItem(
        id="deployment",
        service_name="service",
        repository="owner/service",
        tag="v1.0.0",
        configuration=selected_plan()["configuration"],
    )
    db.data["deployments/deployment"] = item.model_dump() | {
        "idempotency_key": "request"
    }
    db.data["eng_platform_release_locks/service"] = {
        "deployment_id": item.id,
        "idempotency_key": "request",
    }
    db.data["eng_platform_release_executions/execution"] = {"status": "RUNNING"}
    return item


def test_terminal_ack_retry_cannot_overwrite_later_applied_configuration(db):
    seed(db)
    central.finish(db, "execution", "deployment", result())
    assert "eng_platform_release_locks/service" not in db.data
    config = "eng_platform_service_configurations/service"
    db.data[config]["applied_versions"] = {"WM_PASSWORD": "3"}
    central.finish(db, "execution", "deployment", result())
    assert db.data[config]["applied_versions"] == {"WM_PASSWORD": "3"}
    with pytest.raises(plans.ReleasePlanError):
        central.finish(db, "execution", "deployment", {"status": "FAILED"})


def test_failed_rollback_keeps_service_locked(db):
    seed(db)
    central.finish(db, "execution", "deployment", {"status": "ROLLBACK_FAILED"})
    assert "eng_platform_release_locks/service" in db.data
    assert "eng_platform_service_configurations/service" not in db.data


def test_old_execution_cannot_unlock_another_release(db):
    seed(db)
    db.data["eng_platform_release_locks/service"]["deployment_id"] = "newer"
    with pytest.raises(plans.ReleasePlanError):
        central.finish(db, "execution", "deployment", result())
    assert db.data["eng_platform_release_locks/service"]["deployment_id"] == "newer"


def test_reserve_rejects_same_key_with_different_operator(db):
    item = seed(db)
    assert central._reserve(db, item, "request").id == item.id
    item.requested_by = "different"
    with pytest.raises(plans.ReleasePlanError):
        central._reserve(db, item, "request")


def test_immutable_tag_registration_rejects_moved_tag(db):
    plans.register_immutable_tag(
        db, "owner/service", ReleaseTag(name="v1.0.0", sha="a" * 40)
    )
    with pytest.raises(plans.ReleasePlanError):
        plans.register_immutable_tag(
            db, "owner/service", ReleaseTag(name="v1.0.0", sha="b" * 40)
        )


def test_central_callback_rejects_another_run(db):
    db.data["eng_platform_release_executions/execution"] = {
        "identity": {"run_id": "1"},
        "plan": {"kind": "deploy"},
    }
    with (
        patch.object(central, "_db", return_value=db),
        patch.object(plans, "execution_repository", return_value="owner/engine"),
        patch.object(central.workflow_identity, "verify", return_value={"run_id": "2"}),
    ):
        with pytest.raises(central.release_authorization.ReleaseAuthorizationError):
            central.authorized_execution("execution", "oidc")


def test_central_start_persists_plan_before_dispatch_and_never_uses_service_executor(
    db,
):
    from eng_platform_api.services.catalog import get_service

    service = get_service("cgm-sanplat-api")
    selected = selected_plan() | {"target_revision": ""}
    client, engine, source = Mock(), Mock(), Mock()
    client.get_repo.side_effect = [engine, source]
    source.create_deployment.return_value.id = 123
    claims = {
        "jti": "execution",
        "configuration_hash": plans.digest(selected),
        "exp": 4000000000,
    }
    with (
        patch.object(central, "_db", return_value=db),
        patch.object(central.github_deployments, "github_client", return_value=client),
        patch.object(plans, "execution_repository", return_value="owner/engine"),
        patch.object(plans, "runner", return_value="ubuntu-latest"),
        patch.object(plans, "create", return_value=selected),
        patch.object(
            central.release_authorization, "issue", return_value=("signed", claims)
        ),
        patch.object(central.deployment_store, "save"),
    ):
        item = central.start(
            service=service,
            tag=ReleaseTag(name="v1.0.0", sha="a" * 40),
            operator="angel",
            key="request",
        )
    engine.get_workflow.assert_called_once_with("central-release.yml")
    assert (
        engine.get_workflow.return_value.create_dispatch.call_args.kwargs["ref"]
        == "main"
    )
    assert (
        db.data["eng_platform_release_executions/execution"]["deployment_id"] == item.id
    )
    source.get_workflow.assert_not_called()


def test_consume_is_one_use_and_records_exact_run(db):
    item = seed(db)
    selected = selected_plan()
    db.data["eng_platform_release_executions/execution"] = {
        "plan": selected,
        "deployment_id": item.id,
        "status": "QUEUED",
    }
    claims = {
        "jti": "execution",
        "execution_repository": "owner/engine",
        "requested_by": "angel",
        "kind": "deploy",
        "configuration_hash": plans.digest(selected),
    }
    identity = {"repository": "owner/engine", "run_id": "123", "run_attempt": "1"}
    with (
        patch.object(central, "_db", return_value=db),
        patch.object(central.release_authorization, "verify", return_value=claims),
        patch.object(plans, "execution_repository", return_value="owner/engine"),
        patch.object(central.config.auth, "allowed_logins", ("angel",)),
        patch.object(central.workflow_identity, "verify", return_value=identity),
        patch.object(central.deployment_store, "save") as save,
    ):
        assert central.consume("signed", "oidc")["plan"] == selected
        assert save.call_args.args[0].github_run_id == 123
        with pytest.raises(plans.ReleasePlanError):
            central.consume("signed", "oidc")


@pytest.mark.parametrize(
    "executor,operator", [("owner/other", "angel"), ("owner/engine", "revoked")]
)
def test_consume_checks_executor_and_operator_revocation(executor, operator):
    claims = {"execution_repository": executor, "requested_by": operator}
    with (
        patch.object(central.release_authorization, "verify", return_value=claims),
        patch.object(plans, "execution_repository", return_value="owner/engine"),
        patch.object(central.config.auth, "allowed_logins", ("angel",)),
    ):
        with pytest.raises(central.release_authorization.ReleaseAuthorizationError):
            central.consume("signed", "oidc")


def test_create_plan_uses_catalog_and_numeric_snapshot(db):
    from eng_platform_api.services.catalog import get_service

    service = get_service("cgm-sanplat-api")
    repo = Mock()
    repo.get_commit.return_value.sha = "a" * 40
    tag = ReleaseTag(name="v1.0.0", sha="a" * 40)
    with (
        patch.object(plans, "require_quality"),
        patch.object(
            plans.operational_secrets,
            "snapshot",
            return_value={"generation": 1, "secrets": {}},
        ),
    ):
        selected = plans.create(service, tag, repo, db)
        assert selected["project_id"] == service.project_id
        assert selected["configuration"]["generation"] == 1
        repo.get_commit.return_value.sha = "b" * 40
        with pytest.raises(plans.ReleasePlanError):
            plans.create(service, tag, repo, db)


def test_recovery_checkpoint_is_durable_and_cannot_be_overwritten(db):
    item = seed(db)
    record = {"operator": "angel", "plan": selected_plan(), "deployment_id": item.id}
    db.data["eng_platform_release_executions/execution"] = record
    snapshot = result()["runtime_snapshot"]
    with (
        patch.object(central, "authorized_execution", return_value=(db, record)),
        patch.object(central.config.auth, "allowed_logins", ("angel",)),
    ):
        central.checkpoint("execution", "oidc", snapshot)
        assert (
            db.data["eng_platform_release_executions/execution"]["previous_runtimes"]
            == snapshot
        )
        with pytest.raises(plans.ReleasePlanError):
            central.checkpoint("execution", "oidc", snapshot)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot.pop("jobs"),
        lambda snapshot: snapshot["services"].pop("worker"),
        lambda snapshot: snapshot["jobs"]["job"].update(payload="private"),
        lambda snapshot: snapshot["jobs"]["job"].update(image="image:latest"),
        lambda snapshot: snapshot["jobs"]["job"]["secrets"].update(WM_PASSWORD="value"),
        lambda snapshot: snapshot["jobs"]["job"].update(traffic={"job": 100}),
        lambda snapshot: snapshot["services"]["worker"].update(traffic={"other": 100}),
    ],
)
def test_recovery_checkpoint_rejects_non_metadata_or_foreign_destinations(db, mutation):
    snapshot = result()["runtime_snapshot"]
    mutation(snapshot)
    with (
        patch.object(
            central,
            "authorized_execution",
            return_value=(db, {"operator": "angel", "plan": selected_plan()}),
        ),
        patch.object(central.config.auth, "allowed_logins", ("angel",)),
    ):
        with pytest.raises(plans.ReleasePlanError):
            central.checkpoint("execution", "oidc", snapshot)


def test_recovery_checkpoint_rechecks_revocation_before_mutation(db):
    with (
        patch.object(
            central, "authorized_execution", return_value=(db, {"operator": "revoked"})
        ),
        patch.object(central.config.auth, "allowed_logins", ("angel",)),
    ):
        with pytest.raises(central.release_authorization.ReleaseAuthorizationError):
            central.checkpoint("execution", "oidc", {})


def test_expired_unused_dispatch_can_be_reconciled_without_redeploying(db):
    item = seed(db)
    item.execution_id = "execution"
    db.data["eng_platform_release_executions/execution"]["expires_at"] = 1
    with (
        patch.object(central, "_db", return_value=db),
        patch.object(central.deployment_store, "get", return_value=item),
    ):
        central.reconcile(item)
    assert db.data["deployments/deployment"]["status"] == "FAILED"
    assert "eng_platform_release_locks/service" not in db.data


def test_reconciliation_cannot_override_concurrent_checkpoint(db):
    seed(db)
    db.data["eng_platform_release_executions/execution"]["previous_runtimes"] = (
        result()["runtime_snapshot"]
    )
    with pytest.raises(plans.ReleasePlanError):
        central.finish(
            db, "execution", "deployment", {"status": "FAILED"}, before_mutation=True
        )
    assert "eng_platform_release_locks/service" in db.data
