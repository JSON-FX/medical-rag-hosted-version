from dataclasses import FrozenInstanceError

import pytest

from rag_core.config import RagConfig, load_config


def test_defaults_match_the_documented_configuration():
    cfg = load_config(env={})
    assert cfg.profile == "fake"
    assert cfg.embedding.dimension == 768
    assert cfg.chunk.size == 1000
    assert cfg.chunk.overlap == 150
    assert cfg.retrieval.per_leg == 10
    assert cfg.retrieval.top_k == 4
    assert cfg.retrieval.rrf_k == 60
    assert cfg.history_messages == 4


def test_gate_thresholds_are_carried_across_but_marked_invalid():
    """These are the local build's values, measured against nomic-embed-text.
    They are pinned so a change is deliberate, NOT because they are correct
    here — a different embedding model has a different similarity distribution
    (ADR-003). TICKET-8 re-sweeps them and this assertion changes with them."""
    cfg = load_config(env={})
    assert cfg.gate.tau_abstain == 0.70
    assert cfg.gate.tau_strong == 0.75


def test_the_embedding_model_and_dimension_are_pinned_together():
    """768 is in the schema (`vector(768)`), so the model and the reduced
    dimension are one decision, not two. Changing either alone means the index
    and the embedder disagree — which is the failure index_manifest exists to
    catch at startup rather than discover in a wrong answer."""
    cfg = load_config(env={})
    assert cfg.embedding.model_id == "gemini-embedding-001"
    assert cfg.embedding.dimension == 768


def test_the_two_generation_providers_are_different_vendors():
    """ADR-004 buys two independent failure domains. Two models from one vendor
    share a quota and an outage, which is the single point of failure the whole
    fallback chain exists to remove."""
    cfg = load_config(env={})
    assert cfg.generation.primary_provider != cfg.generation.secondary_provider


def test_api_keys_default_to_empty_rather_than_reaching_for_the_environment():
    """Both SDKs will read their key from os.environ if not passed one. This
    module is the single boundary between the environment and the pipeline, so
    the keys are read here and passed explicitly."""
    cfg = load_config(env={})
    assert cfg.providers.gemini_api_key == ""
    assert cfg.providers.groq_api_key == ""
    loaded = load_config(env={"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"})
    assert loaded.providers.gemini_api_key == "g"
    assert loaded.providers.groq_api_key == "q"


def test_env_overrides_are_typed():
    cfg = load_config(
        env={
            "TAU_ABSTAIN": "0.5",
            "CHUNK_SIZE": "800",
            "EMBED_MODEL": "some-embedder-001",
            "TOP_K": "6",
        }
    )
    assert cfg.gate.tau_abstain == 0.5
    assert isinstance(cfg.gate.tau_abstain, float)
    assert cfg.chunk.size == 800
    assert isinstance(cfg.chunk.size, int)
    assert cfg.retrieval.top_k == 6
    assert cfg.embedding.model_id == "some-embedder-001"


def test_both_generation_providers_are_configurable():
    """Two providers behind one port, with failover (ADR-004)."""
    cfg = load_config(env={"PRIMARY_MODEL": "fast-one", "SECONDARY_MODEL": "backup-one"})
    assert cfg.generation.primary_model_id == "fast-one"
    assert cfg.generation.secondary_model_id == "backup-one"


def test_profile_is_read_from_the_environment():
    assert load_config(env={"RAG_PROFILE": "hosted"}).profile == "hosted"


@pytest.mark.parametrize(
    "attribute_path",
    ["gate", "chunk", "retrieval", "embedding", "generation", "database", "providers"],
)
def test_every_config_section_is_frozen(attribute_path):
    cfg = load_config(env={})
    section = getattr(cfg, attribute_path)
    field_name = next(iter(section.__dataclass_fields__))
    with pytest.raises(FrozenInstanceError):
        setattr(section, field_name, "mutated")


def test_the_top_level_config_is_frozen():
    cfg = load_config(env={})
    assert isinstance(cfg, RagConfig)
    with pytest.raises(FrozenInstanceError):
        cfg.history_messages = 99


def test_nothing_outside_load_config_needs_the_environment():
    """Passing an explicit mapping must be sufficient. If any default reached
    for os.environ, this would leak the developer's shell into the test."""
    cfg = load_config(env={})
    assert cfg == RagConfig()
