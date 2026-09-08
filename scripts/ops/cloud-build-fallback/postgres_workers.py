"""Give pytest-xdist workers disposable, loopback-only FND PostgreSQL databases.

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
from urllib.parse import quote, urlencode

import pytest

DSN_VARIABLES = ("WM_TEST_POSTGRES_DSN", "FND_TEST_POSTGRES_DSN")
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


def local_parameters(dsn: str, driver: Driver) -> dict[str, str]:
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


def database_dsn(parameters: dict[str, str], database: str) -> str:
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
    return (
        f"postgresql://{credentials}{host}:{port}/{quote(database, safe='')}"
        f"?{urlencode(parameters)}"
    )


class WorkerDatabases:
    def __init__(self, environ: Any, driver: Driver | None = None) -> None:
        self.driver = driver or load_driver()
        # Validate every destination before creating anything.
        self.sources = {
            name: local_parameters(environ[name], self.driver)
            for name in DSN_VARIABLES
            if environ.get(name)
        }
        if len(self.sources) != len(DSN_VARIABLES):
            raise ValueError(
                "Both WM_TEST_POSTGRES_DSN and FND_TEST_POSTGRES_DSN are required"
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
        try:
            parameters = self.sources["FND_TEST_POSTGRES_DSN"]
            database = f"fallback_{self.run_id}_{safe_worker}"
            self.execute(
                parameters,
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                    "LC_COLLATE 'C' LC_CTYPE 'C'"
                ).format(sql.Identifier(database)),
            )
            self.created[database] = parameters
            assigned["FND_TEST_POSTGRES_DSN"] = database_dsn(parameters, database)
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
        if failures:
            raise RuntimeError(
                f"Failed to clean up {len(failures)} disposable PostgreSQL databases"
            )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: Any) -> None:
    if hasattr(config, "workerinput"):
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
