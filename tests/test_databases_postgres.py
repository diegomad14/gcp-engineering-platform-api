"""Real disposable PostgreSQL validation, opt in with test-only connection envs.

Never point these variables at a service/production database. The optional admin
fixture creates temporary test objects and grants, then removes them.
"""

import os
from time import monotonic

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg import sql
import pytest

from eng_platform_api.services import database_console as console
from eng_platform_api.services.database_registry import Database, DatabaseUnavailable
from eng_platform_api.services.database_sql import QueryRejected


def test_real_complete_snapshot_exceeds_500_and_respects_sql_limit(database):
    columns, batches = [], []
    result = console.stream_query(
        database,
        "WITH RECURSIVE numbers AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM numbers WHERE n<1251) SELECT n FROM numbers ORDER BY n",
        lambda: None,
        columns.extend,
        lambda values: batches.extend(values),
    )
    assert result["row_count"] == 1251
    assert batches == [[number] for number in range(1, 1252)]
    assert columns == [{"key": "0", "name": "n", "data_type": "int4"}]
    rows = []
    result = console.stream_query(
        database,
        "SELECT id FROM sample.items ORDER BY id LIMIT 3",
        lambda: None,
        lambda _: None,
        rows.extend,
    )
    assert result["row_count"] == 3 and rows == [[1], [2], [3]]


def test_real_complete_zero_rows_exact_types_and_values(database):
    columns, rows = [], []
    result = console.stream_query(
        database,
        "SELECT 9007199254740993::bigint AS duplicate, 1234567890.12345678901234567890::numeric AS duplicate",
        lambda: None,
        columns.extend,
        rows.extend,
    )
    assert result["row_count"] == 1
    assert [column["data_type"] for column in columns] == ["int8", "numeric"]
    assert rows == [["9007199254740993", "1234567890.12345678901234567890"]]
    columns, rows = [], []
    result = console.stream_query(
        database,
        "SELECT id FROM sample.items WHERE false",
        lambda: None,
        columns.extend,
        rows.extend,
    )
    assert result["row_count"] == 0 and rows == [] and len(columns) == 1


def test_real_complete_json_numeric_lexemes_and_sql_null_are_lossless(database):
    from decimal import Decimal
    import json

    columns, rows = [], []
    console.stream_query(
        database,
        "SELECT '{\"amount\":0.12345678901234567890,\"id\":9007199254740993}'::jsonb AS payload, 'null'::jsonb AS json_null, NULL::jsonb AS sql_null, ARRAY[0.12345678901234567890::numeric, 9007199254740993::numeric] AS values",
        lambda: None,
        columns.extend,
        rows.extend,
    )
    assert [column["data_type"] for column in columns] == [
        "jsonb",
        "jsonb",
        "jsonb",
        "_numeric",
    ]
    payload = json.loads(rows[0][0], parse_float=Decimal)
    assert payload == {
        "amount": Decimal("0.12345678901234567890"),
        "id": 9007199254740993,
    }
    assert rows[0][1:3] == ["null", None]
    assert json.loads(rows[0][3], parse_float=Decimal) == [
        Decimal("0.12345678901234567890"),
        9007199254740993,
    ]


def test_real_complete_resource_error_timeout_and_watcher_cancel_close(database, admin):
    import threading

    observed = []
    with pytest.raises(console.DatabaseResourceExceeded):
        console.stream_query(
            database,
            "SELECT '" + "x" * 17000 + "'",
            lambda: None,
            lambda _: None,
            lambda _: None,
            on_connection=observed.append,
        )
    assert observed[0].closed
    statement = "SELECT count(*) FROM " + " CROSS JOIN ".join(
        f"sample.items t{index}" for index in range(9)
    )
    started = monotonic()
    with pytest.raises(console.DatabaseResourceExceeded):
        console.stream_query(
            database,
            statement,
            lambda: None,
            lambda _: None,
            lambda _: None,
            timeout_seconds=0.5,
        )
    assert monotonic() - started < 3
    watcher = None
    stop = threading.Event()
    cancelled = threading.Event()
    observed_fetch = threading.Event()
    observer_errors = []
    observed = []

    def watch(connection):
        nonlocal watcher
        observed.append(connection)

        def cancel_active_fetch():
            deadline = monotonic() + 2
            try:
                while not stop.wait(0.01) and monotonic() < deadline:
                    if connection.closed:
                        return
                    activity = admin.execute(
                        "SELECT state, query FROM pg_stat_activity WHERE pid=%s",
                        (connection.info.backend_pid,),
                    ).fetchone()
                    if (
                        activity
                        and activity[0] == "active"
                        and activity[1].startswith(
                            'FETCH FORWARD 16 FROM "eng_platform_complete_results"'
                        )
                    ):
                        observed_fetch.set()
                        cancelled.set()
                        connection.cancel_safe(timeout=0.5)
                        return
            except Exception as exc:
                observer_errors.append(type(exc).__name__)
            finally:
                if not observed_fetch.is_set():
                    # Fail closed and quickly if observation fails. The
                    # assertion below never accepts checkpoint-only stopping.
                    cancelled.set()
                    if not connection.closed:
                        connection.cancel_safe(timeout=0.5)

        watcher = threading.Thread(target=cancel_active_fetch, daemon=True)
        watcher.start()

    def authorize():
        if cancelled.is_set():
            raise DatabaseUnavailable("Synthetic watcher requested cancellation")

    started = monotonic()
    try:
        with pytest.raises(DatabaseUnavailable):
            console.stream_query(
                database,
                statement,
                authorize,
                lambda _: None,
                lambda _: None,
                on_connection=watch,
            )
        assert observed[0].closed
        assert observed_fetch.is_set()
        assert cancelled.is_set()
        assert observer_errors == []
        assert monotonic() - started < 3
    finally:
        stop.set()
        if watcher is not None:
            watcher.join(timeout=2)
            assert not watcher.is_alive()


def _test_dsn(value: str) -> str:
    """Admin fixtures must never accept a service or remote/production DSN."""
    try:
        fields = conninfo_to_dict(value)
    except psycopg.Error:
        pytest.fail("Invalid disposable PostgreSQL test DSN")
    if (
        fields.get("dbname") != "readonly_test"
        or fields.get("host") not in {"localhost", "127.0.0.1", "::1"}
        or any(key in fields for key in ("service", "servicefile", "hostaddr"))
    ):
        pytest.fail(
            "PostgreSQL integration DSNs must target loopback readonly_test without service overrides"
        )
    return value


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("ENG_PLATFORM_DATABASE_TEST_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL reader DSN is not configured")
    for key in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENG_PLATFORM_DATABASE_DSN_TEST", _test_dsn(dsn))
    return Database(
        id="test",
        name="Test",
        schemas=["sample"],
        allowed_logins=["reader"],
        dsn_env="ENG_PLATFORM_DATABASE_DSN_TEST",
    )


@pytest.fixture
def admin(database):
    dsn = os.environ.get("ENG_PLATFORM_DATABASE_TEST_ADMIN_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL admin DSN is not configured")
    with psycopg.connect(_test_dsn(dsn), autocommit=True) as connection:
        yield connection


def run(database, statement, max_rows=100):
    return console.query(database, statement, max_rows, lambda: None)


def test_real_postgres_schema_ordered_results_duplicates_and_row_limits(database):
    schema = console.read_schema(database, lambda: None)
    table = next(item for item in schema["tables"] if item["name"] == "items")
    assert [item["name"] for item in table["columns"]] == [
        "id",
        "name",
        "amount",
        "payload",
    ]
    result = run(
        database, "SELECT id,name,amount,payload FROM sample.items ORDER BY id", 3
    )
    assert result["columns"] == ["id", "name", "amount", "payload"]
    assert result["row_count"] == 3 and result["truncated"]
    assert result["rows"][0] == [1, "item-1", "1.25", {"safe": True}]
    assert run(database, "SELECT 1 AS duplicate,2 AS duplicate;")["rows"] == [[1, 2]]
    result = run(database, "SELECT id FROM sample.items WHERE id < 3 ORDER BY id", 2)
    assert result["rows"] == [[1], [2]] and not result["truncated"]
    result = run(database, "SELECT id FROM sample.items WHERE false")
    assert (
        result["columns"] == ["id"] and not result["rows"] and not result["truncated"]
    )


def test_real_postgres_normal_filters_cte_and_exact_numeric_transport(database):
    result = run(
        database,
        "WITH a AS (SELECT * FROM sample.items) SELECT count(*) FROM a WHERE id BETWEEN 1 AND 3 AND name ILIKE 'item%'",
    )
    assert result["rows"] == [[3]]
    result = run(
        database,
        'SELECT 9007199254740993::bigint, 1234567890.12345678901234567890::numeric, \'{"big":9007199254740993,"decimal":1.1234567890123456789}\'::jsonb',
    )
    assert result["rows"] == [
        [
            "9007199254740993",
            "1234567890.12345678901234567890",
            {"big": "9007199254740993", "decimal": "1.1234567890123456789"},
        ]
    ]


def test_real_postgres_readonly_defaults_transaction_and_always_rollback(database):
    with console._connection(database, lambda: None) as connection:
        assert connection.execute("SHOW default_transaction_read_only").fetchone() == (
            "on",
        )
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert connection.execute("SHOW search_path").fetchone() == ("pg_catalog",)
        assert connection.execute("SHOW statement_timeout").fetchone() == ("5s",)
        assert connection.execute("SHOW lock_timeout").fetchone() == ("1s",)
        observed = connection
    assert observed.closed
    with pytest.raises(DatabaseUnavailable):
        with console._connection(database, lambda: None) as connection:
            connection.execute("INSERT INTO sample.items(id) VALUES (1000000)")
    assert connection.closed
    assert run(database, "SELECT count(*) FROM sample.items WHERE id=1000000")[
        "rows"
    ] == [[0]]
    with pytest.raises(DatabaseUnavailable) as failure:
        run(database, "SELECT 1/0 AS SECRET_SQL")
    assert "SECRET" not in str(failure.value)
    assert run(database, "SELECT 1")["rows"] == [[1]]


def test_real_postgres_cells_rows_and_whole_response_are_bounded(database):
    result = run(database, "SELECT '" + "a" * 20000 + "'")
    assert result["row_count"] == 0 and result["truncated"]
    result = run(
        database, "SELECT x||x||x FROM (SELECT '" + "b" * 24000 + "' AS x) AS q"
    )
    assert result["row_count"] == 0 and result["truncated"]
    result = run(
        database,
        "SELECT '" + "c" * 10000 + "' FROM sample.items a CROSS JOIN sample.items b",
        500,
    )
    assert 0 < result["row_count"] < 144 and result["truncated"]
    import json

    assert len(json.dumps(result).encode()) <= console.MAX_RESPONSE_BYTES
    with pytest.raises(QueryRejected):
        run(database, "SELECT " + ",".join("1" for _ in range(101)))


def test_real_postgres_statement_timeout_and_lock_timeout(database, admin):
    statement = "SELECT count(*) FROM " + " CROSS JOIN ".join(
        f"sample.items t{index}" for index in range(9)
    )
    started = monotonic()
    with pytest.raises(DatabaseUnavailable):
        run(database, statement)
    assert monotonic() - started < 8
    admin.execute("BEGIN")
    try:
        admin.execute("LOCK TABLE sample.items IN ACCESS EXCLUSIVE MODE")
        started = monotonic()
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT * FROM sample.items")
        assert monotonic() - started < 3
    finally:
        admin.execute("ROLLBACK")


@pytest.fixture
def unsafe_objects(database, admin):
    with psycopg.connect(os.environ[database.dsn_env]) as reader:
        login = reader.execute("SELECT current_user").fetchone()[0]
    identity = sql.Identifier(login)
    admin.execute("CREATE SCHEMA console_outside")
    admin.execute("CREATE TYPE sample.console_enum AS ENUM ('one')")
    admin.execute("CREATE TABLE sample.console_custom(value sample.console_enum)")
    admin.execute("CREATE TABLE sample.console_rls(id integer)")
    admin.execute("ALTER TABLE sample.console_rls ENABLE ROW LEVEL SECURITY")
    admin.execute(
        "CREATE FUNCTION sample.console_unsafe() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN INSERT INTO sample.items(id) VALUES(1000001); RETURN 1; END $$"
    )
    admin.execute(
        "CREATE VIEW sample.console_view AS SELECT sample.console_unsafe() AS value"
    )
    admin.execute("CREATE TABLE sample.console_parent(id integer)")
    admin.execute(
        "CREATE TABLE console_outside.console_child() INHERITS(sample.console_parent)"
    )
    admin.execute(
        sql.SQL(
            "GRANT SELECT ON sample.console_custom,sample.console_rls,sample.console_view,sample.console_parent,console_outside.console_child TO {}"
        ).format(identity)
    )
    try:
        yield login
    finally:
        admin.execute("DROP SCHEMA console_outside CASCADE")
        admin.execute("DROP VIEW sample.console_view")
        admin.execute("DROP FUNCTION sample.console_unsafe()")
        admin.execute(
            "DROP TABLE sample.console_custom,sample.console_rls,sample.console_parent"
        )
        admin.execute("DROP TYPE sample.console_enum")


def test_real_postgres_excludes_views_rls_custom_types_and_inheritance_escape(
    database, unsafe_objects
):
    names = {
        item["name"] for item in console.read_schema(database, lambda: None)["tables"]
    }
    assert not names & {"console_custom", "console_rls", "console_view"}
    for table in ("console_custom", "console_rls", "console_view", "console_parent"):
        with pytest.raises(QueryRejected):
            run(database, f"SELECT * FROM sample.{table}")
    assert run(database, "SELECT count(*) FROM sample.items WHERE id=1000001")[
        "rows"
    ] == [[0]]


@pytest.mark.parametrize(
    "grant,revoke",
    [
        (
            "GRANT UPDATE(name) ON sample.items TO {}",
            "REVOKE UPDATE(name) ON sample.items FROM {}",
        ),
        (
            "GRANT REFERENCES(name) ON sample.items TO {}",
            "REVOKE REFERENCES(name) ON sample.items FROM {}",
        ),
        (
            "GRANT TRIGGER ON sample.items TO {}",
            "REVOKE TRIGGER ON sample.items FROM {}",
        ),
        (
            "GRANT TEMP ON DATABASE readonly_test TO {}",
            "REVOKE TEMP ON DATABASE readonly_test FROM {}",
        ),
        (
            "GRANT CREATE ON SCHEMA sample TO {}",
            "REVOKE CREATE ON SCHEMA sample FROM {}",
        ),
    ],
)
def test_real_postgres_refuses_effective_column_trigger_temp_create_grants(
    database, admin, grant, revoke
):
    with psycopg.connect(os.environ[database.dsn_env]) as reader:
        identity = sql.Identifier(reader.execute("SELECT current_user").fetchone()[0])
    admin.execute(sql.SQL(grant).format(identity))
    try:
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT 1")
    finally:
        admin.execute(sql.SQL(revoke).format(identity))
    assert run(database, "SELECT 1")["rows"] == [[1]]


def test_real_postgres_refuses_write_grants_in_pgdata_and_role_memberships(
    database, admin
):
    with psycopg.connect(os.environ[database.dsn_env]) as reader:
        login = reader.execute("SELECT current_user").fetchone()[0]
    identity = sql.Identifier(login)
    admin.execute("CREATE SCHEMA pgdata")
    admin.execute("CREATE TABLE pgdata.outside(id integer)")
    admin.execute(sql.SQL("GRANT UPDATE ON pgdata.outside TO {}").format(identity))
    try:
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT 1")
    finally:
        admin.execute("DROP SCHEMA pgdata CASCADE")
    admin.execute("CREATE ROLE console_group")
    admin.execute(sql.SQL("GRANT console_group TO {}").format(identity))
    try:
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT 1")
    finally:
        admin.execute(sql.SQL("REVOKE console_group FROM {}").format(identity))
        admin.execute("DROP ROLE console_group")


def test_real_postgres_refuses_sequence_ownership_and_administrative_role(
    database, admin, monkeypatch
):
    with psycopg.connect(os.environ[database.dsn_env]) as reader:
        login = reader.execute("SELECT current_user").fetchone()[0]
    identity = sql.Identifier(login)
    admin.execute("CREATE SEQUENCE sample.console_sequence")
    admin.execute(
        sql.SQL("GRANT USAGE ON SEQUENCE sample.console_sequence TO {}").format(
            identity
        )
    )
    try:
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT 1")
    finally:
        admin.execute("DROP SEQUENCE sample.console_sequence")
    admin.execute("CREATE TABLE sample.console_owned(id integer)")
    admin.execute(
        sql.SQL("ALTER TABLE sample.console_owned OWNER TO {}").format(identity)
    )
    try:
        with pytest.raises(DatabaseUnavailable):
            run(database, "SELECT 1")
    finally:
        admin.execute("DROP TABLE sample.console_owned")
    monkeypatch.setenv(
        database.dsn_env, _test_dsn(os.environ["ENG_PLATFORM_DATABASE_TEST_ADMIN_DSN"])
    )
    with pytest.raises(DatabaseUnavailable):
        run(database, "SELECT 1")


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://localhost/production",
        "postgresql://remote.example/readonly_test",
        "dbname=readonly_test host=127.0.0.1 hostaddr=203.0.113.1",
        "dbname=readonly_test host=localhost service=production",
        "dbname=readonly_test",
    ],
)
def test_integration_dsns_refuse_non_disposable_targets(value):
    with pytest.raises(pytest.fail.Exception):
        _test_dsn(value)
