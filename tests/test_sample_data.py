"""Tests for the generated sample dataset.

The most important test in this file is the determinism one. If the dataset
changes between runs, every evaluation score we produce later is meaningless,
because we could never tell a real improvement from a different dataset.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from policydesk.sample_data import EXPENSES, generate_all


def test_all_artefacts_are_created(sample_data: dict[str, Path]) -> None:
    expected = {
        "travel_policy_pdf",
        "approval_policy_pdf",
        "scanned_receipt_pdf",
        "expense_workbook",
        "sqlite_db",
    }
    assert set(sample_data) == expected
    for label, path in sample_data.items():
        assert path.exists(), f"{label} was not written"
        assert path.stat().st_size > 1000, f"{label} looks suspiciously small"


def test_generation_is_deterministic(tmp_path: Path) -> None:
    """Build the dataset twice and compare the database contents exactly."""

    def dump(db_path: Path) -> list[tuple]:
        connection = sqlite3.connect(db_path)
        try:
            return connection.execute(
                "SELECT id, employee_id, expense_date, category, amount_usd, status "
                "FROM expenses ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    first = generate_all(tmp_path / "a", tmp_path / "a" / "db.sqlite")
    second = generate_all(tmp_path / "b", tmp_path / "b" / "db.sqlite")

    assert dump(first["sqlite_db"]) == dump(second["sqlite_db"])


def test_database_row_counts(sample_data: dict[str, Path]) -> None:
    connection = sqlite3.connect(sample_data["sqlite_db"])
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("departments", "employees", "expenses", "budgets")
        }
    finally:
        connection.close()

    assert counts["departments"] == 4
    assert counts["employees"] == 5
    assert counts["expenses"] == len(EXPENSES)
    assert counts["budgets"] == 8


def test_priya_has_seven_october_expenses(sample_data: dict[str, Path]) -> None:
    """This is the data behind the project's flagship question."""
    connection = sqlite3.connect(sample_data["sqlite_db"])
    try:
        rows = connection.execute(
            """SELECT e.id, e.category, e.amount_usd, e.country, e.receipt_attached,
                      e.approval_ref
               FROM expenses e JOIN employees em ON em.id = e.employee_id
               WHERE em.name = 'Priya Raman'
                 AND e.expense_date BETWEEN '2026-10-01' AND '2026-10-31'
               ORDER BY e.id"""
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == 7

    by_id = {row[0]: row for row in rows}
    # The three planted violations, each breaking a different policy section.
    assert by_id[1101][2] == 112.40  # meals in Japan, over the USD 85 international cap
    assert by_id[1105][5] is None  # business class with no VP approval reference
    assert by_id[1106][4] == 0  # USD 89 with no receipt, over the USD 75 threshold


def test_policy_limits_are_not_in_the_database(sample_data: dict[str, Path]) -> None:
    """The whole design depends on this.

    If the limits were a database column, a single SQL query could answer the
    flagship question and there would be no reason to build an agent. The limits
    live only in the PDF, so the system is forced to use both sources.
    """
    connection = sqlite3.connect(sample_data["sqlite_db"])
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        columns: list[str] = []
        for table in tables:
            columns += [
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            ]
    finally:
        connection.close()

    assert set(tables) == {"departments", "employees", "expenses", "budgets"}
    forbidden = {"limit", "limit_usd", "per_diem", "cap", "policy_limit", "threshold"}
    assert not forbidden.intersection(c.lower() for c in columns)


def test_database_rejects_an_invalid_status(sample_data: dict[str, Path]) -> None:
    """The schema itself enforces the rule, not just the code that writes to it.

    A constraint in the database is the only kind nobody can forget to apply.
    """
    connection = sqlite3.connect(sample_data["sqlite_db"])
    try:
        try:
            connection.execute(
                "INSERT INTO expenses (id, employee_id, expense_date, category, merchant, "
                "amount_usd, country, city, description, receipt_attached, approval_ref, "
                "status, submitted_at) VALUES "
                "(99999, 1, '2026-10-01', 'meals', 'X', 10.0, 'USA', 'SF', 'd', 1, NULL, "
                "'not_a_real_status', '2026-10-02')"
            )
        except sqlite3.IntegrityError:
            pass  # expected
        else:
            raise AssertionError("the status CHECK constraint did not fire")
    finally:
        connection.rollback()
        connection.close()
