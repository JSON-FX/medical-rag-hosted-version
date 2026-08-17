import asyncio
from dataclasses import replace

import pytest

from rag_adapters.fakes import FakeDenseStore, FakeEmbedder, FakeLexicalStore
from rag_core.contracts import Chunk, EmbeddedChunk, Scored
from rag_core.pipeline import _hydrate, retrieve

PERMISSIVE = {"tau_abstain": -1.0, "tau_strong": -1.0}


def permissive(cfg):
    """A gate that always proceeds, so a test can inspect what was DELIVERED
    rather than what the (not-yet-retuned) thresholds happened to allow."""
    return replace(cfg, gate=replace(cfg.gate, **PERMISSIVE))


# --- the empty-corpus path ----------------------------------------------


async def test_empty_corpus_declines_without_calling_the_gate_thresholds(cfg, embedder):
    result = await retrieve("anything", embedder, FakeDenseStore(), FakeLexicalStore(), cfg)
    assert result.decision.proceed is False
    assert result.decision.reason == "empty_corpus"
    assert result.chunks == []


async def test_a_populated_corpus_with_no_matches_is_off_domain_not_empty(
    cfg, embedder, dense, lexical
):
    """An unpopulated index is a deployment fault; an unanswerable question is a
    normal outcome. Conflating them gives the reader the wrong message."""
    result = await retrieve("what is the capital of France?", embedder, dense, lexical, cfg)
    assert result.decision.proceed is False
    assert result.decision.reason == "off_domain"


async def test_count_is_not_queried_when_either_leg_returns_a_hit(cfg, embedder, dense, lexical):
    """The local build counted on every query. The count only matters when both
    legs come back empty, so it must stay off the hot path."""
    calls = []
    original = dense.count

    async def counting():
        calls.append(1)
        return await original()

    dense.count = counting
    await retrieve("metformin dose", embedder, dense, lexical, cfg)
    assert calls == []


async def test_count_is_queried_exactly_once_when_both_legs_are_empty(cfg, embedder):
    calls = []
    store = FakeDenseStore()
    original = store.count

    async def counting():
        calls.append(1)
        return await original()

    store.count = counting
    await retrieve("anything", embedder, store, FakeLexicalStore(), cfg)
    assert len(calls) == 1


# --- signals -------------------------------------------------------------


async def test_top_similarity_is_derived_from_distance_not_read_raw(cfg, embedder, lexical):
    """similarity = 1 - cosine distance. A store returning similarity directly
    would silently invert every threshold, so the conversion lives here and the
    port documents the direction."""
    chunk = Chunk(
        id="metformin_0",
        document_id="metformin",
        ordinal=0,
        anchor="1",
        content="Metformin dosing.",
        document_title="Metformin",
    )

    class FixedDistanceStore:
        async def search(self, vector, k):
            return [Scored(item=chunk, score=0.15)]

        async def upsert(self, chunks):  # pragma: no cover - unused
            ...

        async def count(self):  # pragma: no cover - unused
            return 1

    result = await retrieve("metformin", embedder, FixedDistanceStore(), lexical, cfg)
    assert result.decision.signals["top_similarity"] == pytest.approx(0.85)


async def test_lexical_support_is_true_when_a_delivered_chunk_came_from_the_lexical_leg(
    cfg, embedder, dense, lexical
):
    result = await retrieve("metformin starting dose", embedder, dense, lexical, permissive(cfg))
    assert result.decision.signals["lexical_support"] is True


async def test_lexical_support_is_false_when_the_lexical_leg_finds_nothing(cfg, embedder, dense):
    result = await retrieve("metformin", embedder, dense, FakeLexicalStore(), permissive(cfg))
    assert result.decision.signals["lexical_support"] is False


async def test_mean_similarity_ignores_chunks_the_model_never_saw(cfg, embedder, dense, lexical):
    """Averaged over delivered chunks, not the candidate pool — a chunk that
    never reached the top_k should not drag the number down."""
    narrow = replace(cfg, retrieval=replace(cfg.retrieval, top_k=1))
    result = await retrieve("metformin dose", embedder, dense, lexical, permissive(narrow))
    signals = result.decision.signals
    assert signals["mean_similarity"] == pytest.approx(signals["top_similarity"])


# --- retrieval and hydration --------------------------------------------


async def test_a_dense_only_hit_is_still_delivered(cfg, embedder, dense):
    """With an empty lexical leg, fusion has one ranking to work from, so the
    dense ordering survives intact into what is delivered."""
    result = await retrieve("metformin", embedder, dense, FakeLexicalStore(), permissive(cfg))
    delivered = [c.chunk_id for c in result.chunks]
    assert delivered, "a dense hit with no lexical support must still be delivered"
    assert delivered[0].startswith("metformin")


async def test_a_lexical_only_hit_is_still_delivered(cfg, embedder, lexical, corpus):
    """The signature of a lexically-rescued answer: exact terminology matching
    where the vector neighbourhood is unhelpful. Hydration must find it even
    though the dense leg never returned it."""
    result = await retrieve(
        "hypertension 50 mg", embedder, FakeDenseStore(), lexical, permissive(cfg)
    )
    assert "atenolol_0" in [c.chunk_id for c in result.chunks]


async def test_hydration_carries_title_and_page_for_citation(cfg, embedder, dense, lexical):
    result = await retrieve("metformin dose", embedder, dense, lexical, permissive(cfg))
    first = result.chunks[0]
    assert first.title in {"Metformin", "Atenolol"}
    assert isinstance(first.page_number, int)
    assert first.text


async def test_a_declined_gate_delivers_no_chunks(cfg, embedder, dense, lexical):
    result = await retrieve("what is the capital of France?", embedder, dense, lexical, cfg)
    assert result.decision.proceed is False
    assert result.chunks == []


async def test_top_k_bounds_what_is_delivered(cfg, embedder, dense, lexical):
    narrow = replace(cfg, retrieval=replace(cfg.retrieval, top_k=2))
    result = await retrieve("metformin dose", embedder, dense, lexical, permissive(narrow))
    assert len(result.chunks) <= 2


def test_hydrating_an_id_no_leg_returned_raises_rather_than_dropping_it():
    """The local build skipped unresolvable ids, because a Chroma vector could
    be orphaned from its SQLite row. There is no second store to orphan from
    now, so a miss means a fusion or adapter bug — and a silent drop is the
    hardest version of that bug to trace: the system refuses everything and
    nothing in the logs says why.

    `retrieve()` cannot reach this state by construction, since every fused id
    comes from a leg. That is exactly why the guard is asserted here directly:
    it is protecting an invariant, not handling a data condition."""
    present = Chunk(
        id="metformin_0",
        document_id="metformin",
        ordinal=0,
        anchor="1",
        content="Metformin dosing.",
        document_title="Metformin",
    )
    with pytest.raises(AssertionError, match="neither leg returned"):
        _hydrate(["metformin_0", "phantom_9"], {present.id: present})


def test_hydration_preserves_fusion_rank_order():
    chunks = {
        f"metformin_{i}": Chunk(
            id=f"metformin_{i}",
            document_id="metformin",
            ordinal=i,
            anchor=str(i + 1),
            content=f"body {i}",
            document_title="Metformin",
        )
        for i in range(3)
    }
    ordered = ["metformin_2", "metformin_0", "metformin_1"]
    assert [c.chunk_id for c in _hydrate(ordered, chunks)] == ordered


# --- concurrency ---------------------------------------------------------


async def test_both_legs_are_issued_concurrently(cfg, embedder, corpus):
    """Sequential legs would take the sum of the two delays; concurrent legs
    take the max. The retrieval budget is ~400ms p50 of a 2.5s TTFT target."""
    order = []

    class SlowDense(FakeDenseStore):
        async def search(self, vector, k):
            order.append("dense-start")
            await asyncio.sleep(0.05)
            order.append("dense-end")
            return await super().search(vector, k)

    class SlowLexical(FakeLexicalStore):
        async def search(self, query, k):
            order.append("lexical-start")
            await asyncio.sleep(0.05)
            order.append("lexical-end")
            return await super().search(query, k)

    dense_store = SlowDense()
    await dense_store.upsert(
        [EmbeddedChunk(chunk=c, embedding=FakeEmbedder()._vector(c.content)) for c in corpus]
    )
    await retrieve("metformin", embedder, dense_store, SlowLexical(corpus), permissive(cfg))

    # Both start before either finishes. Sequential execution would produce
    # dense-start, dense-end, lexical-start, lexical-end.
    assert order[:2] == ["dense-start", "lexical-start"]


async def test_an_embedding_failure_fails_the_request(cfg, dense, lexical):
    """ARCHITECTURE.md §8: there is no meaningful degraded retrieval without a
    query vector, so this must not silently fall back to lexical-only."""
    from rag_adapters.fakes import ExplodingEmbedder
    from rag_core.errors import ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        await retrieve("metformin", ExplodingEmbedder(), dense, lexical, cfg)
