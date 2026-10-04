"""Red-team tests for the SQL layer.

These are the most important tests in the project. Every one of them is an attempt
to make the system do something it must not do.

Note which layer each test exercises. Layer 2 (string validation) gives good error
messages. Layers 3 (the SQLite authorizer) and 4 (the read-only connection) are the
ones that actually hold, because they do not depend on parsing a string correctly.
A test suite that only covered layer 2 would prove very little.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from policydesk.tools.sql_safety import (
    ALLOWED_TABLES,
    UnsafeQuery,
    _make_authorizer,
    describe_schema,
    ensure_limit,
    referenced_tables,
    run_read_only_query,
    strip_comments,
    validate_sql,
)

# ---------------------------------------------------------------------------
# Layer 2: static validation
# ---------------------------------------------------------------------------


def test_a_plain_select_is_accepted() -> None:
    assert validate_sql("SELECT id, amount_usd FROM expenses") is not None


def test_a_common_table_expression_is_accepted() -> None:
    """WITH ... SELECT is normal analytical SQL and must not be blocked."""
    sql = "WITH totals AS (SELECT employee_id, SUM(amount_usd) s FROM expenses GROUP BY 1) "
    sql += "SELECT * FROM totals"
    assert validate_sql(sql) is not None


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO expenses (id) VALUES (1)",
        "UPDATE expenses SET amount_usd = 0",
        "DELETE FROM expenses",
        "DROP TABLE expenses",
        "ALTER TABLE expenses ADD COLUMN x TEXT",
        "CREATE TABLE evil (x TEXT)",
        "REPLACE INTO expenses (id) VALUES (1)",
    ],
)
def test_every_writing_statement_is_rejected(statement: str) -> None:
    with pytest.raises(UnsafeQuery):
        validate_sql(statement)


def test_a_second_statement_cannot_be_smuggled_in() -> None:
    """The classic injection shape: finish the query, then start another."""
    with pytest.raises(UnsafeQuery, match="single statement"):
        validate_sql("SELECT 1 FROM expenses; DROP TABLE expenses")


def test_a_statement_hidden_behind_a_comment_is_rejected() -> None:
    """Comments are stripped BEFORE checking, so this cannot hide anything."""
    with pytest.raises(UnsafeQuery):
        validate_sql("SELECT amount_usd FROM expenses /* harmless */ ; DELETE FROM expenses")


def test_a_keyword_inside_a_comment_does_not_cause_a_false_rejection() -> None:
    """The other side of the same coin: a legitimate query must not be blocked."""
    assert validate_sql("SELECT amount_usd FROM expenses -- no DELETE happens here") is not None


@pytest.mark.parametrize("statement", ["PRAGMA table_list", "VACUUM", "REINDEX expenses"])
def test_database_administration_statements_are_rejected(statement: str) -> None:
    with pytest.raises(UnsafeQuery):
        validate_sql(statement)


def test_attaching_another_database_file_is_rejected() -> None:
    """ATTACH would let a query read or write a file outside our database."""
    with pytest.raises(UnsafeQuery, match="ATTACH"):
        validate_sql("ATTACH DATABASE '/etc/passwd' AS leak")


def test_sqlite_internal_tables_are_not_allow_listed() -> None:
    """Reading sqlite_master would expose the entire schema and all other tables."""
    with pytest.raises(UnsafeQuery, match="may not be queried"):
        validate_sql("SELECT name FROM sqlite_master")


def test_a_forbidden_table_in_a_subquery_is_still_caught() -> None:
    with pytest.raises(UnsafeQuery, match="may not be queried"):
        validate_sql("SELECT (SELECT COUNT(*) FROM sqlite_master) AS n FROM expenses")


def test_a_query_touching_no_table_is_rejected() -> None:
    """`SELECT 1` is harmless but meaningless here, and usually a sign of confusion."""
    with pytest.raises(UnsafeQuery, match="does not read from any table"):
        validate_sql("SELECT 1")


def test_an_empty_query_is_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="empty"):
        validate_sql("   ")


def test_an_enormous_query_is_rejected() -> None:
    with pytest.raises(UnsafeQuery, match="over the"):
        validate_sql("SELECT id FROM expenses WHERE " + " OR ".join(["id = 1"] * 500))


def test_comment_stripping() -> None:
    assert "DELETE" not in strip_comments("SELECT 1 -- DELETE")
    assert "DELETE" not in strip_comments("SELECT /* DELETE\nmore */ 1")


def test_table_extraction_finds_joins() -> None:
    sql = "SELECT * FROM expenses e JOIN employees em ON em.id = e.employee_id"
    assert referenced_tables(sql) == {"expenses", "employees"}


# ---------------------------------------------------------------------------
# Row limits
# ---------------------------------------------------------------------------


def test_a_limit_is_appended_when_missing() -> None:
    assert "LIMIT 50" in ensure_limit("SELECT id FROM expenses", 50)


def test_an_existing_limit_is_not_duplicated() -> None:
    sql = ensure_limit("SELECT id FROM expenses LIMIT 5", 50)
    assert sql.lower().count("limit") == 1


# ---------------------------------------------------------------------------
# Layer 3: the authorizer. Tested directly, because it is the layer that holds.
# ---------------------------------------------------------------------------


def test_authorizer_allows_reading_an_allow_listed_table() -> None:
    authorizer = _make_authorizer(ALLOWED_TABLES)
    assert (
        authorizer(sqlite3.SQLITE_READ, "expenses", "amount_usd", "main", None) == sqlite3.SQLITE_OK
    )


def test_authorizer_denies_reading_anything_else() -> None:
    """No string parsing involved, so no clever SQL can get around this."""
    authorizer = _make_authorizer(ALLOWED_TABLES)
    for table in ("sqlite_master", "secrets", "sqlite_temp_master"):
        verdict = authorizer(sqlite3.SQLITE_READ, table, "x", "main", None)
        assert verdict == sqlite3.SQLITE_DENY, table


def test_authorizer_denies_every_operation_it_does_not_recognise() -> None:
    """Default-deny. A new SQLite action code is refused, not waved through."""
    authorizer = _make_authorizer(ALLOWED_TABLES)
    for action in (
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_PRAGMA,
        9999,  # an action code that does not exist yet
    ):
        assert authorizer(action, "expenses", None, "main", None) == sqlite3.SQLITE_DENY


# ---------------------------------------------------------------------------
# Layer 4: the read-only connection
# ---------------------------------------------------------------------------


def test_the_connection_itself_forbids_writing(sample_data: dict[str, Path]) -> None:
    """Proof that layers 2 and 3 are not the only thing standing in the way.

    Here we bypass all of our own validation and go straight to the database with
    the same read-only URI the tool uses. The write still fails.
    """
    uri = f"file:{sample_data['sqlite_db'].as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE expenses SET amount_usd = 0")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Running real queries
# ---------------------------------------------------------------------------


def test_a_real_query_returns_real_rows(sample_data: dict[str, Path]) -> None:
    result = run_read_only_query(
        sample_data["sqlite_db"],
        "SELECT id, amount_usd FROM expenses WHERE employee_id = 1 "
        "AND expense_date BETWEEN '2026-10-01' AND '2026-10-31' ORDER BY id",
    )

    assert result.columns == ("id", "amount_usd")
    assert result.row_count == 7
    assert result.rows[0] == (1101, 112.4)
    assert not result.truncated


def test_engineering_q3_travel_spend_is_exactly_1464_80(sample_data: dict[str, Path]) -> None:
    """A known-answer test. Verified by hand against the seeded rows.

    This is the shape of test that catches a text-to-SQL regression: a question
    whose correct answer we computed ourselves, independently of the code.
    """
    result = run_read_only_query(
        sample_data["sqlite_db"],
        """SELECT ROUND(SUM(e.amount_usd), 2) AS spend
           FROM expenses e
           JOIN employees em ON em.id = e.employee_id
           JOIN departments d ON d.id = em.department_id
           WHERE d.name = 'Engineering'
             AND e.category IN ('airfare', 'hotel', 'meals', 'ground_transport')
             AND e.expense_date BETWEEN '2026-07-01' AND '2026-09-30'""",
    )
    assert result.rows[0][0] == 1464.80


def test_truncation_is_reported_not_hidden(sample_data: dict[str, Path]) -> None:
    """A partial result presented as complete is a wrong answer."""
    result = run_read_only_query(sample_data["sqlite_db"], "SELECT id FROM expenses", row_limit=3)

    assert result.row_count == 3
    assert result.truncated is True


def test_a_rejected_query_never_reaches_the_database(sample_data: dict[str, Path]) -> None:
    with pytest.raises(UnsafeQuery):
        run_read_only_query(sample_data["sqlite_db"], "DELETE FROM expenses")


def test_a_missing_database_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(UnsafeQuery, match="no database at"):
        run_read_only_query(tmp_path / "nope.db", "SELECT id FROM expenses")


# ---------------------------------------------------------------------------
# Schema description
# ---------------------------------------------------------------------------


def test_schema_lists_only_allow_listed_tables(sample_data: dict[str, Path]) -> None:
    schema = describe_schema(sample_data["sqlite_db"])

    for table in ALLOWED_TABLES:
        assert f"{table}(" in schema
    # There is no reason to tell the model about a table it may not read.
    assert "sqlite_master" not in schema


def test_schema_lists_the_values_of_low_cardinality_columns(
    sample_data: dict[str, Path],
) -> None:
    """The fix for the real failure described in `describe_schema`.

    Without these values the model does not know that 'travel' is not a category,
    and answers CANNOT_ANSWER or invents a filter that matches nothing.
    """
    schema = describe_schema(sample_data["sqlite_db"])

    assert "expenses.category values:" in schema
    assert "'airfare'" in schema
    assert "'ground_transport'" in schema
    assert "expenses.status values:" in schema
    assert "departments.name values:" in schema


def test_schema_does_not_dump_high_cardinality_columns(sample_data: dict[str, Path]) -> None:
    """Listing every merchant would waste the prompt budget and help nobody."""
    schema = describe_schema(sample_data["sqlite_db"])
    assert "expenses.merchant values:" not in schema
    assert "expenses.description values:" not in schema
