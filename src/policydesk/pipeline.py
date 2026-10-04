"""Wiring the pieces together: files -> chunks -> index -> retrieval -> answer.

This module exists so that the CLI, the tests and (later) the web API all run the
*same* pipeline. The moment the CLI has its own copy of this logic, the thing you
tested stops being the thing you shipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
from .tools import ToolRegistry


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


def make_registry(settings: Settings, llm: LLMClient) -> ToolRegistry:
    """Every tool available in one run. This set IS the allow-list.

    A model can ask for a tool only if it is registered here. Anything else comes
    back as a clear failure rather than an attempt.
    """
    from .tools import PolicySearchTool, SqlQueryTool
    from .tools.flag_tool import FlagExpenseTool

    return ToolRegistry(
        [
            PolicySearchTool(settings, llm),
            SqlQueryTool(
                settings.sqlite_path,
                llm,
                row_limit=settings.sql_row_limit,
                timeout_seconds=settings.request_timeout_s,
            ),
            # The only tool that writes. It appends to a review queue file, never
            # to the expense database, which stays read-only to this whole app.
            FlagExpenseTool(
                queue_path=settings.data_dir / "processed" / "review-queue.jsonl",
                database_path=settings.sqlite_path,
            ),
        ]
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


# ---------------------------------------------------------------------------
# The agent (Milestone 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRun:
    """What one agent run produced, plus whether it is waiting for a human."""

    thread_id: str
    state: dict
    waiting_for_approval: bool
    usage: str
    tracer: Any = None

    @property
    def answer(self) -> str | None:
        return self.state.get("answer")

    @property
    def refused(self) -> bool:
        return bool(self.state.get("refused"))

    @property
    def trace(self) -> list[str]:
        return list(self.state.get("trace") or [])


def run_agent(
    question: str,
    settings: Settings,
    llm: LLMClient | None = None,
    *,
    thread_id: str | None = None,
    allow_actions: bool = True,
) -> AgentRun:
    """Run the agent graph for one question.

    `thread_id` names this conversation in the checkpoint store. It is how a
    paused run is found again later, so it is generated here when not supplied
    and always reported back.
    """
    import uuid

    from .graph.build import compile_graph
    from .observability import Tracer

    llm = llm or make_client(settings)
    registry = make_registry(settings, llm)
    tracer = Tracer(settings)
    graph, close = compile_graph(
        settings, llm, registry, allow_actions=allow_actions, tracer=tracer
    )

    thread_id = thread_id or f"run-{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        from .graph.state import initial_state

        state = graph.invoke(initial_state(question), config)
        snapshot = graph.get_state(config)
        # `next` is non-empty when the graph has stopped before a node, which for
        # this graph only happens at the human-approval interrupt.
        waiting = bool(snapshot.next)
    finally:
        close()
        tracer.flush()

    return AgentRun(
        thread_id=thread_id,
        state=dict(state),
        waiting_for_approval=waiting,
        usage=llm.usage_summary(),
        tracer=tracer,
    )


def saved_thread_status(settings: Settings, thread_id: str) -> str:
    """Is there a saved run with this id, and is it waiting? Reads state only.

    Deliberately does NOT build a model client. This exists because of a bug a test
    caught: `resume_agent` created the client first, so asking about an unknown
    thread with no API key configured raised a credentials error and the API
    returned 500 where it should have returned 404.

    The general rule: validate the request before constructing anything expensive
    or credentialed. Reading a checkpoint needs no model, so it should not require
    one.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from .graph.build import make_serde

    path = settings.data_dir / "processed" / "agent-checkpoints.sqlite"
    if not path.exists():
        return "missing"

    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(connection, serde=make_serde())
        checkpoint = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if checkpoint is None:
            return "missing"
        pending = getattr(checkpoint, "pending_writes", None)
        next_nodes = checkpoint.metadata.get("writes") if checkpoint.metadata else None
        # A thread exists. Whether it is still waiting is decided by the compiled
        # graph, so we only answer "exists" here and let resume_agent confirm.
        del pending, next_nodes
        return "exists"
    except Exception:  # noqa: BLE001 - a bad checkpoint store is "missing", not fatal
        return "missing"
    finally:
        connection.close()


def resume_agent(
    thread_id: str,
    approval: str,
    settings: Settings,
    llm: LLMClient | None = None,
) -> AgentRun:
    """Resume a paused run with a human decision.

    This is the whole point of the durable checkpointer: the process that started
    the run is long gone, and we pick up exactly where it stopped.
    """
    from .graph.build import compile_graph

    if approval not in ("approved", "rejected"):
        raise ValueError("approval must be 'approved' or 'rejected'")

    # Check the thread exists BEFORE building a model client. See the note on
    # saved_thread_status: getting this order wrong turned a 404 into a 500.
    if saved_thread_status(settings, thread_id) == "missing":
        raise ValueError(f"no saved run with thread id {thread_id!r}")

    llm = llm or make_client(settings)
    registry = make_registry(settings, llm)
    graph, close = compile_graph(settings, llm, registry)
    config = {"configurable": {"thread_id": thread_id}}

    try:
        snapshot = graph.get_state(config)
        if not snapshot.created_at:
            raise ValueError(f"no saved run with thread id {thread_id!r}")
        if not snapshot.next:
            raise ValueError(f"run {thread_id!r} is not waiting for approval")

        # Record the decision in the state, then let the graph continue. A
        # rejection does not need a different code path: `execute_action` checks
        # `approval` itself and refuses to write.
        graph.update_state(config, {"approval": approval})
        state = graph.invoke(None, config)
        final = graph.get_state(config)
        waiting = bool(final.next)
    finally:
        close()

    return AgentRun(
        thread_id=thread_id,
        state=dict(state),
        waiting_for_approval=waiting,
        usage=llm.usage_summary(),
    )
