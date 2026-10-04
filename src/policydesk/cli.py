"""Command line entry point.

Commands
--------
doctor         check configuration and report readiness
generate-data  build the sample policy PDFs, spreadsheet and database
extract        read the sample files and show what came out, with locations
inspect-db     show the database tables and a couple of example queries

Every command is read-only with respect to your settings, prints what it did,
and returns a process exit code (0 = fine). That last part matters: a command
that prints "error" but exits 0 will quietly pass in a CI pipeline forever.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import Settings, get_settings
from .ingest import ExtractionReport, extract_excel, extract_pdf
from .logging_setup import configure_logging, get_logger


def _load_settings_or_explain() -> Settings | None:
    """Load settings, or print a clear explanation and return None."""
    try:
        return get_settings()
    except ValidationError as exc:
        print("Configuration problem. The app did not start.\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(settings)"
            print(f"  - {location}: {error['msg']}", file=sys.stderr)
        print("\nHint: copy .env.example to .env and fill it in.", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor() -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    log = get_logger("policydesk.doctor")

    print("PolicyDesk configuration")
    print("-" * 46)
    for key, value in settings.safe_summary().items():
        print(f"  {key:<20} {value}")
    print("-" * 46)

    ready = settings.is_test or settings.llm_provider != "openai" or bool(settings.openai_api_key)
    print("Status: READY" if ready else "Status: NOT READY - no model credentials")

    # Proof that redaction works: we deliberately try to log a fake secret.
    log.info("doctor_check_complete", ready=ready, demo_leak="sk-THIS_SHOULD_BE_HIDDEN_12345")
    return 0 if ready else 1


# ---------------------------------------------------------------------------
# generate-data
# ---------------------------------------------------------------------------


def cmd_generate_data() -> int:
    from .sample_data import generate_all  # imported lazily: heavy, rarely needed

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    created = generate_all(settings.data_dir, settings.sqlite_path)

    print("Sample data created:\n")
    for label, path in created.items():
        size_kb = path.stat().st_size / 1024
        print(f"  {label:<22} {path.relative_to(Path.cwd()) if path.is_absolute() else path}")
        print(f"  {'':<22} {size_kb:,.1f} KB")
    print("\nNext: policydesk extract")
    return 0


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def _print_report(report: ExtractionReport, show_blocks: int) -> None:
    print(f"\n{'=' * 78}")
    print(f"FILE     {report.source_id}")
    print(f"TYPE     {report.source_type}")
    if report.pages is not None:
        print(f"PAGES    {report.pages}")
    if report.sheets:
        print(f"SHEETS   {', '.join(report.sheets)}")
    print(f"BLOCKS   {report.block_count}  ({report.total_chars:,} characters)")

    for note in report.notes:
        print(f"  note     {note}")
    for warning in report.warnings:
        print(f"  WARNING  {warning}")

    for block in report.blocks[:show_blocks]:
        preview = block.text.replace("\n", " ")
        if len(preview) > 150:
            preview = preview[:150] + "..."
        print(f"\n  [{block.kind}] cite as: {block.citation}")
        print(f"      {preview}")

    hidden = report.block_count - min(show_blocks, report.block_count)
    if hidden > 0:
        print(f"\n  ... {hidden} more block(s) not shown (use --show to see more)")


def cmd_extract(show_blocks: int) -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    raw = settings.data_dir / "raw"
    pdfs = sorted(raw.glob("**/*.pdf"))
    workbooks = sorted(raw.glob("**/*.xlsx"))

    if not pdfs and not workbooks:
        print(f"No source files under {raw}.", file=sys.stderr)
        print("Run this first:  policydesk generate-data", file=sys.stderr)
        return 1

    reports = [extract_pdf(path) for path in pdfs] + [extract_excel(path) for path in workbooks]
    for report in reports:
        _print_report(report, show_blocks)

    print(f"\n{'=' * 78}")
    print("SUMMARY")
    for report in reports:
        print(f"  {report.summary_line()}")

    needs_review = [r for r in reports if r.needs_human_review]
    print(f"\n  {len(reports)} file(s), {sum(r.block_count for r in reports)} block(s) total")
    if needs_review:
        print(f"  {len(needs_review)} file(s) need a human to look at them:")
        for report in needs_review:
            print(f"    - {report.source_id}")
        print("\n  This is the point of the exercise. A pipeline that hides these")
        print("  files indexes nothing and reports success.")

    # Exit code stays 0: files needing review is expected, not a crash.
    return 0


# ---------------------------------------------------------------------------
# inspect-db
# ---------------------------------------------------------------------------

EXAMPLE_QUERIES: list[tuple[str, str]] = [
    (
        "Rows per table",
        """SELECT 'departments' AS table_name, COUNT(*) AS rows FROM departments
           UNION ALL SELECT 'employees', COUNT(*) FROM employees
           UNION ALL SELECT 'expenses', COUNT(*) FROM expenses
           UNION ALL SELECT 'budgets', COUNT(*) FROM budgets""",
    ),
    (
        "Q3 2026 travel-related spend by department",
        """SELECT d.name AS department, ROUND(SUM(e.amount_usd), 2) AS spend_usd
           FROM expenses e
           JOIN employees   em ON em.id = e.employee_id
           JOIN departments d  ON d.id  = em.department_id
           WHERE e.expense_date BETWEEN '2026-07-01' AND '2026-09-30'
           GROUP BY d.name
           ORDER BY spend_usd DESC""",
    ),
    (
        "Priya Raman's October 2026 expenses",
        """SELECT e.id, e.expense_date, e.category, e.amount_usd, e.country,
                  e.receipt_attached, e.approval_ref
           FROM expenses e
           JOIN employees em ON em.id = e.employee_id
           WHERE em.name = 'Priya Raman'
             AND e.expense_date BETWEEN '2026-10-01' AND '2026-10-31'
           ORDER BY e.expense_date""",
    ),
]


def cmd_inspect_db() -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    if not settings.sqlite_path.exists():
        print(f"No database at {settings.sqlite_path}.", file=sys.stderr)
        print("Run this first:  policydesk generate-data", file=sys.stderr)
        return 1

    # Opening read-only, because nothing in this command should ever write.
    uri = f"file:{settings.sqlite_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        for title, sql in EXAMPLE_QUERIES:
            print(f"\n{title}")
            print("-" * len(title))
            cursor = connection.execute(sql)
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
            widths = [
                max(len(columns[i]), *(len(str(row[i])) for row in rows))
                if rows
                else len(columns[i])
                for i in range(len(columns))
            ]
            print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
            print("  " + "  ".join("-" * widths[i] for i in range(len(columns))))
            for row in rows:
                print("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))
    finally:
        connection.close()

    print("\nNote: the policy LIMITS are not in this database on purpose.")
    print("They live only in the PDF. That is what forces the agent to use both.")
    return 0


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="policydesk", description="PolicyDesk tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check configuration and report readiness")
    sub.add_parser("generate-data", help="build the sample PDFs, spreadsheet and database")
    extract_parser = sub.add_parser("extract", help="read source files and show what came out")
    extract_parser.add_argument(
        "--show",
        type=int,
        default=3,
        metavar="N",
        help="how many extracted blocks to preview per file (default 3)",
    )
    sub.add_parser("inspect-db", help="show the database contents and example queries")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "generate-data":
        return cmd_generate_data()
    if args.command == "extract":
        return cmd_extract(show_blocks=args.show)
    if args.command == "inspect-db":
        return cmd_inspect_db()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
