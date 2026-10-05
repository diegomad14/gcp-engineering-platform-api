"""Durable sorted views of an existing immutable capture; never executes SQL."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Iterator

from fastapi import HTTPException, Request

from . import database_jobs as jobs
from . import database_job_store as store
from . import database_result_store as results
from . import database_tasks as tasks
from .database_registry import DatabaseUnavailable

MAX_VIEWS = 16


def require_view(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str,
) -> dict:
    source = jobs._execution(request, database_id, workspace_id, execution_id)
    if source["state"] != "completed":
        raise HTTPException(409, "Database result is not complete")
    value = store.get("view", view_id)
    if not value or any(
        value.get(key) != source.get(key)
        for key in (
            "execution_id",
            "workspace_id",
            "database_id",
            "session_id",
            "policy_version",
        )
    ):
        raise HTTPException(404, "Database view not found")
    if value["expires_at"] <= time.time() or value["state"] in {"purged", "expired"}:
        raise HTTPException(410, "Database view is unavailable")
    return value


def _public(value: dict) -> dict:
    dto = {
        key: value.get(key)
        for key in (
            "view_id",
            "execution_id",
            "column_key",
            "direction",
            "expires_at",
            "row_count",
            "elapsed_ms",
            "error_code",
        )
    }
    dto["status"] = value["state"]
    return dto


def view(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str,
) -> dict:
    value = require_view(request, database_id, workspace_id, execution_id, view_id)
    jobs._ensure_dispatch("view", view_id)
    return _public(
        require_view(request, database_id, workspace_id, execution_id, value["view_id"])
    )


def create_view(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    column_key: str,
    direction: str,
    client_request_id: str,
) -> dict:
    source = jobs._execution(request, database_id, workspace_id, execution_id)
    if source["state"] != "completed" or not source.get("manifest"):
        raise HTTPException(409, "Database result is not complete")
    if direction not in {"asc", "desc"} or column_key not in {
        column["key"] for column in source["columns"]
    }:
        raise HTTPException(422, "Invalid database ordering")
    request_id = hashlib.sha256(
        f"view|{source['session_id']}|{execution_id}|{client_request_id}".encode()
    ).hexdigest()
    fingerprint = hashlib.sha256(f"{column_key}|{direction}".encode()).hexdigest()
    previous = store.get("request", request_id)
    if previous:
        if previous.get("fingerprint") != fingerprint or not previous.get("view_id"):
            raise HTTPException(409, "Database request identity conflicts")
        return view(
            request, database_id, workspace_id, execution_id, previous["view_id"]
        )
    identity = secrets.token_hex(16)
    # Bound live control metadata, independently of total rows/previous sorts.
    tasks.enqueue("cleanup", identity, schedule_at=source["expires_at"])
    value = {
        key: source[key]
        for key in (
            "execution_id",
            "workspace_id",
            "database_id",
            "session_id",
            "login",
            "policy_version",
            "expires_at",
        )
    }
    value.update(
        kind="view",
        view_id=identity,
        state="queued",
        column_key=column_key,
        direction=direction,
        created_at=time.time(),
        dispatch_deadline=time.time() + jobs.DISPATCH_SECONDS,
        columns=source["columns"],
        row_count=None,
        elapsed_ms=None,
        error_code=None,
        manifest=None,
        claim=None,
        export_id=None,
        bytes=0,
    )
    selected = {"id": identity}

    def change(values):
        previous = values[store.key("request", request_id)]
        if previous:
            if previous.get("fingerprint") != fingerprint or not previous.get(
                "view_id"
            ):
                raise HTTPException(409, "Database request identity conflicts")
            selected["id"] = previous["view_id"]
            return {}
        current = values[store.key("execution", execution_id)]
        place = values[store.key("workspace", workspace_id)]
        session = values[store.key("session", source["session_id"])]
        policy = values["control:active_policy"]
        if (
            not current
            or current["state"] != "completed"
            or current["expires_at"] <= time.time()
            or not place
            or place["state"] != "active"
            or place["expires_at"] <= time.time()
            or not session
            or session["revoked_at"]
            or session["expires_at"] <= time.time()
            or not policy
            or policy.get("enabled") is not True
            or policy.get("version") != source["policy_version"]
            or policy.get("fingerprint") != jobs.policy_fingerprint()
        ):
            raise HTTPException(403, "Database permission changed")
        if len(current.get("view_ids", [])) >= MAX_VIEWS:
            raise HTTPException(429, "Database view budget is full")
        current.setdefault("view_ids", []).append(identity)
        control = jobs._coordinator(values["control:budget"])
        jobs._reserve(control, identity, source["login"], 0)
        return {
            store.key("view", identity): value,
            store.key("request", request_id): {
                "kind": "request",
                "request_id": request_id,
                "fingerprint": fingerprint,
                "view_id": identity,
                "expires_at": place["expires_at"],
            },
            store.key("execution", execution_id): current,
            "control:budget": control,
        }

    store.mutate(
        [
            ("view", identity),
            ("request", request_id),
            ("execution", execution_id),
            ("workspace", workspace_id),
            ("session", source["session_id"]),
            ("control", "active_policy"),
            ("control", "budget"),
        ],
        change,
    )
    if selected["id"] == identity:
        jobs._ensure_dispatch("view", identity)
    return view(request, database_id, workspace_id, execution_id, selected["id"])


class SortObjects:
    """Reserve every upload before it starts; release only confirmed deletions."""

    def __init__(self, context: jobs.RunContext):
        self.context = context
        self.identity = context.value["view_id"]

    def resize(self, delta: int) -> None:
        self.context.authorize(force=True)

        def change(values):
            control = jobs._coordinator(values["control:budget"])
            existing = control["reservations"].get(self.identity)
            if existing is None:
                raise DatabaseUnavailable("Database view reservation is unavailable")
            if existing["bytes"] + delta < 0:
                raise DatabaseUnavailable("Invalid database view accounting")
            jobs._reserve(
                control,
                self.identity,
                self.context.value["login"],
                max(0, existing["bytes"] + delta),
            )
            return {"control:budget": control}

        store.mutate([("control", "budget")], change)
        self.context.authorize(force=True)

    def write_run(self, name: str, pieces: Iterator[bytes]) -> dict:
        if not re.fullmatch(r"run-[0-9]{6,12}\.ndjson", name):
            raise DatabaseUnavailable("Invalid database sort run")
        chunks: list[dict] = []
        buffer = bytearray()

        def flush():
            if not buffer:
                return
            self.resize(len(buffer))
            reference = results.put(
                f"results/{self.identity}/sort-{name[:-7]}-{len(chunks):06d}.ndjson",
                bytes(buffer),
                "application/x-ndjson",
            )
            self.context.authorize()
            chunks.append(reference)
            buffer.clear()

        for piece in pieces:
            self.context.authorize()
            if (
                not isinstance(piece, bytes)
                or len(piece) > results.ROW_BYTES + 1024
                or not piece.endswith(b"\n")
            ):
                raise DatabaseUnavailable("Invalid database sort record")
            if len(buffer) + len(piece) > results.CHUNK_BYTES:
                flush()
            buffer.extend(piece)
        flush()
        return {"chunks": chunks, "bytes": sum(chunk["bytes"] for chunk in chunks)}

    def read_run(self, reference: dict) -> Iterator[bytes]:
        for chunk in reference["chunks"]:
            self.context.authorize()
            raw = results.read(chunk)
            self.context.authorize()
            for line in raw.splitlines(keepends=True):
                self.context.authorize()
                yield line

    def delete_run(self, reference: dict) -> None:
        for chunk in reference["chunks"]:
            self.context.authorize()
            results.delete(chunk)
            self.resize(-chunk["bytes"])


def run_sort(identity: str) -> None:
    value = jobs._claim("view", identity)
    if value is None:
        return
    context = jobs.RunContext(value, kind="view")
    objects = SortObjects(context)
    try:
        context.authorize(force=True)
        source = store.get("execution", value["execution_id"])
        if not source or source["state"] != "completed":
            raise DatabaseUnavailable("Database result is unavailable")
        saved = results.manifest(source["manifest"])
        from .database_sort import sort_rows

        writer = results.ResultWriter(
            identity, context.authorize, on_store=objects.resize
        )
        writer.on_columns(saved["columns"])
        with (
            jobs._permit("export", value["login"], authorize=context.authorize),
            context.watching(),
        ):
            sort_rows(
                saved["columns"],
                results.rows(saved, context.authorize),
                value["column_key"],
                value["direction"],
                authorize=context.authorize,
                write_run=objects.write_run,
                read_run=objects.read_run,
                delete_run=objects.delete_run,
                on_rows=writer.on_rows,
                deadline=context.started + jobs.WALL_SECONDS,
            )
            reference = writer.finish()
            if writer.row_count != saved["row_count"]:
                raise DatabaseUnavailable("Database view count differs")
            context.authorize(force=True)
            jobs._finish(
                identity,
                value["claim"],
                "completed",
                kind="view",
                changes={
                    "manifest": reference,
                    "bytes": writer.stored_bytes,
                    "row_count": writer.row_count,
                    "elapsed_ms": round((time.monotonic() - context.started) * 1000),
                },
            )
    except Exception as exc:
        from .database_console import DatabaseResourceExceeded

        error = (
            "RESOURCE_EXCEEDED"
            if isinstance(exc, DatabaseResourceExceeded)
            or isinstance(exc, HTTPException)
            and exc.status_code in {422, 429}
            else "QUERY_TIMEOUT"
            if time.monotonic() - context.started >= jobs.WALL_SECONDS
            else "SORT_FAILED"
        )
        jobs._finish(identity, value["claim"], "failed", kind="view", error=error)
    finally:
        jobs._worker_closed("view", identity, value["claim"])
    current = store.get("view", identity)
    if current and current["state"] != "completed":
        tasks.enqueue("cleanup", identity)
        cleanup_view(identity)


def delete_view(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str,
) -> dict:
    require_view(request, database_id, workspace_id, execution_id, view_id)

    def invalidate(values):
        current = values[store.key("view", view_id)]
        if current:
            current.update(
                state="purged",
                manifest=None,
                expires_at=min(current["expires_at"], time.time()),
            )
        return {store.key("view", view_id): current}

    store.mutate([("view", view_id)], invalidate)
    tasks.enqueue("cleanup", view_id)
    return {"purged": True}


def cleanup_view(
    identity: str, *, deadline: float | None = None, force: bool = False
) -> None:
    deadline = deadline if deadline is not None else time.monotonic() + 230
    jobs._check_cleanup_deadline(deadline)
    value = store.get("view", identity)
    jobs._check_cleanup_deadline(deadline)
    if not value or value.get("cleanup_done"):
        return
    if (
        not force
        and value["state"] not in {"failed", "cancelled", "expired", "purged"}
        and value["expires_at"] > time.time()
    ):
        return

    def invalidate(values):
        current = values[store.key("view", identity)]
        if current:
            current.update(
                state="purged"
                if force or current["state"] == "purged"
                else "expired"
                if current["expires_at"] <= time.time()
                else current["state"],
                manifest=None,
            )
        return {store.key("view", identity): current}

    store.mutate([("view", identity)], invalidate)
    jobs._check_cleanup_deadline(deadline)
    exported = [
        item
        for item in store.find("export", "execution_id", value["execution_id"], limit=8)
        if item.get("source_view_id") == identity and item["state"] != "completed"
    ]
    jobs._check_cleanup_deadline(deadline)
    for item in exported:
        jobs._check_cleanup_deadline(deadline)
        store.mutate(
            [("export", item["export_id"])],
            lambda values: {
                key: {**current, "state": "purged", "file": None} if current else None
                for key, current in values.items()
            },
        )
    if value.get("worker_deadline", 0) > time.time() or any(
        item.get("worker_deadline", 0) > time.time() for item in exported
    ):
        raise DatabaseUnavailable("Database view cleanup is pending")
    results.purge(identity, deadline=deadline)
    jobs._check_cleanup_deadline(deadline)
    jobs._release_storage(identity)
    for item in exported:
        jobs._check_cleanup_deadline(deadline)
        jobs._cleanup_export(item)
    jobs._check_cleanup_deadline(deadline)
    references = [("view", identity), ("execution", value["execution_id"])] + [
        ("export", item["export_id"]) for item in exported
    ]

    def cleaned(values):
        updates = {
            key: {
                **current,
                "cleanup_done": True,
                "expires_at": time.time() + jobs.TTL_SECONDS
                if current["state"] in {"purged", "expired"}
                else current["expires_at"],
                "manifest": None,
                "file": None,
                "columns": [],
            }
            for key, current in values.items()
            if current and key != store.key("execution", value["execution_id"])
        }
        parent = values[store.key("execution", value["execution_id"])]
        if parent:
            parent["view_ids"] = [
                item for item in parent.get("view_ids", []) if item != identity
            ]
            updates[store.key("execution", value["execution_id"])] = parent
        return updates

    store.mutate(references, cleaned)


def cleanup_children(execution_id: str, *, deadline: float) -> None:
    jobs._check_cleanup_deadline(deadline)
    parent = store.get("execution", execution_id)
    for identity in (parent or {}).get("view_ids", []):
        jobs._check_cleanup_deadline(deadline)
        cleanup_view(identity, deadline=deadline, force=True)


def sweep_views(deadline: float) -> None:
    jobs._check_cleanup_deadline(deadline)
    for value in store.find("view", "state", "running", limit=64):
        jobs._check_cleanup_deadline(deadline)
        if value.get("worker_deadline", 0) < time.time():
            jobs._finish(
                value["view_id"],
                value.get("claim"),
                "failed",
                kind="view",
                error="WORKER_LOST",
            )
            cleanup_view(value["view_id"], deadline=deadline)
    jobs._check_cleanup_deadline(deadline)
    for value in store.find("view", "expires_at", time.time(), limit=64):
        jobs._check_cleanup_deadline(deadline)
        if value.get("cleanup_done"):
            store.mutate(
                [("view", value["view_id"])], lambda values: {next(iter(values)): None}
            )
        else:
            try:
                cleanup_view(value["view_id"], deadline=deadline)
            except DatabaseUnavailable:
                continue
