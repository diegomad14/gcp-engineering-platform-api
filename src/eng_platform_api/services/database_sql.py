"""Parse PostgreSQL syntax and admit a deliberately small SELECT language."""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.parser import ParseError


class QueryRejected(ValueError):
    """The statement is outside the console's supported read-only language."""


SAFE_FUNCTIONS = frozenset(
    "count sum avg min max abs ceil ceiling floor round trunc lower upper length "
    "char_length character_length octet_length substring substr left right trim "
    "btrim ltrim rtrim replace concat concat_ws date_trunc date_part extract "
    "now transaction_timestamp age to_char row_number rank dense_rank lag lead "
    "first_value last_value nth_value nullif greatest least".split()
)
SAFE_TYPES = frozenset(
    "bool boolean int2 int4 int8 smallint integer bigint numeric decimal real "
    "float4 float8 double precision text varchar bpchar char character date "
    "timestamp timestamptz time timetz interval uuid json jsonb bytea".split()
)
SAFE_OPERATORS = frozenset(
    "+ - * / % ^ = <> != < > <= >= ~~ !~~ ~~* !~~* || -> ->> #> #>> @> <@ ? ?| ?&".split()
)
SAFE_NODES = frozenset(
    "SelectStmt ResTarget ColumnRef A_Star A_Const Integer Float String Boolean "
    "RangeVar RangeSubselect JoinExpr Alias BoolExpr A_Expr SortBy TypeCast TypeName "
    "FuncCall SQLValueFunction CoalesceExpr MinMaxExpr NullTest BooleanTest SubLink "
    "CaseExpr CaseWhen RowExpr A_ArrayExpr A_Indices A_Indirection WindowDef "
    "GroupingSet GroupingFunc CommonTableExpr WithClause".split()
)


def _names(nodes) -> list[str]:
    if not nodes or any(not isinstance(item, ast.String) for item in nodes):
        raise QueryRejected("Unsupported identifier")
    return [item.sval for item in nodes]


def validate_query(sql: str, schemas: list[str]) -> set[tuple[str, str]]:
    """Validate every nested statement and track CTE lexical visibility correctly."""
    try:
        statements = parse_sql(sql)
        if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
            raise QueryRejected("Only one SELECT is supported")
        relations: set[tuple[str, str]] = set()
        visited = 0

        def visit(node, visible: set[str]) -> None:
            nonlocal visited
            if isinstance(node, (tuple, list)):
                for item in node:
                    visit(item, visible)
                return
            if not isinstance(node, ast.Node):
                return
            visited += 1
            if visited > 4096 or type(node).__name__ not in SAFE_NODES:
                raise QueryRejected("Unsupported SQL construct")
            if isinstance(node, ast.SelectStmt):
                if node.intoClause or node.lockingClause or node.valuesLists:
                    raise QueryRejected("SELECT INTO and locking are disabled")
                local = set(visible)
                if node.withClause:
                    for cte in node.withClause.ctes:
                        scope = (
                            local | {cte.ctename}
                            if node.withClause.recursive
                            else local
                        )
                        if not isinstance(cte.ctequery, ast.SelectStmt):
                            raise QueryRejected("Writing CTE is disabled")
                        visit(cte.ctequery, scope)
                        if cte.search_clause or cte.cycle_clause:
                            raise QueryRejected("Unsupported CTE construct")
                        visit(cte.aliascolnames, local)
                        local.add(cte.ctename)
                for name in node:
                    if name != "withClause":
                        visit(getattr(node, name), local)
                return
            if isinstance(node, ast.RangeVar):
                if node.catalogname or node.relname is None:
                    raise QueryRejected("Unsupported relation")
                if node.schemaname is None:
                    if node.relname not in visible:
                        raise QueryRejected("Tables must be schema-qualified")
                elif node.schemaname not in schemas:
                    raise QueryRejected("Schema is not authorized")
                else:
                    relations.add((node.schemaname, node.relname))
            if isinstance(node, ast.FuncCall):
                names = _names(node.funcname)
                if (
                    len(names) > 2
                    or (len(names) == 2 and names[0] != "pg_catalog")
                    or names[-1] not in SAFE_FUNCTIONS
                ):
                    raise QueryRejected("Function is not supported")
            if isinstance(node, ast.A_Expr):
                names = _names(node.name)
                between = (
                    node.kind.name
                    in {
                        "AEXPR_BETWEEN",
                        "AEXPR_NOT_BETWEEN",
                        "AEXPR_BETWEEN_SYM",
                        "AEXPR_NOT_BETWEEN_SYM",
                    }
                    and len(names) == 1
                    and names[0]
                    in {
                        "BETWEEN",
                        "NOT BETWEEN",
                        "BETWEEN SYMMETRIC",
                        "NOT BETWEEN SYMMETRIC",
                    }
                )
                if not between and (len(names) != 1 or names[0] not in SAFE_OPERATORS):
                    raise QueryRejected("Operator is not supported")
            if isinstance(node, ast.SortBy) and node.useOp:
                raise QueryRejected("Custom ordering is disabled")
            if isinstance(node, ast.TypeName):
                names = _names(node.names)
                if (
                    len(names) > 2
                    or (len(names) == 2 and names[0] != "pg_catalog")
                    or names[-1] not in SAFE_TYPES
                    or node.setof
                    or node.pct_type
                ):
                    raise QueryRejected("Type is not supported")
            if isinstance(node, ast.SQLValueFunction) and node.op.name not in {
                "SVFOP_CURRENT_DATE",
                "SVFOP_CURRENT_TIME",
                "SVFOP_CURRENT_TIME_N",
                "SVFOP_CURRENT_TIMESTAMP",
                "SVFOP_CURRENT_TIMESTAMP_N",
                "SVFOP_LOCALTIME",
                "SVFOP_LOCALTIME_N",
                "SVFOP_LOCALTIMESTAMP",
                "SVFOP_LOCALTIMESTAMP_N",
            }:
                raise QueryRejected("SQL value function is not supported")
            for name in node:
                visit(getattr(node, name), visible)

        visit(statements[0].stmt, set())
        return relations
    except QueryRejected:
        raise
    except (ParseError, ValueError, TypeError, AttributeError, RecursionError):
        raise QueryRejected("Invalid or unsupported SELECT") from None
