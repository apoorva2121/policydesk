"""Tests for chunking: the stage that decides how good retrieval can possibly be."""

from __future__ import annotations

from pathlib import Path

import pytest

from policydesk.ingest import extract_excel, extract_pdf
from policydesk.rag.chunking import chunk_blocks, chunk_stats


@pytest.fixture
def policy_blocks(sample_data: dict[str, Path]) -> list:
    return extract_pdf(sample_data["travel_policy_pdf"]).blocks


@pytest.fixture
def workbook_blocks(sample_data: dict[str, Path]) -> list:
    return extract_excel(sample_data["expense_workbook"]).blocks


def test_section_strategy_keeps_each_rule_whole(policy_blocks: list) -> None:
    """The point of the section strategy: one chunk = one complete policy rule."""
    chunks = chunk_blocks(policy_blocks, strategy="section")

    meal_chunks = [c for c in chunks if c.section and "Meal allowances" in c.section]
    assert len(meal_chunks) == 1, "the meal rule should be exactly one chunk, not split"

    text = meal_chunks[0].text
    # Both limits must be in the SAME chunk, or a question about one retrieves
    # a chunk that does not contain the other.
    assert "USD 60 per day" in text
    assert "USD 85 per day" in text


def test_every_section_heading_becomes_a_chunk_label(policy_blocks: list) -> None:
    chunks = chunk_blocks(policy_blocks, strategy="section")
    sections = {c.section for c in chunks if c.section}

    for expected in ("1. Purpose and scope", "2. Meal allowances (per diem)", "4. Air travel"):
        assert expected in sections


def test_spreadsheet_rows_are_never_split(workbook_blocks: list) -> None:
    """Half a row is meaningless. A tiny chunk size must not change that."""
    chunks = chunk_blocks(workbook_blocks, strategy="fixed", size=200, overlap=20)
    row_chunks = [c for c in chunks if c.kind == "row"]

    assert len(row_chunks) == 7  # one per October expense, unchanged
    for chunk in row_chunks:
        assert "Expense ID:" in chunk.text  # the header pairing survived
        assert chunk.index_in_block == 0  # never split, so always piece zero


def test_tables_are_never_split(policy_blocks: list) -> None:
    chunks = chunk_blocks(policy_blocks, strategy="fixed", size=200, overlap=20)
    tables = [c for c in chunks if c.kind == "table"]

    assert len(tables) == 1
    # Every row of the limits table must stay together, or a lookup finds
    # "Meals" without finding the number next to it.
    assert "Category: Meals" in tables[0].text
    assert "Category: Mileage" in tables[0].text


def test_chunk_ids_are_deterministic(policy_blocks: list) -> None:
    """Stable ids let us cache embeddings and name chunks in an evaluation set."""
    first = chunk_blocks(policy_blocks, strategy="section")
    second = chunk_blocks(policy_blocks, strategy="section")

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert len(set(c.chunk_id for c in first)) == len(first), "ids must be unique"


def test_embed_text_carries_a_context_header_but_citation_does_not(policy_blocks: list) -> None:
    """We embed the header to help retrieval; we never show it to the user."""
    chunks = chunk_blocks(policy_blocks, strategy="section")
    chunk = next(c for c in chunks if c.section and "Air travel" in c.section)

    assert chunk.embed_text.startswith("[travel-and-expense-policy.pdf | p.1 | 4. Air travel]")
    assert chunk.text in chunk.embed_text
    assert "[" not in chunk.text[:1]  # the clean text has no header
    assert chunk.citation == "travel-and-expense-policy.pdf (p.1, 4. Air travel)"


def test_overlap_must_be_smaller_than_size(policy_blocks: list) -> None:
    """Equal or larger overlap makes a fixed splitter loop forever."""
    with pytest.raises(ValueError, match="smaller than size"):
        chunk_blocks(policy_blocks, strategy="fixed", size=500, overlap=500)


def test_unknown_strategy_is_rejected(policy_blocks: list) -> None:
    with pytest.raises(ValueError, match="unknown chunk strategy"):
        chunk_blocks(policy_blocks, strategy="magic")


def test_a_very_long_section_is_split_but_keeps_its_heading() -> None:
    """One enormous section must not be allowed to fill the whole prompt."""
    from policydesk.ingest.models import SourceBlock

    long_body = " ".join(f"Sentence number {n} about expenses." for n in range(300))
    block = SourceBlock(
        source_id="big.pdf",
        source_type="pdf",
        kind="text",
        page=1,
        text=f"1. A very long rule\n\n{long_body}",
    )

    chunks = chunk_blocks([block], strategy="section", size=400, overlap=50)

    assert len(chunks) > 1
    # Every piece still knows which rule it belongs to.
    assert all(chunk.section == "1. A very long rule" for chunk in chunks)


def test_both_strategies_cover_the_same_source_text(policy_blocks: list) -> None:
    """A sanity check: neither strategy may silently drop content."""
    section_stats = chunk_stats(chunk_blocks(policy_blocks, strategy="section"))
    fixed_stats = chunk_stats(chunk_blocks(policy_blocks, strategy="fixed"))

    # Fixed chunking repeats text at the overlaps, so it has MORE characters.
    assert fixed_stats["total_chars"] >= section_stats["total_chars"]
    # Neither may lose a meaningful amount of the source.
    source_chars = sum(block.char_count for block in policy_blocks)
    assert section_stats["total_chars"] > source_chars * 0.9
