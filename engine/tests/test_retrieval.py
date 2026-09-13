"""Retrieval: chunking, embedding, ranking, citation."""

import pytest
from silkscreen.agents.model import ModelError
from silkscreen.agents.retrieval import (
    Chunk,
    HashEmbedder,
    VectorIndex,
    chunk_pages,
    cosine,
)

PAGES = [
    "STM32F103C8T6 datasheet. General description and block diagram.",
    "Absolute maximum ratings. Storage temperature -65 to 150 C.",
    "Pin definitions. Pin 1 VBAT power. Pin 8 VSSA ground analog. "
    "Pin 9 VDDA power analog supply decoupling required.",
    "Electrical characteristics. Operating current 36 mA at 72 MHz.",
]


def test_chunks_are_one_based_and_keep_their_page():
    chunks = chunk_pages(PAGES, size=40, overlap=10)
    assert chunks
    assert min(c.page for c in chunks) == 1
    assert max(c.page for c in chunks) == len(PAGES)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_page_numbers_are_rejected_below_one():
    with pytest.raises(ValueError, match="1-based"):
        Chunk(text="x", page=0, index=0)


def test_blank_pages_produce_no_chunks():
    assert chunk_pages(["", "   ", "\n"]) == []


def test_overlap_must_be_smaller_than_the_chunk():
    with pytest.raises(ValueError, match="overlap"):
        chunk_pages(PAGES, size=100, overlap=100)


def test_overlap_actually_overlaps():
    """A table split mid-row must appear whole in some chunk."""
    text = "PIN NAME FUNCTION " * 40
    chunks = chunk_pages([text], size=100, overlap=50)
    joined = [c.text for c in chunks]
    assert len(joined) > 1
    # Consecutive chunks share text, which is the entire point of overlap.
    assert any(
        joined[i][-20:] in joined[i + 1] for i in range(len(joined) - 1)
    )


def test_cosine_bounds_and_orthogonality():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_mismatched_dimensions():
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine([1.0], [1.0, 2.0])


def test_retrieval_finds_the_page_holding_the_answer():
    index = VectorIndex(embedder=HashEmbedder())
    index.add(chunk_pages(PAGES))
    assert len(index) == len(PAGES)

    hits = index.search("Which pin is VDDA analog supply?", k=2)
    assert hits, "expected at least one hit"
    assert hits[0].chunk.page == 3, "the pin table is on page 3"
    assert hits[0].citation == "p.3"
    assert hits[0].score > 0


def test_results_are_sorted_by_score_descending():
    index = VectorIndex(embedder=HashEmbedder())
    index.add(chunk_pages(PAGES))
    hits = index.search("storage temperature rating", k=4)
    assert [h.score for h in hits] == sorted(
        (h.score for h in hits), reverse=True
    )


def test_pages_cited_is_sorted_and_deduplicated():
    index = VectorIndex(embedder=HashEmbedder())
    index.add(chunk_pages(PAGES, size=40, overlap=5))
    hits = index.search("pin", k=6)
    pages = index.pages_cited(hits)
    assert pages == sorted(set(pages))


def test_empty_index_returns_no_hits():
    assert VectorIndex(embedder=HashEmbedder()).search("anything") == []


def test_k_must_be_positive():
    index = VectorIndex(embedder=HashEmbedder())
    index.add(chunk_pages(PAGES))
    with pytest.raises(ValueError, match="k must be positive"):
        index.search("x", k=0)


def test_min_score_filters_weak_matches():
    index = VectorIndex(embedder=HashEmbedder())
    index.add(chunk_pages(PAGES))
    assert index.search("pin", k=4, min_score=1.01) == []


def test_index_rejects_an_embedder_that_miscounts():
    class Broken:
        dimension = 4

        def embed(self, texts, *, query=False):
            return [[1.0, 0.0, 0.0, 0.0]]  # always one, regardless of input

    index = VectorIndex(embedder=Broken())
    with pytest.raises(ModelError, match="vectors for"):
        index.add(chunk_pages(PAGES))


def test_hash_embedder_is_deterministic_and_fixed_width():
    e = HashEmbedder(dimension=64)
    a = e.embed(["VDDA analog supply"])[0]
    b = e.embed(["VDDA analog supply"])[0]
    assert a == b
    assert len(a) == 64
