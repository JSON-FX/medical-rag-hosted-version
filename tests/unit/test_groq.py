"""Groq adapter, driven by a fake client — no key, no network."""

from types import SimpleNamespace

import httpx
import pytest
from groq import APIConnectionError, APIStatusError, RateLimitError

from rag_adapters.groq import GroqGenerator
from rag_core.config import load_config
from rag_core.errors import ProviderProtocolError, ProviderUnavailable

CFG = load_config(env={})


def chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


class FakeGroqClient:
    def __init__(self, chunks=None, raises=None):
        self._chunks = chunks if chunks is not None else ["Met", "formin", " is."]
        self._raises = raises
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, *, messages, model, stream):
        self.calls.append({"messages": messages, "model": model, "stream": stream})
        if self._raises is not None:
            raise self._raises

        async def gen():
            for c in self._chunks:
                yield chunk(c)

        return gen()


def status_error(status: int, cls=APIStatusError):
    request = httpx.Request("POST", "https://api.groq.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


async def test_streaming_yields_text_deltas():
    stream = GroqGenerator(CFG, client=FakeGroqClient()).stream([{"role": "user", "content": "q"}])
    assert "".join([t async for t in stream]) == "Metformin is."


async def test_none_and_empty_deltas_are_skipped():
    """delta.content is None on the final chunk and on role-only chunks.
    Yielding it would put a non-str into a str stream; yielding "" would
    confuse the sentinel filter's buffer accounting."""
    client = FakeGroqClient(chunks=["Met", None, "", "formin"])
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    assert [t async for t in stream] == ["Met", "formin"]


async def test_messages_pass_through_unchanged():
    """Groq is OpenAI-compatible, so build_messages output needs no
    translation. Gemini needs the system message hoisted and the assistant role
    renamed — the asymmetry is easy to miss reading the two side by side."""
    client = FakeGroqClient()
    messages = [
        {"role": "system", "content": "cite your sources"},
        {"role": "user", "content": "what is the dose?"},
    ]
    stream = GroqGenerator(CFG, client=client).stream(messages)
    [t async for t in stream]
    assert client.calls[0]["messages"] == messages


async def test_streaming_is_requested():
    client = FakeGroqClient()
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    [t async for t in stream]
    assert client.calls[0]["stream"] is True


async def test_the_stream_reports_which_model_served():
    gen = GroqGenerator(CFG, model_id="groq-test", client=FakeGroqClient())
    stream = gen.stream([{"role": "user", "content": "q"}])
    assert stream.served_by is None, "unknown until the first token"
    [t async for t in stream]
    assert stream.served_by == "groq-test"


async def test_a_rate_limit_becomes_provider_unavailable():
    """The failure this whole fallback chain exists for — what a free tier does
    when the demo gets attention."""
    client = FakeGroqClient(raises=status_error(429, RateLimitError))
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    with pytest.raises(ProviderUnavailable):
        [t async for t in stream]


async def test_a_connection_error_becomes_provider_unavailable():
    request = httpx.Request("POST", "https://api.groq.com/")
    client = FakeGroqClient(raises=APIConnectionError(request=request))
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    with pytest.raises(ProviderUnavailable):
        [t async for t in stream]


async def test_a_server_error_becomes_provider_unavailable():
    client = FakeGroqClient(raises=status_error(503))
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    with pytest.raises(ProviderUnavailable):
        [t async for t in stream]


async def test_a_bad_request_is_a_protocol_error_not_an_outage():
    """A 4xx that is not a rate limit fails identically on the secondary, so it
    must not be classified as something worth failing over for."""
    client = FakeGroqClient(raises=status_error(400))
    stream = GroqGenerator(CFG, client=client).stream([{"role": "user", "content": "q"}])
    with pytest.raises(ProviderProtocolError):
        [t async for t in stream]


async def test_the_module_name_does_not_shadow_the_sdk():
    """`rag_adapters/groq.py` next to `import groq` looks like a shadowing bug.
    Python 3 resolves absolute imports, so it is not — pinned here because the
    next reader will otherwise "fix" it."""
    import rag_adapters.groq as adapter

    assert adapter.AsyncGroq.__module__.startswith("groq")
    assert adapter.__name__ == "rag_adapters.groq"
