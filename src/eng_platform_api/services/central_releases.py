"""Durable release intents and exact-run callbacks for the trusted executor."""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from ..config import config
from ..models import CatalogService, DeploymentItem, ReleaseTag, RunnerLabel
from . import catalog, deployment_store, github_deployments, operational_secrets
from . import release_authorization, release_plan, workflow_identity


def _db():
    if not os.getenv("ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION"):
        raise release_plan.ReleasePlanError("Durable deployments are not configured")
    return operational_secrets.database()


def execution(db, execution_id):
    return db.collection("eng_platform_release_executions").document(execution_id)


def _deployment(db, deployment_id):
    return db.collection(
        os.environ["ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION"]
    ).document(deployment_id)


def _lock(db, service_name):
    return db.collection("eng_platform_release_locks").document(service_name)


def _reserve(db, item: DeploymentItem, key: str) -> DeploymentItem | None:
    lock = _lock(db, item.service_name)

    @firestore.transactional
    def commit(transaction):
        held = lock.get(transaction=transaction).to_dict()
        if held:
            if held["idempotency_key"] == key:
                current = (
                    _deployment(db, held["deployment_id"])
                    .get(transaction=transaction)
                    .to_dict()
                )
                if current:
                    previous = DeploymentItem.model_validate(current)
                    fields = ("requested_by", "sha", "tag", "kind", "runner_label")
                    if any(
                        getattr(previous, field) != getattr(item, field)
                        for field in fields
                    ):
                        raise release_plan.ReleasePlanError(
                            "Idempotency key belongs to another request"
                        )
                    return previous
            raise release_plan.ReleasePlanError(
                "A release is active or requires reconciliation"
            )
        transaction.create(lock, {"deployment_id": item.id, "idempotency_key": key})
        transaction.create(
            _deployment(db, item.id), {**item.model_dump(), "idempotency_key": key}
        )
        return None

    return commit(db.transaction())


def start(
    *,
    service: CatalogService,
    tag: ReleaseTag,
    operator: str,
    key: str,
    requested_runner: RunnerLabel = "",
    target: DeploymentItem | None = None,
) -> DeploymentItem:
    db = _db()
    client = github_deployments.github_client()
    engine_name = release_plan.execution_repository()
    engine = client.get_repo(engine_name)
    repo = client.get_repo(service.repository)
    label = release_plan.runner(engine, requested_runner)
    plan = release_plan.create(service, tag, repo, db, target)
    now = datetime.now(timezone.utc).isoformat()
    item = DeploymentItem(
        id=str(uuid.uuid4()),
        service_name=service.service_name,
        repository=service.repository,
        tag=tag.name,
        sha=tag.sha,
        execution_repository=engine_name,
        configuration=plan["configuration"],
        kind=plan["kind"],
        requested_by=operator,
        created_at=now,
        updated_at=now,
        runner_label=requested_runner,
        effective_runner_label=label,
        stages=github_deployments.default_stages(plan["kind"]),
    )
    existing = _reserve(db, item, key)
    if existing:
        return existing
    try:
        deployed = repo.create_deployment(
            ref=tag.sha,
            task=plan["kind"],
            auto_merge=False,
            required_contexts=[],
            environment=f"{service.service_name}-production",
            description=f"Engineering Platform {item.id}",
            payload={
                "deployment_id": item.id,
                "configuration_hash": release_plan.digest(plan),
            },
        )
        item.github_deployment_id = deployed.id
        token, claims = release_authorization.issue(
            repository=service.repository,
            service_name=service.service_name,
            tag=tag.name,
            sha=tag.sha,
            github_deployment_id=deployed.id,
            requested_by=operator,
            kind=item.kind,
            target_revision=plan["target_revision"],
            execution_repository=engine_name,
            configuration=plan,
        )
        item.execution_id = claims["jti"]
        execution(db, item.execution_id).create(
            {
                "plan": plan,
                "deployment_id": item.id,
                "operator": operator,
                "configuration_hash": claims["configuration_hash"],
                "status": "QUEUED",
                "expires_at": claims["exp"],
            }
        )
        deployment_store.save(item, key)
        dispatched = engine.get_workflow("central-release.yml").create_dispatch(
            ref="main",
            inputs={
                "deployment_id": item.id,
                "platform_authorization": token,
                "runner_label": label,
            },
        )
        if not dispatched:
            raise release_plan.ReleasePlanError("Dispatch was not acknowledged")
    except Exception:
        # Do not release the durable lock: GitHub may have accepted the dispatch.
        item.error = "Release dispatch could not be confirmed; reconciliation required"
        item.current_stage = "dispatch-uncertain"
        deployment_store.save(item, key)
        raise release_plan.ReleasePlanError(item.error) from None
    return item


def consume(token: str, oidc: str) -> dict:
    claims = release_authorization.verify(token, {})
    if claims.get("execution_repository") != release_plan.execution_repository():
        raise release_authorization.ReleaseAuthorizationError("Wrong executor")
    if claims.get("requested_by", "").lower() not in config.auth.allowed_logins:
        raise release_authorization.ReleaseAuthorizationError("Operator revoked")
    identity = workflow_identity.verify(
        oidc, claims["execution_repository"], claims["kind"]
    )
    db = _db()
    ref = execution(db, claims["jti"])

    @firestore.transactional
    def commit(transaction):
        record = ref.get(transaction=transaction).to_dict()
        if not record or release_plan.digest(record["plan"]) != claims.get(
            "configuration_hash"
        ):
            raise release_authorization.ReleaseAuthorizationError(
                "Release configuration mismatch"
            )
        if record.get("identity") or record.get("status") != "QUEUED":
            raise release_plan.ReleasePlanError(
                "Release authorization already consumed"
            )
        transaction.update(ref, {"identity": identity, "status": "RUNNING"})
        return record

    record = commit(db.transaction())
    item = DeploymentItem.model_validate(
        _deployment(db, record["deployment_id"]).get().to_dict()
    )
    item.github_run_id = int(identity["run_id"])
    item.github_run_url = (
        f"https://github.com/{identity['repository']}/actions/runs/{identity['run_id']}"
    )
    item.logs_url = item.github_run_url
    item.status = "BUILDING" if item.kind == "deploy" else "ROLLING_BACK"
    item.current_stage = "build" if item.kind == "deploy" else "rollback"
    deployment_store.save(item, "")
    return {"execution_id": claims["jti"], "plan": record["plan"]}


def authorized_execution(execution_id: str, oidc: str) -> tuple[Any, dict]:
    db = _db()
    ref = execution(db, execution_id)
    record = ref.get().to_dict()
    if not record or not record.get("identity"):
        raise release_authorization.ReleaseAuthorizationError(
            "Unknown release execution"
        )
    identity = workflow_identity.verify(
        oidc, release_plan.execution_repository(), record["plan"]["kind"]
    )
    if identity != record["identity"]:
        raise release_authorization.ReleaseAuthorizationError(
            "Release belongs to another workflow run"
        )
    return db, record


def report(execution_id: str, oidc: str, result: dict) -> None:
    db, record = authorized_execution(execution_id, oidc)
    validate_result(record["plan"], result)
    finish(db, execution_id, record["deployment_id"], result)


def progress(execution_id: str, oidc: str, stage: str) -> None:
    """Recheck release policy at both infrastructure boundaries and record progress."""
    states = {
        "deploy-candidate": "DEPLOYING_CANDIDATE",
        "validate-candidate": "VALIDATING_CANDIDATE",
        "promote": "PROMOTING",
        "validate-production": "VALIDATING_PRODUCTION",
        "rollback": "ROLLING_BACK",
    }
    if stage not in states:
        raise release_plan.ReleasePlanError("Invalid release stage")
    db, record = authorized_execution(execution_id, oidc)
    if record["operator"].lower() not in config.auth.allowed_logins:
        raise release_authorization.ReleaseAuthorizationError("Operator revoked")
    plan = record["plan"]
    if plan["kind"] == "deploy" and stage in {"deploy-candidate", "promote"}:
        service = catalog.get_service(plan["service_name"])
        if service is None:
            raise release_plan.ReleasePlanError(
                "Release service is no longer configured"
            )
        repo = github_deployments.github_client().get_repo(plan["repository"])
        if repo.get_commit(plan["tag"]).sha != plan["sha"]:
            raise release_plan.ReleasePlanError("Release tag moved since authorization")
        release_plan.require_quality(repo, plan["sha"], service)
    ref = execution(db, execution_id)
    deployment = _deployment(db, record["deployment_id"])

    @firestore.transactional
    def commit(transaction):
        current = ref.get(transaction=transaction).to_dict() or {}
        raw_item = deployment.get(transaction=transaction).to_dict() or {}
        if current.get("result"):
            raise release_plan.ReleasePlanError("Release is already complete")
        item = DeploymentItem.model_validate(raw_item)
        keys = [step.key for step in item.stages]
        if stage not in keys:
            raise release_plan.ReleasePlanError(
                "Stage does not match release operation"
            )
        for index, step in enumerate(item.stages):
            step.status = (
                "succeeded"
                if index < keys.index(stage)
                else "running"
                if step.key == stage
                else "pending"
            )
        transaction.update(
            deployment,
            {
                "status": states[stage],
                "current_stage": stage,
                "stages": [step.model_dump() for step in item.stages],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    commit(db.transaction())


def checkpoint(execution_id: str, oidc: str, snapshot: dict) -> None:
    """Persist recovery evidence before the executor is allowed to mutate runtimes."""
    db, record = authorized_execution(execution_id, oidc)
    if record["operator"].lower() not in config.auth.allowed_logins:
        raise release_authorization.ReleaseAuthorizationError("Operator revoked")
    validate_snapshot(record["plan"], snapshot)
    ref = execution(db, execution_id)

    @firestore.transactional
    def commit(transaction):
        current = ref.get(transaction=transaction).to_dict() or {}
        if current.get("result") or current.get("previous_runtimes"):
            raise release_plan.ReleasePlanError(
                "Execution already checkpointed or completed"
            )
        transaction.update(ref, {"previous_runtimes": snapshot})

    commit(db.transaction())


def reconcile(item: DeploymentItem) -> DeploymentItem:
    """Automatically resolve only executions proven never to have mutated PROD."""
    if not item.execution_id or item.status in github_deployments.TERMINAL_STATUSES:
        return item
    db = _db()
    ref = execution(db, item.execution_id)
    record = ref.get().to_dict() or {}
    if record.get("result"):
        return deployment_store.get(item.id) or item
    if not record.get("identity"):
        if time.time() <= record.get("expires_at", float("inf")) + 30:
            return item
    else:
        repo = github_deployments.github_client().get_repo(item.execution_repository)
        run = repo.get_workflow_run(int(record["identity"]["run_id"]))
        if run.status != "completed":
            return item
        if record.get("previous_runtimes"):
            item.error = "Executor stopped after recovery checkpoint; runtime reconciliation required"
            return item
    # Firestore checks again: a concurrent consume/checkpoint wins over this observation.
    finish(db, item.execution_id, item.id, {"status": "FAILED"}, before_mutation=True)
    return deployment_store.get(item.id) or item


def validate_result(plan: dict, result: dict) -> None:
    """Accept only resource metadata for the destinations authorized in this plan."""
    state = result["status"]
    if state not in {"SUCCEEDED", "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"}:
        raise release_plan.ReleasePlanError("Invalid terminal release state")
    for key in ("candidate_revision", "production_revision"):
        if result.get(key) and not re.fullmatch(
            re.escape(plan["service_name"]) + r"-[a-z0-9-]{1,50}", result[key]
        ):
            raise release_plan.ReleasePlanError("Invalid runtime revision")
    if state != "SUCCEEDED":
        if set(result) - {"status", "candidate_revision"}:
            raise release_plan.ReleasePlanError(
                "Failed release cannot apply configuration"
            )
        return
    prefix = f"{plan['region']}-docker.pkg.dev/{plan['project_id']}/{plan['artifact_repository']}/{plan['image_name']}"
    if not re.fullmatch(
        re.escape(prefix) + r"@sha256:[0-9a-f]{64}", result.get("image_digest", "")
    ):
        raise release_plan.ReleasePlanError("Invalid image digest")
    if not result.get("production_revision"):
        raise release_plan.ReleasePlanError("Production revision is required")
    runtimes = result.get("runtime_snapshot", {})
    validate_snapshot(plan, runtimes, image=result["image_digest"])
    if runtimes["services"][plan["service_name"]]["traffic"] != {
        result["production_revision"]: 100
    }:
        raise release_plan.ReleasePlanError(
            "Production revision does not match runtime evidence"
        )


def _validate_traffic(kind: str, name: str, traffic: dict) -> None:
    if kind == "jobs":
        if traffic:
            raise release_plan.ReleasePlanError("Job cannot have traffic")
        return
    if len(traffic) != 1 or list(traffic.values()) != [100]:
        raise release_plan.ReleasePlanError("Invalid production traffic")
    if not next(iter(traffic)).startswith(name + "-"):
        raise release_plan.ReleasePlanError("Traffic belongs to another service")


def _validate_references(references: dict, required: dict) -> None:
    for key, reference in references.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not re.fullmatch(
            r"[A-Za-z0-9_-]+:[1-9][0-9]*", reference
        ):
            raise release_plan.ReleasePlanError("Non-numeric secret reference")
    for key, reference in required.items():
        parts = reference.split("/")
        if references.get(key) != f"{parts[-3]}:{parts[-1]}":
            raise release_plan.ReleasePlanError("Operational configuration mismatch")


def validate_snapshot(plan: dict, runtimes: dict, *, image: str = "") -> None:
    """Snapshots contain only exact destinations, immutable images and numeric refs."""
    expected = {
        "services": [plan["service_name"], *plan["auxiliary_services"]],
        "jobs": plan["auxiliary_jobs"],
    }
    if set(runtimes) != set(expected):
        raise release_plan.ReleasePlanError("Incomplete runtime snapshot")
    for kind, names in expected.items():
        if set(runtimes[kind]) != set(names):
            raise release_plan.ReleasePlanError("Unexpected runtime destination")
        for name, runtime in runtimes[kind].items():
            if set(runtime) != {"image", "secrets", "traffic"}:
                raise release_plan.ReleasePlanError("Unexpected runtime metadata")
            prefix = f"{plan['region']}-docker.pkg.dev/{plan['project_id']}/{plan['artifact_repository']}/"
            if not re.fullmatch(
                re.escape(prefix) + r"[A-Za-z0-9_-]+@sha256:[0-9a-f]{64}",
                runtime["image"],
            ) or (image and runtime["image"] != image):
                raise release_plan.ReleasePlanError("Runtime image mismatch")
            _validate_references(
                runtime["secrets"], plan["configuration"]["secrets"] if image else {}
            )
            _validate_traffic(kind, name, runtime["traffic"])


def finish(
    db,
    execution_id: str,
    deployment_id: str,
    result: dict,
    *,
    before_mutation: bool = False,
) -> None:
    """ACK retry, applied configuration and lock release are one atomic transaction."""
    ref = execution(db, execution_id)
    deployment = _deployment(db, deployment_id)

    @firestore.transactional
    def commit(transaction):
        record = ref.get(transaction=transaction).to_dict() or {}
        if before_mutation and record.get("previous_runtimes"):
            raise release_plan.ReleasePlanError("Runtime reconciliation required")
        if record.get("result") is not None:
            if record["result"] != result:
                raise release_plan.ReleasePlanError("Conflicting terminal result")
            return
        raw_item = deployment.get(transaction=transaction).to_dict() or {}
        item = DeploymentItem.model_validate(raw_item)
        lock = _lock(db, item.service_name)
        held = lock.get(transaction=transaction).to_dict() or {}
        if held.get("deployment_id") != item.id:
            raise release_plan.ReleasePlanError(
                "Release no longer owns the service lock"
            )
        item = item.model_copy(
            update={
                **result,
                "current_stage": "complete",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        for stage in item.stages:
            if result["status"] == "SUCCEEDED":
                stage.status = "succeeded"
        transaction.set(
            deployment,
            {
                **item.model_dump(),
                "idempotency_key": raw_item.get("idempotency_key", ""),
            },
        )
        if result["status"] == "SUCCEEDED":
            versions = {
                key: value.rsplit("/", 1)[-1]
                for key, value in item.configuration.get("secrets", {}).items()
            }
            transaction.set(
                operational_secrets.document(db, item.service_name),
                {"applied_versions": versions},
                merge=True,
            )
        transaction.update(
            ref,
            {
                "status": result["status"],
                "result": result,
                "completed_at": item.updated_at,
            },
        )
        # A failed rollback requires reconciliation before another release is allowed.
        if result["status"] != "ROLLBACK_FAILED":
            transaction.delete(lock)

    commit(db.transaction())
