"""One suite per port, run against every implementation.

ARCHITECTURE.md §11: "every adapter runs the same suite against its port, so
local and hosted are held to one specification."

Only the fakes are registered in this ticket. TICKET-2 (Postgres) and TICKET-3
(Gemini, Groq) each add a builder to the relevant list below — one line — and
inherit the whole suite. If either has to restructure this file, the seam was
built wrong.

The score-direction tests are the highest-value assertions here. They are the
only thing standing between a helpfully-normalising adapter and a gate whose
thresholds have quietly stopped meaning anything (ADR-003).
"""

import pytest

from rag_adapters.fakes import (
    DIMENSIONS,
    FakeDenseStore,
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalStore,
)
from rag_core.contracts import Chunk, EmbeddedChunk

CORPUS = [
    Chunk(
        id="metformin_0",
        document_id="metformin",
        ordinal=0,
        anchor="1",
        content="Metformin adult starting dose is 500 mg twice daily with meals.",
        document_title="Metformin",
    ),
    Chunk(
        id="metformin_1",
        document_id="metformin",
        ordinal=1,
        anchor="2",
        content="Metformin is contraindicated in severe renal impairment.",
        document_title="Metformin",
    ),
    Chunk(
        id="atenolol_0",
        document_id="atenolol",
        ordinal=0,
        anchor="1",
        content="Atenolol initial dose for hypertension is 50 mg once daily.",
        document_title="Atenolol",
    ),
]


# --- registries: add one entry per new adapter ---------------------------

EMBEDDERS = [pytest.param(FakeEmbedder, id="fake")]
DENSE_STORES = [pytest.param(FakeDenseStore, id="fake")]
LEXICAL_STORES = [pytest.param(FakeLexicalStore, id="fake")]
GENERATORS = [pytest.param(FakeGenerator, id="fake")]


async def _populated_dense(builder, embedder):
    store = builder()
    embeddings = await embedder.embed_documents([c.content for c in CORPUS])
    await store.upsert(
        [EmbeddedChunk(chunk=c, embedding=e) for c, e in zip(CORPUS, embeddings, strict=True)]
    )
    return store


# --- EmbeddingProvider ---------------------------------------------------


@pytest.mark.parametrize("builder", EMBEDDERS)
async def test_embedder_returns_one_vector_per_input(builder):
    """A count mismatch would misalign chunk text with vectors and silently
    poison the store — the failure retrieval cannot detect."""
    embedder = builder()
    vectors = await embedder.embed_documents(["a", "b", "c"])
    assert len(vectors) == 3


@pytest.mark.parametrize("builder", EMBEDDERS)
async def test_embedder_vectors_all_match_the_declared_dimension(builder):
    embedder = builder()
    vectors = await embedder.embed_documents(["a", "b"])
    assert all(len(v) == embedder.dimension for v in vectors)
    assert len(await embedder.embed_query("q")) == embedder.dimension


@pytest.mark.parametrize("builder", EMBEDDERS)
async def test_embedder_declared_dimension_matches_the_index_schema(builder):
    """768 is in the schema (`vector(768)`), not a runtime preference."""
    assert builder().dimension == DIMENSIONS


@pytest.mark.parametrize("builder", EMBEDDERS)
async def test_embedder_handles_an_empty_batch(builder):
    assert await builder().embed_documents([]) == []


@pytest.mark.parametrize("builder", EMBEDDERS)
async def test_embedder_reports_a_model_id(builder):
    """The manifest records it, and startup compares against it."""
    assert builder().model_id


# --- DenseStore ----------------------------------------------------------


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_results_are_ordered_by_ascending_distance(builder):
    """Cosine DISTANCE, closest first. An adapter that returns similarity
    instead inverts every gate threshold without raising anything."""
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    hits = await store.search(await embedder.embed_query("metformin"), 3)
    scores = [h.score for h in hits]
    assert scores == sorted(scores), f"expected ascending distance, got {scores}"


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_nearest_result_is_the_semantically_closest(builder):
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    hits = await store.search(await embedder.embed_query("atenolol"), 1)
    assert hits[0].item.id == "atenolol_0"


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_k_larger_than_the_corpus_returns_fewer_rows_not_an_error(builder):
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    assert len(await store.search(await embedder.embed_query("metformin"), 500)) == len(CORPUS)


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_non_positive_k_returns_empty(builder):
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    vector = await embedder.embed_query("metformin")
    assert await store.search(vector, 0) == []
    assert await store.search(vector, -1) == []


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_search_on_an_empty_store_returns_empty(builder):
    assert await builder().search([0.0] * DIMENSIONS, 5) == []


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_upsert_is_idempotent_on_chunk_id(builder):
    """Re-running ingestion must converge, not accumulate (PRD F4)."""
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    before = await store.count()
    embeddings = await embedder.embed_documents([c.content for c in CORPUS])
    await store.upsert(
        [EmbeddedChunk(chunk=c, embedding=e) for c, e in zip(CORPUS, embeddings, strict=True)]
    )
    assert await store.count() == before


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_count_reflects_what_was_stored(builder):
    embedder = FakeEmbedder()
    assert await builder().count() == 0
    store = await _populated_dense(builder, embedder)
    assert await store.count() == len(CORPUS)


@pytest.mark.parametrize("builder", DENSE_STORES)
async def test_dense_returns_full_chunks_not_bare_ids(builder):
    """Hydration is a dict lookup over what the legs returned, so a store that
    omits content or title breaks citation rather than retrieval — and it
    breaks it silently."""
    embedder = FakeEmbedder()
    store = await _populated_dense(builder, embedder)
    hit = (await store.search(await embedder.embed_query("metformin"), 1))[0]
    assert hit.item.content
    assert hit.item.document_title
    assert hit.item.anchor


# --- LexicalStore --------------------------------------------------------


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_results_are_ordered_by_descending_rank(builder):
    """Opposite direction to the dense leg, because the underlying measures run
    in opposite directions. Fusion reads position only, but each list must be
    ordered correctly before RRF sees it."""
    store = builder(CORPUS)
    hits = await store.search("metformin dose contraindicated", 5)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), f"expected descending rank, got {scores}"


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_finds_an_exact_terminology_match(builder):
    """Exact drug and dose matching is what the lexical leg is for — the case
    where cosine similarity is mediocre but the term is right there."""
    hits = await builder(CORPUS).search("hypertension", 5)
    assert "atenolol_0" in [h.item.id for h in hits]


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_query_that_sanitises_to_nothing_returns_empty_not_an_error(builder):
    """A raw question full of punctuation must not raise. In the local build
    this exact class of input produced `fts5: syntax error` on questions as
    ordinary as "What's the max dose?"."""
    assert await builder(CORPUS).search("?!!  ...", 5) == []
    assert await builder(CORPUS).search("", 5) == []


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_non_positive_k_returns_empty(builder):
    assert await builder(CORPUS).search("metformin", 0) == []


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_search_on_an_empty_store_returns_empty(builder):
    assert await builder().search("metformin", 5) == []


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_index_is_idempotent_on_chunk_id(builder):
    store = builder(CORPUS)
    await store.index(list(CORPUS))
    hits = await store.search("metformin", 50)
    assert len({h.item.id for h in hits}) == len(hits), "duplicate ids after re-indexing"


@pytest.mark.parametrize("builder", LEXICAL_STORES)
async def test_lexical_returns_full_chunks_not_bare_ids(builder):
    hit = (await builder(CORPUS).search("metformin", 1))[0]
    assert hit.item.content
    assert hit.item.document_title


# --- GenerationProvider --------------------------------------------------


@pytest.mark.parametrize("builder", GENERATORS)
async def test_generator_stream_is_iterable_without_awaiting_the_call(builder):
    """`stream` is a plain `def` returning an async iterator. Awaiting the call
    itself would be the natural mistake and it must not be required."""
    stream = builder().stream([{"role": "user", "content": "q"}])
    tokens = [t async for t in stream]
    assert tokens


@pytest.mark.parametrize("builder", GENERATORS)
async def test_generator_reports_a_model_id(builder):
    """Every response names the provider that served it (ADR-004)."""
    assert builder().model_id


@pytest.mark.parametrize("builder", GENERATORS)
async def test_generator_yields_string_tokens(builder):
    stream = builder().stream([{"role": "user", "content": "q"}])
    assert all(isinstance(t, str) for t in [t async for t in stream])
