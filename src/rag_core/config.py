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
    # gemini-embedding-001 is natively 3072-dimensional and reduced to 768 via
    # output_dimensionality, which the SDK documents as truncating "excessive
    # values in the output embedding ... from the end". Truncating a matryoshka
    # embedding breaks its unit norm, so the adapter renormalises — see
    # rag_adapters/gemini.py. That answers the first half of PRD open question
    # 1; whether skipping it *measurably* hurts ranking is still unmeasured.
    #
    # Chosen over the natively-768 text-embedding-004 because it is the current
    # generation and less likely to be retired during the demo's life, which
    # PRD success criterion 6 cares about.
    model_id: str = "gemini-embedding-001"
    dimension: int = 768
    batch_size: int = 32
    request_timeout_s: int = 120


@dataclass(frozen=True)
class GenerationConfig:
    # Two providers behind one port, with automatic failover (ADR-004). Free
    # tiers change quota without notice, so a single-provider demo is a demo
    # with an expiry date nobody told you about.
    #
    # Groq leads because time-to-first-token is the NFR with a hard number
    # (<2.5s p50) and its inference is substantially faster. Gemini as the
    # secondary keeps the two failure domains genuinely independent — different
    # company, different quota, different outage — which is what ADR-004 is
    # actually buying.
    primary_provider: str = "groq"
    primary_model_id: str = "llama-3.3-70b-versatile"
    secondary_provider: str = "gemini"
    # An ALIAS, deliberately, where the primary is pinned.
    #
    # Pinning was the first instinct and the evidence contradicted it. In one
    # session the live suite got 404s from two successive pinned defaults:
    # gemini-2.0-flash ("no longer available") and then gemini-2.5-flash ("no
    # longer available to new users"). A pinned model that retires requires
    # touching code, and ADR-004's whole purpose is "surviving a provider
    # outage or a quota change WITHOUT touching code" — on an artifact that
    # has to still work thirty days after anyone last looked at it (PRD
    # success criterion 6, "the one most likely to fail").
    #
    # The cost is that the model underneath can change, and ADR-004 also
    # requires prompt behaviour to be validated against both models. That is
    # what the `live`-marked sentinel test covers: it checks the refusal
    # behaviour against whatever the alias currently resolves to, so drift
    # surfaces as a failing test rather than as a wrong answer. TICKET-6's
    # scheduled check is what runs it unattended.
    secondary_model_id: str = "gemini-flash-latest"
    request_timeout_s: int = 300


@dataclass(frozen=True)
class ProvidersConfig:
    # Empty by default so the fake profile needs nothing. Both SDKs will read
    # these from the environment themselves if not passed a key — which is
    # exactly why they are read here instead. This module's docstring says it
    # is the single boundary between os.environ and the pipeline, and an SDK
    # quietly reaching around it makes that a lie and the tests non-hermetic.
    gemini_api_key: str = ""
    groq_api_key: str = ""


@dataclass(frozen=True)
class DatabaseConfig:
    # Empty by default so the fake profile needs no database at all. The hosted
    # profile fails at open() rather than at import, which is what lets a
    # profile be built and inspected without a reachable server.
    dsn: str = ""
    # Small minimum: serverless invocations are short-lived, and a large
    # minimum opens connections the invocation never uses and the provider
    # still counts against the quota.
    pool_min_size: int = 1
    pool_max_size: int = 10


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
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
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
        database=DatabaseConfig(
            dsn=_s("DATABASE_URL", ""),
            pool_min_size=_i("DB_POOL_MIN", 1),
            pool_max_size=_i("DB_POOL_MAX", 10),
        ),
        embedding=EmbeddingConfig(
            model_id=_s("EMBED_MODEL", "gemini-embedding-001"),
            dimension=_i("EMBED_DIMENSIONS", 768),
            batch_size=_i("EMBED_BATCH_SIZE", 32),
            request_timeout_s=_i("EMBED_TIMEOUT_S", 120),
        ),
        providers=ProvidersConfig(
            gemini_api_key=_s("GEMINI_API_KEY", ""),
            groq_api_key=_s("GROQ_API_KEY", ""),
        ),
        generation=GenerationConfig(
            primary_provider=_s("PRIMARY_PROVIDER", "groq"),
            primary_model_id=_s("PRIMARY_MODEL", "llama-3.3-70b-versatile"),
            secondary_provider=_s("SECONDARY_PROVIDER", "gemini"),
            secondary_model_id=_s("SECONDARY_MODEL", "gemini-flash-latest"),
            request_timeout_s=_i("GENERATION_TIMEOUT_S", 300),
        ),
        chunk=ChunkConfig(size=_i("CHUNK_SIZE", 1000), overlap=_i("CHUNK_OVERLAP", 150)),
        retrieval=RetrievalConfig(
            per_leg=_i("RETRIEVE_N", 10), top_k=_i("TOP_K", 4), rrf_k=_i("RRF_K", 60)
        ),
        gate=GateConfig(tau_abstain=_f("TAU_ABSTAIN", 0.70), tau_strong=_f("TAU_STRONG", 0.75)),
        history_messages=_i("HISTORY_MESSAGES", 4),
    )
