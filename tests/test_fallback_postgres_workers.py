"""Safety and lifecycle coverage for the external xdist PostgreSQL plugin."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts/ops/cloud-build-fallback/postgres_workers.py"
)
SPEC = importlib.util.spec_from_file_location("fallback_postgres_workers", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)


class SQL:
    @staticmethod
    def SQL(value):
        return value

    @staticmethod
    def Identifier(value):
        return f'"{value}"'


def parse_dsn(value):
    parsed = urlsplit(value)
    parameters = {
        "host": parsed.hostname,
        "dbname": parsed.path.removeprefix("/"),
        "user": parsed.username or "postgres",
    }
    parameters.update(
        {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    )
    return parameters


class Connection:
    def __init__(self, driver):
        self.driver = driver

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, statement):
        self.driver.statements.append(statement)
        if self.driver.fail_on and self.driver.fail_on(statement):
            raise RuntimeError("database operation failed")

    def close(self):
        self.driver.closed += 1


class FakeDriver:
    sql = SQL
    parse = staticmethod(parse_dsn)

    def __init__(self):
        self.statements = []
        self.connections = []
        self.closed = 0
        self.fail_on = None

    def connect(self, **parameters):
        self.connections.append(parameters)
        return Connection(self)


def environment(second_database="test"):
    return {
        "WM_TEST_POSTGRES_DSN": "postgresql://postgres@127.0.0.1/wm_test",
        "FND_TEST_POSTGRES_DSN": f"postgresql://postgres@127.0.0.1/{second_database}",
    }


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@production.example/test",
        "postgresql://postgres@10.0.0.1/test",
        "postgresql:///test",
        "postgresql://postgres@127.0.0.1/test?hostaddr=10.0.0.1",
        "postgresql://postgres@127.0.0.1/test?service=production",
        "postgresql://postgres@127.0.0.1/",
    ],
)
def test_rejects_nonlocal_or_implicit_destinations_before_any_connection(dsn):
    driver = FakeDriver()
    settings = environment()
    settings["FND_TEST_POSTGRES_DSN"] = dsn
    with pytest.raises(ValueError):
        plugin.WorkerDatabases(settings, driver)
    assert not driver.connections


def test_requires_both_integration_suites_to_be_configured():
    with pytest.raises(ValueError, match="Both WM_TEST"):
        plugin.WorkerDatabases(
            {"WM_TEST_POSTGRES_DSN": environment()["WM_TEST_POSTGRES_DSN"]},
            FakeDriver(),
        )


def test_workers_get_unique_fnd_databases_and_preserve_wm_schema_isolation():
    driver = FakeDriver()
    state = plugin.WorkerDatabases(environment(), driver)
    first, second = state.allocate("gw0"), state.allocate("gw1")
    assert urlsplit(first["WM_TEST_POSTGRES_DSN"]).path == "/wm_test"
    assert first["WM_TEST_POSTGRES_DSN"] == second["WM_TEST_POSTGRES_DSN"]
    assert first["FND_TEST_POSTGRES_DSN"] != second["FND_TEST_POSTGRES_DSN"]
    assert first != second
    assert len(driver.connections) == 2
    assert state.allocate("gw0") == first
    assert len(driver.connections) == 2
    assert all(item["dbname"] == "postgres" for item in driver.connections)
    assert all(item["hostaddr"] == "127.0.0.1" for item in driver.connections)
    assert all("TEMPLATE template0 ENCODING 'UTF8'" in s for s in driver.statements)
    state.close()
    state.close()
    assert len(driver.statements) == 4
    assert all("DROP DATABASE" in s for s in driver.statements[2:])
    assert not state.created
    assert driver.closed == 4


def test_separate_runs_never_reuse_names():
    first = plugin.WorkerDatabases(environment(), FakeDriver()).allocate("gw0")
    second = plugin.WorkerDatabases(environment(), FakeDriver()).allocate("gw0")
    assert first != second


def test_partial_creation_rolls_back_already_created_database():
    driver = FakeDriver()
    driver.fail_on = lambda statement: (
        "CREATE DATABASE" in statement and "_gw1" in statement
    )
    state = plugin.WorkerDatabases(environment(), driver)
    state.allocate("gw0")
    with pytest.raises(RuntimeError, match="database operation failed"):
        state.allocate("gw1")
    assert len(driver.statements) == 3
    assert "DROP DATABASE" in driver.statements[-1]
    assert not state.created
    assert "gw1" not in state.workers
    assert driver.closed == 3


def test_cleanup_attempts_every_database_and_surfaces_failure_then_can_retry():
    driver = FakeDriver()
    state = plugin.WorkerDatabases(environment(), driver)
    state.allocate("gw0")
    state.allocate("gw1")
    driver.fail_on = lambda statement: (
        "DROP DATABASE" in statement and "_gw0" in statement
    )
    with pytest.raises(RuntimeError, match="Failed to clean up 1"):
        state.close()
    assert len(driver.statements) == 4
    assert len(state.created) == 1
    driver.fail_on = None
    state.close()
    assert not state.created


def test_uri_preserves_credentials_ipv6_and_options_without_environment_redirect():
    parameters = {
        "host": "::1",
        "user": "u@ser",
        "password": "p:/?#",
        "port": "5438",
        "dbname": "original",
        "sslmode": "disable",
        "hostaddr": "::1",
    }
    value = plugin.database_dsn(parameters, "worker_db")
    parsed = urlsplit(value)
    assert parsed.hostname == "::1"
    assert parsed.port == 5438
    assert parsed.username == "u%40ser"
    assert parsed.password == "p%3A%2F%3F%23"
    assert parsed.path == "/worker_db"
    assert parse_qs(parsed.query) == {"sslmode": ["disable"], "hostaddr": ["::1"]}
    assert parameters["dbname"] == "original"


def test_controller_hooks_assign_worker_environment_and_clean_up(monkeypatch):
    state = plugin.WorkerDatabases(environment(), FakeDriver())
    config = SimpleNamespace(**{plugin.STATE_ATTRIBUTE: state})
    node = SimpleNamespace(
        config=config, gateway=SimpleNamespace(id="gw0"), workerinput={}
    )
    plugin.pytest_configure_node(node)
    for variable in plugin.DSN_VARIABLES:
        monkeypatch.setenv(variable, "original")
    plugin.pytest_configure(SimpleNamespace(workerinput=node.workerinput))
    import os

    assert (
        os.environ["WM_TEST_POSTGRES_DSN"]
        == state.workers["gw0"]["WM_TEST_POSTGRES_DSN"]
    )
    plugin.pytest_sessionfinish(SimpleNamespace(config=config), 1)
    plugin.pytest_unconfigure(config)
    assert not state.created
