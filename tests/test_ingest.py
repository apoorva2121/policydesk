"""Tests for extraction: the text, and the location that makes citations possible."""

from __future__ import annotations

from pathlib import Path

from policydesk.ingest import extract_excel, extract_pdf

# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_policy_pdf_extracts_every_page(sample_data: dict[str, Path]) -> None:
    report = extract_pdf(sample_data["travel_policy_pdf"])

    assert report.pages == 3
    assert not report.warnings
    text_blocks = [b for b in report.blocks if b.kind == "text"]
    assert len(text_blocks) == 3
    assert {b.page for b in text_blocks} == {1, 2, 3}


def test_the_policy_numbers_survive_extraction(sample_data: dict[str, Path]) -> None:
    """These exact strings are the ground truth the agent must find later."""
    report = extract_pdf(sample_data["travel_policy_pdf"])
    everything = "\n".join(block.text for block in report.blocks)

    assert "USD 60 per day" in everything  # domestic meal per diem
    assert "USD 85 per day" in everything  # international meal per diem
    assert "USD 300" in everything  # international hotel limit
    assert "USD 75 or more" in everything  # receipt threshold
    assert "Vice President" in everything  # business class approval rule


def test_tables_are_extracted_separately_with_headers_repeated(
    sample_data: dict[str, Path],
) -> None:
    """A retrieved table row must make sense on its own, with no neighbours."""
    report = extract_pdf(sample_data["travel_policy_pdf"])
    tables = [block for block in report.blocks if block.kind == "table"]

    assert len(tables) == 1
    table = tables[0]
    assert table.page == 3
    first_line = table.text.splitlines()[0]
    assert "Category: Meals" in first_line
    assert "International (USD): 85" in first_line


def test_scanned_pdf_is_detected_not_silently_skipped(sample_data: dict[str, Path]) -> None:
    """The single most valuable test in this file.

    A scanned page extracts to nothing. If the pipeline shrugs and carries on,
    the document is absent from the index while appearing to be present, and the
    assistant answers "I could not find that" forever with no explanation.
    """
    report = extract_pdf(sample_data["scanned_receipt_pdf"])

    assert report.is_empty
    assert report.needs_human_review
    joined = " ".join(report.warnings)
    assert "OCR" in joined
    assert "scan" in joined.lower()


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    """Pipelines process many files. One bad file must not stop the run."""
    report = extract_pdf(tmp_path / "does-not-exist.pdf")

    assert report.is_empty
    assert any("does not exist" in warning for warning in report.warnings)


def test_pdf_citation_format(sample_data: dict[str, Path]) -> None:
    report = extract_pdf(sample_data["travel_policy_pdf"])
    block = next(b for b in report.blocks if b.kind == "text" and b.page == 2)

    assert block.locator == "p.2"
    assert block.citation == "travel-and-expense-policy.pdf (p.2)"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def test_header_is_found_not_assumed(sample_data: dict[str, Path]) -> None:
    """The header is on row 5, under a title block. Row 1 would be wrong."""
    report = extract_excel(sample_data["expense_workbook"])

    assert any("header detected on row 5" in note for note in report.notes)


def test_one_block_per_data_row_with_column_names_attached(
    sample_data: dict[str, Path],
) -> None:
    report = extract_excel(sample_data["expense_workbook"])
    rows = [block for block in report.blocks if block.kind == "row"]

    assert len(rows) == 7  # Priya's seven October expenses

    first = rows[0]
    assert first.sheet == "October 2026"
    assert first.row == 6  # directly below the header on row 5
    assert "Expense ID: 1101" in first.text
    assert "Amount (USD): 112.4" in first.text
    assert first.citation == "expense-report-2026-10-raman.xlsx (October 2026!row 6)"


def test_totals_row_is_not_indexed_as_data(sample_data: dict[str, Path]) -> None:
    """Junk in the index is worse than nothing, because it looks like evidence."""
    report = extract_excel(sample_data["expense_workbook"])

    assert not any(block.text.strip() == "Merchant: TOTAL" for block in report.blocks)
    assert any("label or totals rows" in note for note in report.notes)


def test_unresolved_formula_is_warned_about(sample_data: dict[str, Path]) -> None:
    """A formula never calculated reads as EMPTY, not as zero. That is a trap."""
    report = extract_excel(sample_data["expense_workbook"])
    joined = " ".join(report.warnings)

    assert "formula cell" in joined
    assert "=SUM(E6:E12)" in joined
    assert "not as zero" in joined


def test_notes_sheet_is_kept_as_free_text(sample_data: dict[str, Path]) -> None:
    """The notes sheet has no header, but its content is genuinely relevant:
    it mentions a verbal approval that the policy says must be written."""
    report = extract_excel(sample_data["expense_workbook"])
    notes_blocks = [block for block in report.blocks if block.sheet == "Notes"]

    assert len(notes_blocks) == 1
    assert "approved verbally by VP Engineering" in notes_blocks[0].text
    assert notes_blocks[0].locator == "Notes"


def test_every_block_knows_where_it_came_from(sample_data: dict[str, Path]) -> None:
    """The invariant the whole citation feature rests on."""
    reports = [
        extract_pdf(sample_data["travel_policy_pdf"]),
        extract_pdf(sample_data["approval_policy_pdf"]),
        extract_excel(sample_data["expense_workbook"]),
    ]

    for report in reports:
        for block in report.blocks:
            assert block.source_id
            assert block.text.strip()
            assert block.locator and "unknown" not in block.locator
            assert block.citation.startswith(block.source_id)
