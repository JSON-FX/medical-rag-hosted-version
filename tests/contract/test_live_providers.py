"""The real APIs. Costs quota; never runs in CI.

Everything else in this suite runs against doubles, which proves the adapters
honour their ports but proves nothing about whether the vendor behaves as
documented. These are the cheap check that it does — most importantly, that
`output_dimensionality=768` really returns 768 values, since the schema hard-codes
that width and a mismatch is a deployment-time failure.

    export GEMINI_API_KEY=... GROQ_API_KEY=...
    uv run pytest -m live -v

ADR-004 action item 3 — validating the prompt against both models on the
evaluation set — is TICKET-8's, and needs the eval harness. This is the smoke
test half: both models can emit the exact sentinel when told to.
"""

import math
import os

import pytest

from rag_adapters.gemini import GeminiEmbedder, GeminiGenerator
from rag_adapters.groq import GroqGenerator
from rag_core.config import load_config
from rag_core.prompts import SENTINEL, build_messages
from rag_core.sentinel import filter_sentinel

pytestmark = pytest.mark.live


@pytest.fixture
def cfg():
    config = load_config(env=dict(os.environ))
    if not config.providers.gemini_api_key or not config.providers.groq_api_key:
        pytest.skip("GEMINI_API_KEY and GROQ_API_KEY must both be set for the live tests")
    return config


async def test_the_embedder_returns_the_width_the_schema_expects(cfg):
    """`vector(768)` is in the migration. If the model ever stops honouring
    output_dimensionality, ingestion fails at insert — better to learn it here."""
    vector = await GeminiEmbedder(cfg).embed_query("What is the adult dose of metformin?")
    assert len(vector) == cfg.embedding.dimension == 768


async def test_live_embeddings_are_unit_length(cfg):
    """The renormalisation, against the real truncation rather than a double."""
    vector = await GeminiEmbedder(cfg).embed_query("metformin contraindications")
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0, abs=1e-6)


async def test_document_and_query_task_types_produce_different_vectors(cfg):
    """Asymmetric embedding is the whole reason task_type exists. If these came
    back identical, the parameter would be doing nothing and retrieval would be
    quietly worse than it should be."""
    embedder = GeminiEmbedder(cfg)
    text = "Metformin adult starting dose is 500 mg twice daily."
    as_document = (await embedder.embed_documents([text]))[0]
    as_query = await embedder.embed_query(text)
    assert as_document != as_query


async def test_a_batch_returns_one_vector_per_input(cfg):
    vectors = await GeminiEmbedder(cfg).embed_documents(
        ["metformin dosing", "atenolol dosing", "amoxicillin dosing"]
    )
    assert len(vectors) == 3
    assert all(len(v) == 768 for v in vectors)


@pytest.mark.parametrize("build", [GroqGenerator, GeminiGenerator], ids=["groq", "gemini"])
async def test_each_generator_streams_real_tokens(build, cfg):
    stream = build(cfg).stream(
        [
            {"role": "system", "content": "Answer in one short sentence."},
            {"role": "user", "content": "What is 2 + 2?"},
        ]
    )
    tokens = [t async for t in stream]
    assert tokens, "no tokens returned"
    assert stream.served_by == build(cfg).model_id
    assert "4" in "".join(tokens)


@pytest.mark.parametrize("build", [GroqGenerator, GeminiGenerator], ids=["groq", "gemini"])
async def test_each_model_emits_the_exact_sentinel_when_context_is_insufficient(build, cfg):
    """Stage 2 of the gate depends on the model emitting INSUFFICIENT_CONTEXT
    exactly, with nothing around it. The prompt forbids a preamble and
    filter_sentinel tolerates a short one, but a model that ignores the
    instruction entirely would answer ungrounded — which is the failure this
    whole system exists to prevent.

    ADR-004: "prompt behaviour must be checked against both models, not one."
    """
    from rag_core.prompts import ContextChunk

    irrelevant = [
        ContextChunk(
            chunk_id="atenolol_0",
            title="Atenolol",
            page_number=1,
            text="Atenolol initial dose for hypertension is 50 mg once daily.",
        )
    ]
    messages = build_messages(
        "What is the recommended pediatric dose of amoxicillin for otitis media?",
        irrelevant,
        history=[],
    )
    stream = build(cfg).stream(messages)
    events = list(filter_sentinel(iter([t async for t in stream])))
    kinds = [kind for kind, _ in events]
    assert "declined" in kinds, f"model did not decline; emitted {events[:2]}"
    assert SENTINEL not in "".join(text for kind, text in events if kind == "token")
