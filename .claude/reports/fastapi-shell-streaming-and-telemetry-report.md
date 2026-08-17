# Implementation Report — TICKET-5: FastAPI shell

**Plan**: `.claude/plans/fastapi-shell-streaming-and-telemetry.md`
**Branch**: `feature/fastapi-shell-streaming-and-telemetry` (stacked on `feature/offline-ingestion-job`)
**Status**: COMPLETE

## Summary

`src/rag_api/` composes four tickets' worth of machinery into one streaming endpoint. A question goes in;
`meta` → `token`* → `sources` → `done` comes back as NDJSON, with telemetry split so a refusal explains itself
before the decline text renders.

Verified end to end against real Gemini embeddings and real Groq generation: **TTFT 1099ms** against a 2.5s
p50 budget, answer grounded and cited, refusal at 794ms naming which gate condition failed.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | Scaffold + `api` extra + Vercel entrypoint | `src/rag_api/__init__.py` (CREATE), `pyproject.toml` (UPDATE) |
| 2 | Async sentinel twin | `src/rag_core/sentinel.py` (UPDATE), `tests/unit/test_sentinel_async.py` (CREATE) |
| 3 | Fused scores | `src/rag_core/pipeline.py` (UPDATE) |
| 4 | State + lifespan + manifest check | `src/rag_api/state.py`, `main.py` (CREATE) |
| 5 | Telemetry assembly | `src/rag_api/telemetry.py` (CREATE) |
| 6 | Error mapping | `src/rag_api/errors.py` (CREATE) |
| 7 | The endpoint | `src/rag_api/streaming.py`, `chat.py`, `health.py` (CREATE) |
| 8 | Integration tests | `tests/integration/test_chat_api.py`, `test_chat_api_postgres.py` (CREATE) |
| 9 | Docs | `docs/ARCHITECTURE.md`, `README.md` (UPDATE) |

## Tests added

**433 tests: 360 without a database, 65 Postgres, 8 live. All passing.**

| File | Tests | Note |
|---|---|---|
| `tests/integration/test_chat_api.py` | 33 | Frame order, both timing tests, both decline paths, failures, validation, serviceability |
| `tests/integration/test_chat_api_postgres.py` | 6 | The shell over the real ingested corpus; citations resolving to real pages |
| `tests/unit/test_sentinel_async.py` | 21 | 15 equivalence pairs against the sync original, plus leak and laziness checks |

## Validation results

| Check | Result |
|---|---|
| Default suite (no database, no keys) | **360 passed**, 1.82s |
| Postgres suite (all tickets) | **65 passed**, no regressions |
| Types | `mypy --strict` clean, 33 source files |
| Lint + format | `ruff` clean, 64 files |
| Purity | `rag_core` pulls in no framework or SDK |
| Timing tests | proven to **fail** when the shell buffers |

### End to end, real providers

```
+  603ms  meta     gate ok, similarity_ok, lexical_support, retrieval latency
+ 1075ms  sources  metformin_1 … page 1
+ 1075ms  token    "The adult starting dose of metformin is 500 mg"   ← the 44-char sentinel buffer flushing
+ 1076ms  token    " orally" … 20 more tokens
+ 1105ms  done     ttft_ms=1099, total_tokens=21, provider=llama-3.3-70b-versatile
```

Refusal: `meta` at 794ms with `proceed=false`, `reason=off_domain`, `similarity_ok=false`,
`top_similarity=0.481` — then the decline copy, then `done`. Malformed body → 400. Manifest mismatch → health
`serviceable: false` with the reason, chat → 503.

All six acceptance criteria met.

## Deviations from the plan

**1. Two timing tests, not one — because the planned one was vacuous.** The plan said to use
`httpx.ASGITransport` with `client.stream()`. Measured: **ASGITransport buffers the entire response.** The
first chunk arrives only after the generator finishes, with zero spread between lines. So the in-process test
could not tell streaming from buffering — precisely the trap the ticket exists to avoid. Replaced with:
- `test_the_answer_generator_yields_lazily_rather_than_collecting` — consumes `answer_stream` directly,
  proving the half we own is lazy.
- `test_tokens_reach_a_real_client_progressively` — runs **uvicorn on a real socket** in-process, which is the
  only arrangement that could have caught the bug the local build shipped.

Both were verified to fail: with a deliberately-buffering endpoint the socket test went red (spread 7e-05 vs
required >0.1) while the generator test correctly stayed green, since the buffering was in the endpoint rather
than the generator. They catch different layers.

**2. The lifespan respects a pre-set `app.state.rag`.** Not in the plan, and required: uvicorn runs the
lifespan, which rebuilt state and replaced the injected fakes with a real, unserviceable profile. A pre-set
state is an injected one and the lifespan now leaves it alone.

**3. The async sentinel twin lives in its own test file.** The plan put the equivalence suite in
`test_sentinel.py`. That file is the vendored regression suite and should stay diffable against the local
build, so the new material is in `test_sentinel_async.py` beside it.

**4. `test_a_short_answer_arrives_as_one_token_frame` — a property the plan did not anticipate.** The sentinel
filter buffers 44 characters before it can rule out a refusal, so an answer shorter than that is emitted as a
single frame and TTFT is gated by the buffer rather than by the model's first token. Visible in the real run
above: the first token frame is 46 characters. Worth pinning, since it looks like a streaming bug and is not.

**5. The Postgres decline test induces the decline by raising τ, not by asking an off-domain question.**
`FakeEmbedder` maps every unrecognised term to one shared "unrelated" axis. Over the 71-chunk corpus that
includes continuation pages which never repeat the drug name, so *"what is the capital of France?"* scores a
perfect match against them. That is an artifact of the fake, not the gate — TICKET-4 verified the real
behaviour (0.4813 → refused), and the manual run above reproduces it. The test now proves what it honestly
can: that the decline path composes with real storage, with no model call.

**6. `create_app()` lost its `cfg` parameter.** Superseded by deviation 2 — tests inject state directly.

## Issues encountered

**`httpx.ASGITransport` buffers.** Measured before working around it: first chunk at 0.28s after a 0.25s
generator, zero spread, with both `aiter_lines` and `aiter_raw`. Any in-process streaming assertion is
therefore vacuous. This is the single most useful thing found in this ticket, because the obvious test would
have shipped green and proved nothing.

**A gap worth knowing before TICKET-7.** There is no profile pairing Postgres storage with a fake embedder, so
the API cannot be run locally against the ingested corpus without real keys — `RAG_PROFILE=fake` uses
in-memory stores and correctly reports an empty index. The frontend work will want one. `register_profile` is
already the seam for it; I did not add it because it is scope this ticket did not have.

**Three test expectations were wrong, not the code.** `sources` precedes the first token (matching the local
build — it is emitted the moment stage 2 clears); a short answer coalesces into one frame; and a provider
dying before the sentinel buffer resolves correctly shows *no* partial text, because flushing an undecided
buffer could stream half an `INSUFFICIENT_CONTEXT` to the reader. Each is now a test that documents the
behaviour rather than contradicting it.

## Ready for the next step

All work committed. CI needs a push.

Next: `piv-create-pr`. TICKET-6 (rate limiting, health) and TICKET-7 (frontend) both unblock and are genuinely
parallel — TICKET-7 builds against the frame contract rather than the running app.
