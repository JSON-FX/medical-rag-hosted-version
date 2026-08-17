"""Profile selection — the composition root.

ARCHITECTURE.md §4: "Profile selection is one environment variable read at
startup, resolved once into a container of adapters. No conditional provider
logic anywhere below that."

This module lives in `rag_adapters` rather than `rag_core` because resolving a
profile means importing concrete adapters, and the core is forbidden from doing
that (ARCHITECTURE.md §3, enforced by tests/unit/test_core_purity.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rag_core.config import RagConfig
from rag_core.ports import DenseStore, EmbeddingProvider, GenerationProvider, LexicalStore

from .fakes import FakeDenseStore, FakeEmbedder, FakeGenerator, FakeLexicalStore


@dataclass(frozen=True)
class Profile:
    """The four adapters, resolved once."""

    name: str
    embedder: EmbeddingProvider
    generator: GenerationProvider
    dense: DenseStore
    lexical: LexicalStore


def _build_fake(cfg: RagConfig) -> Profile:
    return Profile(
        name="fake",
        embedder=FakeEmbedder(dimension=cfg.embedding.dimension),
        generator=FakeGenerator(model_id=cfg.generation.primary_model_id),
        dense=FakeDenseStore(),
        lexical=FakeLexicalStore(),
    )


# The registration seam. TICKET-2 and TICKET-3 each add one entry here rather
# than growing an if/elif chain — which is what keeps "no conditional provider
# logic below this point" true as the number of profiles grows.
_REGISTRY: dict[str, Callable[[RagConfig], Profile]] = {
    "fake": _build_fake,
}


def register_profile(name: str, builder: Callable[[RagConfig], Profile]) -> None:
    _REGISTRY[name] = builder


def build_profile(cfg: RagConfig) -> Profile:
    """Resolve the configured profile into a container of adapters.

    An unknown name raises rather than falling back to a default. A silent
    fallback would let a production deployment quietly serve from fakes, which
    is a demo that answers confidently from nothing.
    """
    try:
        builder = _REGISTRY[cfg.profile]
    except KeyError:
        valid = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown profile {cfg.profile!r}; expected one of: {valid}") from None
    return builder(cfg)
