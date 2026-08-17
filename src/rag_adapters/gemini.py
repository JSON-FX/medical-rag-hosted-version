"""Gemini adapters — embeddings, and generation as the failover secondary.

The embedder is the more delicate of the two. `gemini-embedding-001` is
natively 3072-dimensional and reduced to the schema's 768 via
`output_dimensionality`, which the SDK documents as truncating "excessive
values in the output embedding ... from the end". Truncating a matryoshka
embedding breaks the unit norm it started with, so this adapter renormalises —
see `_unit`.

The generator has a translation the Groq one does not need: Gemini takes a
system instruction separately from the conversation, and calls the assistant
role "model".
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from rag_core.config import RagConfig
from rag_core.contracts import Token, TokenStream, Vector
from rag_core.errors import ProviderProtocolError, ProviderUnavailable

from ._backoff import Sleeper, retry_with_backoff
from ._client import LazyClient

# Documents and queries are embedded ASYMMETRICALLY, on purpose. This is the
# same trap the local build documents for nomic-embed-text, which needs
# `search_document: ` and `search_query: ` prefixes and "degrades retrieval
# silently" without them. Gemini spells it `task_type`, and gets it just as
# wrong just as quietly.
#
# Following the local build's lead, the task type is chosen by WHICH METHOD YOU
# CALL and is not a parameter. No caller can forget it.
_DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
_QUERY_TASK = "RETRIEVAL_QUERY"


def _as_provider_error(exc: genai_errors.APIError) -> Exception:
    """Map an SDK error into this codebase's error family.

    Done at the boundary so nothing SDK-shaped escapes to a caller catching
    `ProviderError` — the same escape the local build closed in `ollama.py`,
    where a malformed 200 had to surface the way an unreachable host does
    rather than as a raw parsing error.

    The split matters for retry policy. A 429 is a `ClientError`, not a
    `ServerError`, so retrying only on `ServerError` would skip the rate limit
    — the one failure the embedder exists to survive. Anything else in the 4xx
    range is a bad request that a second attempt cannot fix.
    """
    code = getattr(exc, "code", None)
    if isinstance(exc, genai_errors.ServerError) or code == 429:
        return ProviderUnavailable(f"gemini unavailable ({code}): {exc}")
    return ProviderProtocolError(f"gemini rejected the request ({code}): {exc}")


# Retry on our own type rather than the SDK's, so the rule reads as policy
# instead of as a list of vendor exception classes.
_RETRYABLE = (ProviderUnavailable,)


def _unit(vector: list[float], model_id: str) -> Vector:
    """Renormalise to unit length after dimension reduction.

    `output_dimensionality` truncates from the end, which leaves the vector
    shorter than unit length by however much energy lived in the discarded
    tail. Cosine still computes, but the magnitude now varies per vector — and
    the gate thresholds on raw cosine similarity, so that variation lands
    directly on the quantity τ is measured against (ADR-003).
    """
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        # Downstream this becomes a NaN cosine distance from pgvector and a
        # gate that refuses everything for no discoverable reason. Fail here,
        # where the cause is still visible.
        raise ProviderProtocolError(
            f"{model_id} returned a zero vector, which cannot be normalised and "
            "would produce a NaN cosine distance at query time"
        )
    return [x / norm for x in vector]


class GeminiEmbedder:
    """EmbeddingProvider over `gemini-embedding-001`."""

    def __init__(
        self, cfg: RagConfig, client: Any | None = None, sleep: Sleeper | None = None
    ) -> None:
        self.model_id = cfg.embedding.model_id
        self.dimension = cfg.embedding.dimension
        self._batch_size = cfg.embedding.batch_size
        # Deferred: genai.Client(api_key="") raises, and building a profile
        # must succeed without a key (see _client.LazyClient).
        self._lazy = LazyClient(lambda: genai.Client(api_key=cfg.providers.gemini_api_key), client)
        # Injectable for the same reason as the client: tests assert the retry
        # policy without waiting out the delays it specifies.
        self._sleep = sleep
        self.batches = 0  # lets tests assert batching without a network call

    @property
    def _client(self) -> Any:
        return self._lazy.get()

    async def _embed(self, texts: list[str], task_type: str) -> list[Vector]:
        if not texts:
            # An empty `contents` list is an API error, and the contract suite
            # requires an empty batch to be handled without calling out.
            return []

        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_one_batch(batch, task_type))
        return vectors

    async def _embed_one_batch(self, batch: list[str], task_type: str) -> list[Vector]:
        async def call() -> Any:
            try:
                return await self._client.aio.models.embed_content(
                    model=self.model_id,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=self.dimension,
                    ),
                )
            except genai_errors.APIError as exc:
                raise _as_provider_error(exc) from exc

        self.batches += 1
        response = await retry_with_backoff(call, retry_on=_RETRYABLE, sleep=self._sleep)

        raw = [e.values for e in (response.embeddings or [])]

        # Both guards are carried across from the local build. A count mismatch
        # would misalign chunk text with vectors and silently poison the store,
        # which is a failure retrieval cannot detect: every query still returns
        # results, they are just the wrong ones.
        if len(raw) != len(batch):
            raise ProviderProtocolError(
                f"{self.model_id} returned {len(raw)} embeddings for {len(batch)} "
                "inputs. Refusing to continue: mismatched counts would misalign "
                "chunk text with vectors and silently poison the store."
            )
        for values in raw:
            if values is None or len(values) != self.dimension:
                got = "none" if values is None else len(values)
                raise ProviderProtocolError(
                    f"expected {self.dimension}-dim embeddings from {self.model_id}, got {got}"
                )
        return [_unit(list(values), self.model_id) for values in raw if values is not None]

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        return await self._embed(texts, _DOCUMENT_TASK)

    async def embed_query(self, text: str) -> Vector:
        return (await self._embed([text], _QUERY_TASK))[0]


def to_gemini_contents(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[types.Content]]:
    """Translate an OpenAI-style message list into Gemini's shape.

    Two differences, and both fail quietly if missed:

    1. The system message is not part of the conversation. It goes in
       `GenerateContentConfig.system_instruction`. Leaving it in `contents`
       drops it — and the system prompt is what carries the sentinel
       instruction and the citation instruction, so losing it disables stage 2
       of the gate and stops the model citing, with nothing raising.
    2. The assistant role is called "model".

    Written to handle history even though the hosted profile is single-turn,
    because `prompts._trim_history` is ported and tested and a translation that
    only works for one shape is a trap for whoever enables multi-turn.
    """
    system: str | None = None
    contents: list[types.Content] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system = message["content"] if system is None else f"{system}\n\n{message['content']}"
            continue
        contents.append(
            types.Content(
                role="model" if role == "assistant" else "user",
                parts=[types.Part.from_text(text=message["content"])],
            )
        )
    return system, contents


class GeminiGenerator:
    """GenerationProvider over Gemini. The failover secondary (ADR-004)."""

    def __init__(self, cfg: RagConfig, model_id: str | None = None, client: Any | None = None):
        self.model_id = model_id or cfg.generation.secondary_model_id
        self._lazy = LazyClient(lambda: genai.Client(api_key=cfg.providers.gemini_api_key), client)

    @property
    def _client(self) -> Any:
        return self._lazy.get()

    def stream(self, messages: list[dict[str, str]]) -> TokenStream:
        return TokenStream(self._stream(messages), model_id=self.model_id)

    async def _stream(self, messages: list[dict[str, str]]) -> AsyncIterator[Token]:
        system, contents = to_gemini_contents(messages)
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self.model_id,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system),
            )
            async for chunk in stream:
                text = getattr(chunk, "text", None)
                # A chunk with no text is normal — safety metadata, usage, a
                # final empty delta. Yielding "" would confuse the sentinel
                # filter's buffer accounting.
                if text:
                    yield text
        except genai_errors.APIError as exc:
            raise _as_provider_error(exc) from exc
