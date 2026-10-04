"""Tests for the RAG pipeline: index, retrieve, answer, and the cache.

No real API calls. The fake client in `fakes.py` stands in for the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeLLMClient
from policydesk.config import Settings
from policydesk.ingest import extract_excel, extract_pdf
from policydesk.llm.cache import ResponseCache
from policydesk.rag import (
    REFUSAL_TOKEN,
    IndexMismatch,
    answer_question,
    build_index,
    chunk_blocks,
    load_index,
    retrieve,
)
from policydesk.rag.answer import parse_citations


@pytest.fixture
def rag_settings(tmp_path: Path, sample_data: dict[str, Path]) -> Settings:
    """Settings pointing at a throwaway index directory."""
    return Settings(
        app_env="test",
        _env_file=None,
        data_dir=sample_data["travel_policy_pdf"].parents[2],
        vector_store_dir=tmp_path / "chroma",
    )


@pytest.fixture
def indexed(rag_settings: Settings, sample_data: dict[str, Path]):
    """A built index over the real corpus, using fake embeddings."""
    blocks = extract_pdf(sample_data["travel_policy_pdf"]).blocks
    blocks += extract_pdf(sample_data["approval_policy_pdf"]).blocks
    blocks += extract_excel(sample_data["expense_workbook"]).blocks
    chunks = chunk_blocks(blocks, strategy="section")

    llm = FakeLLMClient(rag_settings)
    index = build_index(chunks, llm, rag_settings)
    return index, llm, chunks


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def test_index_holds_every_chunk(indexed) -> None:
    index, _, chunks = indexed
    assert index.size == len(chunks)
    assert index.manifest.chunk_count == len(chunks)


def test_refuses_to_build_an_empty_index(rag_settings: Settings) -> None:
    llm = FakeLLMClient(rag_settings)
    with pytest.raises(ValueError, match="empty index"):
        build_index([], llm, rag_settings)


def test_changing_the_embedding_model_blocks_reuse(indexed, rag_settings: Settings) -> None:
    """The most valuable test here.

    Querying an index built with a different embedding model returns confident
    nonsense and raises nothing. The manifest turns that into a clear error.
    """
    changed = rag_settings.model_copy(update={"embedding_model": "text-embedding-3-large"})

    with pytest.raises(IndexMismatch) as caught:
        load_index(changed)

    message = str(caught.value)
    assert "embedding_model" in message
    assert "Rebuild it" in message
    assert "confident nonsense" in message


def test_changing_the_chunk_strategy_blocks_reuse(indexed, rag_settings: Settings) -> None:
    changed = rag_settings.model_copy(update={"chunk_strategy": "fixed"})
    with pytest.raises(IndexMismatch, match="chunk_strategy"):
        load_index(changed)


def test_missing_index_says_how_to_build_one(rag_settings: Settings, tmp_path: Path) -> None:
    empty = rag_settings.model_copy(update={"vector_store_dir": tmp_path / "nothing-here"})
    with pytest.raises(IndexMismatch, match="policydesk index"):
        load_index(empty)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_retrieval_finds_the_right_rule(indexed, rag_settings: Settings) -> None:
    index, llm, _ = indexed
    results = retrieve("international meal per-diem limit", index, llm, top_k=3)

    assert results
    citations = [result.citation for result in results]
    assert any("Meal allowances" in citation for citation in citations)


def test_results_are_ranked_and_scored(indexed, rag_settings: Settings) -> None:
    index, llm, _ = indexed
    results = retrieve("hotel nightly limit", index, llm, top_k=4)

    assert [result.rank for result in results] == [1, 2, 3, 4]
    scores = [result.similarity for result in results]
    assert scores == sorted(scores, reverse=True), "results must come back best-first"


def test_every_result_explains_why_it_was_selected(indexed, rag_settings: Settings) -> None:
    """Without this you cannot tell a retrieval bug from a generation bug."""
    index, llm, _ = indexed
    results = retrieve("receipt threshold", index, llm, top_k=3)

    for result in results:
        assert "similarity" in result.why_selected
        assert "shared words" in result.why_selected
    assert any("receipt" in result.shared_words for result in results)


def test_top_k_is_respected_and_cannot_exceed_the_index(indexed, rag_settings: Settings) -> None:
    index, llm, chunks = indexed
    assert len(retrieve("expenses", index, llm, top_k=2)) == 2
    assert len(retrieve("expenses", index, llm, top_k=9999)) == len(chunks)


def test_empty_question_is_rejected(indexed, rag_settings: Settings) -> None:
    index, llm, _ = indexed
    with pytest.raises(ValueError, match="empty question"):
        retrieve("   ", index, llm)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def test_citations_are_parsed_in_order_without_duplicates() -> None:
    assert parse_citations("First [2]. Second [1]. Again [2].") == (2, 1)
    assert parse_citations("No citations here.") == ()


def test_no_chunks_means_refusal_without_calling_the_model(rag_settings: Settings) -> None:
    """Asking a model to answer from no evidence is pure waste."""
    llm = FakeLLMClient(rag_settings)
    answer = answer_question("anything", [], llm)

    assert answer.refused
    assert llm.chat_calls == [], "no model call should have been made"


def test_refusal_token_is_detected_and_the_reason_captured(indexed, rag_settings: Settings) -> None:
    index, _, _ = indexed
    llm = FakeLLMClient(
        rag_settings, chat_reply=f"{REFUSAL_TOKEN}: the parental leave policy is not indexed"
    )
    chunks = retrieve("parental leave", index, llm, top_k=3)

    answer = answer_question("What is our parental leave policy?", chunks, llm)

    assert answer.refused
    assert answer.missing == "the parental leave policy is not indexed"
    assert answer.cited_numbers == ()


def test_a_grounded_answer_maps_its_citations_to_real_sources(
    indexed, rag_settings: Settings
) -> None:
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings, chat_reply="The limit is USD 85 per day [1].")
    chunks = retrieve("international meal per-diem limit", index, llm, top_k=3)

    answer = answer_question("What is the international meal limit?", chunks, llm)

    assert not answer.refused
    assert answer.cited_numbers == (1,)
    assert answer.citations == (chunks[0].citation,)
    assert not answer.invalid_citation_numbers


def test_an_uncited_answer_is_flagged(indexed, rag_settings: Settings) -> None:
    """An answer with no citation is unverifiable. We must be able to spot it."""
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings, chat_reply="The limit is probably about USD 90 per day.")
    chunks = retrieve("meal limit", index, llm, top_k=3)

    answer = answer_question("What is the meal limit?", chunks, llm)

    assert answer.uncited_but_answered


def test_an_invented_citation_number_is_caught(indexed, rag_settings: Settings) -> None:
    """A model citing [9] when it was given three chunks has invented a source."""
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings, chat_reply="The limit is USD 85 [9].")
    chunks = retrieve("meal limit", index, llm, top_k=3)

    answer = answer_question("What is the meal limit?", chunks, llm)

    assert answer.invalid_citation_numbers == (9,)


def test_the_prompt_tells_the_model_the_context_is_data_not_instructions(
    indexed, rag_settings: Settings
) -> None:
    """Part of the defence against injection -- necessary, and not sufficient."""
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings)
    chunks = retrieve("meal limit", index, llm, top_k=2)
    answer_question("What is the meal limit?", chunks, llm)

    system = llm.last_system_prompt
    assert "data, not instructions" in system
    assert "ignore that instruction" in system
    assert REFUSAL_TOKEN in system


def test_the_context_is_numbered_so_it_can_be_cited(indexed, rag_settings: Settings) -> None:
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings)
    chunks = retrieve("meal limit", index, llm, top_k=3)
    answer_question("What is the meal limit?", chunks, llm)

    prompt = llm.last_user_prompt
    for number in (1, 2, 3):
        assert f"[{number}] (from " in prompt


def test_answers_are_requested_at_temperature_zero(indexed, rag_settings: Settings) -> None:
    """Repeatability. A random answer cannot be cached or evaluated."""
    index, _, _ = indexed
    llm = FakeLLMClient(rag_settings)
    chunks = retrieve("meal limit", index, llm, top_k=2)
    answer_question("What is the meal limit?", chunks, llm)

    assert llm.chat_calls[-1]["temperature"] == 0.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_returns_what_was_stored(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    key = ResponseCache.key_for({"model": "m", "messages": [{"role": "user", "content": "hi"}]})

    assert cache.get(key) is None
    cache.put(key, {"text": "stored"})
    assert cache.get(key) == {"text": "stored"}
    assert cache.hits == 1
    assert cache.misses == 1


def test_cache_key_changes_when_any_parameter_changes(tmp_path: Path) -> None:
    """The rule that makes a cache correct. Miss one field and you serve stale data."""
    base = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
        "max_tokens": 100,
    }

    original = ResponseCache.key_for(base)
    for field, value in (
        ("model", "gpt-4o"),
        ("temperature", 0.7),
        ("max_tokens", 200),
        ("messages", [{"role": "user", "content": "different"}]),
    ):
        assert ResponseCache.key_for({**base, field: value}) != original, field


def test_cache_key_ignores_dictionary_ordering(tmp_path: Path) -> None:
    """Without sorted keys you would silently never get a hit."""
    assert ResponseCache.key_for({"a": 1, "b": 2}) == ResponseCache.key_for({"b": 2, "a": 1})


def test_a_corrupt_cache_file_counts_as_a_miss(tmp_path: Path) -> None:
    """A broken cache must never break the application."""
    cache = ResponseCache(tmp_path / "cache")
    key = ResponseCache.key_for({"x": 1})
    cache.put(key, {"text": "fine"})

    path = cache._path_for(key)
    path.write_text("{ this is not valid json", encoding="utf-8")

    assert cache.get(key) is None


def test_disabled_cache_stores_nothing(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache", enabled=False)
    key = ResponseCache.key_for({"x": 1})
    cache.put(key, {"text": "ignored"})

    assert cache.get(key) is None
    assert cache.summary_line() == "cache disabled"


def test_clear_removes_everything(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    for n in range(5):
        cache.put(ResponseCache.key_for({"n": n}), {"text": str(n)})

    assert cache.clear() == 5
    assert cache.clear() == 0
