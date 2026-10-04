"""Reading spreadsheets, one row at a time, without assuming they are tidy.

Why spreadsheets are harder than they look
------------------------------------------
A spreadsheet is a drawing surface that happens to hold data. Real ones contain:

- a title and some notes above the actual table, so **the header is not row 1**
- blank rows used as visual spacing
- totals rows at the bottom that are not data
- formulas instead of values
- several sheets, only some of which hold data

If you assume "row 1 is the header, everything below is data", you will index
the company logo as a column name. So we **find** the header instead of assuming
it, and we report what we decided so a human can check.

One row per block, with the column names repeated
------------------------------------------------
We turn each data row into a line like:

    Expense ID: 1101 | Date: 2026-10-03 | Category: meals | Amount (USD): 112.4

The column names are repeated on every row on purpose. Later, search will return
one row out of hundreds, with no neighbours for context. A bare `112.4` is
meaningless; `Amount (USD): 112.4` can be cited and used.

A real limitation, stated honestly
----------------------------------
We read with `data_only=True`, which asks for the *last calculated value* of a
formula rather than the formula text. Excel stores that cached value only after
Excel itself has opened and saved the file. A file written by a script has no
cached values, so formula cells read as empty. We detect that and warn, rather
than reporting a total of zero. This is a genuine, common data-quality trap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .models import ExtractionReport, SourceBlock

# A candidate header row must have at least this many non-empty cells, and at
# least this share of them must be text rather than numbers. Headers are words.
MIN_HEADER_CELLS = 3
MIN_HEADER_TEXT_SHARE = 0.6
# How far down the sheet we are willing to look for a header before giving up.
MAX_HEADER_SEARCH_ROWS = 15
# A data row must fill at least this many columns. One filled cell under a
# 9-column header is a totals or label row, not a record.
MIN_PAIRS_PER_ROW = 2


def _as_text(value: Any) -> str:
    """Render one cell as a short string, with no surprises."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _looks_like_header(cells: list[Any]) -> bool:
    """Decide whether a row is the column-name row.

    The rule, in words: enough filled cells, and most of them are words rather
    than numbers. It is a heuristic -- a guess with a reason. It will be wrong on
    some real spreadsheets, which is exactly why we report the row we chose.
    """
    filled = [_as_text(cell) for cell in cells if _as_text(cell)]
    if len(filled) < MIN_HEADER_CELLS:
        return False

    def is_wordy(text: str) -> bool:
        return not text.replace(".", "", 1).replace("-", "", 1).isdigit()

    wordy = sum(1 for text in filled if is_wordy(text))
    return wordy / len(filled) >= MIN_HEADER_TEXT_SHARE


def _find_header_row(rows: list[tuple[Any, ...]]) -> int | None:
    """Return the 0-based index of the header row, or None if we cannot find one."""
    limit = min(len(rows), MAX_HEADER_SEARCH_ROWS)
    for index in range(limit):
        if not _looks_like_header(list(rows[index])):
            continue
        # A header must be followed by at least one row with some data in it.
        following = rows[index + 1 : index + 4]
        if any(any(_as_text(cell) for cell in row) for row in following):
            return index
    return None


def _find_unresolved_formulas(path: Path) -> dict[str, list[str]]:
    """Find formula cells that have no cached value, per sheet.

    How this works: we open the file a second time with `data_only=False`, which
    shows us the formula TEXT (for example "=SUM(E6:E12)"). Any cell holding a
    formula whose value side came back empty is an unresolved formula.

    Why bother: without this check, an unresolved total silently reads as blank.
    A blank total looks like "no expenses", which is a wrong answer delivered
    confidently. Detecting it costs one extra read of the file.
    """
    unresolved: dict[str, list[str]] = {}
    try:
        workbook = load_workbook(path, read_only=True, data_only=False)
    except Exception:  # noqa: BLE001 - this is a best-effort diagnostic
        return unresolved

    try:
        for sheet_name in workbook.sheetnames:
            found: list[str] = []
            for row_index, row in enumerate(
                workbook[sheet_name].iter_rows(values_only=True), start=1
            ):
                for col_index, value in enumerate(row, start=1):
                    if isinstance(value, str) and value.startswith("="):
                        found.append(f"row {row_index}, column {col_index}: {value}")
            if found:
                unresolved[sheet_name] = found
    finally:
        workbook.close()
    return unresolved


def extract_excel(path: Path, source_id: str | None = None) -> ExtractionReport:
    """Read one workbook into one block per data row, per sheet."""
    path = Path(path)
    report = ExtractionReport(source_id=source_id or path.name, source_type="excel", path=str(path))

    if not path.exists():
        report.warnings.append(f"file does not exist: {path}")
        return report

    try:
        # read_only makes large files cheap; data_only asks for values, not formulas.
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the report
        report.warnings.append(f"could not open workbook: {type(exc).__name__}: {exc}")
        return report

    formulas = _find_unresolved_formulas(path)
    blocks: list[SourceBlock] = []
    skipped_label_rows = 0

    try:
        report.sheets = list(workbook.sheetnames)

        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                report.notes.append(f"sheet '{sheet_name}' is empty")
                continue

            header_index = _find_header_row(rows)

            # No header found: this is probably a notes sheet, not a table. Keep
            # the text anyway -- the notes on Priya's sheet mention a verbal
            # approval, which is genuinely relevant -- but keep it as prose.
            if header_index is None:
                text = "\n".join(
                    " ".join(_as_text(cell) for cell in row if _as_text(cell))
                    for row in rows
                    if any(_as_text(cell) for cell in row)
                ).strip()
                if text:
                    blocks.append(
                        SourceBlock(
                            source_id=report.source_id,
                            source_type="excel",
                            kind="text",
                            text=text,
                            sheet=sheet_name,
                        )
                    )
                    report.notes.append(
                        f"sheet '{sheet_name}': no header row found, captured as free text"
                    )
                continue

            header = [_as_text(cell) for cell in rows[header_index]]
            # Excel rows are 1-based, so the spreadsheet row number is index + 1.
            report.notes.append(
                f"sheet '{sheet_name}': header detected on row {header_index + 1}"
                f" -> {', '.join(c for c in header if c)}"
            )

            for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
                values = [_as_text(cell) for cell in row]
                if not any(values):
                    continue  # blank spacer row

                pairs = [
                    f"{header[i]}: {value}"
                    for i, value in enumerate(values)
                    if value and i < len(header) and header[i]
                ]
                if len(pairs) < MIN_PAIRS_PER_ROW:
                    # One or two filled cells under a nine-column header is a
                    # label or totals row, not a record. Indexing it adds a
                    # meaningless block like "Merchant: TOTAL" that can still be
                    # retrieved and cited. Junk in the index is worse than
                    # nothing, because it looks like evidence.
                    skipped_label_rows += 1
                    continue

                blocks.append(
                    SourceBlock(
                        source_id=report.source_id,
                        source_type="excel",
                        kind="row",
                        text=" | ".join(pairs),
                        sheet=sheet_name,
                        row=offset,
                    )
                )
    finally:
        workbook.close()

    report.blocks = blocks

    if skipped_label_rows:
        report.notes.append(
            f"skipped {skipped_label_rows} row(s) that filled fewer than {MIN_PAIRS_PER_ROW} "
            "columns - label or totals rows, not records"
        )

    for sheet_name, cells in formulas.items():
        report.warnings.append(
            f"sheet '{sheet_name}' contains {len(cells)} formula cell(s) with no cached value "
            f"({cells[0]}). A formula written by a script has never been calculated, so it "
            "reads as EMPTY, not as zero. Never treat such a cell as a total - recompute it "
            "from the rows yourself."
        )

    if report.is_empty:
        report.warnings.append("no data rows extracted from this workbook")

    return report
