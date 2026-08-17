"""The fallback chain, in every failure shape (ADR-004)."""

import asyncio

import pytest

from rag_adapters.failover import FailoverGenerator
from rag_core.contracts import Token, TokenStream
from rag_core.errors import AllProvidersUnavailable, ProviderProtocolError, ProviderUnavailable

MESSAGES = [{"role": "user", "content": "What is the metformin dose?"}]


class ScriptedGenerator:
    """A generator whose failures can be placed precisely.

    `fail_before` raises without producing anything; `fail_after` raises once
    that many tokens have been yielded, which is the case failover must NOT
    cover.
    """

    def __init__(self, model_id, tokens=None, fail_before=None, fail_after=None):
        self.model_id = model_id
        self._tokens = tokens if tokens is not None else ["one ", "two ", "three"]
        self._fail_before = fail_before
        self._fail_after = fail_after
        self.calls = 0

    def stream(self, messages: list[dict[str, str]]) -> TokenStream:
        self.calls += 1

        async def gen():
            if self._fail_before is not None:
                raise self._fail_before
            for i, token in enumerate(self._tokens):
                if self._fail_after is not None and i == self._fail_after:
                    raise ProviderUnavailable(f"{self.model_id} died mid-stream")
                yield token

        return TokenStream(gen(), model_id=self.model_id)


class FlakyGenerator:
    """Fails the first `failures` attempts, then succeeds."""

    def __init__(self, model_id, failures):
        self.model_id = model_id
        self._remaining = failures
        self.calls = 0

    def stream(self, messages: list[dict[str, str]]) -> TokenStream:
        self.calls += 1
        should_fail = self._remaining > 0
        self._remaining -= 1

        async def gen():
            if should_fail:
                raise ProviderUnavailable(f"{self.model_id} rate limited")
            yield "ok"

        return TokenStream(gen(), model_id=self.model_id)


async def collect(stream: TokenStream) -> tuple[str, str | None]:
    text = "".join([t async for t in stream])
    return text, stream.served_by


# --- the happy path ------------------------------------------------------


async def test_the_primary_serves_when_it_works():
    primary = ScriptedGenerator("groq-primary")
    secondary = ScriptedGenerator("gemini-secondary")
    text, served = await collect(FailoverGenerator(primary, secondary).stream(MESSAGES))
    assert text == "one two three"
    assert served == "groq-primary"
    assert secondary.calls == 0, "the secondary must not be touched on success"


async def test_the_decorator_reports_the_primary_as_its_model_id():
    """It serves the overwhelming majority of requests. `served_by` on the
    stream is the authoritative per-request answer."""
    chain = FailoverGenerator(ScriptedGenerator("groq-primary"), ScriptedGenerator("gemini"))
    assert chain.model_id == "groq-primary"


# --- retry then failover -------------------------------------------------


async def test_the_primary_is_retried_once_before_falling_through():
    primary = FlakyGenerator("groq-primary", failures=1)
    secondary = ScriptedGenerator("gemini-secondary")
    text, served = await collect(FailoverGenerator(primary, secondary).stream(MESSAGES))
    assert primary.calls == 2, "one retry"
    assert secondary.calls == 0
    assert served == "groq-primary"
    assert text == "ok"


async def test_two_primary_failures_fall_through_to_the_secondary():
    primary = FlakyGenerator("groq-primary", failures=2)
    secondary = ScriptedGenerator("gemini-secondary")
    text, served = await collect(FailoverGenerator(primary, secondary).stream(MESSAGES))
    assert primary.calls == 2, "one retry, then give up"
    assert secondary.calls == 1
    assert served == "gemini-secondary", "the response must name who actually served it"
    assert text == "one two three"


async def test_both_providers_failing_raises_all_providers_unavailable():
    """Never a partial answer presented as a whole one (PRD F16)."""
    primary = ScriptedGenerator("groq", fail_before=ProviderUnavailable("429"))
    secondary = ScriptedGenerator("gemini", fail_before=ProviderUnavailable("503"))
    stream = FailoverGenerator(primary, secondary).stream(MESSAGES)
    with pytest.raises(AllProvidersUnavailable) as exc:
        [t async for t in stream]
    assert "429" in str(exc.value) and "503" in str(exc.value), "both causes reported"


async def test_a_non_retryable_error_does_not_consume_the_fallback():
    """A malformed request fails identically on the secondary. Burning the
    fallback on it doubles the latency of the failure and turns one clear error
    into two."""
    primary = ScriptedGenerator("groq", fail_before=ProviderProtocolError("bad request"))
    secondary = ScriptedGenerator("gemini")
    stream = FailoverGenerator(primary, secondary).stream(MESSAGES)
    with pytest.raises(ProviderProtocolError):
        [t async for t in stream]
    assert primary.calls == 1, "not even retried"
    assert secondary.calls == 0


# --- the mid-stream boundary ---------------------------------------------


async def test_a_mid_stream_failure_propagates_rather_than_failing_over():
    """Once a token is on the wire it cannot be retracted — the same constraint
    sentinel.py buffers for. Failing over here would duplicate text."""
    primary = ScriptedGenerator("groq", fail_after=2)
    secondary = ScriptedGenerator("gemini")
    stream = FailoverGenerator(primary, secondary).stream(MESSAGES)

    seen = []
    with pytest.raises(ProviderUnavailable, match="mid-stream"):
        async for token in stream:
            seen.append(token)

    assert seen == ["one ", "two "], "what was emitted stays emitted"
    assert secondary.calls == 0, "no failover after the commitment point"
    assert stream.served_by == "groq", "the partial answer is still attributed"


async def test_a_failure_on_the_very_first_token_still_fails_over():
    """fail_after=0 raises before yielding anything, so nothing is committed
    and the chain may still fall through."""
    primary = ScriptedGenerator("groq", fail_after=0)
    secondary = ScriptedGenerator("gemini-secondary")
    text, served = await collect(FailoverGenerator(primary, secondary).stream(MESSAGES))
    assert served == "gemini-secondary"
    assert text == "one two three"


async def test_a_mid_stream_failure_on_the_secondary_also_propagates():
    primary = ScriptedGenerator("groq", fail_before=ProviderUnavailable("429"))
    secondary = ScriptedGenerator("gemini", fail_after=1)
    stream = FailoverGenerator(primary, secondary).stream(MESSAGES)
    seen = []
    with pytest.raises(ProviderUnavailable, match="mid-stream"):
        async for token in stream:
            seen.append(token)
    assert seen == ["one "]


# --- provenance under concurrency ----------------------------------------


async def test_served_by_is_none_before_iteration():
    stream = FailoverGenerator(ScriptedGenerator("groq"), ScriptedGenerator("gemini")).stream(
        MESSAGES
    )
    assert stream.served_by is None


async def test_concurrent_requests_do_not_share_provenance():
    """The reason provenance lives on the stream and not on the generator.
    One FailoverGenerator instance serves every request, so an attribute would
    be a race that reports the wrong provider under exactly the load a demo
    gets while it is being evaluated."""
    primary = ScriptedGenerator("groq-primary")
    secondary = ScriptedGenerator("gemini-secondary")
    chain = FailoverGenerator(primary, secondary)

    streams = [chain.stream(MESSAGES) for _ in range(8)]
    results = await asyncio.gather(*(collect(s) for s in streams))

    assert {served for _, served in results} == {"groq-primary"}
    assert all(text == "one two three" for text, _ in results)


async def test_a_slow_and_a_fast_request_do_not_cross_provenance():
    """Interleaves two requests that resolve to DIFFERENT providers, which is
    where a shared attribute would visibly report the wrong one."""
    working = ScriptedGenerator("groq-primary")
    broken = ScriptedGenerator("groq-primary", fail_before=ProviderUnavailable("429"))
    secondary = ScriptedGenerator("gemini-secondary")

    good = FailoverGenerator(working, secondary).stream(MESSAGES)
    bad = FailoverGenerator(broken, secondary).stream(MESSAGES)

    # Start the failing one first so its fallback resolves while the other runs.
    bad_result, good_result = await asyncio.gather(collect(bad), collect(good))

    assert bad_result[1] == "gemini-secondary"
    assert good_result[1] == "groq-primary"


# --- shape ---------------------------------------------------------------


async def test_the_chain_is_itself_a_generation_provider():
    """It must be substitutable for either of the things it wraps."""
    chain = FailoverGenerator(ScriptedGenerator("a"), ScriptedGenerator("b"))
    stream = chain.stream(MESSAGES)
    assert isinstance(stream, TokenStream)
    tokens: list[Token] = [t async for t in stream]
    assert all(isinstance(t, str) for t in tokens)
