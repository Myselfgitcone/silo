"""Decide whether a piece of SQL is safe to run against the lake.

The rules are deliberately narrow: one read-only query over the published
tables, nothing that touches files, the network, settings or the catalog. The
engine also runs with external access switched off, so this is the first of two
locks, and the one that explains to the agent what it did wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class QueryRejected(ValueError):
    """The SQL broke a rule. The message is written for the agent to act on."""


# Anything that writes, changes state or reaches outside the query.
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Pragma,
    exp.Set,
    exp.Use,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.LoadData,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

# Function names that read files, the environment or engine internals.
_FORBIDDEN_FUNCTION_PREFIXES = (
    "read_",
    "glob",
    "getenv",
    "query",
    "sniff_",
    "parquet_",
    "duckdb_",
    "pragma_",
    "iceberg_",
    "delta_",
    "http",
    "current_setting",
    "load",
    "install",
)


@dataclass(frozen=True)
class CheckedQuery:
    sql: str
    """The query as it will run, with a row cap applied."""
    tables: frozenset[str]


def check(sql: str, allowed_tables: set[str], max_rows: int) -> CheckedQuery:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise QueryRejected("The query is empty.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as error:
        raise QueryRejected(f"Couldn't parse the SQL: {_first_line(error)}") from None

    if len(statements) != 1:
        raise QueryRejected("Send one statement at a time.")
    query = statements[0]
    if not isinstance(query, exp.Query):
        raise QueryRejected("Only SELECT queries are allowed. The lake is read-only.")

    for node in query.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise QueryRejected(f"{node.key.upper()} isn't allowed. The lake is read-only.")

    for function in query.find_all(exp.Func):
        name = _function_name(function)
        if name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
            raise QueryRejected(f"The function {name}() isn't allowed. Query the lake tables.")

    ctes = {cte.alias_or_name.lower() for cte in query.find_all(exp.CTE)}
    used = set()
    for table in query.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise QueryRejected("Table functions aren't allowed. Query the lake tables by name.")
        if table.args.get("db") or table.args.get("catalog"):
            raise QueryRejected(f"Use bare table names, not {table.sql('duckdb')}.")
        name = table.name.lower()
        if name in ctes:
            continue
        if name not in allowed_tables:
            known = ", ".join(sorted(allowed_tables))
            raise QueryRejected(f"There's no table named {table.name}. Tables: {known}.")
        used.add(name)

    # Ask for one row past the cap so the caller can tell the result was cut.
    capped = f"select * from ({query.sql(dialect='duckdb')}) as q limit {max_rows + 1}"
    return CheckedQuery(sql=capped, tables=frozenset(used))


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return str(function.this).lower()
    return function.sql_name().lower()


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0]
