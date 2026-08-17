# Feature: TICKET-5 — FastAPI shell: NDJSON streaming, orchestration, telemetry

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for ported lessons:** `/Users/jsonse/Documents/development/interview/medical-rag/backend/` — referred to as **SRC**. A different repository; read from it, never write to it.

---

## Feature Description

The transport layer. HTTP in, NDJSON frames out, telemetry assembled on the way past.

Everything below this has existed for four tickets and nothing has been able to *call* it. This ticket composes
`retrieve → gate → prompt → generate → sentinel` into one streaming endpoint, and adds the startup check that
makes serving an index built by a different embedding model impossible.

The shell owns transport only. Architecture §3 draws the line precisely: *"if a behaviour would be identical
over gRPC, it belongs in `rag_core`, not here."* That is the test to apply to every function this ticket adds.

## User Story

As a technical evaluator with ten minutes and a phone
I want to ask a clinical question and watch a grounded, cited answer stream back
So that I can judge the system in under a minute — including seeing it refuse, and seeing why.

## Problem Statement

`rag_core` is complete, both stores are real, both providers are real, and the corpus is ingested. There is no
way to reach any of it. The pipeline can only be invoked from a Python REPL or a test.

Downstream: TICKET-6 has no app to rate-limit, TICKET-7 has no endpoint to stream from, and TICKET-10 has
nothing to deploy.

## Solution Statement

`src/rag_api/` — a FastAPI app with one streaming endpoint, one health endpoint, a lifespan that resolves the
profile once and verifies the manifest, and a telemetry assembler.

Three decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **Telemetry splits across `meta` and `done`.** `meta` carries retrieval latency, gate decision, both gate conditions, fused scores. `done` carries TTFT, total tokens, serving provider. | What is known before generation arrives before generation. On a refusal the strip is fully populated *before* the decline text renders, which is what makes PRD §7's "<800ms, fast and visibly so" visible rather than merely true. A refusal is the outcome readers assume is a bug; the strip has to explain itself at exactly that moment. |
| D2 | **Manifest mismatch → stay up, refuse every request with 503.** | Architecture §5 says "refuse to serve, log loudly". On serverless a startup crash is not loud, it is opaque: every invocation returns a platform 500 and the real reason is in logs you have to go find. Staying up to say *which two model ids disagree* refuses just as hard and is diagnosable. |
| D3 | **Streaming is verified locally; Vercel verification belongs to TICKET-10.** | Vercel's docs state the Python runtime supports streaming, which answers the PRD's spike question. Confirming it on the real platform needs a Vercel project, which is TICKET-10's job. This ticket proves progressive delivery under uvicorn with a timing test that fails if tokens arrive batched. |

## Out of Scope / Non-Goals

- **Not included: rate limiting.** PRD F15 and the Upstash counters are TICKET-6. This ticket adds no
  throttling and no per-IP state.
- **Not included: the health check that exercises the secondary provider** (ADR-004 action item 4). TICKET-6.
  This ticket's health endpoint reports serviceability and the manifest, nothing more.
- **Not included: the frontend.** TICKET-7. This ticket defines and emits the frames; nothing renders them.
- **Not included: deployment.** No Vercel project, no `vercel.json` beyond what documents the entrypoint, no
  production `DATABASE_URL`. TICKET-10.
- **Not included: sessions, history or persistence.** PRD §4 non-goal — single-turn only. SRC's `ChatSession`,
  `ChatMessage` and the `finally`-block persistence dance at `chat/views.py:223-255` do **not** port. That is
  the trickiest code in the local shell and none of it is needed; do not recreate it.
- **Not included: re-tuning τ.** The gate uses whatever `config.py` currently holds, still provisional until
  TICKET-8.
- **Not changing:** the vendored port's behaviour, any adapter, or the schema. Two small `rag_core`
  additions are named in Tasks 2 and 3 and justified there.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium-High — the logic is composed rather than invented, but streaming has ordering invariants that only fail in production, and the sync/async sentinel seam needs care.
**Primary Systems Affected**: `src/rag_api/` (new), `src/rag_core/{sentinel,pipeline}.py` (two small additions), `pyproject.toml`, `tests/integration/`
**Dependencies**: `fastapi`, `uvicorn`, `httpx` (test client)

## Related Work

**Implements**: TICKET-5 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`

**Back-references**:

- `.claude/plans/port-rag-core-and-scaffold-repo.md` — defines the frame vocabulary, `Telemetry`, `explain_gate`, and the pipeline this composes.
- `.claude/plans/hosted-provider-adapters-and-failover.md` — `TokenStream.served_by` is what the `done` frame reports; `AllProvidersUnavailable` is what the service-message path catches.
- `.claude/plans/offline-ingestion-job.md` — writes the manifest this checks at startup.

**Forward-references**:

- TICKET-6 — adds rate limiting middleware and extends `/api/health`
- TICKET-7 — consumes these frames; the split telemetry (D1) is the contract it renders
- TICKET-10 — deploys this, and verifies streaming on Vercel for real

**Sequencing:** TICKET-3 (#3) and TICKET-4 (#4) are both open. This needs `TokenStream.served_by` and a
populated index, so branch from `feature/offline-ingestion-job` — the tip of the stack.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

**This repository:**

- `src/rag_core/contracts.py` — Why: `FRAME_TYPES`, `frame()`, `Telemetry` and its `as_dict()`,
  `explain_gate()`, `TokenStream`. **Read `TokenStream`'s docstring**: `served_by` is `None` until the first
  token, which is exactly why it can only be reported on `done`.
- `src/rag_core/pipeline.py` — Why: `retrieve()` and `RetrievalResult`. Note it returns `decision` and
  `chunks` only — Task 3 adds the fused scores the telemetry strip needs.
- `src/rag_core/sentinel.py` — Why: `filter_sentinel` is **sync over a sync iterable** and `TokenStream` is
  async. Task 2 is that seam. Read `_is_sentinel` and the `threshold` line; both are load-bearing.
- `src/rag_core/prompts.py` — Why: `build_messages` (raises on empty chunks — the gate must decline first) and
  `decline_text` (server-authored copy, never the model's).
- `src/rag_core/errors.py` — Why: `AllProvidersUnavailable`, `ProviderUnavailable`, `ProviderProtocolError`,
  `ManifestMismatch`. The last has had no producer until now.
- `src/rag_adapters/profile.py` — Why: `build_profile` is synchronous and `Profile.open()/close()` is the
  lifespan pair. Read the docstring on why construction and connection are separate.
- `src/ingest/run.py` (lines 135-165) — Why: how the manifest is written, so the check knows what to compare.
- `pyproject.toml` — Why: the extras pattern (`postgres`, `providers`), the marker + `addopts` pattern, and
  the vendored-port exclusions. Do not widen the exclusions or the mypy override.
- `tests/integration/test_pipeline_against_postgres.py` — Why: the `pg_dsn` fixture and migrate-then-truncate
  setup this ticket's integration tests reuse.

**Source repository (port the lessons, not the code):**

- `SRC/chat/views.py` (306 lines, **read it all**) — Why: this is the same endpoint, built once already, and
  its comments record four things that only fail in production:
  - **lines 143-151** — retrieval runs *inside* the generator. Outside it, an unreachable provider truncates
    the connection with no valid NDJSON at all, because headers are already committed.
  - **lines 209-212** — `sources` is emitted only once **both** gates have cleared.
  - **lines 279-284** — the ASGI/WSGI streaming trap: a sync generator under ASGI was drained entirely before
    anything was sent, measured at *every token arriving at once at 9.5s versus progressive delivery starting
    at 0.7s*. The mechanism differs here (FastAPI is natively async) but the failure mode is identical and
    invisible to a test that only checks the final body.
  - **lines 223-255** — the persistence `finally` block. **Do not port it.** It exists for sessions this
    profile does not have, and it is the single most subtle piece of code in the local shell.
- `SRC/chat/streaming.py` (12 lines) — Why: `frame()`, already ported into `contracts.py`.
- `SRC/rag/generation.py` lines 64-99 — Why: the buffering `filter_sentinel` that Task 2 gives an async twin.

### New Files to Create

```
src/rag_api/
├── __init__.py
├── main.py            # FastAPI app, lifespan, router registration
├── state.py           # AppState: profile, config, serviceability
├── chat.py            # POST /api/chat
├── health.py          # GET /api/health
├── streaming.py       # frame emission + the generator that owns ordering
├── telemetry.py       # assembling Telemetry from pipeline + stream
└── errors.py          # exception -> (status, code, message)
tests/unit/test_api_errors.py
tests/unit/test_api_telemetry.py
tests/integration/test_chat_api.py       # fakes, no database
tests/integration/test_chat_api_live.py  # postgres-marked, real index
```

Modified: `src/rag_core/sentinel.py` (async twin), `src/rag_core/pipeline.py` (fused scores),
`pyproject.toml`, `README.md`, `docs/ARCHITECTURE.md`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [FastAPI — StreamingResponse](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse)
  - Why: takes an async generator directly, which is what makes the ASGI trap from SRC avoidable here.
- [FastAPI — lifespan events](https://fastapi.tiangolo.com/advanced/events/#lifespan)
  - Specific: the `asynccontextmanager` form with `yield`
  - Why: `Profile.open()` before the yield, `close()` after, and the manifest check in between. This is the
    hook `build_profile`'s sync/async split was designed for.
- [Vercel — Python runtime](https://vercel.com/docs/functions/runtimes/python)
  - Specific: *"Vercel Functions support streaming responses in the Python runtime"*, and the entrypoint rule —
    a top-level variable named `app`, or `[tool.vercel] entrypoint = "module:variable"` for a custom layout
  - Why: this answers the PRD's spike question, and it is why the app object must be reachable as
    `rag_api.main:app` rather than buried behind a factory.
- [Vercel — FastAPI](https://vercel.com/docs/frameworks/backend/fastapi)
  - Specific: `maxDuration` in `vercel.json`, keyed to the resolved entrypoint file
  - Why: TICKET-10 needs it; recording it now stops the layout being rebuilt then.
- [Starlette — testing](https://www.starlette.io/testclient/)
  - Why: `httpx.ASGITransport` is how the streaming tests read frames without binding a port.

### Patterns to Follow

**Module docstrings state the failure the module prevents**, citing `ARCHITECTURE.md §N` or an ADR.

**Comments record measured facts and rejected alternatives.** This ticket has unusually good material: SRC's
9.5s-versus-0.7s streaming measurement, and the reason `sources` is late.

**Guards fail closed and explain themselves.** From `src/rag_core/gate.py`:

```python
# A non-finite similarity must fail CLOSED. NaN compares False against every
# threshold, so without this guard it falls through every check and reaches
# `ok` — the most permissive outcome from the most degenerate input, in the
# one component whose job is to decline when uncertain.
```

**Injected seams over patching.** Every adapter takes its client; the app should take its profile the same
way, so tests construct an app over fakes without monkeypatching module globals.

**Test names are full sentences.** `test_sources_are_not_emitted_until_both_gates_have_cleared`.

**Anti-patterns to avoid:** business logic in the shell (if it would be identical over gRPC it belongs in
`rag_core`); a bare `except Exception`; module-level mutable state for the profile (one process serves
concurrent requests — the same race `TokenStream` was designed around); porting SRC's persistence block;
reading `os.environ` outside `load_config`.

---

## IMPLEMENTATION PLAN

### Phase 1: Close the two seams `rag_core` is missing

**Independent of each other** — different files, could be done in either order.

**Tasks:** 1 (scaffold + deps), 2 (async sentinel twin), 3 (fused scores).

### Phase 2: The shell

**Depends on:** Phase 1.

**Tasks:** 4 (state + lifespan + manifest check), 5 (telemetry), 6 (errors), 7 (the chat endpoint).

### Phase 3: Proof

**Depends on:** Phase 2.

**Tasks:** 8 (integration tests, including the streaming-timing test), 9 (docs).

---

## STEP-BY-STEP TASKS

### 1. CREATE `src/rag_api/` scaffold and add the `api` extra

- **IMPLEMENT**: `src/rag_api/__init__.py`, add `"src/rag_api"` to
  `[tool.hatch.build.targets.wheel] packages`, and an `api` extra with `fastapi>=0.115`, `uvicorn>=0.32`.
  Add `httpx` to the dev group (the ASGI test transport). Record the Vercel entrypoint in `pyproject.toml`:
  `[tool.vercel] entrypoint = "rag_api.main:app"`.
- **PATTERN**: the `postgres` and `providers` extras added in TICKET-2 and TICKET-3, and `src/ingest`'s
  package registration in TICKET-4.
- **GOTCHA**: `dependencies` stays `[]` —
  `tests/unit/test_core_purity.py::test_core_declares_no_runtime_dependencies` asserts it, and the purity
  test's `WEB_FRAMEWORKS` list already contains `fastapi`, `starlette` and `uvicorn`. Adding the extra must
  not make `rag_core` import any of them.
- **GOTCHA**: The app object must be reachable as `rag_api.main:app` — a plain module-level variable, not a
  factory function. Vercel's Python runtime resolves an entrypoint variable, and a `create_app()` factory
  would need a wrapper module later.
- **VALIDATE**: `uv sync && uv run pytest tests/unit/test_core_purity.py -v && uv run python -c "import fastapi, uvicorn, httpx; print('api deps ok')"`
- **SATISFIES**: AC #1

### 2. ADD an async twin to `src/rag_core/sentinel.py`

- **IMPLEMENT**: `async def filter_sentinel_async(deltas: AsyncIterator[str], ...) -> AsyncIterator[tuple[str, str | None]]`,
  reusing `_is_sentinel`, `BUFFER_CHARS`, `PREAMBLE_TOLERANCE` and the identical `threshold` computation. Then
  a test module that drives **both** implementations over the same delta sequences and asserts identical
  output.
- **PATTERN**: `filter_sentinel` immediately above it — same buffer, same threshold, same decision points.
- **GOTCHA**: The sync version stays. It is the vendored port, it carries 15 regression tests, and the
  evaluation harness in TICKET-8 consumes a sync token list. Deleting it to "avoid duplication" would break
  both.
- **GOTCHA**: **The duplication is the risk, so pin it.** Reuse `tests/unit/test_sentinel.py`'s delta
  sequences and assert both implementations produce the same events. If the two ever drift — and the
  preamble-boundary case is exactly where they would — that test fails rather than the shell silently leaking
  a raw `INSUFFICIENT_CONTEXT` to a reader.
- **GOTCHA**: Do not reformat the file. `sentinel.py` is in `[tool.ruff.format] exclude`; add the function and
  leave the rest alone.
- **VALIDATE**: `uv run pytest tests/unit/test_sentinel.py -v` — the 15 existing tests plus the equivalence
  suite, all green.
- **SATISFIES**: AC #3

### 3. ADD fused scores to `RetrievalResult`

- **IMPLEMENT**: `fused_scores: list[float]` on `RetrievalResult`, populated with the scores of the delivered
  `top_ids` in rank order. Empty on a decline.
- **PATTERN**: `pipeline.retrieve()` already computes `fused` — the scores are on `FusedHit.score` and are
  currently discarded after the ids are taken.
- **GOTCHA**: Architecture §3 lists "fused scores" among what the telemetry strip shows, and nothing else can
  produce them — the shell has no access to the ranked lists.
- **GOTCHA**: Worth knowing what this will display: ADR-003 points out RRF is nearly constant, `2/(60+1)` when
  both retrievers agree on first place. Showing it *next to* `top_similarity` is not redundant — it is the
  ADR's argument made visible, which is precisely the sort of thing the strip exists for. Do not "fix" the
  near-constant by normalising it.
- **GOTCHA**: A defaulted field on a frozen dataclass keeps every existing constructor call valid. Check
  `tests/unit/test_pipeline.py` still passes untouched.
- **VALIDATE**: `uv run pytest tests/unit/test_pipeline.py -v`
- **SATISFIES**: AC #4

### 4. CREATE `src/rag_api/state.py` and the lifespan in `main.py`

- **IMPLEMENT**: An `AppState` holding `cfg`, `profile`, and `serviceable: bool` with `reason: str | None`.
  A lifespan that: `build_profile(cfg)` → `await profile.open()` → read the manifest → compare
  `embedder.model_id` and `embedder.dimension` → set serviceability → `yield` → `await profile.close()`.
  Store it on `app.state`, never a module global.
- **PATTERN**: `src/rag_adapters/profile.py`'s `open`/`close` pair, built for exactly this hook.
- **GOTCHA**: **D2 — a mismatch does not raise.** The app starts, `serviceable` is False, and every request
  returns 503 naming *both* model ids. Architecture §5's "refuse to serve, log loudly" is satisfied by
  refusing every request; on serverless a startup crash is opaque rather than loud.
- **GOTCHA**: `read_manifest()` returning `None` (no manifest at all) is **also** unserviceable, and gets a
  distinct message. TICKET-4 writes the manifest last precisely so an interrupted ingest leaves none — the
  whole point is that this check catches it.
- **GOTCHA**: One process serves concurrent requests. Anything mutable that is not `app.state` is the race
  `TokenStream` was designed around.
- **GOTCHA**: The lifespan must not raise if the database is unreachable — log it, mark unserviceable, and let
  `/api/health` say so. A deploy that cannot reach Neon should report that, not crash-loop.
- **VALIDATE**: `uv run pytest tests/integration/test_chat_api.py -v -k "manifest or serviceable"`
- **SATISFIES**: AC #5

### 5. CREATE `src/rag_api/telemetry.py`

- **IMPLEMENT**: Two builders, matching D1's split:
  - `meta_payload(result, cfg, retrieval_ms)` → gate proceed/reason, `similarity_ok` and `lexical_support`
    **separately** via `explain_gate`, `top_similarity`, `fused_scores`, `retrieval_ms`.
  - `done_payload(ttft_ms, total_tokens, served_by, truncated)` → the rest.
  Both go through `Telemetry.as_dict()` so non-finite floats are already handled.
- **PATTERN**: `contracts.Telemetry.as_dict()` and `contracts._jsonable`.
- **GOTCHA**: ADR-003 action item 3 is *"report both conditions separately"*, and the ADR's own words are that
  being able to say **which** condition failed "is worth the second parameter on its own". Never collapse them
  into one confidence number — that collapse is what ADR-003 rejected.
- **GOTCHA**: A NaN `top_similarity` must survive `json.dumps`. `_jsonable` already does this; route through
  it rather than formatting floats by hand.
- **GOTCHA**: TTFT is measured to the first **token** frame, not the first frame — `meta` goes out almost
  immediately and would make the number meaningless.
- **VALIDATE**: `uv run pytest tests/unit/test_api_telemetry.py -v`
- **SATISFIES**: AC #4

### 6. CREATE `src/rag_api/errors.py`

- **IMPLEMENT**: Map exceptions to `(status, code, message)` per Architecture §8:
  | Exception | Pre-stream | Mid-stream |
  |---|---|---|
  | `AllProvidersUnavailable` | 503 | `error` frame, code `all_providers_unavailable` |
  | `ProviderUnavailable` | 503 | `error` frame, code `provider_unavailable` |
  | `ProviderProtocolError` | 502 | `error` frame, code `provider_error` |
  | not serviceable | 503 `index_unavailable` | n/a |
  | bad request body | 400 | n/a |
  Messages are plain language, never a raw exception string, and never a key.
- **PATTERN**: `SRC/chat/views.py:154-159` — it maps to a `code` the client can branch on plus a human
  `message`, and distinguishes a missing model from a dead server because *"that is a different fix for the
  user"*.
- **GOTCHA**: **Once streaming starts, status codes are gone.** The response is already 200 with headers
  committed, so every later failure is an `error` frame followed by `done`. That asymmetry is why this module
  has two columns.
- **GOTCHA**: Never let a provider message reach the client verbatim. Groq's 401 body contains
  `'message': 'Invalid API Key'` — harmless, but the habit is what stops a future message leaking a key or a
  DSN. Log the detail, return the category.
- **VALIDATE**: `uv run pytest tests/unit/test_api_errors.py -v`
- **SATISFIES**: AC #6

### 7. CREATE `src/rag_api/streaming.py` and `chat.py` — the endpoint

- **IMPLEMENT**: `POST /api/chat` taking `{"question": str}`, returning
  `StreamingResponse(..., media_type="application/x-ndjson")` with `Cache-Control: no-cache` and
  `X-Accel-Buffering: no`. The generator, in order:
  1. `meta` frame — telemetry is *not* in it yet; retrieval has not run
  2. **inside the generator**: time and `await retrieve(...)`
  3. emit the real `meta` payload… — see the gotcha below; resolve by emitting `meta` **after** retrieval
  4. gate declined → `token` (from `decline_text`) → `done`. **No model call.**
  5. gate passed → `build_messages` → `generator.stream(...)` → `filter_sentinel_async`
  6. first non-declined token → emit `sources`, then the token
  7. sentinel declined → `token` (decline copy) → `done`
  8. normal end → `done` with the timing payload and `stream.served_by`
- **PATTERN**: `SRC/chat/views.py:140-277` for the ordering, minus every line about sessions.
- **GOTCHA**: **Retrieval runs inside the generator** (`SRC/chat/views.py:143-151`). Outside it, a provider
  failure happens before headers are committed and FastAPI can still return a clean 503 — which sounds
  better, but means the two failure paths behave differently depending on timing. Inside, every failure after
  the response begins is an `error` frame, and the client has exactly one thing to handle.
- **GOTCHA**: `meta` carries the retrieval telemetry (D1), so it must be emitted **after** `retrieve()`
  returns — which means the very first byte does not leave until retrieval completes. That is fine and
  intended: retrieval is budgeted at <400ms p50 and a refusal at <800ms total. Do not emit an empty `meta`
  first "to start the stream"; it doubles the frame count and buys nothing a reader can see.
- **GOTCHA**: **`sources` only after both gates clear** (`SRC/chat/views.py:209-212`). Emitting it at gate-pass
  would show sources for an answer the model then declines to give — citations for a refusal.
- **GOTCHA**: `build_messages` raises `ValueError` on empty chunks, deliberately: *"answering with no retrieved
  context is the failure this whole system exists to prevent."* The gate must have declined first. If that
  ValueError ever escapes, the ordering is wrong — do not catch it, fix the order.
- **GOTCHA**: History is `[]`. Single-turn (PRD §4). `_trim_history` is ported and tested but has no caller
  here.
- **GOTCHA**: A client disconnect cancels the generator. There is nothing to persist and nothing to clean up
  beyond the pool, which the lifespan owns — this is the one place the local build needed 30 lines and this
  one needs none.
- **VALIDATE**: `uv run pytest tests/integration/test_chat_api.py -v`
- **SATISFIES**: AC #1, AC #2, AC #3

### 8. CREATE the integration tests, including the streaming-timing test

- **IMPLEMENT**: `tests/integration/test_chat_api.py` over fakes via `httpx.ASGITransport` — no database, no
  keys, in the default suite. Then `test_chat_api_live.py`, `postgres`-marked, against the real ingested
  index. Cases in the Testing Strategy below.
- **PATTERN**: `tests/integration/test_pipeline_against_postgres.py` for the `pg_dsn` fixture.
- **GOTCHA**: **The timing test is the one that matters and the easy version does not work.** Asserting on the
  final body passes whether tokens streamed or arrived in one burst — which is exactly the bug SRC hit
  (9.5s versus 0.7s, invisible to any body assertion). Use a `FakeGenerator` that sleeps between tokens and
  assert the *first* token frame arrives well before the last, reading the response as it streams rather than
  awaiting the whole thing.
- **GOTCHA**: Assert frame **order**, not just presence. `meta` before any `token`; `sources` after the first
  `token`; exactly one `done`, last.
- **GOTCHA**: `httpx.ASGITransport` needs the streaming request form (`client.stream("POST", ...)`) to see
  frames incrementally. The non-streaming form buffers and would make the timing test vacuous.
- **VALIDATE**: `uv run pytest tests/integration -v` and `uv run pytest -m postgres -v`
- **SATISFIES**: AC #1, AC #2, AC #5, AC #6

### 9. UPDATE docs

- **IMPLEMENT**: README gains a "Running the API" section with the uvicorn command and a `curl` that shows the
  frames. ARCHITECTURE §3 gains the concrete frame sequence and the D1 telemetry split; §8's table gets the
  status codes. Note that the PRD's streaming spike is answered by Vercel's documentation, with real-platform
  verification deferred to TICKET-10.
- **PATTERN**: prior tickets' doc reconciliation — amend and date, never silently rewrite.
- **GOTCHA**: Do not mark PRD §11's spike fully resolved. The embedding half is verified (TICKET-4 produced
  real 768-dim vectors) and the streaming half is verified *locally* and *by documentation* — not on Vercel.
  Say which is which.
- **VALIDATE**: `grep -n "x-ndjson\|meta.*done" README.md docs/ARCHITECTURE.md | head`
- **SATISFIES**: AC #6

---

## TESTING STRATEGY

### Unit Tests

`test_api_telemetry.py` — the two payload builders, the separate gate conditions, NaN survival, TTFT measured
to the first token.

`test_api_errors.py` — the exception→status table, and that no provider message reaches the client verbatim.

`test_sentinel.py` (extended) — the async twin against the sync one over identical inputs.

### Integration Tests

`test_chat_api.py` — fakes, no database, in the default suite:

- a grounded question streams `meta` → `token`* → `sources` → `done`, in that order
- **tokens arrive progressively** (the timing test)
- a gate decline emits the decline copy and **never calls the generator** — asserted on a spy
- a stage-2 sentinel decline never leaks `INSUFFICIENT_CONTEXT` into any `token` frame
- `sources` is absent entirely on both decline paths
- `done` carries `served_by`, TTFT and total tokens
- `meta` carries both gate conditions separately
- an embedding failure mid-request yields a well-formed `error` frame plus `done`, not a truncated body
- `AllProvidersUnavailable` yields the service message, never partial text presented as an answer
- a malformed body returns 400 before any streaming starts
- an unserviceable app returns 503 naming both model ids

`test_chat_api_live.py` — `postgres`-marked, real ingested index: a real question returns real chunks with
citations that resolve, and a real off-domain question refuses.

### Edge Cases

- Empty question, whitespace-only question, missing key, non-object body → 400
- Very long question → still works (no length limit is specified; do not invent one)
- Manifest absent entirely vs manifest present but mismatched → different messages
- Database unreachable at startup → app starts, `/api/health` explains, `/api/chat` 503s
- Client disconnects mid-stream → generator cancelled, pool unharmed
- Model emits the sentinel after a short preamble → decline, no leak
- Model emits zero tokens → `done` with `total_tokens` 0 and `served_by` set or null, not a crash
- Two concurrent requests → each gets its own `served_by`, no cross-talk

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

### Level 2: Unit Tests

```bash
uv run pytest tests/unit -v
```

### Level 3: Integration Tests

```bash
uv run pytest -q            # no database, no keys — must stay green

docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
RAG_PROFILE=fake uv run python -m ingest.run
uv run pytest -m postgres -v
```

### Level 4: Manual Validation

```bash
# Purity holds with a web framework installed
uv run python -c "
import sys, rag_core.pipeline, rag_core.ports, rag_core.contracts
loaded = {m.split('.')[0] for m in sys.modules}
for banned in ('fastapi','starlette','uvicorn','google','groq','asyncpg'):
    assert banned not in loaded, f'{banned} pulled in by rag_core'
print('rag_core still pulls in no framework')
"

# The app is reachable at the Vercel entrypoint path
uv run python -c "from rag_api.main import app; print('entrypoint ok:', type(app).__name__)"

# Run it, against the fake profile
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
RAG_PROFILE=fake uv run uvicorn rag_api.main:app --port 8000 &
sleep 3

# Health
curl -s localhost:8000/api/health | python3 -m json.tool

# A grounded question — frames should arrive progressively, not in one burst
curl -N -s -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What is the adult starting dose of metformin?"}' \
  | while IFS= read -r line; do printf '%s  %s\n' "$(date +%H:%M:%S.%N | cut -c1-12)" "${line:0:110}"; done

# A refusal — telemetry must be populated on `meta`, before the decline text
curl -N -s -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What is the capital of France?"}' | head -4

# Malformed body
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' -d '{}'      # expect 400

# Manifest mismatch refuses to serve
EMBED_DIMENSIONS=384 RAG_PROFILE=fake uv run uvicorn rag_api.main:app --port 8001 &
sleep 3
curl -s localhost:8001/api/health | python3 -m json.tool     # unserviceable, names both
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/api/chat \
  -H 'content-type: application/json' -d '{"question":"hi"}'  # expect 503

kill %1 %2 2>/dev/null
docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
git diff --cached | grep -inE "AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}|postgres(ql)?://[^ ]*:[^ ]*@" | grep -v localhost || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-5, plus the standard bar:

- [ ] **AC #1** — A question against fakes streams `meta` → `token`* → `sources` → `done`, in that order, **progressively**
- [ ] **AC #2** — A stage-1 decline returns copy with zero calls to the generation port, asserted on a spy
- [ ] **AC #3** — A stage-2 sentinel decline never leaks the raw `INSUFFICIENT_CONTEXT` token to the stream
- [ ] **AC #4** — Every response carries telemetry with both gate conditions reported independently
- [ ] **AC #5** — Manifest mismatch → the service refuses every request with 503 naming both model ids, and logs it
- [ ] **AC #6** — Embedding-provider failure mid-request → a well-formed `error` frame plus `done`, never a truncated body
- [ ] All validation commands pass with zero errors
- [ ] `mypy --strict` clean; vendored-port exclusions and mypy override unchanged
- [ ] `uv run pytest` stays green with no database and no keys
- [ ] `rag_core` still imports no web framework — `test_core_purity` unchanged and passing
- [ ] No secret in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 9 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full suite green with and without a database
- [ ] Prior tickets' suites unchanged
- [ ] CI green
- [ ] Acceptance criteria all met
- [ ] TICKET-6 has an app to add middleware to; TICKET-7 has frames to render

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 telemetry split across `meta` and `done`; D2 manifest
mismatch stays up and 503s; D3 streaming verified locally, Vercel verification with TICKET-10.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — `src/rag_api/`, named for symmetry with `rag_core` / `rag_adapters` rather than a bare `api`,
   which is generic enough to collide.
2. **Assumed** — the async sentinel is a **twin**, not a replacement. The sync one is vendored, carries 15
   regression tests, and TICKET-8's harness consumes a sync token list. The duplication is pinned by an
   equivalence test.
3. **Assumed** — `RetrievalResult` gains `fused_scores`. Architecture §3 lists it in the telemetry strip and
   nothing outside the pipeline can produce it.
4. **Assumed** — `meta` is emitted *after* retrieval, so the first byte waits on it. Retrieval is budgeted at
   <400ms p50; an empty `meta` first would double the frame count for nothing observable.
5. **Assumed** — CORS is configured permissively for local development and left for TICKET-7/10 to tighten
   once the frontend's origin exists. Flag if the frontend will be same-origin, in which case none is needed.
6. **Assumed** — no request size or question length limit. None is specified anywhere, and inventing one is a
   product decision.
7. **Assumed** — `/api/health` here reports serviceability, the manifest and store reachability. ADR-004's
   scheduled secondary exercise stays with TICKET-6.

---

## NOTES (open canvas)

### The lesson SRC paid for, restated

`SRC/chat/views.py:279-284` is the most valuable comment in the source repository:

> Served under WSGI. StreamingHttpResponse cannot async-iterate a SYNC generator, so under ASGI Django drains
> the whole generator in a threadpool before sending anything — measured: every token arriving at once at 9.5s
> versus progressive delivery starting at 0.7s under WSGI.

FastAPI takes an async generator natively, so the specific trap does not apply. The *class* of trap absolutely
does: buffering anywhere in the chain — the framework, a proxy, a test client that reads the whole body —
turns a streaming demo into a slow non-streaming one, and **every functional assertion still passes**. The
final body is byte-identical either way.

That is why Task 8's timing test is not optional garnish. It is the only test in the suite that can fail for
this reason, and the failure mode it guards is the one that would be discovered by a reviewer watching the
demo rather than by CI.

### Why telemetry splits

The refusal path is the thing PRD G5 wants discoverable in under a minute, and §7 budgets it at <800ms
"fast and visibly so". Putting all telemetry on `done` would mean the strip is blank for the entire time the
reader is deciding whether the refusal is deliberate or a bug — and a refusal with a blank telemetry strip
looks exactly like an error.

With the split, a decline emits: `meta` (gate reason, both conditions, top similarity, retrieval latency) →
`token` (the copy) → `done`. The reader sees *why* before they see *what*, which inverts the usual ordering
and is the right way round for this specific product.

### What the shell must not become

Architecture §3's test — "if a behaviour would be identical over gRPC, it belongs in `rag_core`" — is easy to
state and easy to erode. The pressure points in this ticket:

| Tempting to put in the shell | Belongs in |
|---|---|
| The decline copy | `rag_core/prompts.py` — already there, server-authored |
| The sentinel buffering | `rag_core/sentinel.py` — Task 2 keeps it there |
| Deciding which gate condition failed | `rag_core/contracts.explain_gate` — already there |
| Frame construction | `rag_core/contracts.frame` — already there |
| Ordering of frames | **the shell.** This is genuinely transport. |
| Timing measurement | **the shell.** Latency is an HTTP concern. |

If a task in this ticket seems to need new domain logic, that is a signal the logic belongs one layer down.

### Alternatives weighed and rejected

**Making `filter_sentinel` itself async.** One implementation, no duplication, no equivalence test. Rejected:
it is a vendored port whose 15 regression tests are the parity harness, and TICKET-8's eval harness feeds it a
sync list of collected tokens.

**A `telemetry` frame type.** Cleanest conceptually — telemetry emitted whenever a value is known. Rejected in
D1: `FRAME_TYPES` is the one contract the shell and the frontend agreed on early precisely so TICKET-7 could
be built in parallel, and widening it now spends that.

**Raising at startup on a manifest mismatch.** Louder in a single-process world. Rejected in D2: on serverless
it produces an opaque platform 500 per invocation with the actual reason buried in logs.

**Returning a non-streaming JSON response when the gate declines.** The refusal has no tokens to stream, so a
plain JSON body is arguably simpler. Rejected: the client would need two response shapes and two parsers, and
the refusal path is the one that most needs to look deliberate rather than special-cased.

### Sequencing

Both #3 and #4 are open; branch from `feature/offline-ingestion-job`. After this lands, TICKET-6 (rate
limiting, health) and TICKET-7 (frontend) are genuinely parallel — different files, and TICKET-7 works against
the frame contract rather than the running app.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
