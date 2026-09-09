"""Release history persistence — one JSON record per service."""

import json
import os
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional, cast

from pydantic import ValidationError

from ..models import (
    ReleaseCreateRequest,
    ReleaseItem,
    ReleaseServiceAction,
    ServiceRevision,
)

_DEFAULT_STORE_PATH = Path(os.getenv("RELEASES_STORE_PATH", "data/releases.json"))
_COLLECTION = os.getenv("ENG_PLATFORM_RELEASES_FIRESTORE_COLLECTION", "")
_store_lock = threading.RLock()


class ReleaseConflict(ValueError):
    """A release identity was reused with different immutable inputs."""


@lru_cache(maxsize=4)
def _firestore_client(project_id: str):
    from google.cloud import firestore

    return firestore.Client(project=project_id) if project_id else firestore.Client()


def _firestore_collection():
    if not _COLLECTION:
        return None
    project_id = os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()
    return _firestore_client(project_id).collection(_COLLECTION)


def _resolve_path() -> Path:
    return (
        Path(_DEFAULT_STORE_PATH)
        if _DEFAULT_STORE_PATH.is_absolute()
        else Path.cwd() / _DEFAULT_STORE_PATH
    )


def _load() -> list[dict]:
    path = _resolve_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: list[dict]) -> None:
    path = _resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _normalize_service(service: ServiceRevision) -> ServiceRevision:
    if service.revision or service.action in {"unchanged", "not_included", "missing"}:
        return service
    return ServiceRevision(
        service_name=service.service_name,
        revision="",
        action="missing",
    )


def _repository_for_service(service_name: str, record: dict) -> str:
    if record.get("repository"):
        return str(record["repository"])
    from . import catalog

    service = catalog.get_service(service_name)
    return service.repository if service else ""


def _item(
    *,
    service_name: str,
    repository: str,
    version: str,
    status: str,
    revision: str,
    action: str,
    github_run_url: str,
    created_at: str,
    release_id: str = "",
    source_sha: str = "",
    artifact_digest: str = "",
) -> Optional[ReleaseItem]:
    if not service_name or not repository:
        return None
    try:
        return ReleaseItem(
            service_name=service_name,
            repository=repository,
            version=version,
            status=status,
            release_id=release_id,
            source_sha=source_sha,
            artifact_digest=artifact_digest,
            revision=revision,
            action=cast(ReleaseServiceAction, action),
            github_run_url=github_run_url,
            created_at=created_at,
        )
    except ValidationError:
        return ReleaseItem(
            service_name=service_name,
            repository=repository,
            version=version,
            status=status,
            release_id=release_id,
            source_sha=source_sha,
            artifact_digest=artifact_digest,
            revision=revision,
            action="missing",
            github_run_url=github_run_url,
            created_at=created_at,
        )


def _legacy_fixed_revision_items(record: dict) -> list[ReleaseItem]:
    action = {
        "promoted": "promoted",
        "rolled_back": "rolled_back",
    }.get(record.get("status", ""), "deployed")
    items: list[ReleaseItem] = []
    for revision in (record.get("api_revision", ""), record.get("web_revision", "")):
        if not revision:
            continue
        from . import catalog

        service = next(
            (
                candidate
                for candidate in catalog.get_services().services
                if revision == candidate.service_name
                or revision.startswith(f"{candidate.service_name}-")
            ),
            None,
        )
        if service:
            item = _item(
                service_name=service.service_name,
                repository=service.repository,
                version=str(record.get("version", "")),
                status=str(record.get("status", "")),
                revision=str(revision),
                action=action,
                github_run_url=str(record.get("github_run_url", "")),
                created_at=str(record.get("created_at", "")),
            )
            if item:
                items.append(item)
    return items


def release_items_from_record(record: dict) -> list[ReleaseItem]:
    """Read current records and split historical grouped records by service."""
    if record.get("service_name"):
        item = _item(
            service_name=str(record.get("service_name", "")),
            repository=_repository_for_service(
                str(record.get("service_name", "")), record
            ),
            version=str(record.get("version", "")),
            status=str(record.get("status", "")),
            release_id=str(record.get("release_id", "")),
            source_sha=str(record.get("source_sha", "")),
            artifact_digest=str(record.get("artifact_digest", "")),
            revision=str(record.get("revision", "")),
            action=str(record.get("action", "missing")),
            github_run_url=str(record.get("github_run_url", "")),
            created_at=str(record.get("created_at", "")),
        )
        return [item] if item else []

    raw_services = record.get("services") or []
    if raw_services:
        items: list[ReleaseItem] = []
        for raw_service in raw_services:
            try:
                service = _normalize_service(ServiceRevision(**raw_service))
            except (TypeError, ValidationError):
                service = ServiceRevision(
                    service_name=str(raw_service.get("service_name", "")),
                    revision=str(raw_service.get("revision", "")),
                    action="missing",
                )
            item = _item(
                service_name=service.service_name,
                repository=_repository_for_service(service.service_name, record),
                version=str(record.get("version", "")),
                status=str(record.get("status", "")),
                release_id=str(record.get("release_id", "")),
                source_sha=str(record.get("source_sha", "")),
                artifact_digest=str(record.get("artifact_digest", "")),
                revision=service.revision,
                action=service.action,
                github_run_url=str(record.get("github_run_url", "")),
                created_at=str(record.get("created_at", "")),
            )
            if item:
                items.append(item)
        return items

    return _legacy_fixed_revision_items(record)


def save_release(payload: ReleaseCreateRequest) -> list[ReleaseItem]:
    """Persist one record per service and return the created rows."""
    now = datetime.now(timezone.utc).isoformat()
    items = [
        ReleaseItem(
            service_name=service.service_name,
            repository=payload.repository,
            version=payload.version,
            status=payload.status,
            release_id=payload.release_id,
            source_sha=payload.source_sha,
            artifact_digest=payload.artifact_digest,
            revision=normalized.revision,
            action=normalized.action,
            github_run_url=payload.github_run_url,
            created_at=now,
        )
        for service in payload.services
        for normalized in [_normalize_service(service)]
    ]

    collection = _firestore_collection()
    if collection is not None:
        existing_rows: dict[str, ReleaseItem] = {}
        if payload.release_id:
            immutable = ("repository", "version", "source_sha", "artifact_digest")
            for item in items:
                document = collection.document(
                    f"{payload.release_id}-{item.service_name}"
                )
                existing = document.get()
                if getattr(existing, "exists", False):
                    record = existing.to_dict() or {}
                    if (
                        record.get("release_id", "") != payload.release_id
                        or record.get("service_name", "") != item.service_name
                        or any(
                            record.get(key, "") != getattr(payload, key)
                            for key in immutable
                        )
                    ):
                        raise ReleaseConflict(
                            "Release identity already exists with different immutable inputs"
                        )
                    existing_rows[item.service_name] = ReleaseItem(**record)
            if existing_rows:
                if len(existing_rows) != len(items):
                    raise ReleaseConflict(
                        "Release identity is missing one of its service rows"
                    )
                return [existing_rows[item.service_name] for item in items]
        for item in items:
            doc_id = (
                f"{payload.release_id}-{item.service_name}"
                if payload.release_id
                else f"{item.service_name}-{item.created_at}"
            )
            document = collection.document(doc_id)
            record = {
                **item.model_dump(),
                "triggered_by": payload.triggered_by,
                "rollback_from_version": payload.rollback_from_version,
                "notes": payload.notes,
            }
            if not payload.release_id:
                document.set(record)
                continue
            try:
                # create() makes the release identity reservation atomic across
                # independent API processes; set() could overwrite a raced
                # release with a different source or digest.
                document.create(record)
                existing_rows[item.service_name] = item
            except Exception as exc:
                if exc.__class__.__name__ not in {"AlreadyExists", "Conflict"}:
                    raise
                existing = document.get()
                if not getattr(existing, "exists", False):
                    raise ReleaseConflict(
                        "Release identity disappeared during reservation"
                    ) from exc
                stored = existing.to_dict() or {}
                immutable = ("repository", "version", "source_sha", "artifact_digest")
                if (
                    stored.get("release_id", "") != payload.release_id
                    or stored.get("service_name", "") != item.service_name
                    or any(
                        stored.get(key, "") != getattr(payload, key)
                        for key in immutable
                    )
                ):
                    raise ReleaseConflict(
                        "Release identity already exists with different immutable inputs"
                    ) from exc
                existing_rows[item.service_name] = ReleaseItem(**stored)
        if payload.release_id:
            if len(existing_rows) != len(items):
                raise ReleaseConflict(
                    "Release identity is missing one of its service rows"
                )
            return [existing_rows[item.service_name] for item in items]
        return items

    with _store_lock:
        records = _load()
        if payload.release_id:
            existing = [
                record
                for record in records
                if record.get("release_id") == payload.release_id
            ]
            if existing:
                immutable = ("repository", "version", "source_sha", "artifact_digest")
                if any(
                    record.get(key, "") != getattr(payload, key)
                    for record in existing
                    for key in immutable
                ):
                    raise ReleaseConflict(
                        "Release identity already exists with different immutable inputs"
                    )
                existing_by_service = {
                    record.get("service_name"): record for record in existing
                }
                if all(item.service_name in existing_by_service for item in items):
                    return [
                        ReleaseItem(**existing_by_service[item.service_name])
                        for item in items
                    ]
                raise ReleaseConflict(
                    "Release identity is missing one of its service rows"
                )
        records.extend(
            {
                **item.model_dump(),
                "triggered_by": payload.triggered_by,
                "rollback_from_version": payload.rollback_from_version,
                "notes": payload.notes,
            }
            for item in items
        )
        _save(records)
    return items


def get_releases(
    service_name: Optional[str] = None, limit: int = 20
) -> list[ReleaseItem]:
    collection = _firestore_collection()
    if collection is not None:
        if service_name:
            snapshots = (
                collection.where("service_name", "==", service_name)
                .order_by("created_at", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
        else:
            snapshots = (
                collection.order_by("created_at", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
        records = [snapshot.to_dict() for snapshot in snapshots]
        items = [ReleaseItem(**record) for record in records]
        return items
    else:
        with _store_lock:
            records = _load()
        items = [
            item
            for record in records
            for item in release_items_from_record(record)
            if not service_name or item.service_name == service_name
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]


def get_latest(service_name: str) -> Optional[ReleaseItem]:
    releases = get_releases(service_name=service_name, limit=1)
    return releases[0] if releases else None


def count_releases(service_name: Optional[str] = None) -> int:
    collection = _firestore_collection()
    if collection is not None:
        if service_name:
            return sum(
                1 for _ in collection.where("service_name", "==", service_name).stream()
            )
        return sum(1 for _ in collection.stream())
    with _store_lock:
        records = _load()
    return sum(
        1
        for record in records
        for item in release_items_from_record(record)
        if not service_name or item.service_name == service_name
    )
