"""Reading PDFs, page by page, with tables kept separate from prose.

What a PDF actually is
----------------------
A PDF is a set of drawing instructions: "put this character at this position".
It has no idea what a paragraph, a heading or a table is. Two PDFs that look
identical on screen can extract completely differently.

Three cases we must handle:

1. **A normal PDF** has a "text layer" -- the characters are really there, and we
   can read them. Our generated policy documents are like this.
2. **A scanned PDF** is a photograph of a page. There is no text layer at all.
   Extraction returns an empty string. The fix is OCR (optical character
   recognition), which is a separate step with its own error rate. We do not add
   OCR here; we **detect** the situation and report it, which is the more
   important habit.
3. **Tables** lose their meaning when flattened into a line of prose. "60 85" is
   useless; "Meals, domestic 60, international 85" is an answer. So we pull
   tables out separately and rebuild them as readable rows.

Library choice: `pdfplumber`. It gives per-page text and reasonable table
detection in one library, which is enough here. For complex, real-world layouts,
a cloud document service (such as Azure AI Document Intelligence) does a better
job -- that trade-off is noted in the Azure mapping milestone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .models import ExtractionReport, SourceBlock

# If a page has fewer than this many characters we treat it as suspicious.
# A genuinely blank page is fine; a page of scanned text looks identical to us.
MIN_CHARS_PER_PAGE = 20


# A numbered section heading, e.g. "2. Meal allowances (per diem)".
# Matching this is how we keep the document's structure through extraction.
_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z]")


def looks_like_heading(line: str) -> bool:
    """True for a line like '3. Accommodation'.

    This is a heuristic. A wrapped sentence that happens to begin "5. " would be
    misread as a heading. In exchange we keep the document's section structure,
    which is what makes a chunk equal one complete rule later on.
    """
    return bool(_HEADING_RE.match(line.strip())) and len(line.strip()) < 100


def _clean(text: str) -> str:
    """Tidy extracted text without losing the structure we need later.

    A PDF gives us one line per *visual* line, with no blank lines between
    paragraphs. Simply joining every line (which this function used to do)
    produced one giant paragraph per page and destroyed the headings -- and the
    chunker needs those headings to keep each policy rule intact.

    So we do two things:

    1. Join wrapped lines inside a paragraph, so a sentence stays one sentence.
    2. Start a new paragraph at every section heading, and keep that heading on
       its own line.

    We never rewrite words. Extracted text must stay faithful to the source, or
    citations become lies.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(current.copy())
            current.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if looks_like_heading(line):
            flush()
            paragraphs.append([line])  # the heading is its own paragraph
            continue
        current.append(line)
    flush()

    return "\n\n".join(" ".join(group) for group in paragraphs).strip()


def _table_to_text(table: list[list[str | None]]) -> str:
    """Turn a grid into readable lines that keep the header attached to values.

    Before: [["Category","Domestic"],["Meals","60"]]
    After:  "Category: Meals | Domestic: 60"

    Repeating the header on every row costs a few tokens and buys a lot: a single
    retrieved row still makes sense on its own, with no surrounding context.
    """
    rows = [[(cell or "").strip() for cell in row] for row in table if row]
    if not rows:
        return ""

    header, *body = rows
    if not body:
        return " | ".join(cell for cell in header if cell)

    lines: list[str] = []
    for row in body:
        pairs = [
            f"{header[i] or f'col{i + 1}'}: {value}"
            for i, value in enumerate(row)
            if value and i < len(header)
        ]
        if pairs:
            lines.append(" | ".join(pairs))
    return "\n".join(lines)


def extract_pdf(path: Path, source_id: str | None = None) -> ExtractionReport:
    """Read one PDF into page-level text blocks plus separate table blocks."""
    path = Path(path)
    report = ExtractionReport(source_id=source_id or path.name, source_type="pdf", path=str(path))

    if not path.exists():
        report.warnings.append(f"file does not exist: {path}")
        return report

    blocks: list[SourceBlock] = []
    pages_with_no_text = 0

    try:
        with pdfplumber.open(path) as pdf:
            report.pages = len(pdf.pages)

            for page_number, page in enumerate(pdf.pages, start=1):
                raw_text = page.extract_text() or ""
                text = _clean(raw_text)

                if len(text) < MIN_CHARS_PER_PAGE:
                    pages_with_no_text += 1
                else:
                    blocks.append(
                        SourceBlock(
                            source_id=report.source_id,
                            source_type="pdf",
                            kind="text",
                            text=text,
                            page=page_number,
                        )
                    )

                # Tables are extracted in addition to the page text. Yes, that
                # duplicates some content -- deliberately. The prose version
                # helps a reader; the row version helps a lookup.
                for table_index, table in enumerate(page.extract_tables() or []):
                    table_text = _table_to_text(table)
                    if table_text.strip():
                        blocks.append(
                            SourceBlock(
                                source_id=report.source_id,
                                source_type="pdf",
                                kind="table",
                                text=table_text,
                                page=page_number,
                                table_index=table_index,
                            )
                        )
    except Exception as exc:  # noqa: BLE001 - we want the reason in the report
        # We never swallow the error silently. A file that failed to parse must
        # be visible, with the reason, or it silently disappears from the index.
        report.warnings.append(f"could not parse PDF: {type(exc).__name__}: {exc}")
        return report

    report.blocks = blocks

    if pages_with_no_text:
        report.warnings.append(
            f"{pages_with_no_text} of {report.pages} page(s) had almost no extractable text. "
            "This usually means the page is a scan or an image. Reading it would need OCR, "
            "which is a separate step with its own error rate. Flag for human review."
        )

    if report.is_empty:
        report.warnings.append(
            "no text could be extracted from this PDF at all. Do not index it: an empty "
            "document in the index looks like a working document and silently answers nothing."
        )

    return report
