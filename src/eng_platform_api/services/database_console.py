"""One fresh, read-only PostgreSQL transaction per authorized operation."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import threading
from time import monotonic
from typing import Callable

from fastapi import HTTPException
from pglast import parse_sql
from pglast.stream import RawStream
import psycopg
from psycopg import sql as postgres_sql

from .database_registry import Database, DatabaseUnavailable
from .database_sql import QueryRejected, validate_query

MAX_CELL_BYTES = 16_384
MAX_ROW_BYTES = 65_536
MAX_RESPONSE_BYTES = 1_048_576
MAX_COLUMNS = 100
# A process has at most four simultaneous connections; Cloud Run instance
# concurrency/max-instances must bound aggregate Cloud SQL connection demand.
_slots = threading.BoundedSemaphore(4)

ROLE_AUDIT = """
SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
       OR r.rolbypassrls OR NOT r.rolcanlogin,
       current_user <> session_user,
       EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid),
       pg_catalog.has_database_privilege(current_user, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(current_user, current_database(), 'TEMP'),
       EXISTS (
         SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname !~ '^pg_'
         AND n.nspname <> 'information_schema'
         AND (n.nspowner = r.oid OR
              pg_catalog.has_schema_privilege(current_user, n.oid, 'CREATE'))),
       EXISTS (
         SELECT 1 FROM pg_catalog.pg_class c
         JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND (
           c.relowner = r.oid OR
           CASE WHEN c.relkind = 'S' THEN
             pg_catalog.has_sequence_privilege(current_user, c.oid, 'USAGE,UPDATE')
           WHEN c.relkind IN ('r','p','v','m','f') THEN
             pg_catalog.has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
             OR pg_catalog.has_any_column_privilege(current_user, c.oid, 'INSERT,UPDATE,REFERENCES')
           ELSE false END))
FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
"""

TABLES = """
SELECT n.nspname, c.relname, c.oid, c.relkind, c.relrowsecurity,
       a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
       tn.nspname = 'pg_catalog' AND t.typtype = 'b'
       AND (t.typelem = 0 OR (etn.nspname = 'pg_catalog' AND et.typtype = 'b'))
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
LEFT JOIN pg_catalog.pg_type et ON et.oid = t.typelem
LEFT JOIN pg_catalog.pg_namespace etn ON etn.oid = et.typnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r','p','v','m','f')
AND pg_catalog.has_table_privilege(current_user, c.oid, 'SELECT')
ORDER BY n.nspname, c.relname, a.attnum
LIMIT 10001
"""


@contextmanager
def _connection(database: Database, authorize: Callable[[], None]):
    if not _slots.acquire(blocking=False):
        raise HTTPException(
            429, "Database console is busy", headers={"Retry-After": "1"}
        )
    connection = None
    try:
        authorize()
        dsn = os.environ.get(database.dsn_env, "")
        if not dsn or len(dsn) > 8192:
            raise DatabaseUnavailable("Database connection is unavailable")
        connection = psycopg.connect(
            dsn,
            connect_timeout=5,
            autocommit=False,
            application_name="eng-platform-readonly",
            options=(
                "-c default_transaction_read_only=on -c statement_timeout=5000 "
                "-c lock_timeout=1000 -c search_path=pg_catalog"
            ),
        )
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL search_path = pg_catalog")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute(ROLE_AUDIT)
            flags = cursor.fetchone()
            if flags is None or any(flags):
                raise DatabaseUnavailable(
                    "Dedicated SELECT-only database role is required"
                )
        authorize()
        yield connection
        authorize()
    except QueryRejected:
        raise
    except (psycopg.Error, OSError, ValueError, TypeError, OverflowError):
        # Provider diagnostics can contain SQL, values, users, hosts and DSNs.
        raise DatabaseUnavailable("Database operation is unavailable") from None
    finally:
        try:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
        except psycopg.Error:
            pass
        finally:
            _slots.release()


def _inventory(connection, database: Database) -> tuple[list[dict], dict]:
    """Exclude objects that can execute user code simply by being selected."""
    tables: dict[tuple[str, str], dict] = {}
    with connection.cursor() as cursor:
        cursor.execute(TABLES, (database.schemas,))
        # Bound introspection too; do not serialize an unbounded catalog.
        records = cursor.fetchmany(10_001)
        if len(records) > 10_000:
            raise DatabaseUnavailable("Database schema limit exceeded")
    for schema, name, oid, kind, rls, column, data_type, builtin in records:
        item = tables.setdefault(
            (schema, name),
            {"oid": oid, "safe": kind in {"r", "p"} and not rls, "columns": []},
        )
        item["safe"] = item["safe"] and builtin
        item["columns"].append({"name": column, "data_type": data_type})
    return (
        [
            {
                "schema": schema,
                "name": name,
                "columns": item["columns"],
                "service_names": database.services_for_table(schema, name),
            }
            for (schema, name), item in tables.items()
            if item["safe"]
        ],
        tables,
    )


def _check_relations(connection, database: Database, references: set, inventory: dict):
    if not references:
        return
    for reference in references:
        item = inventory.get(reference)
        if item is None or not item["safe"]:
            raise QueryRejected("Relation is not supported or authorized")
    # Default PostgreSQL table reads include inherited/partitioned children.
    # A child in another schema or a foreign child cannot escape the ACL.
    with connection.cursor() as cursor:
        cursor.execute(
            """WITH RECURSIVE descendants(oid) AS (
                   SELECT pg_catalog.unnest(%s::oid[]) UNION
                   SELECT i.inhrelid FROM pg_catalog.pg_inherits i
                   JOIN descendants d ON d.oid = i.inhparent)
                   SELECT n.nspname, c.relname FROM descendants d
                   JOIN pg_catalog.pg_class c ON c.oid = d.oid
                   JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace LIMIT 10001""",
            ([inventory[reference]["oid"] for reference in references],),
        )
        children = cursor.fetchmany(10_001)
    if len(children) > 10_000 or any(
        schema not in database.schemas
        or (schema, name) not in inventory
        or not inventory[(schema, name)]["safe"]
        for schema, name in children
    ):
        raise QueryRejected("Inherited relation is not supported or authorized")


def read_schema(database: Database, authorize: Callable[[], None]) -> dict:
    with _connection(database, authorize) as connection:
        tables, _ = _inventory(connection, database)
        result = {"tables": tables}
        if len(json.dumps(result).encode()) > MAX_RESPONSE_BYTES:
            raise DatabaseUnavailable("Database schema limit exceeded")
        return result


def query(
    database: Database, statement: str, max_rows: int, authorize: Callable[[], None]
) -> dict:
    references = validate_query(statement, database.schemas)
    normalized = RawStream()(parse_sql(statement)[0].stmt)
    started = monotonic()
    with _connection(database, authorize) as connection:
        _, inventory = _inventory(connection, database)
        _check_relations(connection, database, references, inventory)
        if references:
            with connection.cursor() as cursor:
                # Psycopg Identifier quotes every audited schema/table; this is
                # composed SQL, not SQLAlchemy string interpolation.
                cursor.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                    postgres_sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(
                        postgres_sql.SQL(",").join(
                            postgres_sql.Identifier(schema, table)
                            for schema, table in sorted(references)
                        )
                    )
                )
            # ACCESS SHARE locks prevent concurrent DDL from replacing safe
            # tables/columns/RLS policies between the audit and SQL dispatch.
            _, inventory = _inventory(connection, database)
            _check_relations(connection, database, references, inventory)
        authorize()
        with connection.cursor() as cursor:
            # The SQL-console fragment is a canonical SELECT after recursive
            # AST validation and locked relation audits; it cannot be a bind value.
            cursor.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                postgres_sql.SQL(
                    "SELECT * FROM ({}) AS console_describe LIMIT 0"
                ).format(postgres_sql.SQL(normalized))
            )
            columns = [column.name for column in cursor.description or ()]
        if not columns or len(columns) > MAX_COLUMNS:
            raise QueryRejected("Query column limit exceeded")
        aliases = [
            postgres_sql.Identifier(f"c{index}") for index in range(len(columns))
        ]
        # Bound each transfer before libpq receives it. Position-based aliases
        # preserve duplicate SQL column names and result column order.
        wrapped = postgres_sql.SQL(
            "SELECT CASE WHEN pg_catalog.octet_length(j.value) <= {row_limit} "
            "THEN j.value ELSE NULL END FROM ("
            "SELECT CASE WHEN pg_catalog.pg_column_size(t) <= {row_limit} "
            "THEN pg_catalog.json_build_array({values})::pg_catalog.text ELSE NULL END AS value "
            "FROM ({statement}) AS t({aliases})) AS j"
        ).format(
            row_limit=postgres_sql.Literal(MAX_ROW_BYTES),
            values=postgres_sql.SQL(",").join(aliases),
            statement=postgres_sql.SQL(normalized),
            aliases=postgres_sql.SQL(",").join(aliases),
        )
        rows: list[list] = []
        truncated = False
        size = len(json.dumps(columns).encode()) + 256
        with connection.cursor(name="eng_platform_readonly_results") as cursor:
            # Same validated canonical SELECT; aliases use Identifier and row
            # bounds use Literal, within the audited reader's READ ONLY transaction.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            cursor.execute(wrapped)
            fetched = cursor.fetchmany(max_rows + 1)
            for index, record in enumerate(fetched):
                if index >= max_rows or record[0] is None:
                    truncated = True
                    break
                row = json.loads(
                    record[0],
                    parse_float=str,
                    parse_int=lambda value: (
                        int(value)
                        if abs(int(value)) <= 9_007_199_254_740_991
                        else value
                    ),
                )
                encoded = json.dumps(row, ensure_ascii=True, allow_nan=False).encode()
                if (
                    any(
                        len(json.dumps(cell, ensure_ascii=True).encode())
                        > MAX_CELL_BYTES
                        for cell in row
                    )
                    or size + len(encoded) > MAX_RESPONSE_BYTES
                ):
                    truncated = True
                    break
                size += len(encoded)
                rows.append(row)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": round((monotonic() - started) * 1000),
        }
