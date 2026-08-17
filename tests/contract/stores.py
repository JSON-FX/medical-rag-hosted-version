"""Shared corpus and store-pair plumbing for the contract suite.

TICKET-1 built the registry to hold classes, and the tests called `builder()`
or `builder(CORPUS)`. That only works for a store with no dependencies and no
lifecycle, and it seeded through a constructor — a shortcut that skipped the
write path entirely. A real store needs a connection, setup and teardown, so
the seam is rebuilt here around fixtures.

Seeding now goes through each profile's own write methods, which means every
contract run exercises `upsert` as well as `search`. That is strictly more
coverage than the shape it replaces.
"""

from dataclasses import dataclass

import pytest

from rag_adapters.fakes import FakeEmbedder
from rag_core.contracts import Chunk, EmbeddedChunk
from rag_core.ports import DenseStore, LexicalStore

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


@dataclass
class StorePair:
    """Both retrieval legs of one profile, plus how to populate them.

    Seeding is per-profile because the profiles genuinely differ: the fakes
    hold two independent collections, whereas on Postgres both legs read the
    same row and one write populates both. That difference IS ADR-002, so the
    fixture models it rather than hiding it.
    """

    dense: DenseStore
    lexical: LexicalStore
    _seed_lexical: bool

    async def seed(self, chunks: list[EmbeddedChunk]) -> None:
        await self.dense.upsert(chunks)
        if self._seed_lexical:
            await self.lexical.index([c.chunk for c in chunks])


async def embedded(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    embedder = FakeEmbedder()
    vectors = await embedder.embed_documents([c.content for c in chunks])
    return [EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(chunks, vectors, strict=True)]


STORE_PAIRS = [
    "fake_stores",
    pytest.param("pg_stores", marks=pytest.mark.postgres),
]
