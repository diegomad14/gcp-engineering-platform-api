"""Give pytest-xdist workers disposable, loopback-only FND and Smarti PostgreSQL databases.

Load from outside the tested checkout with ``pytest -p postgres_workers`` so the
release source and its coverage denominator remain unchanged. The controller
owns database creation and cleanup, including databases of crashed workers.
WM keeps its required ``wm_test`` database: those tests already create and drop
a UUID-named schema per case, and explicitly assert the database name.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
from urllib.parse import quote, urlencode, urlsplit

import pytest

DSN_VARIABLES = (
    "WM_TEST_POSTGRES_DSN",
    "FND_TEST_POSTGRES_DSN",
    "SMARTI_TEST_POSTGRES_URL",
)
WORKER_DATABASE_VARIABLES = ("FND_TEST_POSTGRES_DSN", "SMARTI_TEST_POSTGRES_URL")
STATE_ATTRIBUTE = "_fallback_postgres_databases"
WORKER_INPUT_KEY = "fallback_postgres_dsns"


@dataclass
class Driver:
    connect: Any
    parse: Any
    sql: Any


def load_driver() -> Driver:
    try:
        import psycopg2
        from psycopg2 import extensions, sql

        return Driver(psycopg2.connect, extensions.parse_dsn, sql)
    except ImportError:
        import psycopg
        from psycopg import conninfo, sql

        return Driver(psycopg.connect, conninfo.conninfo_to_dict, sql)


def local_parameters(
    dsn: str, driver: Driver, *, plain_url: bool = False
) -> dict[str, str]:
    if plain_url:
        try:
            parsed = urlsplit(dsn)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid disposable Smarti PostgreSQL URL") from exc
        if (
            parsed.scheme != "postgresql"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or port not in {None, 5432}
            or parsed.path != "/smarti_test"
            or "?" in dsn
            or "#" in dsn
            or any(character.isspace() for character in dsn)
        ):
            raise ValueError(
                "Smarti tests require a plain loopback PostgreSQL URL for smarti_test"
            )
    try:
        parameters = driver.parse(dsn)
    except Exception as exc:
        raise ValueError("Invalid disposable PostgreSQL DSN") from exc
    if "service" in parameters:
        raise ValueError("PostgreSQL service aliases are not disposable loopback DSNs")
    for key in ("host", "hostaddr"):
        host = parameters.get(key)
        if host is None and key == "hostaddr":
            continue
        if host == "localhost":
            host = "127.0.0.1"
            parameters[key] = host
        try:
            local = ipaddress.ip_address(host or "").is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError(
                "Disposable PostgreSQL DSNs require an explicit loopback host"
            )
    if not parameters.get("dbname"):
        raise ValueError("Disposable PostgreSQL DSNs require an explicit database")
    # Prevent an inherited PGHOSTADDR from redirecting a loopback hostname.
    parameters.setdefault("hostaddr", parameters["host"])
    parameters["connect_timeout"] = "5"
    return parameters


def database_dsn(
    parameters: dict[str, str], database: str, *, plain_url: bool = False
) -> str:
    """Keep URI form: the target application's PGRepository parses URLs."""
    parameters = parameters.copy()
    parameters.pop("dbname")
    host = parameters.pop("host")
    if ":" in host:
        host = f"[{host}]"
    port = parameters.pop("port", "5432")
    username = parameters.pop("user", "")
    password = parameters.pop("password", "")
    credentials = quote(username, safe="")
    if password:
        credentials += ":" + quote(password, safe="")
    if credentials:
        credentials += "@"
    uri = f"postgresql://{credentials}{host}:{port}/{quote(database, safe='')}"
    # Smarti consumes a plain URL; the trusted controller still connects with
    # explicit hostaddr and timeout. WM/FND retain their existing URI options.
    return uri if plain_url else f"{uri}?{urlencode(parameters)}"


class WorkerDatabases:
    def __init__(self, environ: Any, driver: Driver | None = None) -> None:
        self.driver = driver or load_driver()
        # Validate every destination before creating anything.
        self.sources = {
            name: local_parameters(
                environ[name], self.driver, plain_url=name == "SMARTI_TEST_POSTGRES_URL"
            )
            for name in DSN_VARIABLES
            if environ.get(name)
        }
        if len(self.sources) != len(DSN_VARIABLES):
            raise ValueError(
                "WM_TEST_POSTGRES_DSN, FND_TEST_POSTGRES_DSN and SMARTI_TEST_POSTGRES_URL are required"
            )
        if self.sources["WM_TEST_POSTGRES_DSN"]["dbname"] != "wm_test":
            raise ValueError("WM tests require their schema-isolated wm_test database")
        self.run_id = uuid4().hex
        self.created: dict[str, dict[str, str]] = {}
        self.workers: dict[str, dict[str, str]] = {}

    def execute(self, parameters: dict[str, str], statement: Any) -> None:
        admin = dict(parameters, dbname="postgres")
        connection = self.driver.connect(**admin)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(statement)
        finally:
            connection.close()

    def allocate(self, worker_id: str) -> dict[str, str]:
        if worker_id in self.workers:
            return self.workers[worker_id].copy()
        safe_worker = re.sub(r"[^a-zA-Z0-9_]", "_", worker_id)[:12]
        assigned = {
            "WM_TEST_POSTGRES_DSN": database_dsn(
                self.sources["WM_TEST_POSTGRES_DSN"], "wm_test"
            )
        }
        sql = self.driver.sql
        suffix = f"{self.run_id}_{safe_worker}_{uuid4().hex[:8]}"
        try:
            for name in WORKER_DATABASE_VARIABLES:
                parameters = self.sources[name]
                prefix = "smarti" if name == "SMARTI_TEST_POSTGRES_URL" else "fallback"
                database = f"{prefix}_{suffix}"
                self.execute(
                    parameters,
                    sql.SQL(
                        "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                        "LC_COLLATE 'C' LC_CTYPE 'C'"
                    ).format(sql.Identifier(database)),
                )
                self.created[database] = parameters
                assigned[name] = database_dsn(
                    parameters, database, plain_url=name == "SMARTI_TEST_POSTGRES_URL"
                )
        except BaseException:
            self.close()
            raise
        self.workers[worker_id] = assigned
        return assigned.copy()

    def close(self, databases: list[str] | None = None) -> None:
        failures = []
        for database in list(self.created) if databases is None else databases:
            try:
                self.execute(
                    self.created[database],
                    self.driver.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        self.driver.sql.Identifier(database)
                    ),
                )
            except Exception:
                failures.append(database)
            else:
                del self.created[database]
        # Never return a cached assignment whose databases were dropped.
        for worker_id, assigned in list(self.workers.items()):
            if any(
                urlsplit(assigned[name]).path.removeprefix("/") not in self.created
                for name in WORKER_DATABASE_VARIABLES
            ):
                del self.workers[worker_id]
        if failures:
            raise RuntimeError(
                f"Failed to clean up {len(failures)} disposable PostgreSQL databases"
            )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: Any) -> None:
    if hasattr(config, "workerinput"):
        # Smarti requires a plain URL, so it cannot carry the explicit
        # hostaddr options preserved in WM/FND URIs. Remove inherited libpq
        # redirects only in the worker before exposing its assigned URLs.
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
            os.environ.pop(name, None)
        os.environ.update(config.workerinput[WORKER_INPUT_KEY])
    else:
        state = WorkerDatabases(os.environ)
        setattr(config, STATE_ATTRIBUTE, state)
        if not config.getoption("numprocesses", default=0):
            raise pytest.UsageError(
                "postgres_workers requires pytest-xdist workers (-n)"
            )


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    state = getattr(node.config, STATE_ATTRIBUTE)
    node.workerinput[WORKER_INPUT_KEY] = state.allocate(node.gateway.id)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    state = getattr(session.config, STATE_ATTRIBUTE, None)
    if state is not None:
        state.close()


def pytest_unconfigure(config: Any) -> None:
    state = getattr(config, STATE_ATTRIBUTE, None)
    if state is not None:
        state.close()
