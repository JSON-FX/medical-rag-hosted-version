"""In-memory implementations of all four ports. Deterministic, no network.

These are not test scaffolding that happens to live in the source tree. They
are the second implementation of each port — the thing that justifies the ports
existing at all under ADR-001's rule that a port with only one implementation
should be deleted — and they are what lets the whole pipeline run in
milliseconds with nothing installed and nothing reachable.

The embedder's axis scheme is carried over from the local build's test
fixtures: known terms map to fixed orthogonal axes so cosine distances are
predictable ("france" is maximally far from "metformin"), and everything
unrecognised lands on the unrelated axis. Width matches the real model (768) so
tests cannot pass against a dimensionality the production path would reject.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterable

from rag_core.contracts import Chunk, EmbeddedChunk, IndexManifest, Scored, Token, Vector
from rag_core.errors import ProviderUnavailable

from .tsquery import extract_terms

DIMENSIONS = 768


class FakeEmbedder:
    """EmbeddingProvider."""

    AXES = {"metformin": 0, "atenolol": 1, "amoxicillin": 2}
    UNRELATED_AXIS = 3

    def __init__(self, model_id: str = "fake-embed-001", dimension: int = DIMENSIONS) -> None:
        self.model_id = model_id
        self.dimension = dimension
        self.document_batches = 0  # lets tests assert batching behaviour

    def _vector(self, text: str) -> Vector:
        vector = [0.0] * self.dimension
        lowered = text.lower()
        for term, axis in self.AXES.items():
            if term in lowered:
                vector[axis] = 1.0
                return vector
        vector[self.UNRELATED_AXIS] = 1.0
        return vector

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        self.document_batches += 1
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._vector(text)


class ExplodingEmbedder(FakeEmbedder):
    """Simulates the embedding provider failing.

    ARCHITECTURE.md §8: there is no meaningful degraded retrieval without a
    query vector, so this must fail the request rather than fall back to
    lexical-only.
    """

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        raise ProviderUnavailable("fake embedder exploded")

    async def embed_query(self, text: str) -> Vector:
        raise ProviderUnavailable("fake embedder exploded")


def _cosine_distance(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - (dot / (norm_a * norm_b))


class FakeDenseStore:
    """DenseStore. Returns cosine DISTANCE, ascending — closest first.

    Returning similarity here instead would be the single most damaging thing
    this file could get wrong: it is the reference implementation the contract
    suite holds every real adapter against, so the bug would be enshrined
    rather than caught.
    """

    def __init__(self, chunks: Iterable[EmbeddedChunk] = ()) -> None:
        self._chunks: dict[str, EmbeddedChunk] = {c.chunk.id: c for c in chunks}
        self._manifest: IndexManifest | None = None

    async def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]:
        if k <= 0:
            return []
        scored = [
            Scored(item=c.chunk, score=_cosine_distance(vector, c.embedding))
            for c in self._chunks.values()
        ]
        scored.sort(key=lambda s: (s.score, s.item.id))
        return scored[:k]

    async def upsert(self, chunks: list[EmbeddedChunk]) -> None:
        for c in chunks:
            self._chunks[c.chunk.id] = c

    async def count(self) -> int:
        return len(self._chunks)

    async def read_manifest(self) -> IndexManifest | None:
        return self._manifest

    async def write_manifest(self, manifest: IndexManifest) -> None:
        self._manifest = manifest


class FakeLexicalStore:
    """LexicalStore. Naive term overlap, DESCENDING — best first.

    Query terms come from the same `extract_terms` the real adapter uses. An
    earlier version tokenised the query itself and applied no stopword list, so
    "what is it?" matched every chunk containing "is" here while returning
    nothing on Postgres. Both suites stayed green and the gate's
    `lexical_support` signal silently differed between profiles — exactly the
    divergence a shared contract suite exists to prevent.
    """

    def __init__(self, chunks: Iterable[Chunk] = ()) -> None:
        self._chunks: dict[str, Chunk] = {c.id: c for c in chunks}

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t}

    async def search(self, query: str, k: int) -> list[Scored[Chunk]]:
        if k <= 0:
            return []
        query_terms = set(extract_terms(query))
        if not query_terms:
            return []
        scored = [
            Scored(item=chunk, score=float(len(query_terms & self._terms(chunk.content))))
            for chunk in self._chunks.values()
        ]
        hits = [s for s in scored if s.score > 0.0]
        hits.sort(key=lambda s: (-s.score, s.item.id))
        return hits[:k]

    async def index(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.id] = chunk


class FakeGenerator:
    """GenerationProvider. Yields a scripted token list.

    `stream` is a plain `def` returning an async generator, matching the port.
    """

    def __init__(
        self,
        tokens: list[str] | None = None,
        model_id: str = "fake-generator-001",
        fail_with: Exception | None = None,
    ) -> None:
        self.model_id = model_id
        self._tokens = tokens if tokens is not None else ["A ", "grounded ", "answer [1]."]
        self._fail_with = fail_with
        self.calls = 0

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[Token]:
        self.calls += 1

        async def _generate() -> AsyncIterator[Token]:
            if self._fail_with is not None:
                raise self._fail_with
            for token in self._tokens:
                yield token

        return _generate()
