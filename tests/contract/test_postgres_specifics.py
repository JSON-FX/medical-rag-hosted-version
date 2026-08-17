"""Behaviour that is Postgres's, not the port's.

These do not belong in the shared contract suite: they assert what the database
enforces (dimensions, foreign keys, generated columns) and what only this
adapter can get wrong. Putting them in the contract file would mean either
skipping them for the fakes behind a conditional, or holding the fakes to a
schema they do not have.
"""

import math

import pytest
from stores import CORPUS, embedded

from rag_adapters.postgres import PostgresDenseStore, PostgresLexicalStore
from rag_core.contracts import Chunk, EmbeddedChunk

pytestmark = pytest.mark.postgres


@pytest.fixture
def dense(pg_pool):
    return PostgresDenseStore(pg_pool)


@pytest.fixture
def lexical(pg_pool):
    return PostgresLexicalStore(pg_pool)


async def test_a_vector_of_the_wrong_dimension_is_rejected(dense):
    """768 is in the schema, not a runtime preference (ARCHITECTURE.md §5).
    The database is the authority, and it should say so rather than silently
    storing something the index cannot use."""
    chunk = Chunk(
        id="metformin_9",
        document_id="metformin",
        ordinal=9,
        anchor="1",
        content="text",
        document_title="Metformin",
    )
    with pytest.raises(Exception, match="(?i)dimension|expected"):
        await dense.upsert([EmbeddedChunk(chunk=chunk, embedding=[0.1] * 384)])


async def test_a_zero_vector_yields_nan_and_the_gate_fails_closed(dense):
    """pgvector's cosine distance against a zero vector is NaN. The gate treats
    a non-finite similarity as off-domain rather than falling through to `ok`,
    so this degrades correctly — but the path from "embedder returned zeros" to
    "gate refuses" runs through three modules, and is worth pinning."""
    from rag_core.config import GateConfig
    from rag_core.gate import GateSignals, evaluate_gate

    await dense.upsert(await embedded(CORPUS))
    hits = await dense.search([0.0] * 768, 3)
    assert hits
    assert all(math.isnan(h.score) for h in hits)

    similarity = 1.0 - hits[0].score
    decision = evaluate_gate(
        GateSignals(similarity, similarity, lexical_support=True), GateConfig()
    )
    assert decision.proceed is False
    assert decision.reason == "off_domain"


async def test_upsert_creates_the_parent_document_rows(dense, pg_pool):
    """A chunk whose document row is missing violates the foreign key. Making
    callers order two writes correctly is a trap, not a contract."""
    await dense.upsert(await embedded(CORPUS))
    rows = await pg_pool.pool.fetch("select id, title from document order by id")
    assert [r["id"] for r in rows] == ["atenolol", "metformin"]
    assert {r["title"] for r in rows} == {"Atenolol", "Metformin"}


async def test_the_tsvector_populates_itself_on_insert(dense, pg_pool):
    """ADR-002's structural win, asserted directly: one write, both legs. There
    is no window in which a chunk is searchable one way but not the other."""
    await dense.upsert(await embedded(CORPUS))
    row = await pg_pool.pool.fetchrow(
        "select tsv::text as tsv from chunk where id = $1", "metformin_0"
    )
    assert row is not None
    assert "metformin" in row["tsv"]
    assert "dose" in row["tsv"] or "dos" in row["tsv"]  # Porter-stemmed


async def test_editing_content_regenerates_the_tsvector(dense, pg_pool):
    """The generated column tracks content. If it did not, an edited chunk
    would be findable by its old text — a stale index with no way to notice."""
    from dataclasses import replace

    chunks = await embedded(CORPUS)
    await dense.upsert(chunks)
    edited = replace(chunks[0], chunk=replace(chunks[0].chunk, content="Rifampicin interaction."))
    await dense.upsert([edited])
    row = await pg_pool.pool.fetchrow(
        "select tsv::text as tsv from chunk where id = $1", "metformin_0"
    )
    assert row is not None
    assert "rifampicin" in row["tsv"]
    assert "metformin" not in row["tsv"]


async def test_index_verifies_rather_than_silently_doing_nothing(lexical, dense):
    """A bare no-op would let a caller write only through index(), see success,
    and ship an empty index. The message names the method they wanted."""
    with pytest.raises(ValueError, match="upsert"):
        await lexical.index(list(CORPUS))

    await dense.upsert(await embedded(CORPUS))
    await lexical.index(list(CORPUS))  # now every id exists, so it passes


async def test_index_of_nothing_is_harmless(lexical):
    await lexical.index([])


async def test_the_hnsw_index_is_used_for_a_nearest_neighbour_query(dense, pg_pool):
    """Not a performance assertion — at three rows the planner will sequential
    scan and should. This checks the index EXISTS with the operator class that
    matches the query, which is what makes `<=>` and `vector_cosine_ops` a pair.
    A mismatch there returns a different metric without erroring."""
    row = await pg_pool.pool.fetchrow(
        "select indexdef from pg_indexes where indexname = 'chunk_embedding_hnsw'"
    )
    assert row is not None
    assert "hnsw" in row["indexdef"]
    assert "vector_cosine_ops" in row["indexdef"]


async def test_deleting_a_document_takes_its_chunks(dense, pg_pool):
    await dense.upsert(await embedded(CORPUS))
    await pg_pool.pool.execute("delete from document where id = $1", "metformin")
    assert await dense.count() == 1
