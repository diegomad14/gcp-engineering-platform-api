"""Write-only Secret Manager operations and durable configuration manifests.

Reserve before publishing: an interrupted/uncertain write remains locked for
reconciliation. Never retry add_secret_version automatically; it is not idempotent.
Neither Firestore nor exception messages contain secret values or their hashes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import google.auth
from google.auth import impersonated_credentials
from google.cloud import firestore, secretmanager

from ..models import CatalogService, OperationalSecret


class ConfigurationConflict(ValueError):
    """Stale configuration or unresolved operation."""


def database():
    project = os.environ.get("ENG_PLATFORM_GCP_PROJECT_ID", "")
    if not project:
        raise RuntimeError("Configuration store is not configured")
    return firestore.Client(project=project)


def writer():
    target = os.environ.get("ENG_PLATFORM_SECRETS_WRITER_SERVICE_ACCOUNT", "")
    if not target:
        raise RuntimeError("Secret publishing is not configured")
    source, _ = google.auth.default()
    credentials = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=target,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=300,
    )
    return secretmanager.SecretManagerServiceClient(credentials=credentials)


def document(db, service_name: str):
    return db.collection("eng_platform_service_configurations").document(service_name)


def state(service_name: str) -> dict:
    return document(database(), service_name).get().to_dict() or {
        "generation": 0,
        "versions": {},
        "applied_versions": {},
    }


def resource(service: CatalogService, secret: OperationalSecret) -> str:
    return f"projects/{service.project_id}/secrets/{secret.secret_id}"


def metadata(service: CatalogService) -> dict:
    current = state(service.service_name)
    client = writer() if service.operational_secrets else None
    items = []
    for secret in service.operational_secrets:
        version = current.get("versions", {}).get(secret.key)
        enabled = False
        if version and client is not None:
            info = client.get_secret_version(
                request={"name": f"{resource(service, secret)}/versions/{version}"},
                timeout=15,
            )
            enabled = info.state == secretmanager.SecretVersion.State.ENABLED
        items.append(
            {
                "key": secret.key,
                "description": secret.description,
                "required": secret.required,
                "editable": secret.editable,
                "configured": enabled,
                "pending_version": version,
                "applied_version": current.get("applied_versions", {}).get(secret.key),
            }
        )
    return {
        "items": items,
        "generation": current.get("generation", 0),
        "blocked": bool(current.get("active_operation")),
    }


def reserve(
    db,
    service: CatalogService,
    secret: OperationalSecret,
    operation_id: str,
    generation: int,
    requested_by: str,
) -> dict | None:
    ref = document(db, service.service_name)
    operation = ref.collection("operations").document(operation_id)

    @firestore.transactional
    def commit(transaction):
        current = ref.get(transaction=transaction).to_dict() or {}
        previous = operation.get(transaction=transaction).to_dict()
        if previous:
            if (
                previous["key"] != secret.key
                or previous["requested_by"] != requested_by
            ):
                raise ConfigurationConflict("Operation belongs to another request")
            if previous["status"] == "SAVED":
                return previous
            raise ConfigurationConflict("Operation requires reconciliation")
        if current.get("active_operation"):
            raise ConfigurationConflict("A secret operation requires reconciliation")
        if current.get("generation", 0) != generation:
            raise ConfigurationConflict("Configuration changed; refresh before saving")
        record = {
            "operation_id": operation_id,
            "key": secret.key,
            "requested_by": requested_by,
            "generation": generation,
            "status": "PUBLISHING",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        transaction.set(operation, record)
        transaction.set(ref, {"active_operation": operation_id}, merge=True)
        return None

    return commit(db.transaction())


def finalize(
    db, service_name: str, secret_key: str, operation_id: str, version: str
) -> dict:
    ref = document(db, service_name)
    operation = ref.collection("operations").document(operation_id)

    @firestore.transactional
    def commit(transaction):
        current = ref.get(transaction=transaction).to_dict() or {}
        record = operation.get(transaction=transaction).to_dict() or {}
        if current.get("active_operation") != operation_id:
            raise ConfigurationConflict("Operation requires reconciliation")
        versions = dict(current.get("versions", {}))
        versions[secret_key] = version
        generation = current.get("generation", 0) + 1
        record.update(status="SAVED", version=version, generation=generation)
        transaction.set(operation, record)
        transaction.set(
            ref,
            {"versions": versions, "generation": generation, "active_operation": None},
            merge=True,
        )
        transaction.set(
            ref.collection("manifests").document(str(generation)),
            {"versions": versions, "generation": generation},
        )
        return record

    return commit(db.transaction())


def publish(
    service: CatalogService,
    secret: OperationalSecret,
    value: str,
    operation_id: str,
    generation: int,
    requested_by: str,
) -> dict:
    client = writer()
    db = database()
    previous = reserve(db, service, secret, operation_id, generation, requested_by)
    if previous:
        return {
            key: previous[key]
            for key in ("operation_id", "status", "version", "generation")
        }
    # A failure from this point is intentionally left PUBLISHING, blocking
    # further writes until reconciliation establishes whether a version exists.
    version = client.add_secret_version(
        request={
            "parent": resource(service, secret),
            "payload": {"data": value.encode("utf-8")},
        },
        retry=None,
        timeout=30,
    )
    number = version.name.rsplit("/", 1)[-1]
    if not number.isdecimal():
        raise RuntimeError("Secret Manager returned an invalid version")
    record = finalize(db, service.service_name, secret.key, operation_id, number)
    return {
        key: record[key] for key in ("operation_id", "status", "version", "generation")
    }


def snapshot(service: CatalogService) -> dict:
    """Capture numeric references, never secret payloads, for one deployment."""
    current = state(service.service_name)
    if current.get("active_operation"):
        raise ConfigurationConflict("A secret operation requires reconciliation")
    available = metadata(service)
    if any(item["required"] and not item["configured"] for item in available["items"]):
        raise ConfigurationConflict("Required operational secrets are not configured")
    if available["generation"] != current.get("generation", 0):
        raise ConfigurationConflict("Configuration changed; retry deployment")
    return {
        "generation": current.get("generation", 0),
        "secrets": {
            secret.key: f"{resource(service, secret)}/versions/{current['versions'][secret.key]}"
            for secret in service.operational_secrets
            if current.get("versions", {}).get(secret.key)
        },
    }
