"""Wiring the pieces together: files -> chunks -> index -> retrieval -> answer.

This module exists so that the CLI, the tests and (later) the web API all run the
*same* pipeline. The moment the CLI has its own copy of this logic, the thing you
tested stops being the thing you shipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .ingest import ExtractionReport, extract_excel, extract_pdf
from .llm.cache import ResponseCache
from .llm.client import LLMClient
from .llm.pricing import CostLedger
from .rag import (
    Answer,
    Chunk,
    answer_question,
    build_index,
    chunk_blocks,
    load_index,
    retrieve,
)


def make_client(settings: Settings) -> LLMClient:
    """One configured client, with the cache and the spend cap attached."""
    return LLMClient(
        settings=settings,
        ledger=CostLedger(limit_usd=settings.max_spend_usd_per_run),
        cache=ResponseCache(
            directory=settings.data_dir / "processed" / "llm-cache",
            enabled=settings.llm_cache_enabled,
        ),
    )


def extract_all(settings: Settings) -> list[ExtractionReport]:
    """Read every source file under data/raw."""
    raw = settings.data_dir / "raw"
    reports = [extract_pdf(path) for path in sorted(raw.glob("**/*.pdf"))]
    reports += [extract_excel(path) for path in sorted(raw.glob("**/*.xlsx"))]
    return reports


def build_chunks(settings: Settings) -> tuple[list[Chunk], list[ExtractionReport]]:
    """Extract and chunk, using the configured strategy."""
    reports = extract_all(settings)
    blocks = [block for report in reports for block in report.blocks]
    chunks = chunk_blocks(
        blocks,
        strategy=settings.chunk_strategy,
        size=settings.chunk_size_chars,
        overlap=settings.chunk_overlap_chars,
    )
    return chunks, reports


def reindex(settings: Settings, llm: LLMClient) -> tuple[int, list[ExtractionReport]]:
    """Rebuild the vector index from the files on disk. Returns the chunk count."""
    chunks, reports = build_chunks(settings)
    build_index(chunks, llm, settings)
    return len(chunks), reports


@dataclass(frozen=True)
class AskResult:
    answer: Answer
    usage: str


def ask(question: str, settings: Settings, llm: LLMClient | None = None) -> AskResult:
    """The plain-RAG path: retrieve, then answer. One question in, one answer out."""
    llm = llm or make_client(settings)
    index = load_index(settings)
    chunks = retrieve(question, index, llm, top_k=settings.retrieval_top_k)
    answer = answer_question(question, chunks, llm)
    return AskResult(answer=answer, usage=llm.usage_summary())
