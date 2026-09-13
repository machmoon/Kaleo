"""Retrieval over datasheet text.

A datasheet is 300 pages and the answer to "which pin is AVDD" is on one of
them. Putting all of it in front of the model every time is slow, expensive,
and worse at the task than putting the right four pages in front of it.

So this is a real retrieval pipeline: split the document into overlapping
chunks that remember their page, embed each chunk, embed the question, and
return the nearest chunks by cosine similarity. Every chunk carries its page
number, which is what makes a citation possible downstream.

Embedding runs through an :class:`Embedder` seam for the same reason model
calls do -- :class:`HashEmbedder` makes the whole pipeline exercisable offline.
It is a deterministic stand-in, not a semantic model, and is named so nobody
mistakes it for one.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Protocol

from .model import ModelError, request_timeout_ms

__all__ = [
    "Chunk",
    "Embedder",
    "GeminiEmbedder",
    "HashEmbedder",
    "Retrieved",
    "VectorIndex",
    "chunk_pages",
    "cosine",
]

#: Gemini's embedding model.
EMBED_MODEL = "gemini-embedding-001"

#: Chunk size in characters. Large enough to hold a pinout table row group,
#: small enough that a hit is worth citing.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class Chunk:
    """A slice of a document that remembers where it came from."""

    text: str
    page: int
    index: int

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"page is 1-based, got {self.page}")


@dataclass(frozen=True)
class Retrieved:
    """A chunk and how well it matched."""

    chunk: Chunk
    score: float

    @property
    def citation(self) -> str:
        return f"p.{self.chunk.page}"


class Embedder(Protocol):
    """Anything that turns text into vectors."""

    dimension: int

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


def chunk_pages(pages: list[str], *, size: int = CHUNK_CHARS,
                overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """Split per-page text into overlapping chunks, preserving page numbers.

    Overlap matters here specifically: a pin table split mid-row puts the pin
    name in one chunk and its function in the next, and neither retrieves.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError(f"overlap must be in [0, {size}), got {overlap}")

    chunks: list[Chunk] = []
    for page_no, text in enumerate(pages, start=1):
        collapsed = re.sub(r"[ \t]+", " ", text).strip()
        if not collapsed:
            continue
        start = 0
        while start < len(collapsed):
            piece = collapsed[start : start + size].strip()
            if piece:
                chunks.append(Chunk(text=piece, page=page_no, index=len(chunks)))
            if start + size >= len(collapsed):
                break
            start += size - overlap
    return chunks


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 if either vector has no magnitude."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class GeminiEmbedder:
    """The live path, through google-genai. Requires ``GOOGLE_API_KEY``."""

    def __init__(self, model: str = EMBED_MODEL, *, api_key: str | None = None,
                 dimension: int = 768, timeout_s: float | None = None):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - import guard
            raise ModelError(
                "google-genai is not installed. pip install google-genai"
            ) from exc

        import os

        key = (
            api_key
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY")
        )
        if not key:
            raise ModelError("GOOGLE_API_KEY is not set.")
        # Same per-request deadline as GeminiModel, resolved by the same
        # function: a hung embedding call would stall retrieval exactly the
        # way a hung generate call stalls propose.
        timeout_ms = request_timeout_ms(timeout_s)
        self.timeout_s = timeout_ms / 1000.0
        self._client = genai.Client(
            api_key=key, http_options={"timeout": timeout_ms}
        )
        self.model = model
        self.dimension = dimension

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        # Asymmetric task types: a question and a passage are embedded into the
        # same space but from different sides. Using one type for both measurably
        # degrades retrieval.
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        try:
            resp = self._client.models.embed_content(
                model=self.model,
                contents=texts,
                config={"task_type": task, "output_dimensionality": self.dimension},
            )
        except Exception as exc:
            raise ModelError(f"{self.model} embedding failed: {exc}") from exc

        vectors = [list(e.values) for e in getattr(resp, "embeddings", [])]
        if len(vectors) != len(texts):
            raise ModelError(
                f"expected {len(texts)} embeddings, got {len(vectors)}"
            )
        return vectors


@dataclass
class HashEmbedder:
    """Deterministic offline stand-in. Not a semantic model.

    Hashes token trigrams into a fixed-width bag of counts. Documents sharing
    vocabulary land near each other, which is enough to exercise chunking,
    indexing, ranking and citation without a network call. It will not
    generalise across synonyms, and nothing in the codebase pretends it does.
    """

    dimension: int = 256

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        for token in tokens:
            for gram in (token, *(token[i : i + 3] for i in range(len(token) - 2))):
                digest = hashlib.blake2b(gram.encode(), digest_size=4).digest()
                vec[int.from_bytes(digest, "big") % self.dimension] += 1.0
        return vec


@dataclass
class VectorIndex:
    """Chunks plus their vectors, searchable by cosine similarity.

    Linear scan. A datasheet is thousands of chunks, not millions, and an exact
    scan over a few thousand vectors is faster than the network call that
    produced them -- an approximate index here would add a dependency and a
    recall cliff to save nothing.
    """

    embedder: Embedder
    chunks: list[Chunk] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        vectors = self.embedder.embed([c.text for c in chunks])
        if len(vectors) != len(chunks):
            raise ModelError(
                f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
            )
        self.chunks.extend(chunks)
        self.vectors.extend(vectors)

    def search(self, question: str, *, k: int = 4,
               min_score: float = 0.0) -> list[Retrieved]:
        """The ``k`` best-matching chunks, best first."""
        if k <= 0:
            raise ValueError("k must be positive")
        if not self.chunks:
            return []
        query_vec = self.embedder.embed([question], query=True)[0]
        scored = [
            Retrieved(chunk=chunk, score=cosine(query_vec, vec))
            for chunk, vec in zip(self.chunks, self.vectors, strict=True)
        ]
        scored.sort(key=lambda r: (-r.score, r.chunk.index))
        return [r for r in scored[:k] if r.score >= min_score]

    def pages_cited(self, results: list[Retrieved]) -> list[int]:
        """Sorted, de-duplicated page numbers behind a result set."""
        return sorted({r.chunk.page for r in results})

    def __len__(self) -> int:
        return len(self.chunks)
