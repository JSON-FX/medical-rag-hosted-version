"""The provider probe. Over fakes — no keys, no network."""

import asyncio

from rag_adapters.failover import FailoverGenerator
from rag_adapters.fakes import FakeDenseStore, FakeEmbedder, FakeGenerator, FakeLexicalStore
from rag_adapters.profile import Profile
from rag_api.probe import probe_generators, probe_store
from rag_core.contracts import TokenStream
from rag_core.errors import ProviderUnavailable


def profile_with(generator):
    return Profile(
        name="test",
        embedder=FakeEmbedder(),
        generator=generator,
        dense=FakeDenseStore(),
        lexical=FakeLexicalStore(),
    )


def chain(primary_ok=True, secondary_ok=True):
    primary = FakeGenerator(
        model_id="groq/primary",
        fail_with=None if primary_ok else ProviderUnavailable("groq is down"),
    )
    secondary = FakeGenerator(
        model_id="gemini/secondary",
        fail_with=None
        if secondary_ok
        else ProviderUnavailable("gemini-2.0-flash is no longer available"),
    )
    return FailoverGenerator(primary, secondary)


# --- probing both halves of the chain ------------------------------------


async def test_both_providers_are_probed_individually():
    """A probe THROUGH the chain would only ever exercise the primary, because
    that is what the chain is for. ADR-004 item 4 needs the far half."""
    results = await probe_generators(profile_with(chain()))
    assert [r.name for r in results] == ["primary", "secondary"]
    assert [r.model_id for r in results] == ["groq/primary", "gemini/secondary"]
    assert all(r.ok for r in results)


async def test_a_dead_secondary_is_reported_with_a_healthy_primary():
    """The exact state that went unnoticed until TICKET-4: primary healthy,
    every test passing, fallback already retired."""
    results = await probe_generators(profile_with(chain(secondary_ok=False)))
    by_name = {r.name: r for r in results}
    assert by_name["primary"].ok is True
    assert by_name["secondary"].ok is False


async def test_a_failed_probe_names_the_provider_and_the_reason():
    """A check reporting a capability present when it is not converts a clear
    failure into an unexplained one later. The message is the whole value."""
    results = await probe_generators(profile_with(chain(secondary_ok=False)))
    secondary = next(r for r in results if r.name == "secondary")
    assert "gemini/secondary" == secondary.model_id
    assert "no longer available" in secondary.detail


async def test_a_probe_failure_does_not_raise():
    """The point is to report the state, not to become the failure."""
    results = await probe_generators(profile_with(chain(primary_ok=False, secondary_ok=False)))
    assert len(results) == 2
    assert not any(r.ok for r in results)


async def test_a_lone_generator_is_probed_without_a_chain():
    results = await probe_generators(profile_with(FakeGenerator(model_id="solo")))
    assert [r.name for r in results] == ["generator"]
    assert results[0].model_id == "solo"


async def test_a_provider_returning_nothing_is_not_ok():
    """A model that streams zero tokens is not healthy, even though nothing
    raised."""
    results = await probe_generators(profile_with(FakeGenerator(tokens=[])))
    assert results[0].ok is False
    assert "no tokens" in results[0].detail


async def test_a_hung_provider_times_out_rather_than_hanging():
    class HungGenerator(FakeGenerator):
        def stream(self, messages):
            async def gen():
                await asyncio.sleep(60)
                yield "never"

            return TokenStream(gen(), model_id=self.model_id)

    import rag_api.probe as probe_module

    original = probe_module.TIMEOUT_S
    probe_module.TIMEOUT_S = 0.05
    try:
        results = await probe_generators(profile_with(HungGenerator()))
    finally:
        probe_module.TIMEOUT_S = original

    assert results[0].ok is False
    assert "timed out" in results[0].detail


async def test_probes_record_latency():
    results = await probe_generators(profile_with(chain()))
    assert all(r.latency_ms >= 0 for r in results)


# --- the store probe ------------------------------------------------------


async def test_an_empty_store_is_not_ok():
    """An index with no chunks cannot answer anything, so reporting it healthy
    would be the same lie the manifest check exists to prevent."""
    result = await probe_store(profile_with(FakeGenerator()))
    assert result.ok is False
    assert "0 chunks" in result.detail


async def test_a_populated_store_is_ok():
    from rag_core.contracts import Chunk, EmbeddedChunk

    profile = profile_with(FakeGenerator())
    chunk = Chunk(
        id="metformin_0",
        document_id="metformin",
        ordinal=0,
        anchor="1",
        content="text",
        document_title="Metformin",
    )
    await profile.dense.upsert([EmbeddedChunk(chunk=chunk, embedding=[0.1] * 768)])
    result = await probe_store(profile)
    assert result.ok is True
    assert "1 chunks" in result.detail


async def test_an_unreachable_store_is_reported_not_raised():
    class BrokenStore(FakeDenseStore):
        async def count(self):
            raise ConnectionError("neon unreachable")

    profile = Profile(
        name="test",
        embedder=FakeEmbedder(),
        generator=FakeGenerator(),
        dense=BrokenStore(),
        lexical=FakeLexicalStore(),
    )
    result = await probe_store(profile)
    assert result.ok is False
    assert "neon unreachable" in result.detail
