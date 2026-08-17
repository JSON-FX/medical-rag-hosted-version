"""Gemini adapters, driven entirely by fake clients — no key, no network."""

import math
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from rag_adapters.gemini import GeminiEmbedder, GeminiGenerator, to_gemini_contents
from rag_core.config import load_config
from rag_core.errors import ProviderProtocolError, ProviderUnavailable

CFG = load_config(env={})


def embedding_response(vectors):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])


def unit_vector(dim=768, axis=0):
    v = [0.0] * dim
    v[axis] = 3.0  # deliberately not unit length, so renormalisation is observable
    return v


class FakeEmbedClient:
    """Records every call so the task type and batching can be asserted."""

    def __init__(self, responder=None):
        self.calls = []
        self._responder = responder or (
            lambda contents, **_: embedding_response([unit_vector() for _ in contents])
        )
        self.aio = SimpleNamespace(models=SimpleNamespace(embed_content=self._embed))

    async def _embed(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._responder(contents, config=config)


async def no_sleep(_seconds: float) -> None:
    """Tests assert the retry policy, not the wall-clock it specifies."""


def api_error(code: int, cls=genai_errors.ClientError):
    return cls(code, {"error": {"message": f"boom {code}"}})


# --- task types ----------------------------------------------------------


async def test_documents_and_queries_use_different_task_types():
    """The same trap the local build documents for nomic's search_document: /
    search_query: prefixes. Using one where the other belongs produces
    plausible vectors with worse ranking and no error anywhere."""
    client = FakeEmbedClient()
    embedder = GeminiEmbedder(CFG, client=client)

    await embedder.embed_documents(["a chunk of a drug label"])
    await embedder.embed_query("what is the dose?")

    assert client.calls[0]["config"].task_type == "RETRIEVAL_DOCUMENT"
    assert client.calls[1]["config"].task_type == "RETRIEVAL_QUERY"


async def test_the_task_type_is_not_a_caller_parameter():
    """Chosen by which method you call, following the local build's lead of
    making the prefixes impossible to forget."""
    import inspect

    for method in (GeminiEmbedder.embed_documents, GeminiEmbedder.embed_query):
        params = set(inspect.signature(method).parameters)
        assert "task_type" not in params


async def test_the_reduced_dimension_is_requested():
    client = FakeEmbedClient()
    await GeminiEmbedder(CFG, client=client).embed_query("q")
    assert client.calls[0]["config"].output_dimensionality == 768


# --- renormalisation -----------------------------------------------------


async def test_vectors_are_renormalised_to_unit_length():
    """output_dimensionality truncates from the end, which breaks the unit norm
    the full-width embedding has. The gate thresholds on raw cosine similarity,
    so a varying magnitude lands directly on what tau measures (ADR-003)."""
    client = FakeEmbedClient()
    vector = await GeminiEmbedder(CFG, client=client).embed_query("metformin")
    norm = math.sqrt(sum(x * x for x in vector))
    assert norm == pytest.approx(1.0, abs=1e-9)


async def test_renormalisation_preserves_direction():
    """Only the magnitude changes; cosine ranking must be unaffected by the fix
    itself, otherwise it is not a normalisation."""
    raw = [3.0, 4.0] + [0.0] * 766
    client = FakeEmbedClient(lambda contents, **_: embedding_response([raw]))
    vector = await GeminiEmbedder(CFG, client=client).embed_query("q")
    assert vector[0] == pytest.approx(0.6)
    assert vector[1] == pytest.approx(0.8)


async def test_a_zero_vector_is_rejected_rather_than_normalised():
    """Downstream this becomes a NaN cosine distance from pgvector and a gate
    that refuses everything for no discoverable reason."""
    client = FakeEmbedClient(lambda contents, **_: embedding_response([[0.0] * 768]))
    with pytest.raises(ProviderProtocolError, match="zero vector"):
        await GeminiEmbedder(CFG, client=client).embed_query("q")


# --- guards --------------------------------------------------------------


async def test_a_count_mismatch_raises_and_names_both_numbers():
    """Mismatched counts would misalign chunk text with vectors and silently
    poison the store — a failure retrieval cannot detect, because every query
    still returns results, just the wrong ones."""
    client = FakeEmbedClient(lambda contents, **_: embedding_response([unit_vector()]))
    with pytest.raises(ProviderProtocolError, match="returned 1 embeddings for 3"):
        await GeminiEmbedder(CFG, client=client).embed_documents(["a", "b", "c"])


async def test_a_wrong_dimension_raises_and_names_both_numbers():
    client = FakeEmbedClient(lambda contents, **_: embedding_response([unit_vector(dim=384)]))
    with pytest.raises(ProviderProtocolError, match="expected 768-dim.*got 384"):
        await GeminiEmbedder(CFG, client=client).embed_query("q")


async def test_a_missing_values_field_raises():
    client = FakeEmbedClient(lambda contents, **_: embedding_response([None]))
    with pytest.raises(ProviderProtocolError, match="got none"):
        await GeminiEmbedder(CFG, client=client).embed_query("q")


# --- batching and backoff ------------------------------------------------


async def test_an_empty_batch_never_calls_the_api():
    """An empty `contents` list is an API error, and the contract suite
    requires this to be handled without calling out."""
    client = FakeEmbedClient()
    assert await GeminiEmbedder(CFG, client=client).embed_documents([]) == []
    assert client.calls == []


async def test_inputs_are_split_into_batches_preserving_order():
    from dataclasses import replace

    cfg = replace(CFG, embedding=replace(CFG.embedding, batch_size=2))
    seen: list[list[str]] = []

    def responder(contents, **_):
        seen.append(list(contents))
        return embedding_response([unit_vector(axis=i) for i, _ in enumerate(contents)])

    client = FakeEmbedClient(responder)
    vectors = await GeminiEmbedder(cfg, client=client).embed_documents(["a", "b", "c", "d", "e"])

    assert seen == [["a", "b"], ["c", "d"], ["e"]]
    assert len(vectors) == 5


async def test_a_rate_limit_is_retried():
    """429 is a ClientError, not a ServerError. Retrying only on ServerError
    would skip the one failure the embedder exists to survive."""
    attempts = []

    def responder(contents, **_):
        attempts.append(1)
        if len(attempts) < 2:
            raise api_error(429)
        return embedding_response([unit_vector() for _ in contents])

    client = FakeEmbedClient(responder)
    vector = await GeminiEmbedder(CFG, client=client, sleep=no_sleep).embed_query("q")
    assert len(attempts) == 2
    assert len(vector) == 768


async def test_a_bad_request_is_not_retried():
    attempts = []

    def responder(contents, **_):
        attempts.append(1)
        raise api_error(400)

    client = FakeEmbedClient(responder)
    with pytest.raises(ProviderProtocolError):
        await GeminiEmbedder(CFG, client=client).embed_query("q")
    assert len(attempts) == 1


@pytest.mark.parametrize("code", [401, 403])
async def test_an_auth_failure_is_treated_as_unavailable_not_malformed(code):
    """Same reasoning as the Groq adapter: a credential is provider-specific,
    so a revoked key must reach the fallback (PRD success criterion 4)."""
    client = FakeEmbedClient(lambda contents, **_: (_ for _ in ()).throw(api_error(code)))
    with pytest.raises(ProviderUnavailable):
        await GeminiEmbedder(CFG, client=client, sleep=no_sleep).embed_query("q")


async def test_sdk_errors_never_escape_as_themselves():
    """A caller catching ProviderError must not have to also catch the SDK's
    exception tree — the same escape the local build closed in ollama.py."""
    client = FakeEmbedClient(
        lambda contents, **_: (_ for _ in ()).throw(api_error(503, genai_errors.ServerError))
    )
    with pytest.raises(ProviderUnavailable):
        await GeminiEmbedder(CFG, client=client, sleep=no_sleep).embed_query("q")


# --- message translation -------------------------------------------------


def test_the_system_message_is_hoisted_out_of_the_conversation():
    """Leaving it in `contents` drops it, and the system prompt is what carries
    the sentinel instruction and the citation instruction — so losing it
    disables stage 2 of the gate and stops the model citing, silently."""
    system, contents = to_gemini_contents(
        [
            {"role": "system", "content": "You are a clinical reference assistant."},
            {"role": "user", "content": "What is the dose?"},
        ]
    )
    assert system == "You are a clinical reference assistant."
    assert len(contents) == 1
    assert contents[0].role == "user"


def test_the_assistant_role_becomes_model():
    _, contents = to_gemini_contents(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "now"},
        ]
    )
    assert [c.role for c in contents] == ["user", "model", "user"]


def test_conversation_order_is_preserved():
    _, contents = to_gemini_contents(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
    )
    assert [c.parts[0].text for c in contents] == ["first", "second"]


def test_no_system_message_yields_none():
    system, contents = to_gemini_contents([{"role": "user", "content": "q"}])
    assert system is None
    assert len(contents) == 1


# --- generation ----------------------------------------------------------


class FakeGenClient:
    def __init__(self, chunks=None, raises=None):
        self._chunks = chunks if chunks is not None else ["Met", "formin", " is."]
        self._raises = raises
        self.calls = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._stream))

    async def _stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._raises is not None:
            raise self._raises

        async def gen():
            for c in self._chunks:
                yield SimpleNamespace(text=c)

        return gen()


async def test_generation_yields_text_deltas():
    gen = GeminiGenerator(CFG, client=FakeGenClient())
    stream = gen.stream([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    assert "".join([t async for t in stream]) == "Metformin is."


async def test_chunks_without_text_are_skipped():
    """Safety metadata, usage and final empty deltas are normal. Yielding ""
    would confuse the sentinel filter's buffer accounting."""
    client = FakeGenClient(chunks=["Met", None, "", "formin"])
    stream = GeminiGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    assert [t async for t in stream] == ["Met", "formin"]


async def test_the_system_instruction_reaches_the_api():
    client = FakeGenClient()
    stream = GeminiGenerator(CFG, client=client).stream(
        [{"role": "system", "content": "cite your sources"}, {"role": "user", "content": "q"}]
    )
    [t async for t in stream]
    assert client.calls[0]["config"].system_instruction == "cite your sources"


async def test_the_stream_reports_which_model_served():
    gen = GeminiGenerator(CFG, model_id="gemini-test", client=FakeGenClient())
    stream = gen.stream([{"role": "user", "content": "q"}])
    assert stream.served_by is None, "unknown until the first token"
    [t async for t in stream]
    assert stream.served_by == "gemini-test"


async def test_generation_errors_map_into_the_provider_family():
    client = FakeGenClient(raises=api_error(429))
    stream = GeminiGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    with pytest.raises(ProviderUnavailable):
        [t async for t in stream]
