from rag_core.chunking import PageText, ChunkDraft, chunk_pages
from rag_core.config import ChunkConfig

CFG = ChunkConfig(size=100, overlap=20)


def test_short_page_becomes_one_chunk():
    chunks = chunk_pages([PageText(1, "Metformin 500mg twice daily.")], CFG)
    assert len(chunks) == 1
    assert chunks[0] == ChunkDraft(chunk_index=0, page_number=1, text="Metformin 500mg twice daily.")


def test_chunks_never_span_a_page_boundary():
    pages = [PageText(1, "alpha " * 40), PageText(2, "beta " * 40)]
    chunks = chunk_pages(pages, CFG)
    for c in chunks:
        assert not ("alpha" in c.text and "beta" in c.text)


def test_page_number_is_preserved_per_chunk():
    pages = [PageText(7, "gamma " * 40), PageText(8, "delta " * 40)]
    chunks = chunk_pages(pages, CFG)
    assert {c.page_number for c in chunks} == {7, 8}
    assert all(c.page_number == 7 for c in chunks if "gamma" in c.text)


def test_chunk_index_is_monotonic_across_pages():
    pages = [PageText(1, "one " * 40), PageText(2, "two " * 40), PageText(3, "three " * 40)]
    chunks = chunk_pages(pages, CFG)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_blank_pages_are_skipped_without_consuming_an_index():
    pages = [PageText(1, "content here"), PageText(2, "   \n  "), PageText(3, "more content")]
    chunks = chunk_pages(pages, CFG)
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert [c.page_number for c in chunks] == [1, 3]


def test_overlap_carries_tail_of_previous_chunk():
    page = PageText(1, "".join(f"sentence{i}. " for i in range(40)))
    chunks = chunk_pages([page], ChunkConfig(size=100, overlap=20))
    assert len(chunks) > 1
    tail = chunks[0].text[-20:]
    assert chunks[1].text.startswith(tail)


def test_no_chunk_greatly_exceeds_configured_size():
    page = PageText(1, "x" * 1000)  # no separators at all
    chunks = chunk_pages([page], ChunkConfig(size=100, overlap=0))
    assert all(len(c.text) <= 100 for c in chunks)


def test_empty_document_yields_no_chunks():
    assert chunk_pages([], CFG) == []
    assert chunk_pages([PageText(1, "")], CFG) == []


def test_overlap_is_added_on_top_of_size_not_carved_out_of_it():
    """Documents the size contract: max chunk length is size + overlap.

    Pinned deliberately. If this ever changes, chunk boundaries shift and the
    Phase 3 threshold sweep is no longer comparable to earlier runs.
    """
    page = PageText(1, "x" * 250)
    chunks = chunk_pages([page], ChunkConfig(size=100, overlap=20))
    assert max(len(c.text) for c in chunks) == 120
    assert all(len(c.text) <= 100 + 20 for c in chunks)


def test_non_positive_chunk_size_raises_rather_than_dropping_text():
    """A negative size once made _split_recursive return [] — silently losing
    the entire page. Losing text is unrecoverable: retrieval can never find it."""
    import pytest
    for bad in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            chunk_pages([PageText(1, "content that must not vanish")], ChunkConfig(size=bad, overlap=0))
