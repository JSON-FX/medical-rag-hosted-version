"""Reciprocal rank fusion.

RRF compares only positions in ranked lists, never raw scores. This matters
because the two legs produce incompatible scales — cosine distance in [0, 2]
and ts_rank_cd in positive arbitrary units — with no principled normalisation
between them (ARCHITECTURE.md §7).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

DEFAULT_K = 60


@dataclass(frozen=True)
class FusedHit:
    chunk_id: str
    score: float


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = DEFAULT_K) -> list[FusedHit]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [FusedHit(chunk_id=chunk_id, score=score) for chunk_id, score in ordered]
