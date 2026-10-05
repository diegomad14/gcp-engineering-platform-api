"""Authorization, pinned registry, SQL language and HTTP privacy boundaries."""

from base64 import b64encode
from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import psycopg
import pytest

from eng_platform_api.config import config, load_config
from eng_platform_api.main import app
from eng_platform_api.services import database_console as console
from eng_platform_api.services import database_registry as registry
from eng_platform_api.services.database_sql import QueryRejected, validate_query

HEADERS = {"Origin": "http://localhost:5173", "X-Requested-With": "EngineeringPlatform"}
DATABASE = {
    "id": "sample",
    "name": "Sample",
    "schemas": ["sample"],
    "allowed_logins": ["reader"],
    "dsn_env": "ENG_PLATFORM_DATABASE_DSN_SAMPLE",
}


def client(login="reader", provider="github_oauth"):
    result = TestClient(app)
    if login:
        key = next(
            item
            for item in app.user_middleware
            if item.cls.__name__ == "SessionMiddleware"
        ).kwargs["secret_key"]
        data = {"github_login": login, "github_auth_provider": provider}
        result.cookies.set(
            "session",
            TimestampSigner(key).sign(b64encode(json.dumps(data).encode())).decode(),
        )
    return result


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.databases, "enabled", True)
    monkeypatch.setattr(config.databases, "allowed_logins", ("reader", "other"))
    monkeypatch.setattr(
        config.auth, "session_secret", "test-session-secret-32-characters-long"
    )
    monkeypatch.setattr(config.auth, "github_client_id", "test-client")
    monkeypatch.setattr(config.auth, "github_client_secret", "test-secret")
    monkeypatch.setattr(config.auth, "frontend_url", HEADERS["Origin"])
    path = tmp_path / "databases.json"
    path.write_text(
        json.dumps(
            {
                "databases": [
                    DATABASE,
                    {**DATABASE, "id": "hidden", "allowed_logins": ["other"]},
                ]
            }
        )
    )
    monkeypatch.setattr(config.databases, "registry_path", str(path))
    monkeypatch.setattr(
        config.databases,
        "registry_sha256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        console,
        "query",
        Mock(
            return_value={
                "columns": ["id"],
                "rows": [[1]],
                "row_count": 1,
                "truncated": False,
                "elapsed_ms": 1,
            }
        ),
    )
    monkeypatch.setattr(console, "read_schema", Mock(return_value={"tables": []}))
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE sample.items",
        "INSERT INTO sample.items(id) VALUES(2)",
        "WITH changed AS (DELETE FROM sample.items RETURNING *) SELECT * FROM changed",
        "SELECT * INTO sample.copy FROM sample.items",
        "SELECT * FROM sample.items FOR UPDATE",
        "SELECT * FROM sample.items FOR SHARE",
        "SELECT pg_sleep(1)",
        "SELECT nextval('sample.seq')",
        "SELECT set_config('transaction_read_only','off',true)",
        "SELECT dblink_exec('secret','DELETE FROM x')",
        "SELECT sample.mutate()",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_export(1,'/tmp/file')",
        "SELECT * FROM items",
        "SELECT * FROM other.items",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT 1 OPERATOR(sample.+) 2",
        "SELECT 'x'::sample.custom",
        "SELECT * FROM sample.items ORDER BY id USING OPERATOR(sample.<)",
        "SELECT 'x' COLLATE sample.custom",
        "SELECT * FROM sample.items TABLESAMPLE SYSTEM(50)",
        "SELECT from",
        "",
        "VALUES(1)",
        "SELECT current_user",
        "SELECT * FROM pg_class WHERE EXISTS (WITH pg_class AS (SELECT 1) SELECT 1)",
        "WITH a AS (SELECT * FROM pg_class), pg_class AS (SELECT 1) SELECT * FROM a",
        "WITH pg_class AS (SELECT * FROM pg_class) SELECT * FROM pg_class",
    ],
)
def test_sql_rejects_nested_writes_functions_types_and_scope_escape(sql):
    with pytest.raises(QueryRejected):
        validate_query(sql, ["sample"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*), sum(amount), avg(amount) FROM sample.items",
        "SELECT date_trunc('day', CURRENT_TIMESTAMP), 1::integer",
        "WITH a AS (SELECT * FROM sample.items) SELECT * FROM a WHERE id IN (1,2)",
        "SELECT * FROM sample.items WHERE id BETWEEN 1 AND 2 AND name ILIKE 'item%'",
        "SELECT * FROM sample.items WHERE id NOT BETWEEN 3 AND 4",
        "SELECT * FROM sample.items WHERE id=ANY(ARRAY[1,2])",
        "SELECT CASE WHEN id > 1 THEN lower(name) ELSE 'x' END FROM sample.items",
        "SELECT row_number() OVER (ORDER BY id), id FROM sample.items",
        "SELECT 1 UNION SELECT 2",
        "SELECT ';DROP TABLE' AS harmless",
        "WITH RECURSIVE a AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM a WHERE n<2) SELECT * FROM a",
    ],
)
def test_supported_sql_is_parsed_recursively(sql):
    validate_query(sql, ["sample"])


def test_explicit_configuration_defaults_and_acl_are_separate(monkeypatch):
    for key in ("ENABLED", "ALLOWED_LOGINS", "REGISTRY_PATH", "REGISTRY_SHA256"):
        monkeypatch.delenv("ENG_PLATFORM_DATABASES_" + key, raising=False)
    value = load_config().databases
    assert (
        not value.enabled and not value.allowed_logins and value.registry_path is None
    )
    monkeypatch.setenv("ENG_PLATFORM_DATABASES_ENABLED", "true")
    monkeypatch.setenv("ENG_PLATFORM_DATABASES_ALLOWED_LOGINS", "Reader, Other")
    assert load_config().databases.allowed_logins == ("reader", "other")


def test_list_only_exposes_authorized_public_fields_and_auth_capability(configured):
    result = client().get("/api/databases")
    assert result.status_code == 200
    assert result.json() == {
        "workspace_enabled": False,
        "global_sort_enabled": False,
        "databases": [
            {
                "id": "sample",
                "name": "Sample",
                "schemas": ["sample"],
                "max_rows": 500,
                "timeout_seconds": 5,
                "service_names": [],
            }
        ],
    }
    assert "dsn" not in result.text and "allowed_logins" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert client().get("/api/auth/me").json()["can_query_databases"] is True
    assert client("outsider").get("/api/auth/me").json()["can_query_databases"] is False


@pytest.mark.parametrize(
    "login,provider,mock,status",
    [
        (None, "github_oauth", False, 401),
        ("reader", "mock", False, 401),
        ("reader", "github_oauth", True, 401),
        ("outsider", "github_oauth", False, 403),
    ],
)
def test_only_real_oauth_global_readers_can_query(
    configured, monkeypatch, login, provider, mock, status
):
    monkeypatch.setattr(config, "mock_mode", mock)
    monkeypatch.setattr(config.auth, "trust_iap_identity", True)
    result = client(login, provider).post(
        "/api/databases/sample/query",
        json={"sql": "SELECT 1"},
        headers={
            **HEADERS,
            "X-Goog-Authenticated-User-Email": "accounts.google.com:reader",
        },
    )
    assert result.status_code == status
    assert result.headers["cache-control"] == "no-store"
    console.query.assert_not_called()


@pytest.mark.parametrize("field,value", [("enabled", False), ("allowed_logins", ())])
def test_disabled_feature_and_empty_acl_are_denied(
    configured, monkeypatch, field, value
):
    monkeypatch.setattr(config.databases, field, value)
    assert client().get("/api/databases").status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_secret", "x" * 31),
        ("github_client_id", ""),
        ("github_client_secret", ""),
    ],
)
def test_missing_real_oauth_configuration_denies_access(
    configured, monkeypatch, field, value
):
    monkeypatch.setattr(config.auth, field, value)
    assert client().get("/api/databases").status_code == 403


def test_per_connection_acl_never_connects_hidden_or_unknown(configured):
    for identity in ("hidden", "missing"):
        result = client().post(
            f"/api/databases/{identity}/query",
            json={"sql": "SELECT 1"},
            headers=HEADERS,
        )
        assert result.status_code == 404
    console.query.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example", "X-Requested-With": "EngineeringPlatform"},
        {"Origin": HEADERS["Origin"]},
    ],
)
def test_query_and_schema_require_trusted_json_origin(configured, headers):
    for suffix, body in (("query", {"sql": "SELECT 1"}), ("schema", {})):
        response = client().post(
            f"/api/databases/sample/{suffix}", json=body, headers=headers
        )
        assert response.status_code == 403
    console.query.assert_not_called()
    console.read_schema.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"sql": "SELECT SECRET", "max_rows": 501},
        {"sql": 23},
        {"sql": "SELECT SECRET", "max_rows": True},
        {"sql": "SELECT SECRET", "password": "SECRET"},
        ["SECRET"],
    ],
)
def test_invalid_requests_never_echo_sql_or_values(configured, body):
    response = client().post("/api/databases/sample/query", json=body, headers=HEADERS)
    assert response.status_code == 422 and "SECRET" not in response.text
    assert response.headers["cache-control"] == "no-store"
    console.query.assert_not_called()


def test_body_size_and_media_type_and_querystrings_are_bounded(configured):
    response = client().post(
        "/api/databases/sample/query",
        content=b"x" * 32769,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    response = client().post(
        "/api/databases/sample/query", content="SELECT SECRET", headers=HEADERS
    )
    assert response.status_code == 415 and "SECRET" not in response.text
    response = client().post(
        "/api/databases/sample/query?sql=SECRET", json={}, headers=HEADERS
    )
    assert response.status_code == 422 and "SECRET" not in response.text


def test_success_defaults_schema_body_and_generic_provider_errors(configured):
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT 1"}, headers=HEADERS
    )
    assert response.status_code == 200
    assert console.query.call_args.args[2] == 100
    assert (
        client()
        .post("/api/databases/sample/schema", json={}, headers=HEADERS)
        .status_code
        == 200
    )
    assert (
        client()
        .post("/api/databases/sample/schema", json={"sql": "SECRET"}, headers=HEADERS)
        .status_code
        == 422
    )
    console.query.side_effect = registry.DatabaseUnavailable(
        "SECRET PostgreSQL diagnostics"
    )
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT SECRET"}, headers=HEADERS
    )
    assert response.status_code == 503 and "SECRET" not in response.text
    console.query.side_effect = QueryRejected("SECRET syntax")
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT SECRET"}, headers=HEADERS
    )
    assert response.status_code == 422 and "SECRET" not in response.text


def test_registry_is_revalidated_before_connection_and_after_operation(
    configured, monkeypatch
):
    def revoked(database, statement, max_rows, authorize):
        configured.write_text("corrupt SECRET")
        authorize()

    console.query.side_effect = revoked
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT 1"}, headers=HEADERS
    )
    assert response.status_code == 503 and "SECRET" not in response.text


@pytest.mark.parametrize(
    "contents",
    [b'{"databases":[],"databases":[]}', b'{"databases":NaN}', b"x" * 262145],
)
def test_malformed_duplicate_and_oversize_registry_fails_closed(
    configured, monkeypatch, contents
):
    configured.write_bytes(contents)
    monkeypatch.setattr(
        config.databases, "registry_sha256", hashlib.sha256(contents).hexdigest()
    )
    assert client().get("/api/databases").status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"schemas": ["pg_catalog"]},
        {"schemas": ["information_schema"]},
        {"schemas": ["sample", "sample"]},
        {"allowed_logins": ["reader", "READER"]},
        {"dsn_env": "PASSWORD"},
        {"dsn": "SECRET"},
    ],
)
def test_invalid_registry_document_fails_closed(configured, monkeypatch, change):
    configured.write_text(json.dumps({"databases": [{**DATABASE, **change}]}))
    monkeypatch.setattr(
        config.databases,
        "registry_sha256",
        hashlib.sha256(configured.read_bytes()).hexdigest(),
    )
    assert client().get("/api/databases").status_code == 503


def test_changed_digest_missing_path_nonregular_and_no_fallback(
    configured, monkeypatch, tmp_path
):
    configured.write_text("SECRET")
    assert client().get("/api/databases").status_code == 503
    monkeypatch.setattr(config.databases, "registry_path", str(tmp_path))
    assert client().get("/api/databases").status_code == 503
    monkeypatch.setattr(config.databases, "registry_path", None)
    assert client().get("/api/databases").status_code == 503


def test_connection_always_rolls_back_closes_and_limits_concurrency(monkeypatch):
    database = registry.Database(**DATABASE)
    monkeypatch.setenv(database.dsn_env, "synthetic-only-dsn")
    connection = Mock()
    connection.cursor.return_value.__enter__ = Mock(
        return_value=connection.cursor.return_value
    )
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    connection.cursor.return_value.fetchone.return_value = (False,) * 6
    connect = Mock(return_value=connection)
    monkeypatch.setattr(console.psycopg, "connect", connect)
    authorize = Mock()
    with console._connection(database, authorize):
        pass
    connect.assert_called_once()
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
    with pytest.raises(registry.DatabaseUnavailable):
        with console._connection(database, authorize):
            raise psycopg.OperationalError("SECRET")
    assert connection.rollback.call_count == connection.close.call_count == 2
    with pytest.raises(QueryRejected):
        with console._connection(database, authorize):
            raise QueryRejected("unsupported")
    assert connection.rollback.call_count == connection.close.call_count == 3
    for _ in range(4):
        assert console._slots.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as busy:
            with console._connection(database, authorize):
                pass
        assert busy.value.status_code == 429
    finally:
        for _ in range(4):
            console._slots.release()


def test_missing_credentials_never_constructs_provider(monkeypatch):
    database = registry.Database(**DATABASE)
    monkeypatch.delenv(database.dsn_env, raising=False)
    connect = Mock()
    monkeypatch.setattr(console.psycopg, "connect", connect)
    with pytest.raises(registry.DatabaseUnavailable):
        with console._connection(database, lambda: None):
            pass
    connect.assert_not_called()


def test_final_acl_revalidation_after_connection_cleanup_and_listing(
    configured, monkeypatch
):
    def revoked(database, statement, max_rows, authorize):
        authorize()
        monkeypatch.setattr(config.databases, "allowed_logins", ())
        return {"columns": ["SECRET"], "rows": [["SECRET"]]}

    console.query.side_effect = revoked
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT 1"}, headers=HEADERS
    )
    assert response.status_code == 403 and "SECRET" not in response.text
    monkeypatch.setattr(config.databases, "allowed_logins", ("reader",))
    from eng_platform_api.routers import databases as routes

    visible = registry.databases()[:1]
    monkeypatch.setattr(routes, "_visible", Mock(side_effect=[visible, []]))
    response = client().get("/api/databases")
    assert response.status_code == 403


def test_unexpected_errors_have_generic_no_store_response(configured):
    console.query.side_effect = RuntimeError("SECRET unexpected diagnostic")
    response = client().post(
        "/api/databases/sample/query", json={"sql": "SELECT 1"}, headers=HEADERS
    )
    assert response.status_code == 503 and "SECRET" not in response.text
    assert response.headers["cache-control"] == "no-store"


class ScriptedConnection:
    """A deterministic libpq boundary; the real PostgreSQL suite checks SQL semantics."""

    def __init__(self, records=None, children=None, rows=None, columns=None):
        self.records = (
            records
            if records is not None
            else [("sample", "items", 42, "r", False, "id", "integer", True)]
        )
        self.children = children if children is not None else [("sample", "items")]
        self.rows = rows if rows is not None else [("[1]",)]
        self.columns = columns if columns is not None else ["id"]
        self.commands = []
        self.fetch_sizes = []

    def cursor(self, name=None):
        owner = self

        class Cursor:
            description = None
            command = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement, parameters=None):
                self.command = (
                    statement if isinstance(statement, str) else statement.as_string()
                )
                owner.commands.append((self.command, parameters, name))
                if self.command.startswith("SELECT * FROM ("):
                    self.description = [
                        SimpleNamespace(name=value) for value in owner.columns
                    ]

            def fetchmany(self, amount):
                owner.fetch_sizes.append(amount)
                if self.command == console.TABLES:
                    return owner.records[:amount]
                if self.command.startswith("WITH RECURSIVE"):
                    return owner.children[:amount]
                return owner.rows[:amount]

        return Cursor()


def install_scripted(monkeypatch, connection):
    @contextmanager
    def open_connection(database, authorize, *, admission=None):
        authorize()
        yield connection
        authorize()

    monkeypatch.setattr(console, "_connection", open_connection)


def test_adapter_named_cursor_ordered_json_transport_and_fetch_bounds(monkeypatch):
    connection = ScriptedConnection(
        columns=["duplicate", "duplicate"],
        rows=[("[9007199254740993,1.12345678901234567890]",), ("[2,3]",)],
    )
    install_scripted(monkeypatch, connection)
    result = console.query(
        registry.Database(**DATABASE), "SELECT id,id FROM sample.items", 1, lambda: None
    )
    assert result["columns"] == ["duplicate", "duplicate"]
    assert result["rows"] == [["9007199254740993", "1.12345678901234567890"]]
    assert result["truncated"] and result["row_count"] == 1
    assert connection.fetch_sizes[-1] == 2
    transfer = connection.commands[-1]
    assert transfer[2] == "eng_platform_readonly_results"
    assert "pg_catalog.pg_column_size(t) <= 65536" in transfer[0]
    assert 't("c0","c1")' in transfer[0]
    assert any(command[0].startswith("LOCK TABLE") for command in connection.commands)


@pytest.mark.parametrize(
    "rows,columns,limit,expected_count,truncated",
    [
        ([("[1]",)], ["id"], 100, 1, False),
        ([], ["id"], 100, 0, False),
        ([(None,)], ["id"], 100, 0, True),
        ([(json.dumps(["x" * 17000]),)], ["id"], 100, 0, True),
        ([(json.dumps(["x" * 16000]),)] * 100, ["id"], 100, 65, True),
    ],
)
def test_adapter_truncates_complete_rows_only(
    monkeypatch, rows, columns, limit, expected_count, truncated
):
    connection = ScriptedConnection(rows=rows, columns=columns)
    install_scripted(monkeypatch, connection)
    result = console.query(
        registry.Database(**DATABASE), "SELECT 1", limit, lambda: None
    )
    assert result["row_count"] == expected_count and result["truncated"] is truncated
    assert len(json.dumps(result).encode()) <= console.MAX_RESPONSE_BYTES


@pytest.mark.parametrize(
    "records,children",
    [
        ([("sample", "items", 42, "v", False, "id", "integer", True)], None),
        ([("sample", "items", 42, "r", True, "id", "integer", True)], None),
        ([("sample", "items", 42, "r", False, "id", "custom", False)], None),
        ([], None),
        (None, [("outside", "items")]),
    ],
)
def test_adapter_rejects_unsafe_or_unavailable_relations(
    monkeypatch, records, children
):
    connection = ScriptedConnection(records=records, children=children)
    install_scripted(monkeypatch, connection)
    with pytest.raises(QueryRejected):
        console.query(
            registry.Database(**DATABASE),
            "SELECT id FROM sample.items",
            100,
            lambda: None,
        )
    assert not any(command[2] for command in connection.commands)


def test_adapter_schema_response_inventory_and_column_limits(monkeypatch):
    database = registry.Database(**DATABASE)
    connection = ScriptedConnection()
    install_scripted(monkeypatch, connection)
    assert console.read_schema(database, lambda: None) == {
        "tables": [
            {
                "schema": "sample",
                "name": "items",
                "service_names": [],
                "columns": [{"name": "id", "data_type": "integer"}],
            }
        ]
    }
    connection.columns = ["x"] * 101
    with pytest.raises(QueryRejected):
        console.query(database, "SELECT 1", 100, lambda: None)
    connection.records *= 10001
    with pytest.raises(registry.DatabaseUnavailable):
        console.read_schema(database, lambda: None)
    connection.records = [("sample", "items", 42, "r", False, "id", "integer", True)]
    monkeypatch.setattr(console, "MAX_RESPONSE_BYTES", 20)
    with pytest.raises(registry.DatabaseUnavailable):
        console.read_schema(database, lambda: None)


def test_privileged_database_role_fails_closed_and_connection_errors_are_redacted(
    monkeypatch,
):
    database = registry.Database(**DATABASE)
    monkeypatch.setenv(database.dsn_env, "test-only-dsn")
    connection = Mock()
    connection.cursor.return_value.__enter__ = Mock(
        return_value=connection.cursor.return_value
    )
    connection.cursor.return_value.__exit__ = Mock(return_value=False)
    connection.cursor.return_value.fetchone.return_value = (
        True,
        False,
        False,
        False,
        False,
        False,
    )
    connect = Mock(return_value=connection)
    monkeypatch.setattr(console.psycopg, "connect", connect)
    with pytest.raises(registry.DatabaseUnavailable):
        with console._connection(database, lambda: None):
            pass
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
    connect.side_effect = psycopg.OperationalError("SECRET credentials")
    with pytest.raises(registry.DatabaseUnavailable) as failure:
        with console._connection(database, lambda: None):
            pass
    assert "SECRET" not in str(failure.value)


def test_curated_service_metadata_is_visible_only_under_database_acl(
    configured, monkeypatch
):
    visible = {
        **DATABASE,
        "service_names": ["example-service"],
        "table_services": [
            {"schema": "sample", "name": "items", "service_names": ["example-service"]}
        ],
    }
    hidden = {
        **DATABASE,
        "id": "hidden",
        "allowed_logins": ["other"],
        "service_names": ["hidden-service"],
    }
    configured.write_text(json.dumps({"databases": [visible, hidden]}))
    monkeypatch.setattr(
        config.databases,
        "registry_sha256",
        hashlib.sha256(configured.read_bytes()).hexdigest(),
    )
    response = client().get("/api/databases")
    assert response.status_code == 200
    assert response.json()["databases"][0]["service_names"] == ["example-service"]
    assert (
        "hidden-service" not in response.text and "table_services" not in response.text
    )
    assert (
        client()
        .post("/api/databases/hidden/schema", json={}, headers=HEADERS)
        .status_code
        == 404
    )
    console.read_schema.assert_not_called()


def test_table_services_are_exact_curated_matches_and_never_inferred(monkeypatch):
    database = registry.Database(
        **{
            **DATABASE,
            "schemas": ["sample", "other"],
            "service_names": ["example-service", "second-service"],
            "table_services": [
                {
                    "schema": "sample",
                    "name": "items",
                    "service_names": ["example-service"],
                },
                {
                    "schema": "sample",
                    "name": "missing",
                    "service_names": ["second-service"],
                },
                {
                    "schema": "sample",
                    "name": "unsafe",
                    "service_names": ["second-service"],
                },
            ],
        }
    )
    connection = ScriptedConnection(
        records=[
            ("sample", "items", 42, "r", False, "id", "integer", True),
            ("other", "items", 43, "r", False, "id", "integer", True),
            ("sample", "unknown", 44, "r", False, "id", "integer", True),
            ("sample", "unsafe", 45, "v", False, "id", "integer", True),
        ]
    )
    install_scripted(monkeypatch, connection)
    result = console.read_schema(database, lambda: None)
    assert [
        (table["schema"], table["name"], table["service_names"])
        for table in result["tables"]
    ] == [
        ("sample", "items", ["example-service"]),
        ("other", "items", []),
        ("sample", "unknown", []),
    ]
    assert "second-service" not in json.dumps(result) and "missing" not in json.dumps(
        result
    )


@pytest.mark.parametrize(
    "change",
    [
        {"service_names": ["Invalid-Service"]},
        {"service_names": ["ends-with-"]},
        {"service_names": ["same", "same"]},
        {"service_names": ["a" * 64]},
        {"service_names": [f"service-{index}" for index in range(65)]},
        {
            "table_services": [
                {"schema": "outside", "name": "items", "service_names": []}
            ]
        },
        {
            "table_services": [
                {"schema": "sample", "name": "items", "service_names": ["unlisted"]}
            ]
        },
        {
            "table_services": [
                {"schema": "sample", "name": "items", "service_names": []}
            ]
            * 2
        },
        {"table_services": [{"schema": "sample", "name": "", "service_names": []}]},
        {
            "table_services": [
                {"schema": "sample", "name": "bad\x00name", "service_names": []}
            ]
        },
        {
            "table_services": [
                {"schema": "sample", "name": "é" * 40, "service_names": []}
            ]
        },
        {
            "service_names": ["example-service"],
            "table_services": [
                {
                    "schema": "sample",
                    "name": "items",
                    "service_names": ["example-service", "example-service"],
                }
            ],
        },
        {
            "table_services": [
                {
                    "schema": "sample",
                    "name": "items",
                    "service_names": [],
                    "dsn": "SECRET",
                }
            ]
        },
    ],
)
def test_invalid_service_associations_fail_closed(configured, monkeypatch, change):
    configured.write_text(json.dumps({"databases": [{**DATABASE, **change}]}))
    monkeypatch.setattr(
        config.databases,
        "registry_sha256",
        hashlib.sha256(configured.read_bytes()).hexdigest(),
    )
    response = client().get("/api/databases")
    assert response.status_code == 503 and "SECRET" not in response.text
