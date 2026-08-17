"""Every tunable in one place. No framework imports, no provider SDKs.

Nothing outside this module reads the environment. `load_config` is the single
boundary between os.environ and the pipeline, which is what makes the whole
package testable by passing a dict.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmbeddingConfig:
    # Deliberately not a real model id. The spike confirms which Gemini model
    # emits 768-dimension vectors and whether truncation from the native
    # dimension needs renormalising before cosine comparison (PRD open
    # question 1). A confident-looking default here would be a guess wearing a
    # measurement's clothes.
    model_id: str = "REPLACE_ME_AFTER_SPIKE"
    dimension: int = 768
    batch_size: int = 32
    request_timeout_s: int = 120


@dataclass(frozen=True)
class GenerationConfig:
    # Two providers behind one port, with automatic failover (ADR-004). Free
    # tiers change quota without notice, so a single-provider demo is a demo
    # with an expiry date nobody told you about.
    primary_model_id: str = "REPLACE_ME"
    secondary_model_id: str = "REPLACE_ME"
    request_timeout_s: int = 300


@dataclass(frozen=True)
class ChunkConfig:
    size: int = 1000
    overlap: int = 150


@dataclass(frozen=True)
class RetrievalConfig:
    per_leg: int = 10  # candidates pulled from each of the dense and lexical legs
    top_k: int = 4  # chunks kept after fusion
    rrf_k: int = 60  # reciprocal rank fusion constant


@dataclass(frozen=True)
class GateConfig:
    # THESE VALUES ARE INVALID AND ARE CARRIED ACROSS ONLY SO THE SYSTEM RUNS.
    #
    # They were measured against `nomic-embed-text` on the local build, by a
    # sweep over 40 labelled questions. A different embedding model has a
    # different similarity distribution, so a threshold tuned on one says
    # nothing about the other — ADR-003: "τ from the local build is meaningless
    # here. The old value must be discarded, not carried over."
    #
    # TICKET-8 re-sweeps them against the hosted embedder and replaces both,
    # along with this comment. Until then, treat any gate decision as
    # provisional.
    tau_abstain: float = 0.70
    tau_strong: float = 0.75


@dataclass(frozen=True)
class RagConfig:
    profile: str = "fake"
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    history_messages: int = 4


def load_config(env: Mapping[str, str] | None = None) -> RagConfig:
    e = os.environ if env is None else env

    def _f(key: str, default: float) -> float:
        return float(e.get(key, default))

    def _i(key: str, default: int) -> int:
        return int(e.get(key, default))

    def _s(key: str, default: str) -> str:
        return e.get(key, default)

    return RagConfig(
        profile=_s("RAG_PROFILE", "fake"),
        embedding=EmbeddingConfig(
            model_id=_s("EMBED_MODEL", "REPLACE_ME_AFTER_SPIKE"),
            dimension=_i("EMBED_DIMENSIONS", 768),
            batch_size=_i("EMBED_BATCH_SIZE", 32),
            request_timeout_s=_i("EMBED_TIMEOUT_S", 120),
        ),
        generation=GenerationConfig(
            primary_model_id=_s("PRIMARY_MODEL", "REPLACE_ME"),
            secondary_model_id=_s("SECONDARY_MODEL", "REPLACE_ME"),
            request_timeout_s=_i("GENERATION_TIMEOUT_S", 300),
        ),
        chunk=ChunkConfig(size=_i("CHUNK_SIZE", 1000), overlap=_i("CHUNK_OVERLAP", 150)),
        retrieval=RetrievalConfig(
            per_leg=_i("RETRIEVE_N", 10), top_k=_i("TOP_K", 4), rrf_k=_i("RRF_K", 60)
        ),
        gate=GateConfig(tau_abstain=_f("TAU_ABSTAIN", 0.70), tau_strong=_f("TAU_STRONG", 0.75)),
        history_messages=_i("HISTORY_MESSAGES", 4),
    )
