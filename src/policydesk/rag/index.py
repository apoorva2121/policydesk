"""Storing chunk vectors so we can find the closest ones fast.

What a vector index is, plainly
-------------------------------
We turn each chunk into a list of numbers (an embedding) that represents its
meaning. To answer a question we turn the question into numbers the same way, and
look for the chunks whose numbers are closest. "Closest" is measured with cosine
distance, which compares the *direction* of two vectors and ignores their length.

We use Chroma, which stores the vectors on disk and does that search for us.

The most important thing in this file: the manifest
--------------------------------------------------
An index built with one embedding model and queried with another returns
confident nonsense -- the numbers are not comparable, but nothing crashes. The
same is true if you change the chunking and forget to rebuild.

So every index is written with a small manifest recording exactly how it was
built. Loading it checks the manifest against your current settings and refuses
to continue if they disagree. That turns a silent-wrong-answers bug into a clear
error message, which is the whole job.

Not a production vector store
-----------------------------
Chroma on local disk is right for learning. In production you would use a managed
service (pgvector, Azure AI Search, Pinecone) for backups, access control and
concurrent writers. See the gap list in docs/PLAN.md.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import chromadb
from pydantic import BaseModel

from ..config import Settings
from ..llm.client import LLMClient
from .chunking import Chunk

COLLECTION_NAME = "policydesk_chunks"
MANIFEST_NAME = "index-manifest.json"


class IndexMismatch(RuntimeError):
    """The index on disk was built differently from how you are now configured."""


class IndexManifest(BaseModel):
    """How this index was built. Changing any of these means rebuilding."""

    embedding_model: str
    embedding_dimensions: int
    chunk_strategy: str
    chunk_size_chars: int
    chunk_overlap_chars: int
    chunk_count: int
    built_at: str

    def fingerprint(self) -> tuple:
        """The parts that must match. `chunk_count` and `built_at` are informational."""
        return (
            self.embedding_model,
            self.chunk_strategy,
            self.chunk_size_chars,
            self.chunk_overlap_chars,
        )

    @classmethod
    def from_settings(cls, settings: Settings, chunk_count: int, dimensions: int) -> IndexManifest:
        return cls(
            embedding_model=settings.embedding_model,
            embedding_dimensions=dimensions,
            chunk_strategy=settings.chunk_strategy,
            chunk_size_chars=settings.chunk_size_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            chunk_count=chunk_count,
            built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


class VectorIndex:
    """A thin wrapper over one Chroma collection."""

    def __init__(
        self, collection: chromadb.api.models.Collection.Collection, manifest: IndexManifest
    ):
        self.collection = collection
        self.manifest = manifest

    @property
    def size(self) -> int:
        return self.collection.count()

    def summary_line(self) -> str:
        return (
            f"{self.size} chunks | {self.manifest.embedding_model} "
            f"({self.manifest.embedding_dimensions}d) | strategy="
            f"{self.manifest.chunk_strategy} | built {self.manifest.built_at}"
        )


def _manifest_path(settings: Settings) -> Path:
    return settings.vector_store_dir / MANIFEST_NAME


def _chroma_client(settings: Settings) -> chromadb.ClientAPI:
    settings.vector_store_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(settings.vector_store_dir))


def build_index(chunks: list[Chunk], llm: LLMClient, settings: Settings) -> VectorIndex:
    """Embed every chunk and write a fresh index, replacing any existing one.

    We rebuild rather than update. For a corpus this size it costs almost
    nothing, and it removes a whole class of bug where deleted documents linger
    in the index and get cited long after they were removed.
    """
    if not chunks:
        raise ValueError("refusing to build an empty index: there would be nothing to retrieve")

    # Note we embed `embed_text` (with the context header), not `text`.
    embedded = llm.embed([chunk.embed_text for chunk in chunks], purpose="index_build")

    client = _chroma_client(settings)
    # Delete first so a rebuild cannot leave stale chunks behind. An absent
    # collection is the normal first-run case, so suppressing is correct here.
    with contextlib.suppress(Exception):
        client.delete_collection(COLLECTION_NAME)

    collection = client.create_collection(
        name=COLLECTION_NAME,
        # Cosine distance compares direction, which is what embeddings encode.
        metadata={"hnsw:space": "cosine"},
    )

    collection.add(
        ids=[chunk.chunk_id for chunk in chunks],
        embeddings=embedded.vectors,
        documents=[chunk.text for chunk in chunks],
        # Chroma metadata values must be str/int/float/bool, never None,
        # so an absent section becomes an empty string.
        metadatas=[
            {
                "source_id": chunk.source_id,
                "source_type": chunk.source_type,
                "kind": chunk.kind,
                "locator": chunk.locator,
                "section": chunk.section or "",
                "citation": chunk.citation,
                "char_count": chunk.char_count,
            }
            for chunk in chunks
        ],
    )

    manifest = IndexManifest.from_settings(settings, len(chunks), embedded.dimensions)
    _manifest_path(settings).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    return VectorIndex(collection, manifest)


def load_index(settings: Settings) -> VectorIndex:
    """Open an existing index, refusing to use one built differently."""
    path = _manifest_path(settings)
    if not path.exists():
        raise IndexMismatch(
            f"no index found at {settings.vector_store_dir}. Build one first:\n  policydesk index"
        )

    manifest = IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))
    wanted = IndexManifest.from_settings(
        settings, manifest.chunk_count, manifest.embedding_dimensions
    )

    if manifest.fingerprint() != wanted.fingerprint():
        differences = [
            f"  {name}: index has {getattr(manifest, name)!r}, "
            f"settings say {getattr(wanted, name)!r}"
            for name in (
                "embedding_model",
                "chunk_strategy",
                "chunk_size_chars",
                "chunk_overlap_chars",
            )
            if getattr(manifest, name) != getattr(wanted, name)
        ]
        raise IndexMismatch(
            "this index was built with different settings, so its vectors are not "
            "comparable to what you would produce now:\n"
            + "\n".join(differences)
            + "\n\nRebuild it:  policydesk index\n"
            "(Querying it anyway would return confident nonsense without any error.)"
        )

    client = _chroma_client(settings)
    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception as exc:  # noqa: BLE001
        raise IndexMismatch(
            f"the manifest exists but the collection does not. Rebuild:  policydesk index\n({exc})"
        ) from exc

    return VectorIndex(collection, manifest)


def index_exists(settings: Settings) -> bool:
    return _manifest_path(settings).exists()


def read_manifest(settings: Settings) -> IndexManifest | None:
    path = _manifest_path(settings)
    if not path.exists():
        return None
    return IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))
