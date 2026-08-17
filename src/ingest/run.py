"""Ingest the fixed corpus. Offline, resumable, idempotent.

    DATABASE_URL=postgresql://... RAG_PROFILE=hosted uv run python -m ingest.run

Runs on a developer machine or in CI, never in a request handler
(ARCHITECTURE.md §3). On a free embedding quota a full pass may need to span
more than one day (ARCHITECTURE.md §6), so it must be safe to stop and restart.

RESUMABILITY NEEDS NO CHECKPOINT
--------------------------------
`upsert` is idempotent on chunk id, so the rows already in the database ARE the
progress marker. Resume is a `select`, and an interrupted run costs only the
embeddings it had not yet made.

The one subtlety is that "already there" has to mean "already there WITH THIS
CONTENT". A fixture edit keeps every chunk id and changes the text, so an
id-only check would leave stale vectors pointing at wording that no longer
exists — retrieving the old text forever, with nothing anywhere to notice.

WHY THIS TALKS TO POSTGRES DIRECTLY
-----------------------------------
Reading "what is already stored" is not on the `DenseStore` port and nothing on
the query path wants it. Adding a fifth method for an offline script's benefit
is the over-abstraction ADR-001 warns about, so this job knows it targets
Postgres — which ARCHITECTURE.md §3 already permits: the ingestion job "writes
to the same stores the query path reads".
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rag_adapters.postgres import PostgresDenseStore, PostgresPool
from rag_adapters.profile import build_profile
from rag_core.chunking import chunk_pages
from rag_core.config import RagConfig, load_config
from rag_core.contracts import Chunk, EmbeddedChunk, IndexManifest, make_chunk_id
from rag_core.ports import EmbeddingProvider

from .corpus import document_title, load_manifest, paginate_document


@dataclass(frozen=True)
class IngestResult:
    slug: str
    total: int
    embedded: int
    skipped: int
    deleted: int

    def __str__(self) -> str:
        return (
            f"{self.slug:14} {self.total:4} chunks  "
            f"embedded={self.embedded:4}  skipped={self.skipped:4}  deleted={self.deleted:3}"
        )


def build_chunks(slug: str, cfg: RagConfig) -> list[Chunk]:
    """The chunks this document should contain, in ordinal order.

    `ChunkDraft.chunk_index` is global across the document's pages, so it maps
    straight onto `ordinal`. `page_number` becomes the citation anchor.
    """
    title = document_title(slug)
    drafts = chunk_pages(paginate_document(slug), cfg.chunk)
    return [
        Chunk(
            id=make_chunk_id(slug, draft.chunk_index),
            document_id=slug,
            ordinal=draft.chunk_index,
            anchor=str(draft.page_number),
            content=draft.text,
            document_title=title,
        )
        for draft in drafts
    ]


async def _stored_content(pool: PostgresPool, slug: str) -> dict[str, str]:
    rows = await pool.pool.fetch("select id, content from chunk where document_id = $1", slug)
    return {row["id"]: row["content"] for row in rows}


async def _drop_orphans(pool: PostgresPool, slug: str, keep: int) -> int:
    """Delete chunks past the document's new length.

    A shortened document leaves high-ordinal rows that `upsert` will never
    touch, so they would keep being retrieved forever. Deleting by ordinal
    rather than clearing the document first is what preserves resumability —
    the alternative re-spends the entire embedding quota on every run.
    """
    deleted = await pool.pool.fetch(
        "delete from chunk where document_id = $1 and ordinal >= $2 returning id", slug, keep
    )
    return len(deleted)


async def ingest_document(
    pool: PostgresPool,
    embedder: EmbeddingProvider,
    slug: str,
    cfg: RagConfig,
    *,
    reembed_all: bool = False,
) -> IngestResult:
    chunks = build_chunks(slug, cfg)
    if not chunks:
        raise ValueError(f"{slug} produced no chunks; the fixture is empty or unreadable")

    stored = await _stored_content(pool, slug)
    pending = chunks if reembed_all else [c for c in chunks if stored.get(c.id) != c.content]

    if pending:
        # The embedder batches at cfg.embedding.batch_size and backs off on
        # rate limits already — hand it the whole list rather than
        # re-implementing either.
        vectors = await embedder.embed_documents([c.content for c in pending])
        store = PostgresDenseStore(pool)
        await store.upsert(
            [EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(pending, vectors, strict=True)]
        )

    deleted = await _drop_orphans(pool, slug, keep=len(chunks))
    return IngestResult(
        slug=slug,
        total=len(chunks),
        embedded=len(pending),
        skipped=len(chunks) - len(pending),
        deleted=deleted,
    )


async def ingest_all(
    pool: PostgresPool, embedder: EmbeddingProvider, cfg: RagConfig
) -> list[IngestResult]:
    manifest = load_manifest()

    # Resume skips a chunk whose CONTENT is unchanged — but a vector is made by
    # a model, and the content check cannot see the model. Switching profiles
    # between runs (the `local` profile embeds with FakeEmbedder, `hosted` with
    # Gemini) therefore skipped all 71 chunks and then rewrote the manifest to
    # name the new embedder, leaving fake vectors labelled as Gemini ones.
    #
    # That defeats the startup check exactly where it matters most: the check
    # compares the configured embedder against the manifest, both of which now
    # agree, so the service starts happily and returns the plausible-looking
    # garbage ARCHITECTURE.md §5 exists to prevent. Observed while building
    # TICKET-7, which is the first thing to make switching embedders routine.
    existing = await PostgresDenseStore(pool).read_manifest()
    built_by = existing.embedding_model_id if existing else embedder.model_id
    reembed_all = built_by != embedder.model_id
    if reembed_all:
        print(
            f"index was built by {built_by!r} and this run uses {embedder.model_id!r}: "
            f"re-embedding every chunk, because a stored vector cannot be reused "
            f"across models"
        )

    results = []
    for slug in sorted(manifest["drugs"]):
        result = await ingest_document(pool, embedder, slug, cfg, reembed_all=reembed_all)
        print(f"  {result}")
        results.append(result)

    await _record_source_ids(pool, manifest)

    # The manifest is written LAST, once, only after every document succeeded.
    # An interrupted run therefore leaves none, and the startup check refuses to
    # serve a partially-populated index rather than answering from half a
    # corpus. That is the designed behaviour, not a happy accident.
    #
    # Written from the embedder's own model_id and dimension, not from config:
    # the manifest must record what ACTUALLY ran. Reading config would record
    # what was asked for, which is the same value right up until the moment it
    # matters.
    await PostgresDenseStore(pool).write_manifest(
        IndexManifest(
            embedding_model_id=embedder.model_id,
            dimension=embedder.dimension,
            ingested_at=datetime.now(UTC),
        )
    )
    return results


async def _record_source_ids(pool: PostgresPool, manifest: dict[str, Any]) -> None:
    """Pin each document to the label revision it came from.

    `upsert` writes id and title; the set_id lives in the corpus manifest and
    only this job knows it.
    """
    for slug, meta in sorted(manifest["drugs"].items()):
        await pool.pool.execute(
            "update document set source_set_id = $2 where id = $1", slug, meta["set_id"]
        )


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    cfg = load_config()
    profile = build_profile(cfg)
    pool = PostgresPool(
        dsn=dsn, min_size=cfg.database.pool_min_size, max_size=cfg.database.pool_max_size
    )

    print(f"ingesting with profile={profile.name} embedder={profile.embedder.model_id}")
    await pool.open()
    try:
        results = await ingest_all(pool, profile.embedder, cfg)
    finally:
        await pool.close()

    total = sum(r.total for r in results)
    embedded = sum(r.embedded for r in results)
    print(f"\n{total} chunks across {len(results)} documents ({embedded} embedded this run)")
    print("manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
