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
from dataclasses import dataclass, field
from typing import Protocol

from rag_core.config import RagConfig
from rag_core.ports import DenseStore, EmbeddingProvider, GenerationProvider, LexicalStore

from .fakes import FakeDenseStore, FakeEmbedder, FakeGenerator, FakeLexicalStore


class Lifecycle(Protocol):
    """Something holding a connection that outlives a single call."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Profile:
    """The four adapters, resolved once.

    Construction and connection are separate. `build_profile` stays synchronous
    — an adapter that needs a connection pool cannot be built in a sync
    function, because the pool must be created inside a running event loop, and
    making the whole composition root async would push an `await` into every
    caller for the sake of one adapter. So building a profile always succeeds,
    even against an unreachable database, and `open()` is what actually
    connects. The API shell calls it from its lifespan hook.

    The container stays frozen; adapters that hold a mutable connection keep it
    on themselves and `open`/`close` delegate.
    """

    name: str
    embedder: EmbeddingProvider
    generator: GenerationProvider
    dense: DenseStore
    lexical: LexicalStore
    # Anything needing a connection. Declared explicitly rather than discovered
    # by probing the adapters for an `open` attribute: the ports are Protocols
    # and this codebase does not use runtime_checkable, so a duck-typed probe
    # would silently skip an adapter whose method was renamed.
    resources: tuple[Lifecycle, ...] = field(default_factory=tuple)

    async def open(self) -> None:
        """Connect whatever needs connecting. No-op when nothing does."""
        for resource in self.resources:
            await resource.open()

    async def close(self) -> None:
        """Release connections.

        Idempotent, and safe to call when `open()` never ran — a shell that
        fails during startup still runs its shutdown path, and a close that
        raises there buries the original error.
        """
        for resource in self.resources:
            await resource.close()


def _build_fake(cfg: RagConfig) -> Profile:
    return Profile(
        name="fake",
        embedder=FakeEmbedder(dimension=cfg.embedding.dimension),
        generator=FakeGenerator(model_id=cfg.generation.primary_model_id),
        dense=FakeDenseStore(),
        lexical=FakeLexicalStore(),
    )


# Long enough to clear the sentinel filter's 44-character buffer — a shorter
# answer arrives as one frame and a frontend built against it cannot tell
# progressive rendering from a spinner — and carrying two citation markers,
# which is the only way to exercise the [n] -> sources[n-1] mapping without
# spending a real generation.
LOCAL_ANSWER = (
    "This answer comes from the local development profile: the retrieval and "
    "the citations are real, the words are not. The retrieved context is "
    "cited here [1] and again here [2]."
)
LOCAL_ANSWER_TOKENS = [word + " " for word in LOCAL_ANSWER.split()]


def _build_local(cfg: RagConfig) -> Profile:
    """Real storage, fake inference — the profile you develop the frontend on.

    Distinct from `fake`, which keeps everything in memory and is the
    zero-dependency test profile. This one runs the real Postgres retrieval
    legs over the real ingested corpus, so citations carry real titles and page
    anchors, and swaps only the two adapters that cost money per call.

    The consequence to know about: `FakeEmbedder` is a four-axis one-hot vector
    keyed on drug name, so the stage-1 gate here is coarse. A question naming a
    drug in the corpus scores 1.0 and passes; a question naming none scores 0.0
    and refuses. That makes both paths reachable without an API key, but it
    does NOT reproduce a near-miss refusal — "atenolol in pregnancy" names a
    drug, so it passes stage 1 here and would refuse under real embeddings.
    Judge refusal *behaviour* on `hosted`; use this to build the interface.
    """
    from .postgres import PostgresDenseStore, PostgresLexicalStore, PostgresPool

    pool = PostgresPool(
        dsn=cfg.database.dsn,
        min_size=cfg.database.pool_min_size,
        max_size=cfg.database.pool_max_size,
    )
    return Profile(
        name="local",
        embedder=FakeEmbedder(dimension=cfg.embedding.dimension),
        generator=FakeGenerator(
            tokens=LOCAL_ANSWER_TOKENS,
            # Enough to see tokens land one at a time in a browser without
            # making the page feel broken.
            delay_s=0.05,
        ),
        dense=PostgresDenseStore(pool),
        lexical=PostgresLexicalStore(pool),
        resources=(pool,),
    )


def _build_hosted(cfg: RagConfig) -> Profile:
    """Postgres for both retrieval legs (ADR-002).

    Deliberately does not connect: the pool is created in `Profile.open()`, so
    building a hosted profile against an unreachable database succeeds and
    fails later, where the error can be reported properly.
    """
    from .failover import FailoverGenerator
    from .gemini import GeminiEmbedder
    from .postgres import PostgresDenseStore, PostgresLexicalStore, PostgresPool

    pool = PostgresPool(
        dsn=cfg.database.dsn,
        min_size=cfg.database.pool_min_size,
        max_size=cfg.database.pool_max_size,
    )
    return Profile(
        name="hosted",
        embedder=GeminiEmbedder(cfg),
        generator=FailoverGenerator(
            primary=_build_generator(cfg, cfg.generation.primary_provider, is_primary=True),
            secondary=_build_generator(cfg, cfg.generation.secondary_provider, is_primary=False),
        ),
        dense=PostgresDenseStore(pool),
        lexical=PostgresLexicalStore(pool),
        resources=(pool,),
    )


def _build_generator(cfg: RagConfig, provider: str, *, is_primary: bool) -> GenerationProvider:
    """Resolve a provider name to a generator.

    A name→builder map rather than an if/elif inside FailoverGenerator, so
    ADR-004's "no conditional provider logic anywhere below the composition
    root" stays true: the chain knows it has two generators and nothing about
    who made them.
    """
    from .gemini import GeminiGenerator
    from .groq import GroqGenerator

    model_id = cfg.generation.primary_model_id if is_primary else cfg.generation.secondary_model_id
    builders: dict[str, Callable[[], GenerationProvider]] = {
        "groq": lambda: GroqGenerator(cfg, model_id=model_id),
        "gemini": lambda: GeminiGenerator(cfg, model_id=model_id),
    }
    try:
        return builders[provider]()
    except KeyError:
        valid = ", ".join(sorted(builders))
        raise ValueError(
            f"unknown generation provider {provider!r}; expected one of: {valid}"
        ) from None


# The registration seam. TICKET-2, TICKET-3 and TICKET-7 each add one entry
# here rather than growing an if/elif chain — which is what keeps "no
# conditional provider logic below this point" true as the number of profiles
# grows.
_REGISTRY: dict[str, Callable[[RagConfig], Profile]] = {
    "fake": _build_fake,
    "local": _build_local,
    "hosted": _build_hosted,
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
