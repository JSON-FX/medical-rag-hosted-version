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
    confidently from nothing."""
    with pytest.raises(ValueError, match="unknown profile 'hosted'"):
        build_profile(load_config(env={"RAG_PROFILE": "hosted"}))


def test_the_error_lists_what_is_actually_registered():
    with pytest.raises(ValueError, match="fake"):
        build_profile(load_config(env={"RAG_PROFILE": "nonsense"}))


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
