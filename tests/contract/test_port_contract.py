"""One suite per port, run against every implementation.

ARCHITECTURE.md §11: "every adapter runs the same suite against its port, so
local and hosted are held to one specification."

TICKET-1 wrote here that a new adapter would be "one line in the parametrisation,
and if TICKET-2 has to restructure this file, the seam was built wrong." The seam
was built wrong, and TICKET-2 rebuilt it. The registry held classes and the tests
called `builder()` / `builder(CORPUS)`, which works only for a store with no
dependencies and no lifecycle, and which seeded through a constructor — skipping
the write path entirely.

Stores now come from the `stores` fixture in conftest.py (a `StorePair` per
profile, with a profile-specific `seed`), so a new store adapter is a fixture
plus one entry in `STORE_PAIRS`. Embedders and generators kept the original
shape, because they genuinely have no setup. Every assertion below is the one
TICKET-1 wrote; only how a store is obtained and populated changed.

The score-direction tests are the highest-value assertions here. They are the
only thing standing between a helpfully-normalising adapter and a gate whose
thresholds have quietly stopped meaning anything (ADR-003).
"""

from datetime import UTC, datetime

import pytest
from providers import EMBEDDERS, GENERATORS
from stores import CORPUS, STORE_PAIRS, embedded

from rag_adapters.fakes import DIMENSIONS, FakeEmbedder
from rag_core.contracts import IndexManifest

# --- registries ----------------------------------------------------------
#
# EMBEDDERS and GENERATORS live in providers.py and STORE_PAIRS in stores.py,
# each next to the doubles it needs.
#
# Adding a provider IS one line, as TICKET-1 promised. Adding a store needed a
# fixture and a rebuild of this file, because a store has a connection and a
# lifecycle and a provider does not — which is why TICKET-2 left these two
# registries alone.


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
#
# Every assertion below is TICKET-1's, unchanged. Only how a store is obtained
# and populated differs: `stores` resolves to a profile's pair of legs, and
# `seed()` writes through that profile's own methods rather than a constructor.


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_results_are_ordered_by_ascending_distance(stores):
    """Cosine DISTANCE, closest first. An adapter that returns similarity
    instead inverts every gate threshold without raising anything."""
    await stores.seed(await embedded(CORPUS))
    embedder = FakeEmbedder()
    hits = await stores.dense.search(await embedder.embed_query("metformin"), 3)
    scores = [h.score for h in hits]
    assert scores == sorted(scores), f"expected ascending distance, got {scores}"


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_nearest_result_is_the_semantically_closest(stores):
    await stores.seed(await embedded(CORPUS))
    embedder = FakeEmbedder()
    hits = await stores.dense.search(await embedder.embed_query("atenolol"), 1)
    assert hits[0].item.id == "atenolol_0"


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_k_larger_than_the_corpus_returns_fewer_rows_not_an_error(stores):
    await stores.seed(await embedded(CORPUS))
    embedder = FakeEmbedder()
    hits = await stores.dense.search(await embedder.embed_query("metformin"), 500)
    assert len(hits) == len(CORPUS)


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_non_positive_k_returns_empty(stores):
    await stores.seed(await embedded(CORPUS))
    vector = await FakeEmbedder().embed_query("metformin")
    assert await stores.dense.search(vector, 0) == []
    assert await stores.dense.search(vector, -1) == []


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_search_on_an_empty_store_returns_empty(stores):
    assert await stores.dense.search([0.0] * DIMENSIONS, 5) == []


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_upsert_is_idempotent_on_chunk_id(stores):
    """Re-running ingestion must converge, not accumulate (PRD F4)."""
    chunks = await embedded(CORPUS)
    await stores.seed(chunks)
    before = await stores.dense.count()
    await stores.seed(chunks)
    assert await stores.dense.count() == before


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_upsert_updates_content_in_place(stores):
    """An edit converges rather than adding a second row for the same id."""
    from dataclasses import replace

    chunks = await embedded(CORPUS)
    await stores.seed(chunks)
    edited = [replace(chunks[0], chunk=replace(chunks[0].chunk, content="Revised dosing text."))]
    await stores.seed(edited)
    assert await stores.dense.count() == len(CORPUS)
    hits = await stores.dense.search(await FakeEmbedder().embed_query("metformin"), 5)
    contents = {h.item.id: h.item.content for h in hits}
    assert contents["metformin_0"] == "Revised dosing text."


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_count_reflects_what_was_stored(stores):
    assert await stores.dense.count() == 0
    await stores.seed(await embedded(CORPUS))
    assert await stores.dense.count() == len(CORPUS)


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_returns_full_chunks_not_bare_ids(stores):
    """Hydration is a dict lookup over what the legs returned, so a store that
    omits content or title breaks citation rather than retrieval — and it
    breaks it silently."""
    await stores.seed(await embedded(CORPUS))
    hit = (await stores.dense.search(await FakeEmbedder().embed_query("metformin"), 1))[0]
    assert hit.item.content
    assert hit.item.document_title
    assert hit.item.anchor


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_upsert_of_nothing_is_harmless(stores):
    await stores.dense.upsert([])
    assert await stores.dense.count() == 0


# --- the manifest --------------------------------------------------------


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_manifest_is_absent_until_written(stores):
    """Absent must be distinguishable from a manifest whose fields are empty.
    The startup check maps absent to refuse-to-serve, and a zero-valued
    manifest would instead produce a confusing mismatch message."""
    assert await stores.dense.read_manifest() is None


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_manifest_round_trips(stores):
    written = IndexManifest(
        embedding_model_id="fake-embed-001",
        dimension=DIMENSIONS,
        ingested_at=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
    )
    await stores.dense.write_manifest(written)
    read = await stores.dense.read_manifest()
    assert read is not None
    assert read.embedding_model_id == written.embedding_model_id
    assert read.dimension == written.dimension
    assert read.ingested_at == written.ingested_at


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_writing_the_manifest_twice_leaves_one_manifest(stores):
    first = IndexManifest("model-a", 768, datetime(2026, 8, 1, tzinfo=UTC))
    second = IndexManifest("model-b", 384, datetime(2026, 8, 2, tzinfo=UTC))
    await stores.dense.write_manifest(first)
    await stores.dense.write_manifest(second)
    read = await stores.dense.read_manifest()
    assert read is not None
    assert read.embedding_model_id == "model-b"
    assert read.dimension == 384


# --- LexicalStore --------------------------------------------------------


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_results_are_ordered_by_descending_rank(stores):
    """Opposite direction to the dense leg, because the underlying measures run
    in opposite directions. Fusion reads position only, but each list must be
    ordered correctly before RRF sees it."""
    await stores.seed(await embedded(CORPUS))
    hits = await stores.lexical.search("metformin dose contraindicated", 5)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), f"expected descending rank, got {scores}"


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_finds_an_exact_terminology_match(stores):
    """Exact drug and dose matching is what the lexical leg is for — the case
    where cosine similarity is mediocre but the term is right there."""
    await stores.seed(await embedded(CORPUS))
    hits = await stores.lexical.search("hypertension", 5)
    assert "atenolol_0" in [h.item.id for h in hits]


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_query_that_sanitises_to_nothing_returns_empty_not_an_error(stores):
    """A raw question full of punctuation must not raise. In the local build
    this exact class of input produced `fts5: syntax error` on questions as
    ordinary as "What's the max dose?"; on Postgres the equivalent is
    to_tsquery raising on an empty string."""
    await stores.seed(await embedded(CORPUS))
    assert await stores.lexical.search("?!!  ...", 5) == []
    assert await stores.lexical.search("", 5) == []
    assert await stores.lexical.search("what is it?", 5) == []


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_non_positive_k_returns_empty(stores):
    await stores.seed(await embedded(CORPUS))
    assert await stores.lexical.search("metformin", 0) == []


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_search_on_an_empty_store_returns_empty(stores):
    assert await stores.lexical.search("metformin", 5) == []


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_never_returns_duplicate_ids(stores):
    await stores.seed(await embedded(CORPUS))
    hits = await stores.lexical.search("metformin dose", 50)
    assert len({h.item.id for h in hits}) == len(hits), "duplicate ids in lexical results"


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_returns_full_chunks_not_bare_ids(stores):
    await stores.seed(await embedded(CORPUS))
    hit = (await stores.lexical.search("metformin", 1))[0]
    assert hit.item.content
    assert hit.item.document_title


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_lexical_only_returns_chunks_that_actually_match(stores):
    """A term appearing in no chunk yields nothing, rather than everything at
    rank zero — which would leave the gate's lexical_support permanently True."""
    await stores.seed(await embedded(CORPUS))
    assert await stores.lexical.search("cardiomyopathy", 5) == []


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
