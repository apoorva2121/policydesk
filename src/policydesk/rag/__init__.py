"""Retrieval: turning files into searchable chunks, and chunks into answers."""

from .answer import REFUSAL_TOKEN, Answer, answer_question
from .chunking import Chunk, chunk_blocks, chunk_stats
from .index import IndexMismatch, VectorIndex, build_index, index_exists, load_index, read_manifest
from .retrieve import RetrievedChunk, retrieve

__all__ = [
    "REFUSAL_TOKEN",
    "Answer",
    "Chunk",
    "IndexMismatch",
    "RetrievedChunk",
    "VectorIndex",
    "answer_question",
    "build_index",
    "chunk_blocks",
    "chunk_stats",
    "index_exists",
    "load_index",
    "read_manifest",
    "retrieve",
]
