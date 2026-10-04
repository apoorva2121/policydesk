"""Cutting documents into pieces small enough to search and send to a model.

Why chunk at all
----------------
You cannot paste a 600-page document into a prompt: it would not fit, and even if
it did, the model answers worse when buried in irrelevant text. So we cut the
document into pieces, search over the pieces, and send only the best few.

The whole quality of a RAG system is decided here. Too big, and each chunk
contains three unrelated rules, so retrieval is vague and the prompt is
expensive. Too small, and a rule gets cut in half -- and half a rule is not a
weaker answer, it is a *wrong* answer.

Two strategies, so you can measure instead of guess
---------------------------------------------------
**section** (default here): split at the policy's numbered headings. One chunk
is one complete rule, which is exactly the unit a user asks about. Chunk sizes
end up uneven, which is the price.

**fixed**: split every N characters with a little overlap. Even sizes, so cost
and latency are predictable. But a rule can be cut mid-sentence, and the overlap
only partly compensates.

For a well-structured policy document, `section` almost always wins. For messy
prose with no headings, `fixed` is the only option that works. In Milestone 7 we
measure the difference on this corpus rather than trusting that sentence.

Two rules that are never negotiable
-----------------------------------
1. **Never split a spreadsheet row or a table.** Half a row is meaningless.
2. **Every chunk keeps its origin.** Page, sheet, row, section -- otherwise the
   answer cannot be cited.

The "contextual header" trick
-----------------------------
Each chunk is embedded together with a short header naming its document, page and
section. A chunk reading "The cap covers all meals..." is ambiguous on its own;
"[travel-and-expense-policy.pdf | p.1 | 2. Meal allowances] The cap covers..." is
not. We embed the header, but cite the clean text. It costs a few tokens per
chunk and measurably improves retrieval on short, similar chunks like ours.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..ingest.models import BlockKind, SourceBlock, SourceType
from ..ingest.pdf_extract import looks_like_heading

# A section longer than this many characters is split further, so one enormous
# section cannot blow the prompt budget on its own.
SECTION_HARD_LIMIT_MULTIPLIER = 2.5


class Chunk(BaseModel):
    """One searchable piece of a document, with its origin intact."""

    model_config = {"frozen": True}

    chunk_id: str = Field(description="Stable, deterministic id. Same input, same id")
    source_id: str
    source_type: SourceType
    kind: BlockKind
    text: str = Field(description="The clean text, as it will be shown and cited")
    section: str | None = Field(default=None, description="e.g. '2. Meal allowances (per diem)'")
    locator: str = Field(description="Where in the file, e.g. 'p.1' or 'October 2026!row 6'")
    index_in_block: int = Field(default=0, ge=0)

    @property
    def citation(self) -> str:
        """What a user sees next to a claim."""
        if self.section:
            return f"{self.source_id} ({self.locator}, {self.section})"
        return f"{self.source_id} ({self.locator})"

    @property
    def embed_text(self) -> str:
        """The text we actually embed: a context header plus the content.

        The header is NOT part of the citation or the displayed text. It exists
        only to make the vector more distinctive.
        """
        parts = [self.source_id, self.locator]
        if self.section:
            parts.append(self.section)
        return f"[{' | '.join(parts)}]\n{self.text}"

    @property
    def char_count(self) -> int:
        return len(self.text)


def _make_id(block: SourceBlock, index: int) -> str:
    """Deterministic id: the same document always produces the same ids.

    This matters more than it looks. Stable ids let us update one chunk in the
    index instead of rebuilding it, reuse cached embeddings, and write an
    evaluation set that names the chunk a question should retrieve.
    """
    return f"{block.source_id}::{block.locator}::{index:03d}"


def _split_fixed(text: str, size: int, overlap: int) -> list[str]:
    """Split on paragraph, then sentence, then word boundaries, never mid-word.

    This uses LangChain's recursive splitter, which tries a list of separators in
    order and only falls back to a cruder one when a piece is still too big. It is
    the standard tool for this job, and writing it yourself is a half-day of
    off-by-one errors.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )
    return [piece.strip() for piece in splitter.split_text(text) if piece.strip()]


def _split_by_section(text: str, size: int, overlap: int) -> list[tuple[str | None, str]]:
    """Group paragraphs under their heading. Returns (section, body) pairs.

    Extraction put each heading on its own paragraph, which is the only reason
    this is possible -- see the note in `ingest/pdf_extract.py`.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_body: list[str] = []

    for paragraph in paragraphs:
        if looks_like_heading(paragraph):
            if current_body:
                sections.append((current_heading, current_body))
                current_body = []
            current_heading = paragraph
            continue
        current_body.append(paragraph)

    if current_body:
        sections.append((current_heading, current_body))

    # A single huge section is split further, so one section cannot dominate the
    # prompt. The heading is carried onto every piece of it.
    hard_limit = int(size * SECTION_HARD_LIMIT_MULTIPLIER)
    out: list[tuple[str | None, str]] = []
    for heading, body in sections:
        joined = "\n\n".join(body)
        if len(joined) <= hard_limit:
            out.append((heading, joined))
        else:
            out.extend((heading, piece) for piece in _split_fixed(joined, size, overlap))
    return out


def chunk_blocks(
    blocks: list[SourceBlock],
    *,
    strategy: str = "section",
    size: int = 900,
    overlap: int = 150,
) -> list[Chunk]:
    """Turn extracted blocks into chunks, keeping every origin detail."""
    if overlap >= size:
        raise ValueError(f"overlap ({overlap}) must be smaller than size ({size})")
    if strategy not in {"section", "fixed"}:
        raise ValueError(f"unknown chunk strategy '{strategy}'; use 'section' or 'fixed'")

    chunks: list[Chunk] = []

    for block in blocks:
        # Rule 1: spreadsheet rows and tables are atomic. Splitting them destroys
        # the header-to-value pairing that makes them usable at all.
        if block.kind in {"row", "table"}:
            chunks.append(
                Chunk(
                    chunk_id=_make_id(block, 0),
                    source_id=block.source_id,
                    source_type=block.source_type,
                    kind=block.kind,
                    text=block.text,
                    locator=block.locator,
                    index_in_block=0,
                )
            )
            continue

        if strategy == "section":
            pieces = _split_by_section(block.text, size, overlap)
        else:
            pieces = [(None, piece) for piece in _split_fixed(block.text, size, overlap)]

        for index, (section, piece) in enumerate(pieces):
            chunks.append(
                Chunk(
                    chunk_id=_make_id(block, index),
                    source_id=block.source_id,
                    source_type=block.source_type,
                    kind=block.kind,
                    text=piece,
                    section=section,
                    locator=block.locator,
                    index_in_block=index,
                )
            )

    return chunks


def chunk_stats(chunks: list[Chunk]) -> dict[str, float | int]:
    """Numbers worth looking at before you trust a chunking choice."""
    if not chunks:
        return {"count": 0}
    sizes = sorted(chunk.char_count for chunk in chunks)
    return {
        "count": len(chunks),
        "total_chars": sum(sizes),
        "smallest": sizes[0],
        "median": sizes[len(sizes) // 2],
        "largest": sizes[-1],
        "with_section": sum(1 for chunk in chunks if chunk.section),
    }
