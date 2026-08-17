import os
from dataclasses import replace

import pytest


@pytest.fixture
def pg_dsn() -> str:
    """Skips rather than errors when there is no database, so
    `uv run pytest -m postgres` without one reports a readable reason instead
    of a connection traceback."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL is not set; start a pgvector container to run these")
    return dsn


@pytest.fixture
async def state():
    """A serviceable app state over fakes, with a small corpus loaded.

    Lives here rather than in a test module so both the chat and rate-limit
    suites can use it without importing across test files.
    """
    from rag_adapters.fakes import FakeDenseStore, FakeEmbedder, FakeGenerator, FakeLexicalStore
    from rag_adapters.profile import Profile
    from rag_api.state import AppState
    from rag_core.config import load_config
    from rag_core.contracts import Chunk, EmbeddedChunk

    corpus = [
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
    ]
    embedder = FakeEmbedder()
    dense = FakeDenseStore()
    lexical = FakeLexicalStore()
    vectors = await embedder.embed_documents([c.content for c in corpus])
    await dense.upsert(
        [EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(corpus, vectors, strict=True)]
    )
    await lexical.index(list(corpus))

    base = load_config(env={})
    permissive = replace(base, gate=replace(base.gate, tau_abstain=-1.0, tau_strong=-1.0))
    profile = Profile(
        name="fake",
        embedder=embedder,
        generator=FakeGenerator(),
        dense=dense,
        lexical=lexical,
    )
    return AppState(cfg=permissive, profile=profile, serviceable=True)
