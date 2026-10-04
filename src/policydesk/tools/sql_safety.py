"""Making a model-written SQL query safe to run. Four independent layers.

Why text-to-SQL is dangerous, stated plainly
--------------------------------------------
We are taking text written by a language model and executing it against a
database. The model is helpful, not trustworthy, and the text it writes can be
influenced by anything in its prompt -- including a document someone else wrote.

So the question is never "will the model behave?". It is "what is the worst thing
this query could do if the model were actively hostile?" The answer must be
"nothing", and it must be true because of the code, not because of the prompt.

The four layers, from weakest to strongest
------------------------------------------
1. **The prompt.** We ask for a single read-only SELECT. Helpful for quality,
   worth zero as security. Listed first so its place is clear.
2. **Static validation (this file).** Parse-ish checks on the string: must be one
   SELECT, no dangerous keywords, only allow-listed tables, a LIMIT enforced.
   Good, but string checks can always be fooled by something clever.
3. **A SQLite authorizer callback.** The database itself asks our function to
   approve every single operation it is about to perform, by type and by table.
   This does not involve parsing anything, so cleverness in the SQL string cannot
   evade it. This is the layer that actually holds.
4. **A read-only connection.** The file is opened `mode=ro`, so a write is
   impossible at the operating-system level even if layers 2 and 3 both failed.

Layers 3 and 4 are why this is defensible. Layer 2 exists to give a *clear error
message* -- "you may not query that table" is far more useful than a database
permission failure. Never present layer 1 or 2 as the protection.

Also here: a row limit and a time limit, because a correct, permitted query can
still return a million rows or run for an hour.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Tables the model may read. Anything else is denied -- including SQLite's own
# internal tables, which would otherwise expose the whole schema.
ALLOWED_TABLES: frozenset[str] = frozenset({"departments", "employees", "expenses", "budgets"})

# Whole words that have no business in a read-only query.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "truncate",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "grant",
    "revoke",
    "commit",
    "rollback",
    "savepoint",
    "trigger",
)

MAX_QUERY_CHARS = 2_000
# The progress handler runs every N virtual-machine instructions; we use it to
# enforce a wall-clock deadline, because SQLite has no query timeout of its own.
PROGRESS_INSTRUCTION_INTERVAL = 1_000


class UnsafeQuery(ValueError):
    """The query was rejected before it reached the database."""


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    sql_executed: str
    truncated: bool
    duration_ms: int

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def as_table(self, max_rows: int = 25) -> str:
        """Render as plain text, for a prompt or a terminal."""
        if not self.rows:
            return "(no rows)"
        shown = self.rows[:max_rows]
        widths = [
            max(len(str(self.columns[i])), *(len(str(row[i])) for row in shown))
            for i in range(len(self.columns))
        ]
        header = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(self.columns))
        rule = "  ".join("-" * widths[i] for i in range(len(self.columns)))
        body = [
            "  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)) for row in shown
        ]
        out = [header, rule, *body]
        if len(self.rows) > max_rows:
            out.append(f"... {len(self.rows) - max_rows} more row(s)")
        return "\n".join(out)


def strip_comments(sql: str) -> str:
    """Remove SQL comments before checking anything.

    This has to happen first. `SELECT 1 -- ; DROP TABLE x` and
    `SELECT /* DELETE */ 1` would otherwise either hide a statement or trip a
    keyword check on a harmless comment.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def cte_names(sql: str) -> set[str]:
    """Names defined by a WITH clause.

    Bug this fixes: `WITH totals AS (SELECT ... FROM expenses) SELECT * FROM totals`
    was rejected, because `totals` looked like a table that is not allow-listed.
    It is not a table at all -- it is a name for a result inside this one query.

    Common table expressions are ordinary analytical SQL, so rejecting them would
    have quietly crippled the tool for any question needing two steps. Caught by a
    test, not by inspection, which is rather the point of writing the test.
    """
    pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.IGNORECASE)
    return {match.group(1).lower() for match in pattern.finditer(sql)}


def referenced_tables(sql: str) -> set[str]:
    """Table names appearing after FROM or JOIN.

    Deliberately crude, and that is fine: it is used to produce a good error
    message and to catch the obvious cases. The authorizer, which does not parse
    anything, is what actually enforces the allow-list.
    """
    pattern = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
    return {match.group(1).lower() for match in pattern.finditer(sql)}


def validate_sql(sql: str, allowed_tables: frozenset[str] = ALLOWED_TABLES) -> str:
    """Layer 2. Return the cleaned query, or raise UnsafeQuery with the reason."""
    if not sql or not sql.strip():
        raise UnsafeQuery("the query was empty")

    if len(sql) > MAX_QUERY_CHARS:
        raise UnsafeQuery(f"the query is {len(sql)} characters, over the {MAX_QUERY_CHARS} limit")

    cleaned = strip_comments(sql).strip().rstrip(";").strip()

    # More than one statement left after removing a single trailing semicolon
    # means someone tried to smuggle a second one in.
    if ";" in cleaned:
        raise UnsafeQuery(
            "only a single statement is allowed, and this query contains more than one"
        )

    if not re.match(r"^\s*(with|select)\b", cleaned, re.IGNORECASE):
        first_word = cleaned.split()[0] if cleaned.split() else "(nothing)"
        raise UnsafeQuery(f"only SELECT queries are allowed; this one starts with '{first_word}'")

    lowered = cleaned.lower()
    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            raise UnsafeQuery(f"the query contains the forbidden keyword '{keyword.upper()}'")

    tables = referenced_tables(cleaned)
    # Names defined by a WITH clause are not tables, so they are not subject to
    # the table allow-list. The authorizer still governs the REAL tables they read.
    defined_here = cte_names(cleaned)
    not_allowed = tables - allowed_tables - defined_here
    if not_allowed:
        raise UnsafeQuery(
            f"these tables may not be queried: {', '.join(sorted(not_allowed))}. "
            f"Allowed tables: {', '.join(sorted(allowed_tables))}."
        )
    if not tables - defined_here:
        raise UnsafeQuery("the query does not read from any table")

    return cleaned


def ensure_limit(sql: str, row_limit: int) -> str:
    """Append a LIMIT when there is none.

    A permitted, correct query can still return every row in the table. We cap it
    here for efficiency, and cap it again when fetching, because this string check
    can be fooled by a LIMIT inside a subquery.
    """
    if re.search(r"\blimit\s+\d+\s*$", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {row_limit}"


def _make_authorizer(allowed_tables: frozenset[str]):
    """Layer 3: SQLite asks us to approve every operation, by type and table.

    This is the strongest string-independent control available. SQLite calls this
    function as it prepares the statement, once per operation, and we approve only
    reads of allow-listed tables.
    """

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
        # Reading a column: arg1 is the table, arg2 the column.
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            return sqlite3.SQLITE_OK if table in allowed_tables else sqlite3.SQLITE_DENY
        # SELECT itself, and scalar/aggregate functions, are fine.
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        # Everything else -- every write, every schema change, ATTACH, PRAGMA --
        # is denied by default. Default-deny, not default-allow.
        return sqlite3.SQLITE_DENY

    return authorizer


def run_read_only_query(
    database_path: Path,
    sql: str,
    *,
    row_limit: int = 200,
    timeout_seconds: float = 5.0,
    allowed_tables: frozenset[str] = ALLOWED_TABLES,
) -> QueryResult:
    """Validate and execute a SELECT, with all four layers in place."""
    if not database_path.exists():
        raise UnsafeQuery(f"no database at {database_path}")

    validated = validate_sql(sql, allowed_tables)
    # One more than we will keep, so truncation is detectable. See ensure_limit.
    limited = ensure_limit(validated, row_limit + 1)

    # Layer 4: the file is opened read-only, so writes are impossible even if
    # every check above were somehow bypassed.
    uri = f"file:{database_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)

    started = time.monotonic()
    deadline = started + timeout_seconds

    def abort_if_over_time() -> int:
        # Returning non-zero tells SQLite to abort the statement. This is the
        # only way to stop a long-running query, as SQLite has no timeout of
        # its own for a query that is making progress.
        return 1 if time.monotonic() > deadline else 0

    try:
        connection.set_progress_handler(abort_if_over_time, PROGRESS_INSTRUCTION_INTERVAL)
        connection.set_authorizer(_make_authorizer(allowed_tables))

        cursor = connection.execute(limited)
        columns = tuple(description[0] for description in cursor.description or ())
        # Fetch one more than the limit so we can tell the user it was truncated
        # rather than silently showing a partial answer as if it were complete.
        fetched = cursor.fetchmany(row_limit + 1)
        truncated = len(fetched) > row_limit
        rows = tuple(tuple(row) for row in fetched[:row_limit])
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "interrupted" in message:
            raise UnsafeQuery(
                f"the query took longer than {timeout_seconds:.0f}s and was stopped"
            ) from exc
        if "not authorized" in message or "prohibited" in message:
            raise UnsafeQuery(
                "the database refused that query: it touches something this tool "
                "is not permitted to read"
            ) from exc
        raise UnsafeQuery(f"the query could not be run: {exc}") from exc
    finally:
        connection.set_authorizer(None)
        connection.close()

    return QueryResult(
        columns=columns,
        rows=rows,
        sql_executed=limited,
        truncated=truncated,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# A text column with at most this many distinct values has them listed in the
# schema. See the long comment in `describe_schema` for why this matters so much.
MAX_DISTINCT_VALUES_TO_LIST = 12


def describe_schema(database_path: Path, allowed_tables: frozenset[str] = ALLOWED_TABLES) -> str:
    """A compact schema description for the model's prompt.

    Only allow-listed tables appear: there is no reason to tell the model about a
    table it may not read.

    Why the distinct values are included -- a lesson learned the hard way
    --------------------------------------------------------------------
    The first version of this function listed only column names and types. Asked
    "what did Engineering spend on **travel** in Q3?", the model replied
    CANNOT_ANSWER -- correctly, from its point of view. It could see a column
    called `category`, but had no idea whether "travel" was one of its values.

    In fact `category` holds airfare, hotel, meals, ground_transport and so on.
    "Travel" is several of them together. The model could not know that, because
    nobody told it.

    So for every low-cardinality text column we list the actual values. This is
    the cheapest and largest single improvement you can make to text-to-SQL
    reliability, and it is nearly always missing from a first implementation.

    It is only done for columns with few distinct values. Listing 5,000 merchant
    names would blow the prompt budget and help nobody.
    """
    uri = f"file:{database_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        lines: list[str] = []
        for table in sorted(allowed_tables):
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                continue

            columns = ", ".join(f"{row[1]} {row[2]}" for row in info)
            lines.append(f"{table}({columns})")

            for row in info:
                column, declared_type = row[1], (row[2] or "").upper()
                if "TEXT" not in declared_type:
                    continue
                distinct = connection.execute(
                    f"SELECT DISTINCT {column} FROM {table} "  # noqa: S608 - names from PRAGMA
                    f"WHERE {column} IS NOT NULL LIMIT {MAX_DISTINCT_VALUES_TO_LIST + 1}"
                ).fetchall()
                if 0 < len(distinct) <= MAX_DISTINCT_VALUES_TO_LIST:
                    values = ", ".join(repr(str(value[0])) for value in distinct)
                    lines.append(f"  -- {table}.{column} values: {values}")

        return "\n".join(lines)
    finally:
        connection.close()
