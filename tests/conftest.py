"""Shared fixtures.

The corpus here is three tiny stand-ins for the real FDA labels, chosen so the
fake embedder's orthogonal axes make retrieval outcomes predictable: a
metformin question is maximally far from an atenolol chunk, and an off-domain
question is far from all of them.
"""

import pytest

from rag_adapters.fakes import (
    FakeDenseStore,
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalStore,
)
from rag_core.config import load_config
from rag_core.contracts import Chunk, EmbeddedChunk

CORPUS = [
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
    Chunk(
        id="atenolol_0",
        document_id="atenolol",
        ordinal=0,
        anchor="1",
        content="Atenolol initial dose for hypertension is 50 mg once daily.",
        document_title="Atenolol",
    ),
]


@pytest.fixture
def cfg():
    return load_config(env={})


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def corpus():
    return list(CORPUS)


@pytest.fixture
def dense(embedder, corpus):
    store = FakeDenseStore()
    vectors = [embedder._vector(c.content) for c in corpus]
    store._chunks = {
        c.id: EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(corpus, vectors, strict=True)
    }
    return store


@pytest.fixture
def lexical(corpus):
    return FakeLexicalStore(corpus)


@pytest.fixture
def generator():
    return FakeGenerator()
