"""Postgres adapter — dense and lexical retrieval against one database.

ADR-002. pgvector with an HNSW index for dense, a generated `tsvector` column
with a GIN index for lexical, and both living in the same row of the same
table. That colocation is the point: the local build's split between ChromaDB
and SQLite FTS5 made partial-ingestion states possible where a chunk was
searchable one way but not the other, and a single row cannot disagree with
itself.

The cost ADR-002 accepts is that `ts_rank_cd` is a coverage-density ranking
rather than BM25's term-frequency saturation model, so lexical ordering will
differ from the local profile. That difference is measured on the evaluation
set in TICKET-9, not assumed away here.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

from rag_core.contracts import Chunk, EmbeddedChunk, IndexManifest, Scored, Vector

from .tsquery import build_tsquery

# Every column the stores read, joined to the parent document for the title
# that citations resolve to. Written once because both legs select the same
# shape and a drift between them would mean one leg citing differently.
_CHUNK_COLUMNS = """
    c.id, c.document_id, c.ordinal, c.anchor, c.content, d.title as document_title
"""


def _to_chunk(row: asyncpg.Record) -> Chunk:
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        ordinal=row["ordinal"],
        anchor=row["anchor"],
        content=row["content"],
        document_title=row["document_title"],
    )


async def _init_connection(conn: asyncpg.Connection[Any]) -> None:
    """Teach this connection about the `vector` type.

    Runs via the pool's `init=` hook so it applies to EVERY connection,
    including ones the pool opens later to grow. Registering once on a single
    acquired connection passes a one-connection test and then fails
    intermittently under concurrency, which is the worst way to find out.
    """
    await register_vector(conn)


class PostgresPool:
    """Owns the connection pool, so the stores can share one.

    Construction does not connect (see `Profile` in profile.py): the pool must
    be created inside a running event loop, and the composition root is
    synchronous.
    """

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool[Any] | None = None

    @property
    def pool(self) -> asyncpg.Pool[Any]:
        if self._pool is None:
            raise RuntimeError(
                "the connection pool is not open; call Profile.open() before serving. "
                "Building a profile deliberately does not connect."
            )
        return self._pool

    async def open(self) -> None:
        if self._pool is not None:
            return
        # No statement_cache_size=0 here. Neon's PgBouncer has supported
        # protocol-level prepared statements since 1.22.0, so the workaround
        # that recommendation comes from is obsolete and costs real
        # performance. SQL-level PREPARE/EXECUTE are still unsupported, but
        # asyncpg does not use them.
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            init=_init_connection,
        )

    async def close(self) -> None:
        """Idempotent, and safe when `open()` never ran.

        A shell that fails during startup still runs its shutdown path, and a
        close that raises there buries the original error.
        """
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None


class PostgresDenseStore:
    """DenseStore. Returns cosine DISTANCE, ascending — closest first."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]:
        if k <= 0:
            return []
        # ORDER BY must use the same expression the index was built on, and
        # `<=>` must match `vector_cosine_ops`. Ordering by the `distance`
        # alias, or using `<->` (L2), makes the planner ignore the HNSW index
        # AND returns a different metric — so every gate threshold would keep
        # its number and quietly stop meaning what it says.
        rows = await self._pool.pool.fetch(
            f"""
            select {_CHUNK_COLUMNS}, c.embedding <=> $1 as distance
            from chunk c
            join document d on d.id = c.document_id
            order by c.embedding <=> $1
            limit $2
            """,
            vector,
            k,
        )
        # The raw distance, untransformed (ADR-003). The gate reads magnitude,
        # so converting to similarity here would void every threshold.
        return [Scored(item=_to_chunk(row), score=float(row["distance"])) for row in rows]

    async def upsert(self, chunks: list[EmbeddedChunk]) -> None:
        """Idempotent on chunk id. Writes the parent documents in the same
        transaction, because a chunk whose document row is missing violates the
        foreign key — and requiring callers to order two writes correctly is a
        trap rather than a contract.
        """
        if not chunks:
            return
        documents = {(c.chunk.document_id, c.chunk.document_title) for c in chunks}
        async with self._pool.pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                insert into document (id, title, ingested_at)
                values ($1, $2, now())
                on conflict (id) do update set title = excluded.title
                """,
                sorted(documents),
            )
            await conn.executemany(
                """
                insert into chunk (id, document_id, ordinal, anchor, content, embedding)
                values ($1, $2, $3, $4, $5, $6)
                on conflict (id) do update set
                    document_id = excluded.document_id,
                    ordinal     = excluded.ordinal,
                    anchor      = excluded.anchor,
                    content     = excluded.content,
                    embedding   = excluded.embedding
                """,
                [
                    (
                        c.chunk.id,
                        c.chunk.document_id,
                        c.chunk.ordinal,
                        c.chunk.anchor,
                        c.chunk.content,
                        c.embedding,
                    )
                    for c in chunks
                ],
            )

    async def count(self) -> int:
        result: int | None = await self._pool.pool.fetchval("select count(*) from chunk")
        return result or 0

    async def read_manifest(self) -> IndexManifest | None:
        row = await self._pool.pool.fetchrow(
            "select embedding_model_id, dimension, ingested_at from index_manifest where id = 1"
        )
        if row is None:
            return None
        return IndexManifest(
            embedding_model_id=row["embedding_model_id"],
            dimension=row["dimension"],
            ingested_at=row["ingested_at"],
        )

    async def write_manifest(self, manifest: IndexManifest) -> None:
        await self._pool.pool.execute(
            """
            insert into index_manifest (id, embedding_model_id, dimension, ingested_at)
            values (1, $1, $2, $3)
            on conflict (id) do update set
                embedding_model_id = excluded.embedding_model_id,
                dimension          = excluded.dimension,
                ingested_at        = excluded.ingested_at
            """,
            manifest.embedding_model_id,
            manifest.dimension,
            manifest.ingested_at,
        )


class PostgresLexicalStore:
    """LexicalStore. Returns `ts_rank_cd`, descending — best first."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def search(self, query: str, k: int) -> list[Scored[Chunk]]:
        if k <= 0:
            return []
        expression = build_tsquery(query)
        if not expression:
            # Must come BEFORE the query. `to_tsquery('english', '')` raises a
            # syntax error, so a question made entirely of stopwords — "what is
            # it?" — would 500 instead of simply finding nothing.
            return []
        # ts_rank_cd is coverage density, not BM25 (ADR-002). Normalization is
        # left at the default 0: chunks are near-uniform at ~1000 characters,
        # so there is nothing for length normalisation to correct, and adding
        # the knob now would mean TICKET-9 measures two changes at once.
        rows = await self._pool.pool.fetch(
            f"""
            select {_CHUNK_COLUMNS}, ts_rank_cd(c.tsv, q) as rank
            from chunk c
            join document d on d.id = c.document_id,
                 to_tsquery('english', $1) q
            where c.tsv @@ q
            order by rank desc, c.id
            limit $2
            """,
            expression,
            k,
        )
        return [Scored(item=_to_chunk(row), score=float(row["rank"])) for row in rows]

    async def index(self, chunks: list[Chunk]) -> None:
        """Verify, then do nothing — and that is ADR-002's whole return.

        `tsv` is `GENERATED ALWAYS AS (to_tsvector('english', content)) STORED`,
        so it is populated by the same INSERT that writes the chunk. Dense and
        lexical rows are literally the same row; there is no separate lexical
        index to maintain, and no window in which one exists without the other.

        The local build needed a compensating-delete path, a cleanup that
        swallowed its own exceptions so a failure could not strand a document
        in `processing`, and a `reconcile_vectors` command to repair the states
        that still got through. All of it existed because two stores can
        disagree. None of it is ported.

        This verifies rather than returning silently: a no-op would let a
        caller write only through this method, see success, and ship an empty
        index. Raising names the method they actually wanted.
        """
        if not chunks:
            return
        ids = [c.id for c in chunks]
        found = await self._pool.pool.fetchval(
            "select count(*) from chunk where id = any($1::text[])", ids
        )
        if found != len(set(ids)):
            raise ValueError(
                f"index() was called for {len(set(ids))} chunks but only {found} exist. "
                "On this profile the tsvector is a generated column, so lexical "
                "indexing happens as a side effect of DenseStore.upsert — call that "
                "instead; index() only verifies."
            )
