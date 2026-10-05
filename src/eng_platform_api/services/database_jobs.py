"""Durable database executions; stored snapshots are the only paging/export source."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
import secrets
import threading
import time
from typing import Any, BinaryIO, Callable, cast

from fastapi import HTTPException, Request
from psycopg.conninfo import conninfo_to_dict

from ..config import config
from . import (
    database_job_store as store,
    database_registry,
    database_result_store as results,
    database_sessions,
    database_tasks,
)
from .database_registry import DatabaseUnavailable
from .database_sql import QueryRejected, validate_query

WALL_SECONDS = 240
WORKER_SECONDS = 270
DISPATCH_SECONDS = 300
TTL_SECONDS = 3600
USER_BYTES = 536_870_912
GLOBAL_BYTES = 2_147_483_648
TERMINAL = frozenset({"completed", "failed", "cancelled", "expired", "purged"})


def policy_fingerprint() -> str:
    """Safe deployment attestation; credentials and SQL are never included."""
    database_registry.databases()
    value = {
        "registry_sha256": config.databases.registry_sha256,
        "allowed_logins": sorted(config.databases.allowed_logins),
        "oauth_client_id": config.auth.github_client_id,
        "frontend_url": config.auth.frontend_url,
        "worker_service_account": config.databases.worker_service_account,
        "api_origin": config.databases.api_origin,
        "project_id": config.databases.project_id,
        "collection": config.databases.collection,
        "result_bucket": config.databases.result_bucket,
        "queue_location": config.databases.queue_location,
        "query_queue": config.databases.query_queue,
        "export_queue": config.databases.export_queue,
        "cleanup_queue": config.databases.cleanup_queue,
        "wall": WALL_SECONDS,
        "ttl": TTL_SECONDS,
        "snapshot_bytes": results.SNAPSHOT_BYTES,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def active_policy() -> str:
    from ..security import log_auth_configured

    settings = config.databases
    if (
        config.mock_mode
        or not config.databases.enabled
        or not config.databases.executions_enabled
        or not log_auth_configured()
        or not config.databases.policy_version
        or not all(
            (
                settings.project_id,
                settings.result_bucket,
                settings.query_queue,
                settings.export_queue,
                settings.cleanup_queue,
                settings.worker_service_account,
                settings.api_origin,
            )
        )
        or not settings.api_origin.startswith("https://")
    ):
        raise DatabaseUnavailable("Database executions are unavailable")
    version = config.databases.policy_version
    value = store.get("control", "active_policy")
    if (
        not value
        or value.get("enabled") is not True
        or value.get("version") != version
        or value.get("fingerprint") != policy_fingerprint()
    ):
        raise DatabaseUnavailable("Database execution authority is unavailable")
    return version


def _database(identity: str, login: str) -> database_registry.Database:
    for database in database_registry.databases():
        if database.id == identity and database.can_read(login):
            return database
    raise HTTPException(404, "Database not found")


def _session(request: Request) -> dict:
    from ..security import require_database_reader

    require_database_reader(request)
    active_policy()
    return database_sessions.require(request)


def _workspace(workspace_id: str, session: dict, database_id: str) -> dict:
    value = store.get("workspace", workspace_id)
    if (
        not value
        or value.get("session_id") != session["id"]
        or value.get("database_id") != database_id
    ):
        raise HTTPException(404, "Database workspace not found")
    if value.get("state") != "active" or value["expires_at"] <= time.time():
        raise HTTPException(410, "Database workspace is unavailable")
    if value.get("policy_version") != active_policy():
        raise HTTPException(403, "Database permission changed")
    _database(database_id, session["login"])
    return value


def workspace(request: Request, database_id: str, workspace_id: str) -> dict:
    session = _session(request)
    value = _workspace(workspace_id, session, database_id)
    return {
        name: value.get(name)
        for name in ("workspace_id", "database_id", "expires_at", "execution_id")
    }


def create_workspace(request: Request, database_id: str) -> dict:
    session = _session(request)
    _database(database_id, session["login"])
    identity = secrets.token_hex(16)
    value = {
        "kind": "workspace",
        "workspace_id": identity,
        "database_id": database_id,
        "session_id": session["id"],
        "login": session["login"],
        "policy_version": active_policy(),
        "state": "active",
        "created_at": time.time(),
        "expires_at": session["expires_at"],
        "execution_id": None,
    }

    def insert(values):
        current = values[store.key("session", session["id"])]
        policy = values["control:active_policy"]
        if (
            not current
            or current["revoked_at"]
            or current["expires_at"] <= time.time()
            or not policy
            or policy.get("enabled") is not True
            or policy.get("version") != value["policy_version"]
            or policy.get("fingerprint") != policy_fingerprint()
        ):
            raise HTTPException(403, "Database permission changed")
        identities = current.get("workspace_ids", [])
        if len(identities) >= 32:
            raise HTTPException(429, "Database workspace budget is full")
        current["workspace_ids"] = [*identities, identity]
        return {
            store.key("workspace", identity): value,
            store.key("session", session["id"]): current,
        }

    store.mutate(
        [
            ("workspace", identity),
            ("session", session["id"]),
            ("control", "active_policy"),
        ],
        insert,
    )
    _session(request)
    return {
        name: value.get(name)
        for name in ("workspace_id", "database_id", "expires_at", "execution_id")
    }


def _coordinator(value: dict | None) -> dict:
    return value or {"kind": "control", "permits": {}, "reservations": {}}


def _reserve(control: dict, identity: str, owner: str, size: int) -> None:
    reservations = control["reservations"]
    if identity not in reservations and len(reservations) >= 128:
        raise HTTPException(429, "Database result budget is full")
    others = [record for key, record in reservations.items() if key != identity]
    if (
        sum(record["bytes"] for record in others) + size > GLOBAL_BYTES
        or sum(record["bytes"] for record in others if record["owner"] == owner) + size
        > USER_BYTES
    ):
        raise HTTPException(429, "Database result storage budget is full")
    reservations[identity] = {"owner": owner, "bytes": size}


def create_execution(
    request: Request,
    database_id: str,
    workspace_id: str,
    statement: str,
    client_request_id: str,
) -> dict:
    session = _session(request)
    place = _workspace(workspace_id, session, database_id)
    database = _database(database_id, session["login"])
    validate_query(statement, database.schemas)
    request_id = hashlib.sha256(
        f"{workspace_id}:{client_request_id}".encode()
    ).hexdigest()
    fingerprint = hashlib.sha256(statement.encode()).hexdigest()
    existing = store.get("request", request_id)
    if existing:
        if existing.get("fingerprint") != fingerprint:
            raise HTTPException(409, "Database request identity conflicts")
        _ensure_dispatch("execution", existing["execution_id"])
        return execution(request, database_id, workspace_id, existing["execution_id"])
    identity = secrets.token_hex(16)
    # Schedule cleanup before the first private upload: a process crash between
    # GCS staging and the Firestore CAS still has a durable cleanup trigger.
    database_tasks.enqueue("cleanup", identity, schedule_at=time.time() + TTL_SECONDS)

    def reserve_staging(values):
        control = _coordinator(values["control:budget"])
        _reserve(control, identity, session["login"], results.SNAPSHOT_BYTES)
        return {"control:budget": control}

    store.mutate([("control", "budget")], reserve_staging)
    try:
        input_reference = results.stage_sql(identity, statement)
    except Exception:
        results.purge(identity)
        _release_storage(identity)
        raise
    now = time.time()
    value = {
        "kind": "execution",
        "execution_id": identity,
        "workspace_id": workspace_id,
        "database_id": database_id,
        "session_id": session["id"],
        "login": session["login"],
        "policy_version": place["policy_version"],
        "state": "queued",
        "created_at": now,
        "dispatch_deadline": now + DISPATCH_SECONDS,
        "expires_at": min(place["expires_at"], now + TTL_SECONDS),
        "input": input_reference,
        "columns": [],
        "row_count": None,
        "elapsed_ms": None,
        "manifest": None,
        "error_code": None,
        "claim": None,
        "export_id": None,
    }
    selected = {"id": identity}

    def change(values):
        current = values[store.key("workspace", workspace_id)]
        record = values[store.key("request", request_id)]
        if (
            not current
            or current["state"] != "active"
            or current["expires_at"] <= time.time()
        ):
            raise HTTPException(410, "Database workspace is unavailable")
        live_session = values[store.key("session", session["id"])]
        live_policy = values["control:active_policy"]
        if (
            not live_session
            or live_session["revoked_at"]
            or live_session["expires_at"] <= time.time()
            or not live_policy
            or live_policy.get("enabled") is not True
            or live_policy.get("version") != place["policy_version"]
            or live_policy.get("fingerprint") != policy_fingerprint()
        ):
            raise HTTPException(403, "Database permission changed")
        if record:
            if record["fingerprint"] != fingerprint:
                raise HTTPException(409, "Database request identity conflicts")
            selected["id"] = record["execution_id"]
            return {}
        if current.get("execution_count", 0) >= 256:
            raise HTTPException(429, "Database workspace execution budget is full")
        control = _coordinator(values["control:budget"])
        _reserve(control, identity, session["login"], results.SNAPSHOT_BYTES)
        current["execution_id"] = identity
        current["execution_count"] = current.get("execution_count", 0) + 1
        return {
            "control:budget": control,
            store.key("execution", identity): value,
            store.key("workspace", workspace_id): current,
            store.key("request", request_id): {
                "kind": "request",
                "request_id": request_id,
                "workspace_id": workspace_id,
                "execution_id": identity,
                "fingerprint": fingerprint,
                "expires_at": place["expires_at"],
            },
        }

    try:
        store.mutate(
            [
                ("control", "budget"),
                ("workspace", workspace_id),
                ("request", request_id),
                ("execution", identity),
                ("session", session["id"]),
                ("control", "active_policy"),
            ],
            change,
        )
    except Exception:
        # An uncertain Firestore acknowledgement may have created a queued row;
        # its outbox owns the input/reservation. Do not delete a possible owner.
        if store.get("execution", identity) is None:
            results.delete(input_reference)
            _release_storage(identity)
        raise
    if selected["id"] != identity:
        results.delete(input_reference)
        _release_storage(identity)
        return execution(request, database_id, workspace_id, selected["id"])
    try:
        database_tasks.enqueue("query", identity)
    except DatabaseUnavailable:
        _finish(identity, None, "failed", error="DISPATCH_FAILED")
        current = store.get("execution", identity)
        if current and current["state"] != "running":
            results.purge(identity)
            _release_storage(identity)
        raise
    return execution(request, database_id, workspace_id, identity)


def _public_execution(value: dict) -> dict:
    public = {
        name: value.get(name)
        for name in (
            "execution_id",
            "workspace_id",
            "database_id",
            "columns",
            "row_count",
            "elapsed_ms",
            "expires_at",
            "error_code",
        )
    }
    public["status"] = value["state"]
    return public


def _execution(
    request: Request, database_id: str, workspace_id: str, execution_id: str
) -> dict:
    session = _session(request)
    _workspace(workspace_id, session, database_id)
    value = store.get("execution", execution_id)
    if (
        not value
        or value.get("session_id") != session["id"]
        or value.get("workspace_id") != workspace_id
        or value.get("database_id") != database_id
    ):
        raise HTTPException(404, "Database execution not found")
    if value["expires_at"] <= time.time() or value["state"] in {"purged", "expired"}:
        raise HTTPException(410, "Database result is unavailable")
    if value["policy_version"] != active_policy():
        raise HTTPException(403, "Database permission changed")
    return value


def execution(
    request: Request, database_id: str, workspace_id: str, execution_id: str
) -> dict:
    return _public_execution(
        _execution(request, database_id, workspace_id, execution_id)
    )


def _role(database: database_registry.Database) -> str:
    try:
        values = conninfo_to_dict(os.environ.get(database.dsn_env, ""))
        identity = "|".join(
            str(values.get(field) or "") for field in ("host", "port", "user")
        )
        if not values.get("user") or not values.get("host"):
            raise ValueError("Role identity is absent")
        return hashlib.sha256(identity.encode()).hexdigest()
    except Exception:
        raise DatabaseUnavailable(
            "Database connection authority is unavailable"
        ) from None


@contextmanager
def _permit(
    kind: str,
    owner: str,
    *,
    role: str = "",
    authorize: Callable[[], None] | None = None,
):
    owner = owner.lower()
    token = secrets.token_hex(16)
    if authorize:
        authorize()

    def take(values):
        control = _coordinator(values["control:budget"])
        # The grace period outlives every HTTP/SQL deadline. An abandoned worker
        # cannot admit a replacement while its bounded operation may still run.
        permits = {
            key: value
            for key, value in control["permits"].items()
            if value["expires_at"] > time.time()
        }
        maximum = {"query": 2, "metadata": 2, "read": 2, "export": 1}[kind]
        same = [value for value in permits.values() if value["kind"] == kind]
        role_busy = role and any(
            value.get("role") == role
            for value in permits.values()
            if value["kind"] == kind
        )
        if (
            len(same) >= maximum
            or (
                kind == "query"
                and owner
                and any(value["owner"] == owner for value in same)
            )
            or role_busy
        ):
            raise HTTPException(
                429, "Database operation budget is busy", headers={"Retry-After": "2"}
            )
        permits[token] = {
            "kind": kind,
            "owner": owner,
            "role": role,
            "expires_at": time.time() + 330,
        }
        control["permits"] = permits
        return {"control:budget": control}

    store.mutate([("control", "budget")], take)
    try:
        if authorize:
            authorize()
        yield
    finally:

        def release(values):
            control = _coordinator(values["control:budget"])
            control["permits"].pop(token, None)
            return {"control:budget": control}

        store.mutate([("control", "budget")], release)


def connection_slot(
    database: database_registry.Database,
    kind: str = "metadata",
    owner: str = "",
    authorize: Callable[[], None] | None = None,
):
    active_policy()
    return _permit(kind, owner, role=_role(database), authorize=authorize)


def page(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    page_index: int,
    page_size: int,
    view_id: str | None = None,
) -> dict:
    value = _execution(request, database_id, workspace_id, execution_id)
    if view_id:
        from .database_views import require_view

        value = require_view(request, database_id, workspace_id, execution_id, view_id)
    if value["state"] != "completed" or not value["manifest"]:
        raise HTTPException(409, "Database result is not complete")

    def authorize():
        _execution(request, database_id, workspace_id, execution_id)
        if view_id:
            return require_view(
                request, database_id, workspace_id, execution_id, view_id
            )
        return _execution(request, database_id, workspace_id, execution_id)

    with _permit("read", value["login"], authorize=authorize):
        saved = results.manifest(value["manifest"])
        total_pages = math.ceil(saved["row_count"] / page_size)
        if page_index and page_index >= total_pages:
            raise HTTPException(422, "Database page is outside the result")
        rows = list(
            results.rows(
                saved, authorize, start=page_index * page_size, limit=page_size
            )
        )
        response = {
            "columns": saved["columns"],
            "rows": rows,
            "page_index": page_index,
            "page_size": page_size,
            "row_count": saved["row_count"],
            "total_pages": total_pages,
        }
        if view_id:
            response["view_id"] = view_id
        if len(json.dumps(response, ensure_ascii=True).encode()) > 8_388_608:
            raise DatabaseUnavailable("Database page exceeds the budget")
        authorize()
    authorize()
    return response


def _claim(kind: str, identity: str) -> dict | None:
    claimed: dict[str, Any] = {}

    def change(values):
        value = values[store.key(kind, identity)]
        if not value or value["state"] != "queued":
            return {}
        if value["expires_at"] <= time.time():
            value.update(state="expired", error_code="RESULT_EXPIRED")
        elif value.get("dispatch_deadline", 0) <= time.time():
            value.update(state="failed", error_code="DISPATCH_FAILED")
        else:
            value.update(
                state="running",
                claim=secrets.token_hex(16),
                started_at=time.time(),
                worker_deadline=time.time() + WORKER_SECONDS,
            )
            claimed.update(value)
        return {store.key(kind, identity): value}

    store.mutate([(kind, identity)], change)
    return claimed or None


def _ensure_dispatch(
    kind: str, identity: str, *, deadline: float | None = None
) -> None:
    value = store.get(kind, identity)
    if not value or value["state"] != "queued":
        return
    if value.get("dispatch_deadline", 0) <= time.time():
        _finish(identity, None, "failed", kind=kind, error="DISPATCH_FAILED")
        if kind == "execution":
            cleanup(identity, deadline=deadline)
        elif kind == "view":
            from .database_views import cleanup_view

            cleanup_view(identity, deadline=deadline)
        else:
            _cleanup_export(value)
        return
    database_tasks.enqueue(
        {"execution": "query", "view": "sort", "export": "export"}[kind], identity
    )


def _finish(
    identity: str,
    claim: str | None,
    state: str,
    *,
    kind: str = "execution",
    error: str | None = None,
    changes: dict | None = None,
) -> None:
    initial = store.get(kind, identity)
    if initial is None:
        return
    references = [(kind, identity), ("control", "budget")]
    if state == "completed":
        references += [
            ("workspace", initial["workspace_id"]),
            ("session", initial["session_id"]),
            ("control", "active_policy"),
        ]
        if kind in {"export", "view"}:
            references.append(("execution", initial["execution_id"]))
        if kind == "export" and initial.get("source_view_id"):
            references.append(("view", initial["source_view_id"]))
    fingerprint = policy_fingerprint() if state == "completed" else ""

    def change(values):
        value = values[store.key(kind, identity)]
        if (
            not value
            or value["state"] not in {"queued", "running"}
            or (claim is None and value["state"] == "running" and state == "failed")
            or (claim and value.get("claim") != claim)
        ):
            return {}
        if value["expires_at"] <= time.time():
            state_value = "expired"
        else:
            state_value = state
        if state_value == "completed":
            place = values[store.key("workspace", value["workspace_id"])]
            session = values[store.key("session", value["session_id"])]
            policy = values["control:active_policy"]
            source = (
                values.get(store.key("execution", value["execution_id"]))
                if kind in {"export", "view"}
                else value
            )
            if (
                not place
                or place["state"] != "active"
                or place["expires_at"] <= time.time()
                or not session
                or session["revoked_at"]
                or session["expires_at"] <= time.time()
                or not policy
                or policy.get("enabled") is not True
                or policy.get("version") != value["policy_version"]
                or policy.get("fingerprint") != fingerprint
                or not source
                or (
                    kind in {"export", "view"}
                    and (
                        source["state"] != "completed"
                        or source["expires_at"] <= time.time()
                    )
                )
                or (
                    kind == "export"
                    and value.get("source_view_id")
                    and (
                        not values.get(store.key("view", value["source_view_id"]))
                        or values[store.key("view", value["source_view_id"])]["state"]
                        != "completed"
                        or values[store.key("view", value["source_view_id"])][
                            "expires_at"
                        ]
                        <= time.time()
                    )
                )
            ):
                state_value = "cancelled"
        value.update(changes or {})
        value.update(state=state_value, error_code=error, input=None)
        if state_value == "completed" and kind == "execution":
            value["expires_at"] = time.time() + TTL_SECONDS
        if state_value != "completed":
            value.update(manifest=None, file=None, columns=[])
        control = _coordinator(values["control:budget"])
        reservation = control["reservations"].get(identity)
        if reservation:
            if state_value == "completed":
                reservation["bytes"] = value.get("bytes", 0)
        return {store.key(kind, identity): value, "control:budget": control}

    store.mutate(references, change)


def _release_storage(identity: str) -> None:
    def release(values):
        control = _coordinator(values["control:budget"])
        control["reservations"].pop(identity, None)
        return {"control:budget": control}

    store.mutate([("control", "budget")], release)


def _worker_closed(kind: str, identity: str, claim: str) -> None:
    def close(values):
        current = values[store.key(kind, identity)]
        if current and current.get("claim") == claim:
            current["worker_deadline"] = 0
            return {store.key(kind, identity): current}
        return {}

    store.mutate([(kind, identity)], close)


class RunContext:
    def __init__(self, value: dict, *, kind: str = "execution"):
        self.value = value
        self.kind = kind
        self.connection: Any = None
        self.stop = threading.Event()
        self.cancelled = threading.Event()
        self.started = time.monotonic()
        self.checked = 0.0
        self.lock = threading.Lock()

    def authorize(self, *, force: bool = False) -> None:
        if self.cancelled.is_set() or time.monotonic() - self.started >= WALL_SECONDS:
            raise HTTPException(409, "Database execution stopped")
        with self.lock:
            if not force and time.monotonic() - self.checked < 0.75:
                return
            value = self.value
            if active_policy() != value["policy_version"]:
                raise HTTPException(403, "Database permission changed")
            session = database_sessions.validate(value["session_id"], value["login"])
            _workspace(value["workspace_id"], session, value["database_id"])
            current = store.get(self.kind, value[f"{self.kind}_id"])
            if (
                not current
                or current["state"] != "running"
                or current.get("claim") != value["claim"]
                or current["expires_at"] <= time.time()
            ):
                raise HTTPException(409, "Database execution stopped")
            if self.kind in {"export", "view"}:
                source = store.get("execution", value["execution_id"])
                if (
                    not source
                    or source["state"] != "completed"
                    or source["expires_at"] <= time.time()
                ):
                    raise HTTPException(409, "Database result is unavailable")
                if value.get("source_view_id"):
                    selected = store.get("view", value["source_view_id"])
                    if (
                        not selected
                        or selected["state"] != "completed"
                        or selected["expires_at"] <= time.time()
                    ):
                        raise HTTPException(409, "Database view is unavailable")
            self.checked = time.monotonic()

    def on_connection(self, connection):
        self.connection = connection

    def watch(self):
        while not self.stop.wait(1):
            try:
                self.authorize(force=True)
            except Exception:
                self.cancelled.set()
                if self.connection is not None:
                    try:
                        self.connection.cancel_safe(timeout=2)
                    except Exception:
                        pass
                return

    @contextmanager
    def watching(self):
        thread = threading.Thread(target=self.watch, daemon=True)
        thread.start()
        try:
            yield
        finally:
            self.stop.set()
            thread.join(timeout=3)


def run_query(identity: str) -> None:
    value = _claim("execution", identity)
    if value is None:
        return
    context = RunContext(value)
    writer = results.ResultWriter(identity, context.authorize)
    try:
        context.authorize(force=True)
        database = _database(value["database_id"], value["login"])
        statement = results.take_sql(value["input"])
        from .database_console import stream_query

        with context.watching():
            result = stream_query(
                database,
                statement,
                context.authorize,
                writer.on_columns,
                writer.on_rows,
                on_connection=context.on_connection,
                admission=connection_slot(
                    database, "query", value["login"], context.authorize
                ),
                timeout_seconds=max(
                    0.001, WALL_SECONDS - (time.monotonic() - context.started)
                ),
            )
        reference = writer.finish()
        context.authorize(force=True)
        _finish(
            identity,
            value["claim"],
            "completed",
            changes={
                "manifest": reference,
                "columns": writer.columns,
                "row_count": writer.row_count,
                "bytes": writer.stored_bytes,
                "elapsed_ms": result["elapsed_ms"],
            },
        )
        final_value = store.get("execution", identity)
        if not final_value or final_value["state"] != "completed":
            results.purge(identity)
            _release_storage(identity)
    except Exception as exc:
        if isinstance(exc, QueryRejected):
            error = "QUERY_REJECTED"
        elif isinstance(exc, HTTPException) and exc.status_code == 429:
            error = "RESOURCE_BUSY"
        elif (
            isinstance(exc, HTTPException)
            and exc.status_code == 422
            or exc.__class__.__name__ == "DatabaseResourceExceeded"
        ):
            error = "RESOURCE_EXCEEDED"
        elif (
            context.cancelled.is_set()
            or time.monotonic() - context.started >= WALL_SECONDS
        ):
            error = "QUERY_STOPPED"
        else:
            error = "QUERY_FAILED"
        _finish(identity, value["claim"], "failed", error=error)
        results.purge(identity)
        _release_storage(identity)
    finally:
        _worker_closed("execution", identity, value["claim"])
    # Scheduling failure cannot turn a published capture into a partial result.
    # The periodic sweeper provides the independent cleanup path.
    final_value = store.get("execution", identity)
    if final_value and final_value["state"] == "completed":
        database_tasks.enqueue(
            "cleanup", identity, schedule_at=final_value["expires_at"]
        )


def cancel(
    request: Request, database_id: str, workspace_id: str, execution_id: str
) -> dict:
    value = _execution(request, database_id, workspace_id, execution_id)
    _finish(execution_id, value.get("claim"), "cancelled", error="QUERY_CANCELLED")
    database_tasks.enqueue("cleanup", execution_id)
    return execution(request, database_id, workspace_id, execution_id)


def create_export(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    view_id: str | None = None,
) -> dict:
    source = _execution(request, database_id, workspace_id, execution_id)
    source_kind, source_id = "execution", execution_id
    if view_id:
        from .database_views import require_view

        source = require_view(request, database_id, workspace_id, execution_id, view_id)
        source_kind, source_id = "view", view_id
    if source["state"] != "completed":
        raise HTTPException(409, "Database result is not complete")
    if source.get("export_id"):
        previous = store.get("export", source["export_id"])
        if previous and previous["state"] in {"queued", "running", "completed"}:
            _ensure_dispatch("export", previous["export_id"])
            return export(
                request, database_id, workspace_id, execution_id, source["export_id"]
            )
        if previous:
            _cleanup_export(previous)
    if len(store.find("export", "execution_id", execution_id, limit=8)) >= 8:
        raise HTTPException(429, "Database export retry budget is full")
    identity = secrets.token_hex(16)
    value = {
        "kind": "export",
        "export_id": identity,
        "execution_id": execution_id,
        "workspace_id": workspace_id,
        "database_id": database_id,
        "session_id": source["session_id"],
        "login": source["login"],
        "policy_version": source["policy_version"],
        "state": "queued",
        "created_at": time.time(),
        "dispatch_deadline": time.time() + DISPATCH_SECONDS,
        "expires_at": source["expires_at"],
        "error_code": None,
        "file": None,
        "claim": None,
    }
    if view_id:
        value["source_view_id"] = view_id
    selected = {"id": identity}

    def change(values):
        current = values[store.key(source_kind, source_id)]
        if (
            not current
            or current["state"] != "completed"
            or current["expires_at"] <= time.time()
        ):
            raise HTTPException(409, "Database result is unavailable")
        if current.get("export_id") and current["export_id"] != source.get("export_id"):
            selected["id"] = current["export_id"]
            return {}
        live_session = values[store.key("session", source["session_id"])]
        place = values[store.key("workspace", workspace_id)]
        policy = values["control:active_policy"]
        if (
            not live_session
            or live_session["revoked_at"]
            or live_session["expires_at"] <= time.time()
            or not place
            or place["state"] != "active"
            or place["expires_at"] <= time.time()
            or not policy
            or policy.get("enabled") is not True
            or policy.get("version") != source["policy_version"]
            or policy.get("fingerprint") != policy_fingerprint()
        ):
            raise HTTPException(403, "Database permission changed")
        control = _coordinator(values["control:budget"])
        _reserve(control, identity, source["login"], results.EXPORT_BYTES)
        current["export_id"] = identity
        return {
            "control:budget": control,
            store.key(source_kind, source_id): current,
            store.key("export", identity): value,
        }

    store.mutate(
        [
            (source_kind, source_id),
            ("export", identity),
            ("control", "budget"),
            ("session", source["session_id"]),
            ("workspace", workspace_id),
            ("control", "active_policy"),
        ],
        change,
    )
    if selected["id"] == identity:
        try:
            database_tasks.enqueue("export", identity)
        except DatabaseUnavailable:
            _finish(identity, None, "failed", kind="export", error="DISPATCH_FAILED")
            _cleanup_export(value)
            raise
    return export(request, database_id, workspace_id, execution_id, selected["id"])


def _export(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    export_id: str,
) -> dict:
    _execution(request, database_id, workspace_id, execution_id)
    value = store.get("export", export_id)
    if (
        not value
        or value["execution_id"] != execution_id
        or value["workspace_id"] != workspace_id
    ):
        raise HTTPException(404, "Database export not found")
    if value.get("source_view_id") and value["state"] in {"queued", "running"}:
        from .database_views import require_view

        require_view(
            request, database_id, workspace_id, execution_id, value["source_view_id"]
        )
    if value["expires_at"] <= time.time() or value["state"] in {"expired", "purged"}:
        raise HTTPException(410, "Database export is unavailable")
    return value


def export(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    export_id: str,
) -> dict:
    value = _export(request, database_id, workspace_id, execution_id, export_id)
    path = f"/api/databases/{database_id}/workspaces/{workspace_id}/executions/{execution_id}/exports/{export_id}/file"
    public = {
        "export_id": export_id,
        "status": value["state"],
        "expires_at": value["expires_at"],
        "error_code": value["error_code"],
        "download_path": path if value["state"] == "completed" else None,
    }
    if value.get("source_view_id"):
        public["source_view_id"] = value["source_view_id"]
    return public


class _BoundedSink:
    def __init__(self, sink, authorize):
        self.sink = sink
        self.authorize = authorize
        self.bytes = 0

    def write(self, value):
        self.authorize()
        if self.bytes + len(value) > results.EXPORT_BYTES:
            raise HTTPException(422, "Database export exceeds the resource budget")
        count = self.sink.write(value)
        self.bytes += len(value)
        return count

    def tell(self):
        return self.bytes

    def flush(self):
        return None


def run_export(identity: str) -> None:
    value = _claim("export", identity)
    if value is None:
        return
    context = RunContext(value, kind="export")
    try:
        context.authorize(force=True)
        source = store.get(
            "view" if value.get("source_view_id") else "execution",
            value.get("source_view_id") or value["execution_id"],
        )
        if not source or source["state"] != "completed":
            raise DatabaseUnavailable("Database result is unavailable")
        saved = results.manifest(source["manifest"])
        from .database_xlsx import write_xlsx

        with (
            _permit("export", value["login"], authorize=context.authorize),
            context.watching(),
        ):
            with results.export_sink(value["execution_id"], identity) as sink:
                output = write_xlsx(
                    cast(BinaryIO, _BoundedSink(sink, context.authorize)),
                    saved["columns"],
                    results.rows(saved, context.authorize),
                    context.authorize,
                    timeout_seconds=max(
                        0.001, WALL_SECONDS - (time.monotonic() - context.started)
                    ),
                )
            reference = results.export_reference(value["execution_id"], identity)
            context.authorize(force=True)
            if output["row_count"] != saved["row_count"]:
                raise DatabaseUnavailable("Database export row count differs")
            _finish(
                identity,
                value["claim"],
                "completed",
                kind="export",
                changes={"file": reference, "bytes": reference["bytes"]},
            )
    except Exception as exc:
        error = (
            "RESOURCE_EXCEEDED"
            if (isinstance(exc, HTTPException) and exc.status_code == 422)
            or exc.__class__.__name__ == "DatabaseResourceExceeded"
            else "EXPORT_FAILED"
        )
        _finish(identity, value["claim"], "failed", kind="export", error=error)
    finally:
        _worker_closed("export", identity, value["claim"])
    final_value = store.get("export", identity)
    if final_value and final_value["state"] != "completed":
        _cleanup_export(final_value)


def _cleanup_export(value: dict) -> None:
    current = store.get("export", value["export_id"])
    if current is None:
        return
    value = current
    if (
        value["state"] in {"queued", "running", "completed"}
        and value["expires_at"] > time.time()
    ):
        raise DatabaseUnavailable("Database export cleanup is pending")
    if value.get("worker_deadline", 0) > time.time():
        raise DatabaseUnavailable("Database export cleanup is pending")
    results.purge_export(value["execution_id"], value["export_id"])
    _release_storage(value["export_id"])

    def cleaned(values):
        current = values[store.key("export", value["export_id"])]
        if current:
            current.update(file=None, artifact_cleaned=True, worker_deadline=0)
            if current["expires_at"] <= time.time() or current["state"] in {
                "expired",
                "purged",
            }:
                current.update(
                    state="expired",
                    cleanup_done=True,
                    expires_at=time.time() + TTL_SECONDS,
                )
        return {store.key("export", value["export_id"]): current}

    store.mutate([("export", value["export_id"])], cleaned)


def open_download(
    request: Request,
    database_id: str,
    workspace_id: str,
    execution_id: str,
    export_id: str,
):
    value = _export(request, database_id, workspace_id, execution_id, export_id)
    if value["state"] != "completed" or not value["file"]:
        raise HTTPException(409, "Database export is not complete")

    started = time.monotonic()

    def authorize():
        if time.monotonic() - started >= WORKER_SECONDS:
            raise HTTPException(409, "Database download deadline exceeded")
        return _export(request, database_id, workspace_id, execution_id, export_id)

    permit = _permit("read", value["login"], authorize=authorize)
    permit.__enter__()

    def stream():
        try:
            yield from results.download(value["file"], authorize)
        finally:
            permit.__exit__(None, None, None)

    return stream(), value["file"]["bytes"]


def _check_cleanup_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DatabaseUnavailable("Database cleanup is pending")


def purge_workspace_id(workspace_id: str, *, deadline: float | None = None) -> None:
    deadline = deadline if deadline is not None else time.monotonic() + 230
    _check_cleanup_deadline(deadline)

    def change(values):
        value = values[store.key("workspace", workspace_id)]
        if not value:
            return {}
        value.update(state="purged", expires_at=min(value["expires_at"], time.time()))
        return {store.key("workspace", workspace_id): value}

    store.mutate([("workspace", workspace_id)], change)
    _check_cleanup_deadline(deadline)
    for value in store.find("execution", "workspace_id", workspace_id, limit=256):
        _check_cleanup_deadline(deadline)
        if value.get("cleanup_done"):
            continue
        identity = value["execution_id"]

        def invalidate(values, eid=identity):
            current = values[store.key("execution", eid)]
            control = _coordinator(values["control:budget"])
            if current:
                current.update(state="purged", columns=[], manifest=None, input=None)
            return {store.key("execution", eid): current, "control:budget": control}

        store.mutate([("execution", identity), ("control", "budget")], invalidate)
        _check_cleanup_deadline(deadline)
        for exported in store.find("export", "execution_id", identity, limit=8):
            _check_cleanup_deadline(deadline)

            def remove(values, xid=exported["export_id"]):
                current = values[store.key("export", xid)]
                control = _coordinator(values["control:budget"])
                if current:
                    current.update(state="purged", file=None)
                return {store.key("export", xid): current, "control:budget": control}

            store.mutate(
                [("export", exported["export_id"]), ("control", "budget")], remove
            )
        _check_cleanup_deadline(deadline)
        current = store.get("execution", identity)
        _check_cleanup_deadline(deadline)
        exported_values = store.find("export", "execution_id", identity, limit=8)
        if (
            current
            and current.get("worker_deadline", 0) > time.time()
            or any(
                item.get("worker_deadline", 0) > time.time() for item in exported_values
            )
        ):
            raise DatabaseUnavailable("Database worker cleanup is pending")
        from .database_views import cleanup_children

        cleanup_children(identity, deadline=deadline)
        results.purge(identity, deadline=deadline)
        _check_cleanup_deadline(deadline)
        _release_storage(identity)
        for exported in exported_values:
            _check_cleanup_deadline(deadline)
            _release_storage(exported["export_id"])
        _check_cleanup_deadline(deadline)
        _cleaned_execution(identity, exported_values)

    def tombstone(values):
        current = values[store.key("workspace", workspace_id)]
        if current:
            current.update(cleanup_done=True, expires_at=time.time() + TTL_SECONDS)
        return {store.key("workspace", workspace_id): current}

    _check_cleanup_deadline(deadline)
    store.mutate([("workspace", workspace_id)], tombstone)


def _cleaned_execution(identity: str, exported: list[dict]) -> None:
    references = [("execution", identity)] + [
        ("export", item["export_id"]) for item in exported
    ]

    def change(values):
        updates = {}
        for label, current in values.items():
            if current:
                current.update(
                    cleanup_done=True,
                    expires_at=time.time() + TTL_SECONDS,
                    manifest=None,
                    input=None,
                    columns=[],
                    file=None,
                    state="purged" if current["state"] == "purged" else "expired",
                )
                updates[label] = current
        return updates

    store.mutate(references, change)


def purge_workspace(request: Request, database_id: str, workspace_id: str) -> dict:
    session = _session(request)
    value = store.get("workspace", workspace_id)
    if (
        not value
        or value.get("session_id") != session["id"]
        or value.get("database_id") != database_id
    ):
        raise HTTPException(404, "Database workspace not found")

    def invalidate(values):
        current = values[store.key("workspace", workspace_id)]
        current.update(
            state="purged", expires_at=min(current["expires_at"], time.time())
        )
        return {store.key("workspace", workspace_id): current}

    store.mutate([("workspace", workspace_id)], invalidate)
    database_tasks.enqueue("cleanup", workspace_id)
    return {"workspace_id": workspace_id, "status": "purged"}


def purge_session(session_id: str) -> None:
    for value in store.find("workspace", "session_id", session_id, limit=64):
        identity = value["workspace_id"]

        def invalidate(values):
            current = values[store.key("workspace", identity)]
            if current:
                current.update(
                    state="purged", expires_at=min(current["expires_at"], time.time())
                )
            return {store.key("workspace", identity): current}

        store.mutate([("workspace", identity)], invalidate)
        database_tasks.enqueue("cleanup", identity)


def cleanup(workspace_id: str, *, deadline: float | None = None) -> None:
    deadline = deadline if deadline is not None else time.monotonic() + 230
    _check_cleanup_deadline(deadline)
    value = store.get("workspace", workspace_id)
    if value and (value["state"] == "purged" or value["expires_at"] <= time.time()):
        purge_workspace_id(workspace_id, deadline=deadline)
    if value is None:
        _check_cleanup_deadline(deadline)
        execution_value = store.get("execution", workspace_id)
        if execution_value is None:
            from .database_views import cleanup_view

            if store.get("view", workspace_id):
                cleanup_view(workspace_id, deadline=deadline)
                return
            # Covers a staging process that died before creating its control row.
            results.purge(workspace_id, deadline=deadline)
            _check_cleanup_deadline(deadline)
            _release_storage(workspace_id)
            return
        if execution_value and (
            execution_value["expires_at"] <= time.time()
            or execution_value["state"] in {"failed", "cancelled", "expired", "purged"}
        ):
            # Running owners may still be closing/uploading; retain reservation
            # until the bounded worker deadline, then remove any late object.
            if execution_value.get("worker_deadline", 0) > time.time():
                raise DatabaseUnavailable("Database worker cleanup is pending")
            _check_cleanup_deadline(deadline)
            exported_values = store.find(
                "export", "execution_id", workspace_id, limit=8
            )
            if any(
                item.get("worker_deadline", 0) > time.time() for item in exported_values
            ):
                raise DatabaseUnavailable("Database export cleanup is pending")

            def expire(values):
                current = values[store.key("execution", workspace_id)]
                if current:
                    current.update(
                        state="expired" if current["state"] != "purged" else "purged",
                        manifest=None,
                        input=None,
                        columns=[],
                    )
                return {store.key("execution", workspace_id): current}

            _check_cleanup_deadline(deadline)
            store.mutate([("execution", workspace_id)], expire)
            from .database_views import cleanup_children

            cleanup_children(workspace_id, deadline=deadline)
            results.purge(workspace_id, deadline=deadline)
            _check_cleanup_deadline(deadline)
            _release_storage(workspace_id)
            for exported in exported_values:
                _check_cleanup_deadline(deadline)
                _release_storage(exported["export_id"])
            _check_cleanup_deadline(deadline)
            _cleaned_execution(workspace_id, exported_values)


def sweep() -> None:
    deadline = time.monotonic() + 230
    from .database_views import sweep_views

    sweep_views(deadline)
    for kind in ("execution", "export", "view"):
        for value in store.find(kind, "state", "queued", limit=64):
            if time.monotonic() >= deadline:
                return
            try:
                _ensure_dispatch(kind, value[f"{kind}_id"], deadline=deadline)
            except DatabaseUnavailable:
                continue
    for kind in ("request", "session"):
        for value in store.find(kind, "expires_at", time.time(), limit=64):
            if time.monotonic() >= deadline:
                return
            identity = value.get("id") if kind == "session" else value.get("request_id")
            if identity:
                store.mutate(
                    [(kind, identity)], lambda values: {next(iter(values)): None}
                )
    for value in store.find("workspace", "expires_at", time.time(), limit=64):
        if time.monotonic() >= deadline:
            return
        if value.get("cleanup_done"):

            def remove_workspace(values):
                session = values[store.key("session", value["session_id"])]
                updates: dict[str, Any] = {
                    store.key("workspace", value["workspace_id"]): None
                }
                if session:
                    session["workspace_ids"] = [
                        item
                        for item in session.get("workspace_ids", [])
                        if item != value["workspace_id"]
                    ]
                    updates[store.key("session", value["session_id"])] = session
                return updates

            store.mutate(
                [
                    ("workspace", value["workspace_id"]),
                    ("session", value["session_id"]),
                ],
                remove_workspace,
            )
        else:
            try:
                purge_workspace_id(value["workspace_id"], deadline=deadline)
            except DatabaseUnavailable:
                continue
    for value in store.find("execution", "expires_at", time.time(), limit=64):
        if time.monotonic() >= deadline:
            return
        if value.get("cleanup_done"):
            store.mutate(
                [("execution", value["execution_id"])],
                lambda values: {next(iter(values)): None},
            )
        else:
            try:
                cleanup(value["execution_id"], deadline=deadline)
            except DatabaseUnavailable:
                continue
    for kind in ("execution", "export"):
        for value in store.find(kind, "state", "running", limit=64):
            if time.monotonic() >= deadline:
                return
            if value.get("worker_deadline", 0) < time.time():
                _finish(
                    value[f"{kind}_id"],
                    value.get("claim"),
                    "failed",
                    kind=kind,
                    error="WORKER_LOST",
                )
                if kind == "execution":
                    results.purge(value["execution_id"], deadline=deadline)
                    _release_storage(value["execution_id"])
                else:
                    _cleanup_export(value)
    for value in store.find("export", "expires_at", time.time(), limit=64):
        if time.monotonic() >= deadline:
            return
        if value.get("cleanup_done"):
            store.mutate(
                [("export", value["export_id"])],
                lambda values: {next(iter(values)): None},
            )
        else:
            try:
                _cleanup_export(value)
            except DatabaseUnavailable:
                continue
