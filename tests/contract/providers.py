"""Minimal SDK doubles, so real adapters can run the shared contract suite.

Deliberately separate from the doubles in `tests/unit/test_gemini.py` and
`tests/unit/test_groq.py`, which record every call so the unit tests can assert
*how* the SDK was invoked — task types, batch boundaries, streaming flags.
These need to do none of that. They exist only to make the adapter satisfiable,
so the contract suite can hold it to the same assertions as the fakes.
"""

from types import SimpleNamespace

import pytest

from rag_adapters.fakes import DIMENSIONS, FakeEmbedder, FakeGenerator
from rag_adapters.gemini import GeminiEmbedder, GeminiGenerator
from rag_adapters.groq import GroqGenerator
from rag_core.config import load_config

CFG = load_config(env={})
_EMBEDDER = FakeEmbedder()


class StubGeminiEmbedClient:
    """Returns deterministic, NON-unit vectors of the requested width.

    Non-unit on purpose: the adapter renormalises, and a double that already
    returned unit vectors would let a missing renormalisation pass unnoticed.
    """

    def __init__(self) -> None:
        self.aio = SimpleNamespace(models=SimpleNamespace(embed_content=self._embed))

    async def _embed(self, *, model, contents, config):
        width = config.output_dimensionality or DIMENSIONS
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[x * 7.0 for x in _EMBEDDER._vector(text)[:width]])
                for text in contents
            ]
        )


class StubGeminiStreamClient:
    def __init__(self) -> None:
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._stream))

    async def _stream(self, *, model, contents, config):
        async def gen():
            for text in ("A ", "grounded ", "answer [1]."):
                yield SimpleNamespace(text=text)

        return gen()


class StubGroqClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, *, messages, model, stream):
        async def gen():
            for text in ("A ", "grounded ", "answer [1]."):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                )

        return gen()


def build_gemini_embedder() -> GeminiEmbedder:
    return GeminiEmbedder(CFG, client=StubGeminiEmbedClient())


def build_gemini_generator() -> GeminiGenerator:
    return GeminiGenerator(CFG, client=StubGeminiStreamClient())


def build_groq_generator() -> GroqGenerator:
    return GroqGenerator(CFG, client=StubGroqClient())


# Adding an embedder or a generator here IS one line, as TICKET-1 promised.
# The store registries needed rebuilding in TICKET-2 because a store has a
# connection and a lifecycle; these genuinely do not.
EMBEDDERS = [
    pytest.param(FakeEmbedder, id="fake"),
    pytest.param(build_gemini_embedder, id="gemini"),
]

GENERATORS = [
    pytest.param(FakeGenerator, id="fake"),
    pytest.param(build_groq_generator, id="groq"),
    pytest.param(build_gemini_generator, id="gemini"),
]
