"""The shape of everything we extract from a file.

The single most important idea in this file
-------------------------------------------
We never store a piece of text on its own. Every piece carries **where it came
from**: which file, which page, which sheet, which row.

Why that matters so much: at the end of the pipeline the assistant has to say
"the international meal limit is USD 85 *(travel-and-expense-policy.pdf, p.1)*".
It can only do that if the location travelled alongside the text the whole way
from the file to the answer.

If you throw the location away during extraction, you can never get it back, and
you will be stuck with an assistant that sounds confident and cites nothing.
That is the number one reason document assistants get rejected by auditors.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["pdf", "excel", "sqlite"]
BlockKind = Literal["text", "table", "row"]


class SourceBlock(BaseModel):
    """One citable piece of content from one file.

    A "block" is bigger than a sentence and smaller than a document. For a PDF
    it is usually one page of text or one table. For a spreadsheet it is one row.
    Chunking into model-sized pieces happens later (Milestone 2); here we only
    care about faithful extraction with the location attached.
    """

    model_config = {"frozen": True}  # blocks are facts about a file; never edited in place

    source_id: str = Field(description="Stable name of the file, e.g. 'travel-policy.pdf'")
    source_type: SourceType
    kind: BlockKind
    text: str

    # Location. Which of these is filled depends on the source type.
    page: int | None = Field(default=None, ge=1, description="1-based PDF page number")
    sheet: str | None = Field(default=None, description="Excel sheet name")
    row: int | None = Field(default=None, ge=1, description="1-based Excel row number")
    table_index: int | None = Field(default=None, ge=0, description="n-th table on the page")

    @field_validator("text")
    @classmethod
    def _text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a SourceBlock must contain some text")
        return value

    @property
    def locator(self) -> str:
        """A short, human-readable citation, e.g. 'p.3' or 'October 2026!row 7'.

        This string is what the user eventually sees next to a claim, so it has
        to be something they can act on -- open the file and look.
        """
        if self.source_type == "pdf":
            base = f"p.{self.page}" if self.page else "unknown page"
            if self.kind == "table" and self.table_index is not None:
                return f"{base}, table {self.table_index + 1}"
            return base
        if self.source_type == "excel":
            parts = [self.sheet or "unknown sheet"]
            if self.row:
                parts.append(f"row {self.row}")
            return "!".join(parts)
        return "record"

    @property
    def citation(self) -> str:
        """The full citation shown to a user: 'travel-policy.pdf (p.3)'."""
        return f"{self.source_id} ({self.locator})"

    @property
    def char_count(self) -> int:
        return len(self.text)


class ExtractionReport(BaseModel):
    """What happened when we read one file.

    Extraction is the stage that fails quietly. A scanned PDF yields zero text,
    a password-protected file yields nothing, a merged-cell spreadsheet yields
    nonsense -- and in all three cases a careless pipeline just carries on and
    indexes an empty document. Then the assistant says "I could not find that"
    and nobody knows why.

    So every extraction returns a report, and `warnings` is not decoration: it
    is the list of things a human needs to look at.
    """

    source_id: str
    source_type: SourceType
    path: str
    blocks: list[SourceBlock] = Field(default_factory=list)
    # warnings = a human should look at this. notes = useful but not a problem.
    # Keeping them apart matters: if every file produces a "warning", you stop
    # reading warnings, and then the one that mattered slips past.
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    pages: int | None = None
    sheets: list[str] = Field(default_factory=list)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def total_chars(self) -> int:
        return sum(block.char_count for block in self.blocks)

    @property
    def is_empty(self) -> bool:
        """True when we got no usable text. Almost always a problem worth seeing."""
        return not self.blocks

    @property
    def needs_human_review(self) -> bool:
        return self.is_empty or bool(self.warnings)

    def summary_line(self) -> str:
        state = "EMPTY" if self.is_empty else f"{self.block_count} blocks"
        flag = "  <-- REVIEW" if self.needs_human_review else ""
        return f"{self.source_id:<42} {state:<14} {self.total_chars:>7} chars{flag}"
