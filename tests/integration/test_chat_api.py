"""The API shell over fakes. No database, no keys — in the default suite.

The timing test is the one that matters. Asserting on the final body passes
whether tokens streamed or arrived in one burst, which is exactly the bug the
local build hit under ASGI: every token at 9.5s versus progressive delivery
starting at 0.7s, with a byte-identical body either way.
"""

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from rag_adapters.fakes import (
    ExplodingEmbedder,
    FakeDenseStore,
    FakeEmbedder,
    FakeGenerator,
    FakeLexicalStore,
)
from rag_adapters.profile import Profile
from rag_api.main import create_app
from rag_api.state import AppState
from rag_core.config import load_config
from rag_core.contracts import Chunk, EmbeddedChunk, IndexManifest
from rag_core.errors import AllProvidersUnavailable, ProviderUnavailable
from rag_core.prompts import SENTINEL

CORPUS = [
    Chunk(
        id="metformin_0",
        document_id="metformin",
        ordinal=0,
        anchor="1",
        content="Metformin adult starting dose is 500 mg twice daily with meals.",
        document_title="Metformin",
    ),
    Chunk(
        id="metformin_1",
        document_id="metformin",
        ordinal=1,
        anchor="2",
        content="Metformin is contraindicated in severe renal impairment.",
        document_title="Metformin",
    ),
]

GROUNDED = "What is the metformin starting dose?"
OFF_DOMAIN = "What is the capital of France?"


async def build_state(*, generator=None, embedder=None, serviceable=True, cfg=None):
    embedder = embedder or FakeEmbedder()
    dense = FakeDenseStore()
    lexical = FakeLexicalStore()
    vectors = await FakeEmbedder().embed_documents([c.content for c in CORPUS])
    await dense.upsert(
        [EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(CORPUS, vectors, strict=True)]
    )
    await lexical.index(list(CORPUS))

    base = cfg or load_config(env={})
    # The carried-over thresholds are still provisional (TICKET-8), so tests
    # that inspect what was DELIVERED must not be gated by them.
    permissive = replace(base, gate=replace(base.gate, tau_abstain=-1.0, tau_strong=-1.0))
    profile = Profile(
        name="fake",
        embedder=embedder,
        generator=generator or FakeGenerator(),
        dense=dense,
        lexical=lexical,
    )
    state = AppState(cfg=permissive, profile=profile, serviceable=serviceable)
    if not serviceable:
        state.reason = "index was built by 'other-model' but this service is configured with 'fake'"
    return state


def app_for(state):
    app = create_app()
    app.state.rag = state
    return app


async def frames_for(state, question=GROUNDED):
    """POST and parse every NDJSON frame."""
    app = app_for(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/chat", json={"question": question})
        assert response.status_code == 200, response.text
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def kinds(frames):
    return [f["type"] for f in frames]


def first(frames, kind):
    return next(f for f in frames if f["type"] == kind)


# --- the happy path ------------------------------------------------------


async def test_a_grounded_question_streams_meta_tokens_sources_done():
    frames = await frames_for(await build_state())
    order = kinds(frames)
    assert order[0] == "meta", "meta must lead"
    assert order[-1] == "done", "done must be last"
    assert order.count("done") == 1
    assert "sources" in order
    assert "token" in order


async def test_sources_are_not_emitted_until_both_gates_have_cleared():
    """Emitted at the moment stage 2 clears — which is when the sentinel filter
    produces a non-refusal — and therefore immediately before the first token,
    never at stage-1 gate-pass. Emitting earlier would show citations for an
    answer the model then declines to give."""
    order = kinds(await frames_for(await build_state()))
    assert order.index("sources") == order.index("token") - 1, (
        f"sources must sit immediately before the first token, got {order}"
    )
    assert order.index("meta") < order.index("sources")


async def test_a_short_answer_arrives_as_one_token_frame():
    """A real property of the sentinel filter, worth pinning: it buffers until
    it holds enough characters to rule out a refusal (a tolerated preamble plus
    the full sentinel, 44 characters). An answer shorter than that is decided
    at end-of-stream and emitted in one frame.

    That is the latency the local build called "a few tokens ... imperceptible"
    — and it means time-to-first-token is gated by the buffer, not by the
    model's first token."""
    state = await build_state(generator=FakeGenerator(tokens=["short ", "answer."]))
    frames = await frames_for(state)
    assert [f["type"] for f in frames].count("token") == 1
    assert first(frames, "token")["text"] == "short answer."


async def test_the_answer_text_reassembles_from_the_token_frames():
    frames = await frames_for(await build_state())
    text = "".join(f["text"] for f in frames if f["type"] == "token")
    assert text == "A grounded answer [1]."


async def test_sources_carry_what_a_citation_needs():
    sources = first(await frames_for(await build_state()), "sources")["items"]
    assert sources
    for item in sources:
        assert item["chunk_id"]
        assert item["title"]
        assert isinstance(item["page"], int)
        assert item["snippet"]


# --- the timing tests ----------------------------------------------------
#
# httpx.ASGITransport BUFFERS the whole response: measured, first chunk arrives
# only after the generator finishes, with zero spread between lines. So an
# in-process client cannot tell streaming from buffering — it is precisely the
# vacuous test this ticket exists to avoid writing.
#
# Two tests instead. The first proves OUR generator is lazy. The second runs a
# real uvicorn over a real socket, which is the only arrangement that would
# have caught the bug the local build shipped.


def slow_generator(delay=0.05):
    """Emits enough text to clear the 44-character sentinel buffer early, then
    keeps going — so any token after the first is evidence of streaming."""

    class SlowGenerator(FakeGenerator):
        def stream(self, messages):
            from rag_core.contracts import TokenStream

            async def gen():
                for token in [
                    "The adult starting dose of metformin is 500 mg twice daily with meals. ",
                    "Increase gradually. ",
                    "Assess renal function first. ",
                    "Contraindicated below eGFR 30. ",
                    "See the label for details.",
                ]:
                    await asyncio.sleep(delay)
                    yield token

            return TokenStream(gen(), model_id=self.model_id)

    return SlowGenerator()


async def test_the_answer_generator_yields_lazily_rather_than_collecting():
    """The half we own. If answer_stream materialised its frames before
    returning, every functional assertion would still pass and the demo would
    not stream."""
    from rag_api.streaming import answer_stream

    state = await build_state(generator=slow_generator())
    started = time.perf_counter()
    arrivals = []
    async for raw in answer_stream(GROUNDED, state):
        arrivals.append((time.perf_counter() - started, json.loads(raw)["type"]))

    token_times = [t for t, kind in arrivals if kind == "token"]
    assert len(token_times) >= 4, f"expected several token frames, saw {len(token_times)}"
    spread = token_times[-1] - token_times[0]
    assert spread > 0.1, (
        f"token frames were produced within {spread:.3f}s across a 0.25s generator — "
        "answer_stream collected instead of yielding"
    )


async def test_tokens_reach_a_real_client_progressively():
    """End to end over a real socket, because that is the only place the bug
    lives. The local build measured every token arriving at once at 9.5s under
    ASGI versus progressive delivery from 0.7s under WSGI — with a
    byte-identical body either way."""
    import uvicorn

    state = await build_state(generator=slow_generator())
    app = app_for(state)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]

        arrivals = []
        started = time.perf_counter()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with client.stream("POST", "/api/chat", json={"question": GROUNDED}) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.strip():
                        arrivals.append((time.perf_counter() - started, json.loads(line)["type"]))
    finally:
        server.should_exit = True
        await task

    token_times = [t for t, kind in arrivals if kind == "token"]
    assert len(token_times) >= 4, f"expected several token frames, saw {len(token_times)}"
    spread = token_times[-1] - token_times[0]
    assert spread > 0.1, (
        f"tokens arrived within {spread:.3f}s of each other across a 0.25s generator — "
        "the response was buffered somewhere between the generator and the socket"
    )


# --- stage 1: the gate ---------------------------------------------------


async def test_a_gate_decline_never_calls_the_generator():
    """PRD F10: on gate failure, no language model is called. The spy is what
    makes that checkable rather than assumed."""
    generator = FakeGenerator()
    cfg = load_config(env={})  # real thresholds, so the gate actually declines
    state = await build_state(generator=generator, cfg=cfg)
    state.cfg = cfg

    frames = await frames_for(state, question=OFF_DOMAIN)
    assert generator.calls == 0, "the model was called for a question the gate declined"
    done = first(frames, "done")
    assert done["was_declined"] is True
    assert done["decline_reason"] == "off_domain"


async def test_a_gate_decline_emits_no_sources():
    cfg = load_config(env={})
    state = await build_state(cfg=cfg)
    state.cfg = cfg
    assert "sources" not in kinds(await frames_for(state, question=OFF_DOMAIN))


async def test_a_gate_decline_returns_server_authored_copy():
    cfg = load_config(env={})
    state = await build_state(cfg=cfg)
    state.cfg = cfg
    frames = await frames_for(state, question=OFF_DOMAIN)
    text = "".join(f["text"] for f in frames if f["type"] == "token")
    assert "corpus" in text.lower()
    assert "upload" not in text.lower()


async def test_the_refusal_telemetry_arrives_before_the_refusal_text():
    """The reason the telemetry splits. A refusal is the outcome readers assume
    is a bug, so the strip has to explain itself at that moment, not after."""
    cfg = load_config(env={})
    state = await build_state(cfg=cfg)
    state.cfg = cfg
    frames = await frames_for(state, question=OFF_DOMAIN)

    order = kinds(frames)
    assert order.index("meta") < order.index("token")
    gate = first(frames, "meta")["telemetry"]["gate"]
    assert gate["proceed"] is False
    assert gate["reason"] == "off_domain"


# --- stage 2: the sentinel -----------------------------------------------


async def test_a_sentinel_decline_never_leaks_the_raw_token():
    state = await build_state(generator=FakeGenerator(tokens=[SENTINEL]))
    frames = await frames_for(state)
    text = "".join(f["text"] for f in frames if f["type"] == "token")
    assert SENTINEL not in text
    assert first(frames, "done")["decline_reason"] == "insufficient_context"


async def test_a_sentinel_decline_emits_no_sources():
    """There is no answer to cite."""
    state = await build_state(generator=FakeGenerator(tokens=[SENTINEL]))
    assert "sources" not in kinds(await frames_for(state))


async def test_a_sentinel_decline_still_reports_which_model_refused():
    state = await build_state(generator=FakeGenerator(tokens=[SENTINEL]))
    done = first(await frames_for(state), "done")
    assert done["telemetry"]["provider"] == "fake-generator-001"


# --- telemetry -----------------------------------------------------------


async def test_meta_reports_both_gate_conditions_separately():
    """ADR-003 chose a two-condition gate so telemetry could say WHICH failed."""
    gate = first(await frames_for(await build_state()), "meta")["telemetry"]["gate"]
    assert "similarity_ok" in gate
    assert "lexical_support" in gate
    assert isinstance(gate["similarity_ok"], bool)
    assert isinstance(gate["lexical_support"], bool)


async def test_meta_reports_retrieval_latency_and_fused_scores():
    telemetry = first(await frames_for(await build_state()), "meta")["telemetry"]
    assert telemetry["latency"]["retrieval_ms"] > 0
    assert telemetry["fused_scores"], "the strip shows fused scores (ARCHITECTURE.md §3)"


async def test_done_reports_timing_tokens_and_the_serving_provider():
    telemetry = first(await frames_for(await build_state()), "done")["telemetry"]
    assert telemetry["latency"]["ttft_ms"] > 0
    assert telemetry["total_tokens"] > 0
    assert telemetry["provider"] == "fake-generator-001"
    assert telemetry["truncated"] is False


async def test_every_done_frame_carries_the_same_keys():
    """One frame type, one shape (TICKET-7 D4).

    The retrieval-failure path used to emit a bare
    `{"type": "done", "truncated": true}` while the other three carried
    `telemetry`, `was_declined` and `decline_reason`. A client cannot know
    which shape it is about to get, so it special-cases the odd one forever —
    and every future client inherits the special case. The four `done` sites
    are enumerated here so a fifth cannot be added in a different shape.
    """
    paths = {
        "answer": await build_state(),
        "stage-1 decline": await build_state(),
        "stage-2 decline": await build_state(generator=FakeGenerator(tokens=[SENTINEL])),
        "mid-stream failure": await build_state(generator=dying_generator("x" * 60)),
        "retrieval failure": await build_state(embedder=ExplodingEmbedder()),
    }
    off_domain = load_config(env={})
    paths["stage-1 decline"].cfg = replace(
        off_domain, gate=replace(off_domain.gate, tau_abstain=0.9)
    )

    # What proves each path was actually taken. Without these the test passes
    # while silently exercising the answer path five times.
    took = {
        "answer": lambda f: "sources" in kinds(f),
        "stage-1 decline": lambda f: first(f, "done")["decline_reason"] == "off_domain",
        "stage-2 decline": lambda f: first(f, "done")["decline_reason"] == "insufficient_context",
        "mid-stream failure": lambda f: "error" in kinds(f) and "sources" in kinds(f),
        "retrieval failure": lambda f: kinds(f) == ["error", "done"],
    }

    expected = {"type", "telemetry", "was_declined", "decline_reason"}
    for name, state in paths.items():
        question = OFF_DOMAIN if name == "stage-1 decline" else GROUNDED
        frames = await frames_for(state, question=question)
        assert took[name](frames), f"the {name} path was not exercised: {kinds(frames)}"
        assert set(first(frames, "done")) == expected, (
            f"the {name} path emits a different done frame: {set(first(frames, 'done'))}"
        )


async def test_the_retrieval_failure_done_frame_reports_truncation_in_telemetry():
    """Where every other path reports it. Before D4 it sat at the top level,
    so a client reading `done.telemetry.truncated` saw `false` on the one path
    where the answer was most definitely incomplete."""
    frames = await frames_for(await build_state(embedder=ExplodingEmbedder()))
    done = first(frames, "done")
    assert done["telemetry"]["truncated"] is True
    assert done["telemetry"]["total_tokens"] == 0
    assert done["telemetry"]["provider"] is None
    assert done["was_declined"] is False, "a failure is not a refusal"
    assert done["decline_reason"] is None


async def test_every_frame_is_valid_json_on_its_own_line():
    """The whole point of NDJSON. An answer containing a newline must not split
    a frame."""
    state = await build_state(generator=FakeGenerator(tokens=["line one\nline two"]))
    app = app_for(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/chat", json={"question": GROUNDED})
    lines = [line for line in response.text.splitlines() if line.strip()]
    for line in lines:
        json.loads(line)  # raises if a frame was split
    text = "".join(json.loads(x)["text"] for x in lines if json.loads(x)["type"] == "token")
    assert text == "line one\nline two"


# --- failures ------------------------------------------------------------


async def test_an_embedding_failure_yields_a_frame_not_a_truncated_body():
    """Retrieval runs inside the generator precisely so this is a frame."""
    state = await build_state(embedder=ExplodingEmbedder())
    frames = await frames_for(state)
    assert kinds(frames) == ["error", "done"]
    assert frames[0]["code"] == "provider_unavailable"
    assert frames[1]["telemetry"]["truncated"] is True


async def test_all_providers_unavailable_returns_the_service_message():
    """PRD F16: never an ungrounded answer."""
    state = await build_state(
        generator=FakeGenerator(fail_with=AllProvidersUnavailable("both down"))
    )
    frames = await frames_for(state)
    error = first(frames, "error")
    assert error["code"] == "all_providers_unavailable"
    text = "".join(f.get("text", "") for f in frames if f["type"] == "token")
    assert text == "", "no partial text may be presented as an answer"


def dying_generator(prefix: str):
    class DyingGenerator(FakeGenerator):
        def stream(self, messages):
            from rag_core.contracts import TokenStream

            async def gen():
                yield prefix
                raise ProviderUnavailable("died mid-stream")

            return TokenStream(gen(), model_id=self.model_id)

    return DyingGenerator()


async def test_a_failure_after_tokens_were_emitted_keeps_them_and_marks_truncated():
    """Past the sentinel buffer the provider is committed and text is on the
    wire, so what was emitted stays emitted rather than being retracted."""
    long_enough = "The adult starting dose of metformin is 500 mg twice daily with meals. "
    frames = await frames_for(await build_state(generator=dying_generator(long_enough)))
    text = "".join(f["text"] for f in frames if f["type"] == "token")
    assert text == long_enough, "emitted text stays emitted"
    assert first(frames, "error")["code"] == "provider_unavailable"
    assert first(frames, "done")["telemetry"]["truncated"] is True


async def test_a_failure_before_the_buffer_resolves_shows_no_partial_text():
    """The sentinel filter has not yet decided whether those characters are a
    refusal, so it cannot emit them — flushing on error would risk streaming
    half an INSUFFICIENT_CONTEXT to the reader. The error frame and the
    truncated flag are what tell them the answer is incomplete."""
    frames = await frames_for(await build_state(generator=dying_generator("The dose is ")))
    text = "".join(f["text"] for f in frames if f["type"] == "token")
    assert text == "", "an undecided buffer must not be flushed"
    assert first(frames, "error")["code"] == "provider_unavailable"
    assert first(frames, "done")["telemetry"]["truncated"] is True
    assert "sources" not in kinds(frames), "no answer, no citations"


async def test_no_provider_message_reaches_the_client_verbatim():
    state = await build_state(
        generator=FakeGenerator(fail_with=ProviderUnavailable("Invalid API Key sk-secret123"))
    )
    frames = await frames_for(state)
    assert "sk-secret123" not in json.dumps(frames)
    assert "Invalid API Key" not in json.dumps(frames)


# --- request validation --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "   "}, {"question": 42}, {"question": None}, []],
    ids=["missing", "empty", "whitespace", "non-string", "null", "not-an-object"],
)
async def test_a_malformed_body_is_rejected_before_streaming_starts(body):
    app = app_for(await build_state())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/chat", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


async def test_invalid_json_is_rejected():
    app = app_for(await build_state())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/chat", content=b"{not json", headers={"content-type": "application/json"}
        )
    assert response.status_code == 400


# --- serviceability ------------------------------------------------------


async def test_an_unserviceable_deployment_refuses_every_query():
    """ARCHITECTURE.md §5: refuse to serve. Refusing every request is that,
    and stays diagnosable where a startup crash would not."""
    app = app_for(await build_state(serviceable=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/chat", json={"question": GROUNDED})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "index_unavailable"
    assert "other-model" in body["message"] and "fake" in body["message"], (
        "the message must name both disagreeing model ids"
    )


async def test_health_reports_serviceability_and_the_index():
    state = await build_state()
    state.manifest = IndexManifest("fake-embed-001", 768, __import__("datetime").datetime.now())
    app = app_for(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["serviceable"] is True
    assert body["embedder"]["configured"] == "fake-embed-001"
    assert body["index"]["built_by"] == "fake-embed-001"


async def test_health_says_why_when_unserviceable():
    """A health check reporting a capability present when it is not converts a
    clear failure into an unexplained one later."""
    app = app_for(await build_state(serviceable=False))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["serviceable"] is False
    assert response.json()["reason"]
