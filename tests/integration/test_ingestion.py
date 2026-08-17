"""The ingest job against a real database, with a fake embedder.

Idempotence, resumability and convergence are the whole point of this ticket
and none of them are observable without real storage.
"""

import sys
from pathlib import Path

import pytest

from ingest.corpus import load_manifest, paginate_document
from ingest.run import build_chunks, ingest_all, ingest_document
from rag_adapters.fakes import ExplodingEmbedder, FakeEmbedder
from rag_adapters.postgres import PostgresDenseStore, PostgresPool
from rag_core.config import load_config
from rag_core.contracts import split_chunk_id
from rag_core.errors import ProviderUnavailable

pytestmark = pytest.mark.postgres

CFG = load_config(env={})
SLUGS = sorted(load_manifest()["drugs"])


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
def embedder():
    return FakeEmbedder()


async def chunk_rows(pool, slug=None):
    if slug:
        return await pool.pool.fetch(
            "select id, ordinal, anchor, content from chunk"
            " where document_id = $1 order by ordinal",
            slug,
        )
    return await pool.pool.fetch("select id, ordinal, anchor, content from chunk order by id")


# --- a full pass ---------------------------------------------------------


async def test_a_single_document_ingests_every_chunk(pool, embedder):
    result = await ingest_document(pool, embedder, "metformin", CFG)
    expected = len(build_chunks("metformin", CFG))
    assert result.total == expected
    assert result.embedded == expected
    assert result.skipped == 0
    assert len(await chunk_rows(pool, "metformin")) == expected


async def test_a_full_pass_ingests_all_three_documents(pool, embedder):
    results = await ingest_all(pool, embedder, CFG)
    assert [r.slug for r in results] == SLUGS
    total = sum(len(build_chunks(s, CFG)) for s in SLUGS)
    assert await PostgresDenseStore(pool).count() == total
    assert all(r.embedded > 0 for r in results)


async def test_the_parent_documents_are_pinned_to_their_label_revision(pool, embedder):
    await ingest_all(pool, embedder, CFG)
    rows = await pool.pool.fetch("select id, title, source_set_id from document order by id")
    assert [r["id"] for r in rows] == SLUGS
    manifest = load_manifest()["drugs"]
    for row in rows:
        assert row["source_set_id"] == manifest[row["id"]]["set_id"]
        assert row["title"] == row["id"].title()


# --- idempotence (AC #2) -------------------------------------------------


async def test_rerunning_changes_nothing_and_embeds_nothing(pool, embedder):
    """PRD F4. The second pass must cost no quota at all."""
    first = await ingest_all(pool, embedder, CFG)
    before = {(r["id"], r["content"]) for r in await chunk_rows(pool)}

    second = await ingest_all(pool, embedder, CFG)
    after = {(r["id"], r["content"]) for r in await chunk_rows(pool)}

    assert before == after
    assert sum(r.embedded for r in second) == 0
    assert sum(r.skipped for r in second) == sum(r.total for r in first)


async def test_chunk_ids_are_stable_across_runs(pool, embedder):
    await ingest_all(pool, embedder, CFG)
    first = [r["id"] for r in await chunk_rows(pool)]
    await ingest_all(pool, embedder, CFG)
    assert [r["id"] for r in await chunk_rows(pool)] == first


# --- resumability (AC #3) ------------------------------------------------


async def test_resuming_after_an_interruption_completes_the_corpus(pool, embedder):
    """An interrupted run leaves the documents it finished. Resuming embeds only
    what is missing — which is what makes a multi-day pass on a free quota
    survivable (ARCHITECTURE.md §6)."""
    await ingest_document(pool, embedder, "metformin", CFG)
    partial = await PostgresDenseStore(pool).count()

    results = await ingest_all(pool, embedder, CFG)
    by_slug = {r.slug: r for r in results}

    assert by_slug["metformin"].embedded == 0, "the finished document must not be re-embedded"
    assert by_slug["metformin"].skipped == by_slug["metformin"].total
    assert by_slug["atenolol"].embedded > 0
    assert await PostgresDenseStore(pool).count() > partial
    assert sum(len(build_chunks(s, CFG)) for s in SLUGS) == await PostgresDenseStore(pool).count()


async def test_resuming_leaves_no_duplicates_or_gaps(pool, embedder):
    await ingest_document(pool, embedder, "metformin", CFG)
    await ingest_all(pool, embedder, CFG)
    for slug in SLUGS:
        rows = await chunk_rows(pool, slug)
        ordinals = [r["ordinal"] for r in rows]
        assert ordinals == list(range(len(ordinals))), f"{slug} has gaps or duplicates"
        assert len({r["id"] for r in rows}) == len(rows)


async def test_an_interrupted_run_leaves_no_manifest(pool):
    """Written last, once, only on full success — so the startup check refuses
    to serve a half-populated index rather than answering from part of a
    corpus."""
    with pytest.raises(ProviderUnavailable):
        await ingest_all(pool, ExplodingEmbedder(), CFG)
    assert await PostgresDenseStore(pool).read_manifest() is None


async def test_a_partial_failure_keeps_what_already_succeeded(pool, embedder):
    """The first document's work is not thrown away by the second's failure —
    that is what makes resuming cheap rather than a restart."""
    await ingest_document(pool, embedder, "metformin", CFG)
    stored = await PostgresDenseStore(pool).count()
    with pytest.raises(ProviderUnavailable):
        await ingest_all(pool, ExplodingEmbedder(), CFG)
    assert await PostgresDenseStore(pool).count() == stored


# --- convergence ---------------------------------------------------------


async def test_an_edited_chunk_is_re_embedded_and_nothing_else_is(pool, embedder):
    """A fixture edit keeps every chunk id and changes the text. Comparing ids
    alone would leave a stale vector retrieving wording that no longer exists,
    with nothing anywhere to notice."""
    await ingest_document(pool, embedder, "metformin", CFG)
    await pool.pool.execute(
        "update chunk set content = $2 where id = $1", "metformin_0", "stale text"
    )

    result = await ingest_document(pool, embedder, "metformin", CFG)
    assert result.embedded == 1
    assert result.skipped == result.total - 1

    row = await pool.pool.fetchrow("select content from chunk where id = $1", "metformin_0")
    assert row["content"] != "stale text"
    assert row["content"] == build_chunks("metformin", CFG)[0].content


async def test_orphaned_chunks_beyond_the_new_length_are_deleted(pool, embedder):
    """A shortened document leaves high-ordinal rows that upsert never touches,
    so they would keep being retrieved forever."""
    await ingest_document(pool, embedder, "metformin", CFG)
    real = len(build_chunks("metformin", CFG))

    # Fabricate two chunks past the end, as a shortened fixture would leave.
    for extra in (real, real + 1):
        await pool.pool.execute(
            """
            insert into chunk (id, document_id, ordinal, anchor, content, embedding)
            values ($1, 'metformin', $2, '1', 'orphaned', $3)
            """,
            f"metformin_{extra}",
            extra,
            [0.1] * 768,
        )
    assert await PostgresDenseStore(pool).count() == real + 2

    result = await ingest_document(pool, embedder, "metformin", CFG)
    assert result.deleted == 2
    assert await PostgresDenseStore(pool).count() == real


async def test_convergence_does_not_re_embed_the_whole_document(pool, embedder):
    """Deleting the document and re-inserting would be simpler and would
    re-spend the entire quota on every run."""
    await ingest_document(pool, embedder, "metformin", CFG)
    await pool.pool.execute(
        """
        insert into chunk (id, document_id, ordinal, anchor, content, embedding)
        values ('metformin_999', 'metformin', 999, '1', 'orphaned', $1)
        """,
        [0.1] * 768,
    )
    result = await ingest_document(pool, embedder, "metformin", CFG)
    assert result.deleted == 1
    assert result.embedded == 0


# --- citations (AC #5) ---------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
async def test_every_anchor_names_the_page_the_text_came_from(pool, embedder, slug):
    """What makes a citation true. An off-by-one here sends every "p. N" one
    page adrift, and nothing else in the system would notice."""
    await ingest_document(pool, embedder, slug, CFG)
    pages = {p.page_number: p.text for p in paginate_document(slug)}

    for row in await chunk_rows(pool, slug):
        page_number = int(row["anchor"])
        assert page_number in pages, f"{row['id']} cites a page that does not exist"
        # Overlap prepends the tail of the previous chunk, so match on the tail
        # of the chunk — the part that is genuinely this chunk's own new text.
        tail = row["content"][-120:]
        assert tail in pages[page_number], f"{row['id']} text is not on page {page_number}"


@pytest.mark.parametrize("slug", SLUGS)
async def test_chunk_ids_round_trip(pool, embedder, slug):
    await ingest_document(pool, embedder, slug, CFG)
    for row in await chunk_rows(pool, slug):
        document_id, ordinal = split_chunk_id(row["id"])
        assert document_id == slug
        assert ordinal == row["ordinal"]


# --- the manifest (AC #4) ------------------------------------------------


async def test_the_manifest_records_the_embedder_that_actually_ran(pool, embedder):
    """Read from the embedder, not from config: config records what was asked
    for, which is the same value right up until the moment it matters."""
    await ingest_all(pool, embedder, CFG)
    manifest = await PostgresDenseStore(pool).read_manifest()
    assert manifest is not None
    assert manifest.embedding_model_id == embedder.model_id
    assert manifest.dimension == embedder.dimension
    assert manifest.ingested_at.tzinfo is not None


async def test_a_second_run_leaves_exactly_one_manifest(pool, embedder):
    await ingest_all(pool, embedder, CFG)
    await ingest_all(pool, embedder, CFG)
    assert await pool.pool.fetchval("select count(*) from index_manifest") == 1


# --- the retrieval path over real ingested data --------------------------


async def test_the_ingested_corpus_is_retrievable(pool, embedder):
    """The point of the whole ticket: after this, the gate stops answering
    empty_corpus to every question."""
    from dataclasses import replace

    from rag_adapters.postgres import PostgresLexicalStore
    from rag_core.pipeline import retrieve

    await ingest_all(pool, embedder, CFG)
    permissive = replace(CFG, gate=replace(CFG.gate, tau_abstain=-1.0, tau_strong=-1.0))
    result = await retrieve(
        "What is the adult starting dose of metformin?",
        embedder,
        PostgresDenseStore(pool),
        PostgresLexicalStore(pool),
        permissive,
    )
    assert result.decision.reason != "empty_corpus"
    assert result.chunks
    assert all(c.page_number > 0 for c in result.chunks)
    assert any("metformin" in c.chunk_id for c in result.chunks)
