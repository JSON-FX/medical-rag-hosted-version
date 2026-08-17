"""The shell against the real ingested corpus.

Everything else in this suite runs over in-memory fakes, which proves the
orchestration. This proves the whole stack composes: real chunks out of
Postgres, real citations that resolve to real pages, and a manifest check
against a manifest the ingest job actually wrote.

The embedder is still fake — a real one costs quota and belongs in the `live`
suite. What is real here is the storage, the corpus and the manifest.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

from ingest.corpus import load_manifest, paginate_document
from ingest.run import ingest_all
from rag_adapters.fakes import FakeEmbedder, FakeGenerator
from rag_adapters.postgres import PostgresDenseStore, PostgresLexicalStore, PostgresPool
from rag_adapters.profile import Profile
from rag_api.main import create_app
from rag_api.state import AppState, check_manifest
from rag_core.config import load_config
from rag_core.contracts import IndexManifest

pytestmark = pytest.mark.postgres

CFG = load_config(env={})


@pytest.fixture
async def ingested_state(pg_dsn):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
    from migrate import apply_pending

    await apply_pending(pg_dsn)
    pool = PostgresPool(dsn=pg_dsn, min_size=1, max_size=5)
    await pool.open()
    async with pool.pool.acquire() as conn:
        await conn.execute("truncate document, chunk, index_manifest restart identity cascade")

    embedder = FakeEmbedder()
    await ingest_all(pool, embedder, CFG)

    profile = Profile(
        name="hosted",
        embedder=embedder,
        generator=FakeGenerator(),
        dense=PostgresDenseStore(pool),
        lexical=PostgresLexicalStore(pool),
        resources=(pool,),
    )
    state = AppState(cfg=CFG, profile=profile)
    check_manifest(state, await profile.dense.read_manifest())
    try:
        yield state
    finally:
        await pool.close()


async def post(state, question):
    app = create_app()
    app.state.rag = state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/chat", json={"question": question})
    return response, [json.loads(x) for x in response.text.splitlines() if x.strip()]


def first(frames, kind):
    return next((f for f in frames if f["type"] == kind), None)


async def test_the_ingested_manifest_makes_the_deployment_serviceable(ingested_state):
    """The ingest job wrote it; the startup check reads it; they agree."""
    assert ingested_state.serviceable is True
    assert ingested_state.reason is None
    assert ingested_state.manifest is not None
    assert ingested_state.manifest.embedding_model_id == "fake-embed-001"


async def test_a_grounded_question_returns_real_chunks_from_postgres(ingested_state):
    response, frames = await post(ingested_state, "What is the adult starting dose of metformin?")
    assert response.status_code == 200
    sources = first(frames, "sources")
    assert sources is not None, "a grounded question should reach stage 2"
    assert any("metformin" in item["chunk_id"] for item in sources["items"])


async def test_every_citation_resolves_to_the_page_it_names(ingested_state):
    """The end of the chain that started in TICKET-4: a chunk's anchor is the
    page its text came from, and the shell hands that page to the reader."""
    _, frames = await post(ingested_state, "What is the adult starting dose of metformin?")
    sources = first(frames, "sources")
    assert sources is not None

    for item in sources["items"]:
        slug = item["chunk_id"].rsplit("_", 1)[0]
        pages = {p.page_number: p.text for p in paginate_document(slug)}
        assert item["page"] in pages, f"{item['chunk_id']} cites a page that does not exist"
        # The snippet is the chunk's head, which overlap may have taken from the
        # previous chunk on the same page — so check a distinctive interior run.
        needle = item["snippet"][60:140].strip()
        if needle:
            assert needle in pages[item["page"]], f"{item['chunk_id']} text is not on its page"


async def test_the_decline_path_works_against_real_storage(ingested_state):
    """Induces the decline by raising tau above anything reachable, rather than
    by asking an off-domain question.

    FakeEmbedder maps every unrecognised term to one shared "unrelated" axis.
    Over a 71-chunk corpus that includes continuation pages which never repeat
    the drug name, those chunks land on that same axis — so "what is the capital
    of France?" scores a perfect match against them. That is an artifact of the
    fake, not of the gate: TICKET-4 verified the real behaviour against real
    Gemini vectors, where the same question scored 0.4813 and was refused.

    What this test can honestly prove is that the decline path composes with
    real storage: no sources, the right frames, no model call."""
    from dataclasses import replace as replace_dc

    ingested_state.cfg = replace_dc(
        CFG, gate=replace_dc(CFG.gate, tau_abstain=1.01, tau_strong=1.01)
    )
    generator = ingested_state.profile.generator

    _, frames = await post(ingested_state, "What is the adult starting dose of metformin?")
    done = first(frames, "done")
    assert done["was_declined"] is True
    assert done["decline_reason"] == "off_domain"
    assert first(frames, "sources") is None
    assert generator.calls == 0, "no model call on a stage-1 decline"

    meta = first(frames, "meta")["telemetry"]
    assert meta["gate"]["proceed"] is False
    assert meta["gate"]["similarity_ok"] is False
    assert meta["latency"]["retrieval_ms"] > 0


async def test_the_corpus_covers_all_three_documents(ingested_state):
    """A sanity check that the shell is reading the whole index rather than one
    document that happened to be first."""
    seen = set()
    for slug in sorted(load_manifest()["drugs"]):
        _, frames = await post(ingested_state, f"What is the {slug} dose?")
        sources = first(frames, "sources")
        if sources:
            seen.update(item["chunk_id"].rsplit("_", 1)[0] for item in sources["items"])
    assert seen == set(load_manifest()["drugs"])


async def test_a_manifest_from_a_different_model_refuses_every_query(ingested_state):
    """The failure this check exists for: an index built by one embedder,
    queried by another, returns plausible-looking garbage."""
    await ingested_state.profile.dense.write_manifest(
        IndexManifest("some-other-embedder", 768, ingested_state.manifest.ingested_at)
    )
    check_manifest(ingested_state, await ingested_state.profile.dense.read_manifest())
    assert ingested_state.serviceable is False

    response, _ = await post(ingested_state, "What is the metformin dose?")
    assert response.status_code == 503
    body = response.json()
    assert "some-other-embedder" in body["message"]
    assert "fake-embed-001" in body["message"]
