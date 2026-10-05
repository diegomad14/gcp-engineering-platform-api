"""Single execution, complete snapshots and bounded PostgreSQL transfers."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from eng_platform_api.config import config
from eng_platform_api.services import database_console as console
from eng_platform_api.services.database_registry import Database, DatabaseUnavailable
from eng_platform_api.services.database_sql import QueryRejected


class Connection:
    def __init__(self, rows=(), columns=None, type_rows=None):
        self.source = iter(rows)
        self.columns = (
            columns
            if columns is not None
            else [SimpleNamespace(name="id", type_code=23)]
        )
        self.type_rows = type_rows if type_rows is not None else [(23, "int4")]
        self.commands = []
        self.fetch_sizes = []
        self.closed = False
        self.rolled_back = False

    def cursor(self, name=None, scrollable=None):
        owner = self

        class Cursor:
            command = ""
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, parameters=None):
                self.command = (
                    statement if isinstance(statement, str) else statement.as_string()
                )
                owner.commands.append((self.command, parameters, name))
                if self.command.startswith("SELECT * FROM ("):
                    self.description = owner.columns

            def fetchone(self):
                return (False,) * 6

            def fetchmany(self, amount):
                owner.fetch_sizes.append((amount, name))
                if self.command == console.TABLES:
                    return [("sample", "items", 42, "r", False, "id", "integer", True)]
                if self.command.startswith("WITH RECURSIVE"):
                    return [("sample", "items")]
                if self.command.startswith("SELECT t.oid"):
                    return owner.type_rows
                values = []
                for _ in range(amount):
                    value = next(owner.source, ...)
                    if value is ...:
                        break
                    values.append((value,))
                return values

        return Cursor()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setattr(config.databases, "executions_enabled", False, raising=False)
    monkeypatch.setenv("ENG_PLATFORM_DATABASE_DSN_STREAM_TEST", "test-only-dsn")
    return Database(
        id="stream-test",
        name="Test",
        schemas=["sample"],
        allowed_logins=["reader"],
        dsn_env="ENG_PLATFORM_DATABASE_DSN_STREAM_TEST",
    )


def run(
    monkeypatch,
    database,
    connection,
    statement="SELECT id FROM sample.items",
    authorize=lambda: None,
    **kwargs,
):
    monkeypatch.setattr(console.psycopg, "connect", lambda *args, **kw: connection)
    columns, rows, batches = [], [], []

    def stage(values):
        batches.append(len(values))
        rows.extend(values)

    result = console.stream_query(
        database, statement, authorize, columns.extend, stage, **kwargs
    )
    return result, columns, rows, batches


def test_more_than_500_rows_execute_once_and_release_connection(monkeypatch, database):
    connection = Connection((json.dumps([index]) for index in range(5001)))
    result, columns, rows, batches = run(monkeypatch, database, connection)
    assert result["row_count"] == 5001
    assert rows == [[index] for index in range(5001)]
    assert columns == [{"key": "0", "name": "id", "data_type": "int4"}]
    assert max(batches) <= console.STREAM_BATCH_ROWS
    commands = [command for command in connection.commands if command[2]]
    assert len(commands) == 1
    assert "LIMIT" not in commands[0][0] and "OFFSET" not in commands[0][0]
    assert connection.closed and connection.rolled_back


def test_duplicate_columns_and_exact_numbers_are_staged_by_position(
    monkeypatch, database
):
    connection = Connection(
        ["[9007199254740993,1234567890.12345678901234567890]"],
        columns=[
            SimpleNamespace(name="duplicate", type_code=20),
            SimpleNamespace(name="duplicate", type_code=1700),
        ],
        type_rows=[(20, "int8"), (1700, "numeric")],
    )
    _, columns, rows, _ = run(
        monkeypatch,
        database,
        connection,
        "SELECT 9007199254740993::bigint AS duplicate, 1::numeric AS duplicate",
    )
    assert rows == [["9007199254740993", "1234567890.12345678901234567890"]]
    assert [column["key"] for column in columns] == ["0", "1"]
    assert [column["name"] for column in columns] == ["duplicate", "duplicate"]


@pytest.mark.parametrize(
    "row",
    [None, json.dumps(["x" * 17000]), json.dumps(["🛰" * 1500])],
    ids=["oversized-row", "oversized-cell", "unicode-expansion"],
)
def test_oversized_values_fail_and_are_never_marked_complete(
    monkeypatch, database, row
):
    connection = Connection([row])
    with pytest.raises(console.DatabaseResourceExceeded) as failure:
        run(monkeypatch, database, connection, "SELECT 1")
    assert "🛰" not in str(failure.value) and "xxx" not in str(failure.value)
    assert connection.closed and connection.rolled_back


def test_snapshot_bytes_and_final_serialized_row_budget_are_enforced(
    monkeypatch, database
):
    monkeypatch.setattr(console, "MAX_SNAPSHOT_BYTES", 5)
    connection = Connection(["[1]", "[2]"])
    with pytest.raises(console.DatabaseResourceExceeded):
        run(monkeypatch, database, connection)
    monkeypatch.setattr(console, "MAX_ROW_BYTES", 10)
    with pytest.raises(console.DatabaseResourceExceeded):
        console._stream_row(json.dumps(["ÁÁÁ"]))


def test_revocation_upload_failure_and_admission_close_before_return(
    monkeypatch, database
):
    events = []

    @contextmanager
    def admission():
        events.append("acquired")
        yield
        assert connection.closed
        events.append("released")

    connection = Connection(["[1]"])
    run(
        monkeypatch,
        database,
        connection,
        admission=admission(),
        on_connection=lambda value: events.append(value),
    )
    assert events == ["acquired", connection, "released"]
    connection = Connection(["[1]"])
    monkeypatch.setattr(console.psycopg, "connect", lambda *args, **kw: connection)

    def fail_upload(_):
        raise OSError("PRIVATE credential diagnostic")

    with pytest.raises(DatabaseUnavailable) as failure:
        console.stream_query(
            database, "SELECT 1", lambda: None, lambda _: None, fail_upload
        )
    assert "PRIVATE" not in str(failure.value) and connection.closed
    calls = 0

    def revoked():
        nonlocal calls
        calls += 1
        if calls > 3:
            raise HTTPException(403, "revoked")

    connection = Connection(["[1]"])
    with pytest.raises(HTTPException):
        run(monkeypatch, database, connection, authorize=revoked)
    assert connection.closed


def test_zero_rows_and_unknown_column_type_and_shape(monkeypatch, database):
    result, _, rows, _ = run(monkeypatch, database, Connection())
    assert result["row_count"] == 0 and rows == []
    with pytest.raises(QueryRejected):
        run(monkeypatch, database, Connection(type_rows=[]))
    with pytest.raises(console.DatabaseResourceExceeded):
        run(monkeypatch, database, Connection(columns=[]))
    with pytest.raises(DatabaseUnavailable):
        run(monkeypatch, database, Connection(["[1,2]"]))
    with pytest.raises(DatabaseUnavailable):
        console._stream_row("1")
    with pytest.raises(console.DatabaseResourceExceeded):
        console._stream_row("[NaN]")


def test_wall_budget_and_unsafe_sql_stop_before_data_dispatch(monkeypatch, database):
    with pytest.raises(console.DatabaseResourceExceeded):
        run(monkeypatch, database, Connection(), timeout_seconds=241)
    with pytest.raises(QueryRejected):
        run(monkeypatch, database, Connection(), "DELETE FROM sample.items")
    with pytest.raises(console.DatabaseResourceExceeded):
        console._remaining_timeout(Connection(), -1)
    times = iter([0, 0, 300])
    monkeypatch.setattr(console, "monotonic", lambda: next(times, 300))
    connection = Connection(["[1]"])
    with pytest.raises(console.DatabaseResourceExceeded):
        run(monkeypatch, database, connection)
    assert connection.closed
