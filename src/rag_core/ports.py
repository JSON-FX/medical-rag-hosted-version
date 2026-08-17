"""The four provider ports (ARCHITECTURE.md §4).

Everything provider-specific enters through one of these. Nothing below this
line knows whether it is talking to Gemini or a fake, to Postgres or a list.

Two implementations each is the bar ADR-001 set for a port earning its place:
a real adapter and a fake. The fake is not a testing convenience — it is what
makes the whole pipeline runnable in milliseconds with no network, which is the
stated payoff of the abstraction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .contracts import Chunk, EmbeddedChunk, Scored, Token, Vector


class EmbeddingProvider(Protocol):
    """Turns text into vectors.

    `dimension` is part of the index schema, not a runtime setting — it is
    written to the manifest at ingest and compared at startup
    (ARCHITECTURE.md §5).
    """

    model_id: str
    dimension: int

    async def embed_documents(self, texts: list[str]) -> list[Vector]: ...

    async def embed_query(self, text: str) -> Vector: ...


class GenerationProvider(Protocol):
    """Streams an answer.

    `stream` is declared `def`, not `async def`, and that is deliberate: an
    async generator (`async def` + `yield`) returns an AsyncIterator when
    CALLED, without being awaited. Declaring this `async def` would require
    implementations to be awaited before iteration, which no async generator
    satisfies.
    """

    model_id: str

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[Token]: ...


class DenseStore(Protocol):
    """Vector retrieval.

    SCORE DIRECTION: `search` returns cosine DISTANCE, ascending — closest
    first. similarity = 1 - score. The score is the provider's own, untouched
    (ADR-003): the gate reads raw magnitude, so an adapter that helpfully
    returns similarity instead silently voids every threshold. The contract
    suite asserts the direction; this is not a convention you can quietly
    reverse.
    """

    async def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]: ...

    async def upsert(self, chunks: list[EmbeddedChunk]) -> None: ...

    async def count(self) -> int:
        """Total chunks in the index.

        Not in ARCHITECTURE.md §4's port list. Added because the gate has a
        distinct `empty_corpus` reason with its own user-facing copy, and
        inferring emptiness from "both legs returned nothing" would conflate an
        unpopulated index (a deployment fault) with an off-domain question (a
        normal outcome). The pipeline calls this only when both legs come back
        empty, so it stays off the hot path.
        """
        ...


class LexicalStore(Protocol):
    """Full-text retrieval.

    SCORE DIRECTION: `search` returns a relevance rank, DESCENDING — best
    first. Opposite to DenseStore, because the underlying measures run in
    opposite directions. Fusion only ever reads position, so the two never need
    to be comparable (ARCHITECTURE.md §7) — but each must be ordered correctly
    before RRF sees it.
    """

    async def search(self, query: str, k: int) -> list[Scored[Chunk]]: ...

    async def index(self, chunks: list[Chunk]) -> None: ...
