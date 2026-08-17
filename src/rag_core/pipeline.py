"""Hybrid retrieval plus the stage-1 gate.

The gate reads `top_similarity` from the DENSE leg directly, not from fused
output: RRF deliberately discards score magnitude, so fused scores carry no
similarity information (ADR-003).

Ported from the local build's Django-coupled orchestration. Three things
changed, all of them consequences of both legs now living in one store
(ADR-002) rather than choices made here — see the comments at each site.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .config import RagConfig
from .contracts import Chunk, to_context_chunk
from .fusion import reciprocal_rank_fusion
from .gate import GateDecision, GateSignals, evaluate_gate
from .ports import DenseStore, EmbeddingProvider, LexicalStore
from .prompts import ContextChunk


@dataclass(frozen=True)
class RetrievalResult:
    decision: GateDecision
    chunks: list[ContextChunk]


def _hydrate(top_ids: list[str], found: dict[str, Chunk]) -> list[ContextChunk]:
    """Map fused ids back to chunks, preserving rank order.

    CHANGED FROM THE LOCAL BUILD: this was a database query, because Chroma
    held only vectors and the text lived in SQLite. Both stores now return full
    chunks, so the union of the two legs already contains every id RRF could
    have ranked — hydration is a dict lookup and costs nothing.

    The local build skipped ids it could not resolve, because a vector could be
    orphaned from its row. There is no second store to orphan from now, so a
    miss would mean a fusion bug. Assert rather than skip: silently dropping
    chunks is how a system ends up refusing everything for a reason nobody can
    find.
    """
    missing = [chunk_id for chunk_id in top_ids if chunk_id not in found]
    if missing:
        raise AssertionError(
            f"fusion ranked ids that neither leg returned: {missing}. "
            "Every fused id comes from a retrieval leg by construction, so this "
            "is a bug in fusion or in a store adapter, not a data condition."
        )
    return [to_context_chunk(found[chunk_id]) for chunk_id in top_ids]


async def retrieve(
    question: str,
    embedder: EmbeddingProvider,
    dense: DenseStore,
    lexical: LexicalStore,
    cfg: RagConfig,
) -> RetrievalResult:
    vector = await embedder.embed_query(question)

    # CHANGED FROM THE LOCAL BUILD: the legs are issued concurrently. On the
    # hosted profile both are queries against one database, and the retrieval
    # budget is ~400ms p50 of a 2.5s time-to-first-token target.
    #
    # gather's default return_exceptions=False cancels the sibling when one leg
    # fails and propagates. That is ARCHITECTURE.md §8's position — an
    # embedding or store failure fails the request, because there is no
    # meaningful degraded retrieval — and changing it is an architecture
    # decision, not a local one.
    dense_hits, lexical_hits = await asyncio.gather(
        dense.search(vector, cfg.retrieval.per_leg),
        lexical.search(question, cfg.retrieval.per_leg),
    )

    if not dense_hits and not lexical_hits:
        # CHANGED FROM THE LOCAL BUILD: the count moved behind this condition.
        # The local build counted on every query. If either leg returned a row
        # the corpus cannot be empty, so the only case that needs the count is
        # this one — same gate outcome, zero round trips on the happy path.
        if await dense.count() == 0:
            signals = GateSignals(0.0, 0.0, lexical_support=False, corpus_empty=True)
            return RetrievalResult(evaluate_gate(signals, cfg.gate), [])

    similarities = [1.0 - hit.score for hit in dense_hits]
    top_similarity = max(similarities) if similarities else 0.0

    dense_ids = [hit.item.id for hit in dense_hits]
    lexical_ids = [hit.item.id for hit in lexical_hits]

    fused = reciprocal_rank_fusion([dense_ids, lexical_ids], k=cfg.retrieval.rrf_k)
    top_ids = [hit.chunk_id for hit in fused[: cfg.retrieval.top_k]]

    # Recorded for the eval sweep; the gate itself never reads this (pinned by
    # test_mean_similarity_does_not_affect_the_decision in
    # tests/unit/test_gate.py — do not wire it in here without updating that
    # pin deliberately). Averaged over the chunks actually DELIVERED to the
    # model (top_ids, after fusion) rather than the full per_leg candidate
    # pool: the pool measures ANN-neighbourhood breadth, which is the wrong
    # population for judging delivered-context quality — a chunk ranked 9th
    # of 10 by cosine similarity but never fused into the top_k should not
    # pull this average down, since the model never saw it. A delivered
    # chunk found only by the lexical leg has no vector-space similarity at
    # all and is excluded rather than treated as 0, which would understate a
    # lexically-rescued answer instead of just reflecting that fewer of the
    # top_k have a similarity opinion.
    similarity_by_id = dict(zip(dense_ids, similarities, strict=True))
    delivered_similarities = [similarity_by_id[i] for i in top_ids if i in similarity_by_id]
    mean_similarity = (
        sum(delivered_similarities) / len(delivered_similarities) if delivered_similarities else 0.0
    )

    signals = GateSignals(
        top_similarity=top_similarity,
        mean_similarity=mean_similarity,
        # "Does the question's terminology appear in the text we are about to
        # show the model?" — measured across all delivered chunks, not just the
        # top one. Using top_ids[0] alone made this hinge on RRF tie-breaking:
        # when the legs disagree completely, both rank-1 items score 1/(k+1) and
        # the winner is decided by lexicographic chunk_id sort, so the same
        # disagreement produced either answer depending on document numbering.
        lexical_support=bool(set(top_ids) & set(lexical_ids)),
        corpus_empty=False,
    )
    decision = evaluate_gate(signals, cfg.gate)

    if not decision.proceed:
        return RetrievalResult(decision, [])

    found: dict[str, Chunk] = {}
    for hit in (*dense_hits, *lexical_hits):
        found[hit.item.id] = hit.item
    return RetrievalResult(decision, _hydrate(top_ids, found))
