from dataclasses import FrozenInstanceError

import pytest

from rag_adapters.profile import Profile, build_profile, register_profile
from rag_core.config import load_config


def test_the_fake_profile_resolves_all_four_adapters():
    profile = build_profile(load_config(env={"RAG_PROFILE": "fake"}))
    assert isinstance(profile, Profile)
    assert profile.name == "fake"
    assert profile.embedder is not None
    assert profile.generator is not None
    assert profile.dense is not None
    assert profile.lexical is not None


def test_an_unknown_profile_raises_and_names_the_valid_options():
    """A silent fallback to fakes would let a real deployment answer
    confidently from nothing.

    Uses a name that is not and will not be registered — 'hosted' was the
    original placeholder here and became real in TICKET-2, which would have
    made this test pass for entirely the wrong reason."""
    with pytest.raises(ValueError, match="unknown profile 'sqlite'"):
        build_profile(load_config(env={"RAG_PROFILE": "sqlite"}))


def test_the_error_lists_what_is_actually_registered():
    with pytest.raises(ValueError, match="fake"):
        build_profile(load_config(env={"RAG_PROFILE": "nonsense"}))


def test_the_local_profile_is_real_storage_with_fake_inference():
    """TICKET-7 D3. The point is a running API with real chunks, real titles
    and real page anchors, costing nothing per request — the third ticket to
    want this."""
    from rag_adapters.fakes import FakeEmbedder, FakeGenerator
    from rag_adapters.postgres import PostgresDenseStore, PostgresLexicalStore

    profile = build_profile(load_config(env={"RAG_PROFILE": "local"}))
    assert profile.name == "local"
    assert isinstance(profile.embedder, FakeEmbedder)
    assert isinstance(profile.generator, FakeGenerator)
    assert isinstance(profile.dense, PostgresDenseStore)
    assert isinstance(profile.lexical, PostgresLexicalStore)
    assert profile.resources, "the pool must be opened and closed by the shell"


def test_the_local_profile_is_not_the_fake_profile():
    """Both are needed and they are not interchangeable: `fake` is the
    zero-dependency test profile and must never require a database."""
    local = build_profile(load_config(env={"RAG_PROFILE": "local"}))
    fake = build_profile(load_config(env={"RAG_PROFILE": "fake"}))
    assert type(local.dense) is not type(fake.dense)
    assert fake.resources == (), "the fake profile connects to nothing"


def test_the_local_generator_streams_enough_tokens_to_show_streaming():
    """A scripted answer shorter than the sentinel filter's 44-character buffer
    arrives as a single frame, and a frontend built against that cannot tell
    progressive rendering from a spinner."""
    profile = build_profile(load_config(env={"RAG_PROFILE": "local"}))
    answer = "".join(profile.generator._tokens)
    assert len(answer) > 44
    assert "[1]" in answer and "[2]" in answer, "citations are what the UI has to resolve"


def test_building_the_hosted_profile_does_not_connect():
    """Construction is synchronous and the pool needs a running event loop, so
    building must succeed against a database that does not exist. The failure
    belongs in open(), where it can be reported."""
    profile = build_profile(
        load_config(
            env={
                "RAG_PROFILE": "hosted",
                "DATABASE_URL": "postgresql://nobody@203.0.113.1:5432/nothing",
            }
        )
    )
    assert profile.name == "hosted"
    assert profile.dense is not None
    assert profile.lexical is not None


async def test_using_the_hosted_stores_before_open_says_so():
    """The alternative is an AttributeError on None deep inside a query, which
    tells you nothing about the lifecycle step you actually missed."""
    profile = build_profile(load_config(env={"RAG_PROFILE": "hosted"}))
    with pytest.raises(RuntimeError, match="not open"):
        await profile.dense.count()


async def test_the_fake_profile_opens_and_closes_as_no_ops():
    profile = build_profile(load_config(env={"RAG_PROFILE": "fake"}))
    assert profile.resources == ()
    await profile.open()
    await profile.close()


async def test_close_is_safe_without_open_and_twice_over():
    """A shell that fails during startup still runs its shutdown path, and a
    close that raises there buries the original error."""
    profile = build_profile(
        load_config(env={"RAG_PROFILE": "hosted", "DATABASE_URL": "postgresql://x@127.0.0.1/y"})
    )
    await profile.close()
    await profile.close()


def test_the_profile_container_is_still_frozen():
    profile = build_profile(load_config(env={}))
    with pytest.raises(FrozenInstanceError):
        profile.name = "mutated"


def test_the_embedder_dimension_follows_the_configured_dimension():
    """The dimension is part of the index schema, not a runtime setting — it is
    written to the manifest and compared at startup (ARCHITECTURE.md §5)."""
    profile = build_profile(load_config(env={"EMBED_DIMENSIONS": "384"}))
    assert profile.embedder.dimension == 384


def test_a_new_profile_can_be_registered_without_editing_the_resolver():
    """TICKET-2 and TICKET-3 each add one entry. If either has to restructure
    this, the seam was built wrong."""
    sentinel = object()
    register_profile("temporary", lambda cfg: sentinel)
    try:
        assert build_profile(load_config(env={"RAG_PROFILE": "temporary"})) is sentinel
    finally:
        from rag_adapters.profile import _REGISTRY

        _REGISTRY.pop("temporary", None)
