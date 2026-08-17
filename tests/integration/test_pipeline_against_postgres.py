"""`rag_core.pipeline.retrieve()` against a real database.

The first time the pipeline runs against anything other than a dictionary. A
reversed score direction or a broken join shows up here as a wrong *answer*
rather than a wrong row, which is the level at which those bugs actually get
noticed.

The embedder is still fake — TICKET-3 brings the real one. What is real here is
the storage, the SQL, and the two legs running concurrently against one pool.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from rag_adapters.fakes import FakeEmbedder
from rag_adapters.postgres import PostgresDenseStore, PostgresLexicalStore, PostgresPool
from rag_core.config import load_config
from rag_core.contracts import Chunk, EmbeddedChunk
from rag_core.pipeline import retrieve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contract"))
from stores import CORPUS, embedded  # noqa: E402

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pool(pg_dsn):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
    from migrate import apply_pending

    await apply_pending(pg_dsn)
    pool = PostgresPool(dsn=pg_dsn, min_size=1, max_size=5)
    await pool.open()
    async with pool.pool.acquire() as conn:
        await conn.execute("truncate document, chunk, index_manifest restart identity cascade")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def populated(pool):
    dense = PostgresDenseStore(pool)
    await dense.upsert(await embedded(CORPUS))
    return pool


def permissive(cfg):
    """The carried-over thresholds were measured against a different embedding
    model and are invalid until TICKET-8 re-sweeps them, so tests that want to
    inspect what was DELIVERED must not be gated by them."""
    return replace(cfg, gate=replace(cfg.gate, tau_abstain=-1.0, tau_strong=-1.0))


async def test_a_grounded_question_retrieves_and_cites(populated):
    cfg = permissive(load_config(env={}))
    result = await retrieve(
        "What is the metformin starting dose?",
        FakeEmbedder(),
        PostgresDenseStore(populated),
        PostgresLexicalStore(populated),
        cfg,
    )
    assert result.decision.proceed
    assert result.chunks
    first = result.chunks[0]
    assert first.chunk_id.startswith("metformin")
    # Citation resolution depends on the join carrying the document title and
    # the anchor surviving as an int page number.
    assert first.title == "Metformin"
    assert isinstance(first.page_number, int)


async def test_similarity_is_derived_from_the_stores_raw_distance(populated):
    """End-to-end proof that the adapter returns distance and the pipeline
    converts. If the store returned similarity, this number would be inverted
    and every gate threshold would quietly mean its opposite."""
    cfg = permissive(load_config(env={}))
    result = await retrieve(
        "metformin",
        FakeEmbedder(),
        PostgresDenseStore(populated),
        PostgresLexicalStore(populated),
        cfg,
    )
    top = result.decision.signals["top_similarity"]
    assert top == pytest.approx(1.0, abs=1e-6), f"expected a near-exact match, got {top}"


async def test_an_off_domain_question_is_refused_without_a_model_call(populated):
    cfg = load_config(env={})
    result = await retrieve(
        "What is the capital of France?",
        FakeEmbedder(),
        PostgresDenseStore(populated),
        PostgresLexicalStore(populated),
        cfg,
    )
    assert result.decision.proceed is False
    assert result.decision.reason == "off_domain"
    assert result.chunks == []


async def test_an_empty_database_is_reported_as_an_empty_corpus(pool):
    """Distinct from off-domain: an unpopulated index is a deployment fault and
    has its own user-facing copy."""
    cfg = load_config(env={})
    result = await retrieve(
        "metformin dose",
        FakeEmbedder(),
        PostgresDenseStore(pool),
        PostgresLexicalStore(pool),
        cfg,
    )
    assert result.decision.reason == "empty_corpus"


async def test_a_lexically_rescued_chunk_survives_fusion_and_hydration(populated):
    """The signature of the lexical leg earning its place: exact terminology
    where the vector neighbourhood is unhelpful. Hydration must find it even
    though it came from the other leg's result set."""
    cfg = permissive(load_config(env={}))
    result = await retrieve(
        "hypertension",
        FakeEmbedder(),
        PostgresDenseStore(populated),
        PostgresLexicalStore(populated),
        cfg,
    )
    assert "atenolol_0" in [c.chunk_id for c in result.chunks]


async def test_a_stopword_only_question_does_not_error(populated):
    """to_tsquery('english', '') raises. Without the guard this would 500
    rather than simply finding nothing lexically."""
    cfg = load_config(env={})
    result = await retrieve(
        "what is it?",
        FakeEmbedder(),
        PostgresDenseStore(populated),
        PostgresLexicalStore(populated),
        cfg,
    )
    assert result.decision.signals["lexical_support"] is False


async def test_the_manifest_round_trips_through_the_pipeline_stores(populated):
    from datetime import UTC, datetime

    from rag_core.contracts import IndexManifest

    dense = PostgresDenseStore(populated)
    assert await dense.read_manifest() is None
    await dense.write_manifest(
        IndexManifest("fake-embed-001", 768, datetime(2026, 8, 17, tzinfo=UTC))
    )
    manifest = await dense.read_manifest()
    assert manifest is not None
    assert manifest.embedding_model_id == "fake-embed-001"


async def test_a_document_id_containing_underscores_round_trips(pool):
    """Chunk ids split on the LAST underscore. A slug with its own underscores
    is the case that breaks a first-underscore split — silently, by resolving
    to nothing."""
    dense = PostgresDenseStore(pool)
    chunk = Chunk(
        id="some_drug_name_3",
        document_id="some_drug_name",
        ordinal=3,
        anchor="2",
        content="Metformin adult dose information.",
        document_title="Some Drug Name",
    )
    vector = await FakeEmbedder().embed_query(chunk.content)
    await dense.upsert([EmbeddedChunk(chunk=chunk, embedding=vector)])

    cfg = permissive(load_config(env={}))
    result = await retrieve(
        "metformin dose",
        FakeEmbedder(),
        dense,
        PostgresLexicalStore(pool),
        cfg,
    )
    assert [c.chunk_id for c in result.chunks] == ["some_drug_name_3"]
