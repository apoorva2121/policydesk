"""Finding the chunks most likely to answer a question -- and showing why.

How the search works
--------------------
The question is embedded with the same model used for the chunks. Chroma returns
the chunks whose vectors are closest, as a **distance** (smaller is closer). We
convert that to a similarity score between 0 and 1, because "0.82 similar" is
easier to reason about than "0.18 distant".

Why `why_selected` exists
-------------------------
A retrieval system you cannot inspect is a retrieval system you cannot fix. When
an answer is wrong, there are only two possibilities: the right chunk was never
retrieved (a retrieval problem), or it was retrieved and the model ignored it (a
generation problem). Those have completely different fixes, and you cannot tell
them apart without seeing what came back.

So every result carries its score and the words it shares with the question. That
word overlap is not how the search works -- the search is by meaning -- but it is
a useful hint about *why* this chunk looked relevant, and it instantly exposes the
classic failure where a chunk matched on a common word like "policy".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.client import LLMClient
from .index import VectorIndex

# Words too common to be evidence of anything.
_STOP_WORD_TEXT = (
    "a an and are as at be been by can do does for from has have how i if in is "
    "it its me must my of on or our should that the their there they this to was "
    "we what when where which who why will with you your"
)
STOP_WORDS = frozenset(_STOP_WORD_TEXT.split())


@dataclass(frozen=True)
class RetrievedChunk:
    """One search result, with everything needed to judge it."""

    chunk_id: str
    text: str
    citation: str
    source_id: str
    locator: str
    section: str | None
    kind: str
    similarity: float
    distance: float
    rank: int
    shared_words: tuple[str, ...]

    @property
    def why_selected(self) -> str:
        """A one-line, human explanation of this result."""
        words = ", ".join(self.shared_words[:6]) if self.shared_words else "none"
        return f"similarity {self.similarity:.3f} (rank {self.rank}); shared words: {words}"


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z'-]+", text.lower())
    return {word for word in words if word not in STOP_WORDS and len(word) > 2}


def retrieve(
    question: str,
    index: VectorIndex,
    llm: LLMClient,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """Return the `top_k` chunks closest in meaning to the question."""
    if not question.strip():
        raise ValueError("cannot retrieve for an empty question")

    # The question must be embedded by the same model that built the index.
    # load_index() has already refused to open an index where that is not true.
    model = index.manifest.embedding_model
    embedded = llm.embed([question], purpose="retrieve_query", model=model)

    available = index.size
    if available == 0:
        return []

    result = index.collection.query(
        query_embeddings=embedded.vectors,
        n_results=min(top_k, available),
        include=["documents", "metadatas", "distances"],
    )

    question_words = _significant_words(question)
    out: list[RetrievedChunk] = []

    ids = result["ids"][0]
    documents = result["documents"][0]
    metadatas = result["metadatas"][0]
    distances = result["distances"][0]

    for rank, (chunk_id, text, meta, distance) in enumerate(
        zip(ids, documents, metadatas, distances, strict=True), start=1
    ):
        section = str(meta.get("section") or "") or None
        shared = sorted(question_words & _significant_words(text))
        out.append(
            RetrievedChunk(
                chunk_id=chunk_id,
                text=text,
                citation=str(meta.get("citation", chunk_id)),
                source_id=str(meta.get("source_id", "")),
                locator=str(meta.get("locator", "")),
                section=section,
                kind=str(meta.get("kind", "text")),
                # Chroma's cosine "distance" runs 0 (identical) to 2 (opposite).
                similarity=1.0 - float(distance),
                distance=float(distance),
                rank=rank,
                shared_words=tuple(shared),
            )
        )

    return out
