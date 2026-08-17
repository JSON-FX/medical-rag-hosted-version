# Feature: TICKET-3 — Hosted provider adapters and the failover chain

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for ported logic:** `/Users/jsonse/Documents/development/interview/medical-rag/backend/` — referred to as **SRC**. A different repository; read from it, never write to it.

---

## Feature Description

The two remaining ports get real implementations: Gemini for embeddings, and Groq plus Gemini for generation
behind a failover chain that survives one provider going down or changing its quota without notice.

This is the ticket that makes the demo's *inference* real. TICKET-2 made its storage real. After this,
everything below the API shell is production code and the `fake` profile becomes purely a test convenience.

ADR-004's argument is the whole justification: free inference tiers are rate-limited, carry no SLA, and change
terms without notice. A single-provider demo is "a demo with an expiry date nobody told you about." Two
independent providers, with the serving one reported in every response, converts failover from hidden
infrastructure into a visible design decision — which is the outcome worth having.

## User Story

As a technical evaluator clicking a link months after it was built
I want the system to answer even when one inference provider is rate-limiting or down
So that I see a working demo rather than an error page, and can see in the telemetry which provider served me.

## Problem Statement

`EmbeddingProvider` and `GenerationProvider` have exactly one implementation each — the fakes. Nothing in the
repository can turn text into a real vector or a question into a real answer.

Downstream: TICKET-4 cannot ingest (it needs a real embedder to produce the vectors it stores), TICKET-5's
streaming endpoint has nothing to stream, and TICKET-8's gate re-sweep cannot run because the similarity
distribution it must measure is the one this ticket introduces.

## Solution Statement

`GeminiEmbedder` implementing `EmbeddingProvider`; `GroqGenerator` and `GeminiGenerator` implementing
`GenerationProvider`; and `FailoverGenerator`, a decorator over two generators that retries once and falls
through, reporting which one served.

Three decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **`gemini-embedding-001`, `output_dimensionality=768`, renormalised client-side** | The current GA model rather than the older `text-embedding-004`; more likely to still exist in a year, which PRD criterion 6 cares about. The SDK documents `output_dimensionality` as truncating "excessive values from the end", which breaks the unit norm a matryoshka embedding starts with — so cosine similarity is affected unless we renormalise. **This answers PRD open question 1: yes, it needs renormalisation.** |
| D2 | **Groq primary, Gemini secondary** | Time-to-first-token is the NFR with a hard number (<2.5s p50, <5s p95) and Groq is substantially faster. Two vendors means two independent quota and outage domains, which is what ADR-004 is actually buying. |
| D3 | **Failover only before the first token** | A token on the wire cannot be retracted — the same constraint `sentinel.py` exists for. Mid-stream failure emits an error frame and marks the response truncated, matching the local build. Failover covers the case that dominates in practice: a rate limit rejecting the request outright. |

## Out of Scope / Non-Goals

- **Not included: the API shell.** This ticket produces adapters. Streaming them to a browser, assembling
  telemetry and mapping errors to frames is TICKET-5.
- **Not included: the ingestion job.** `embed_documents` exists here; the CLI that batches a corpus through it
  is TICKET-4.
- **Not included: the health check that exercises the secondary on a schedule** (ADR-004 action item 4). That
  is TICKET-6.
- **Not included: validating the prompt against both models on the evaluation set** (ADR-004 action item 3).
  That needs the eval harness — TICKET-8. This ticket adds a single `live`-marked smoke test that both models
  can emit the exact sentinel, which is the cheap half of that check.
- **Not included: re-tuning τ.** The new embedder changes the similarity distribution and therefore invalidates
  the carried-over thresholds further, but measuring them is TICKET-8.
- **Not included: retries on the embedding path beyond quota backoff.** No circuit breaker, no dead-letter
  queue. Ingestion is offline and re-runnable.
- **Not changing:** the vendored port (`chunking`, `fusion`, `gate`, `prompts`, `sentinel` and their tests),
  the Postgres adapter, or the gate's behaviour.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium — four small adapters, but three genuinely fiddly parts: the Gemini prompt translation, the renormalisation, and expressing provenance through a port that currently cannot.
**Primary Systems Affected**: `src/rag_adapters/`, `src/rag_core/{config,contracts,ports}.py`, `tests/contract/`, CI
**Dependencies**: `google-genai`, `groq`

## Related Work

**Implements**: TICKET-3 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`, decisions D1–D6 in the ticket breakdown

**Back-references**:

- `.claude/plans/port-rag-core-and-scaffold-repo.md` — defines the two ports this implements, the error family, and the contract-suite registries.
- `.claude/plans/postgres-dense-and-lexical-stores.md` — establishes the adapter conventions this follows: optional dependency extra, marker-gated tests, lifecycle via `Profile.resources`, and the finding that fakes under-specify ports.

**Forward-references**:

- TICKET-4 — batches a corpus through `GeminiEmbedder.embed_documents`
- TICKET-5 — streams `FailoverGenerator` and reads `TokenStream.served_by` for telemetry
- TICKET-6 — the scheduled check that exercises the secondary deliberately
- TICKET-8 — re-sweeps τ against the similarity distribution this embedder produces

**Sequencing:** TICKET-2 is on **PR #2, not yet merged**. This ticket touches `profile.py`'s `_REGISTRY`,
`pyproject.toml`'s dependency list, and the contract suite — all of which TICKET-2 also touches. **Branch from
`feature/postgres-dense-and-lexical-stores`, or wait for PR #2 to merge.** Branching from `main` produces
three-way conflicts in exactly those files.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

**This repository:**

- `src/rag_core/ports.py` — Why: the two Protocols. **Read `GenerationProvider`'s docstring on why `stream` is
  `def` and not `async def`** — an async generator returns an `AsyncIterator` when *called*, without being
  awaited, and declaring it `async def` would break every implementation.
- `src/rag_core/errors.py` — Why: `ProviderUnavailable`, `ProviderProtocolError`, `AllProvidersUnavailable`
  already exist and are what these adapters raise. `AllProvidersUnavailable` has had no producer until now.
- `src/rag_adapters/fakes.py` — Why: `FakeEmbedder`, `FakeGenerator`, `ExplodingEmbedder`. `FakeGenerator`
  already takes `fail_with` — that is the seam the failover tests drive.
- `src/rag_adapters/postgres.py` — Why: the adapter conventions set by TICKET-2. Module docstring naming the
  failure it prevents, a lifecycle object with `open`/`close`, comments recording rejected alternatives.
- `src/rag_adapters/profile.py` — Why: `_build_hosted` currently wires `FakeEmbedder`/`FakeGenerator` with a
  comment saying "Placeholder until TICKET-3". This ticket removes that comment by making it true.
- `src/rag_core/config.py` — Why: `EmbeddingConfig.model_id` is still `REPLACE_ME_AFTER_SPIKE`;
  `GenerationConfig` has `primary_model_id`/`secondary_model_id` but no provider names and no API keys.
- `tests/contract/test_port_contract.py` (lines ~33-78, and the generator section at the end) — Why: the
  `EMBEDDERS` / `GENERATORS` registries and the five embedder + three generator assertions every
  implementation must satisfy.
- `pyproject.toml` — Why: the `[project.optional-dependencies] postgres` extra and the `postgres` marker with
  `addopts = '-m "not postgres"'`. This ticket mirrors both. **Do not widen the vendored-port exclusions or
  the mypy override.**

**Source repository (port the reasoning, not the code):**

- `SRC/rag/embeddings.py` (63 lines, read in full) — Why: **the two protocol guards at lines 44-56 port
  verbatim in spirit**: a returned-vector count that does not match the input count, and any vector of the
  wrong dimension, both raise rather than proceed. The error message explains why — "mismatched counts would
  misalign chunk text with vectors and silently poison the store." Also read lines 1-5: nomic needs
  `search_document: ` / `search_query: ` prefixes and "omitting them degrades retrieval silently". Gemini's
  equivalent is `task_type`, and it is the same silent failure.
- `SRC/rag/generation.py` lines 22-46 — Why: `_http_stream` and `stream_chat` are the shape being replaced.
  Note that both wrap transport errors into the provider error family rather than letting `httpx` or
  `json.JSONDecodeError` escape to callers.
- `SRC/rag/ollama.py` lines 39-47 — Why: a malformed 200 response must surface the same way an unreachable
  host does, not escape as a raw parsing error.
- `SRC/tests/unit/test_embeddings.py` (120 lines) — Why: the test shape for a provider adapter with an
  injected transport, including the count/dimension guard cases.

### New Files to Create

```
src/rag_adapters/
├── gemini.py        # GeminiEmbedder + GeminiGenerator
├── groq.py          # GroqGenerator
├── failover.py      # FailoverGenerator
└── _backoff.py      # async retry-with-backoff helper
tests/unit/
├── test_gemini.py       # guards, renormalisation, prompt translation, batching
├── test_groq.py         # streaming, error mapping
└── test_failover.py     # the chain, in every failure shape
tests/contract/
└── test_live_providers.py   # `live`-marked, needs real keys, deselected by default
```

Modified: `src/rag_core/{config,contracts,ports}.py`, `src/rag_adapters/{fakes,profile}.py`,
`tests/contract/test_port_contract.py`, `pyproject.toml`, `.env.example`, `.github/workflows/test.yml`,
`docs/ARCHITECTURE.md`, `docs/PRD.md`, `README.md`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [google-genai — embed_content](https://github.com/googleapis/python-genai#embed-content)
  - Specific: `client.aio.models.embed_content(model=..., contents=[...], config=types.EmbedContentConfig(...))`
  - Why: `contents` takes a **list**, so batching is native — no manual chunking loop beyond quota sizing.
- [google-genai — EmbedContentConfig](https://github.com/googleapis/python-genai/blob/main/google/genai/types.py)
  - Specific: `task_type`, `output_dimensionality`
  - Why: `output_dimensionality` is documented as *"excessive values in the output embedding are truncated
    from the end"* — that is the sentence D1 rests on. `task_type` is the prefix analogue.
- [Gemini embeddings — task types](https://ai.google.dev/gemini-api/docs/embeddings#task-types)
  - Specific: `RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY`
  - Why: asymmetric embedding. Documents and queries are embedded differently on purpose, and using one where
    the other belongs degrades retrieval without erroring.
- [google-genai — async streaming](https://github.com/googleapis/python-genai#streaming)
  - Specific: `client.aio.models.generate_content_stream(...)`, `types.GenerateContentConfig(system_instruction=...)`
  - Why: Gemini does **not** take an OpenAI-style message list. See the translation gotcha in Task 5.
- [groq-python — async client](https://github.com/groq/groq-python#async-usage)
  - Specific: `AsyncGroq`, `await client.chat.completions.create(..., stream=True)` → `AsyncStream[ChatCompletionChunk]`
  - Why: OpenAI-compatible, so `build_messages` output passes straight through.
- [groq-python — error handling](https://github.com/groq/groq-python#handling-errors)
  - Specific: `RateLimitError`, `APIConnectionError`, `APIStatusError`
  - Why: these are the failover triggers. `RateLimitError` is the one that will actually fire in production.

### Patterns to Follow

**An injected client/transport, so tests need no network.** Established in SRC and carried by every adapter
here. From `SRC/rag/embeddings.py:32`:

```python
def __init__(self, cfg: OllamaConfig, transport: Callable[[str, dict], dict] | None = None):
    self.cfg = cfg
    self._transport = transport or (lambda url, payload: _http_transport(url, payload, cfg.request_timeout_s))
```

**Guards that name both numbers.** From `SRC/rag/embeddings.py:46-50`:

```python
raise OllamaProtocolError(
    f"{self.cfg.embed_model} returned {len(vectors)} embeddings for "
    f"{len(inputs)} inputs. Refusing to continue: mismatched counts would "
    f"misalign chunk text with vectors and silently poison the store."
)
```

**No provider SDK reachable from `rag_core`.** Enforced by `tests/unit/test_core_purity.py`, whose
`PROVIDER_SDKS` list already contains `google`, `groq`, `openai`, `anthropic`. Adding these dependencies must
not make that test fail.

**Marker-gated tests that skip readably.** From `tests/contract/conftest.py`:

```python
dsn = os.environ.get("DATABASE_URL", "")
if not dsn:
    pytest.skip("DATABASE_URL is not set; start a pgvector container to run these")
```

**Anti-patterns to avoid:** letting an SDK exception escape to a caller that catches `ProviderError`;
retrying a request that already streamed tokens; normalising a score; reading `os.environ` anywhere except
`load_config`; a bare `except Exception`.

---

## IMPLEMENTATION PLAN

### Phase 1: Contract surface

The port cannot currently express which provider served a request, which ADR-004 and PRD F14 both require.
Fix that before writing adapters against it.

**Tasks:** 1 (dependencies + config), 2 (`TokenStream` + port amendment).

### Phase 2: The embedding path

**Depends on:** Phase 1 (config).
**Independent of:** Phase 3 — different file, different port. Genuinely parallelisable.

**Tasks:** 3 (backoff helper), 4 (`GeminiEmbedder`).

### Phase 3: The generation path

**Depends on:** Phase 1 (`TokenStream`).

**Tasks:** 5 (`GeminiGenerator`), 6 (`GroqGenerator`), 7 (`FailoverGenerator`).

### Phase 4: Wiring and verification

**Depends on:** Phases 2 and 3.

**Tasks:** 8 (profile), 9 (contract suite + live marker + CI), 10 (docs).

---

## STEP-BY-STEP TASKS

### 1. UPDATE `pyproject.toml`, `src/rag_core/config.py`, `.env.example`

- **IMPLEMENT**: Add a `providers` extra: `google-genai>=1.0`, `groq>=0.13`. Add a `live` marker and extend
  `addopts` to `-m "not postgres and not live"`. In config:
  - `EmbeddingConfig.model_id` default → `"gemini-embedding-001"`, replacing the `REPLACE_ME_AFTER_SPIKE`
    placeholder and its comment.
  - `GenerationConfig` gains `primary_provider: str = "groq"` and `secondary_provider: str = "gemini"`, plus
    sensible model defaults for each.
  - New `ProvidersConfig(gemini_api_key: str = "", groq_api_key: str = "")` read from `GEMINI_API_KEY` and
    `GROQ_API_KEY`; add to `RagConfig`.
  - `.env.example` gains all four keys with placeholder values and a secret warning.
- **PATTERN**: `DatabaseConfig` in `src/rag_core/config.py` — added by TICKET-2 with the same shape, including
  the empty-by-default rationale.
- **GOTCHA**: Both SDKs read their key from the environment if you do not pass one. **Do not rely on that.**
  `config.py`'s docstring states that it is "the single boundary between os.environ and the pipeline", and an
  SDK quietly reading `GEMINI_API_KEY` behind its back makes the config a lie and the tests non-hermetic.
  Pass the key explicitly, always.
- **GOTCHA**: The extra is `providers`, separate from `postgres`, and **`dependencies` stays `[]`** —
  `tests/unit/test_core_purity.py::test_core_declares_no_runtime_dependencies` asserts it.
- **GOTCHA**: A command-line `-m` **replaces** `addopts`, so `uv run pytest -m live` selects only live tests.
  Verify rather than assume.
- **VALIDATE**: `uv sync && uv run pytest tests/unit/test_config.py -q && uv run python -c "import google.genai, groq; print('sdks ok')"`
- **SATISFIES**: AC #5

### 2. UPDATE `src/rag_core/contracts.py`, `ports.py`, `src/rag_adapters/fakes.py` — `TokenStream`

- **IMPLEMENT**: A `TokenStream` in `contracts.py` — an async iterator over tokens that also reports which
  model produced them:

```python
class TokenStream:
    """Tokens, plus which model actually produced them.

    `served_by` is None until the first token arrives, and is the model_id of
    whichever provider produced it thereafter. It cannot be known earlier: the
    whole point of the failover chain is that the answer to "who served this"
    depends on whether the primary accepted the request.
    """

    def __init__(self, tokens: AsyncIterator[Token], model_id: str | None = None) -> None: ...
    served_by: str | None
    def __aiter__(self) -> TokenStream: ...
    async def __anext__(self) -> Token: ...
```

  Change the port to `def stream(self, messages: list[dict[str, str]]) -> TokenStream`. Update `FakeGenerator`
  to return one.
- **PATTERN**: TICKET-2's manifest amendment to `DenseStore` — same shape of change, same justification style
  in the docstring.
- **GOTCHA**: This is a port amendment, the fourth across three tickets, and for the same recurring reason:
  **the fakes under-specify the ports.** A fake generator always knows its own `model_id` up front, so nothing
  forced the port to express provenance. A failover chain does not know until it tries. ADR-004 requires "the
  serving provider reported in every response" and PRD F14 repeats it, so the port must carry it.
- **GOTCHA**: Do **not** solve this with a mutable attribute on the generator. `Profile` holds one generator
  instance and TICKET-5 will serve concurrent requests through it; a `self.last_served` field is a race that
  will report the wrong provider under load and never fail a test.
- **GOTCHA**: `TokenStream` must be an honest `AsyncIterator` — `__aiter__` returning self and `__anext__`
  raising `StopAsyncIteration` — so `async for` and `[t async for t in stream]` both work unchanged.
- **VALIDATE**: `uv run mypy && uv run pytest tests/contract -q` — the three existing generator contract
  assertions must pass unmodified.
- **SATISFIES**: AC #2

### 3. CREATE `src/rag_adapters/_backoff.py`

- **IMPLEMENT**: `async def retry_with_backoff(operation, *, attempts, base_delay, retry_on)` — exponential
  delay, re-raising the last exception when attempts are exhausted.
- **PATTERN**: none in this repository; keep it small and dependency-free.
- **GOTCHA**: Used by the **embedder only**. The failover chain retries exactly once with no delay
  (ADR-004: "one retry before falling through") — a request path cannot afford backoff sleeps inside a 2.5s
  time-to-first-token budget. Do not reuse this helper there.
- **GOTCHA**: No jitter is needed for a single-client offline job, and adding it would need a seeded random
  for tests. If concurrency ever arrives, revisit.
- **GOTCHA**: `retry_on` must be an explicit exception tuple. Retrying every exception would retry a
  dimension-mismatch guard, which cannot succeed on a second attempt and would triple the time to a clear error.
- **VALIDATE**: `uv run pytest tests/unit/test_backoff.py -v` — succeeds first try, succeeds on retry, exhausts
  and re-raises, does not retry an excluded exception, and sleeps between attempts (assert on an injected
  sleeper, not on wall-clock).
- **SATISFIES**: AC #1

### 4. CREATE `src/rag_adapters/gemini.py` — `GeminiEmbedder`

- **IMPLEMENT**: `EmbeddingProvider` over `google.genai`.
  - `embed_documents(texts)` → `task_type="RETRIEVAL_DOCUMENT"`, batched at `cfg.embedding.batch_size`, each
    batch wrapped in `retry_with_backoff` on rate-limit errors.
  - `embed_query(text)` → `task_type="RETRIEVAL_QUERY"`, single item.
  - Both: `config=types.EmbedContentConfig(task_type=..., output_dimensionality=cfg.embedding.dimension)`.
  - **Renormalise every returned vector to unit length** (D1).
  - Port SRC's two guards: count mismatch and per-vector dimension mismatch, both raising
    `ProviderProtocolError` with a message naming both numbers.
  - Accept an injected client so tests need no key.
- **PATTERN**: `SRC/rag/embeddings.py` in full — same guards, same prefix/task asymmetry, same injected seam.
- **GOTCHA**: **`task_type` is this model's version of nomic's `search_document: ` / `search_query: `
  prefixes.** SRC's docstring says omitting the prefixes "degrades retrieval silently"; the same is true here.
  Documents and queries are embedded asymmetrically *by design*, and using `RETRIEVAL_DOCUMENT` for a query
  produces plausible vectors with worse ranking and no error anywhere.
- **GOTCHA**: **Renormalise after truncation.** `output_dimensionality` truncates from the end, which breaks
  the unit norm the full-width embedding has. Cosine over non-unit vectors still works arithmetically, but the
  magnitudes vary per vector — and the gate thresholds on raw cosine similarity, so the variation lands
  directly on the thing τ measures. Pin it: `assert abs(norm(v) - 1.0) < 1e-6`.
- **GOTCHA**: A zero vector cannot be normalised. Guard it and raise `ProviderProtocolError`, because the
  downstream symptom is a NaN cosine distance from pgvector and a gate that refuses everything for no
  discoverable reason (see `tests/contract/test_postgres_specifics.py`).
- **GOTCHA**: `embed_documents([])` must return `[]` without calling the API — the contract suite asserts it,
  and an empty `contents` list is an API error.
- **GOTCHA**: Map `google.genai.errors` exceptions into the provider family. Nothing SDK-shaped may escape to a
  caller catching `ProviderError`; that is the same escape SRC closed in `ollama.py:39-47`.
- **VALIDATE**: `uv run pytest tests/unit/test_gemini.py -v -k embed`
- **SATISFIES**: AC #1, AC #4

### 5. ADD `GeminiGenerator` to `src/rag_adapters/gemini.py`

- **IMPLEMENT**: `GenerationProvider` over `client.aio.models.generate_content_stream`. Translate
  `build_messages` output into Gemini's shape, yield text deltas, wrap in a `TokenStream`.
- **PATTERN**: `SRC/rag/generation.py:35-45` — extract the text delta, skip chunks that carry none.
- **GOTCHA**: **Gemini does not take an OpenAI-style message list.** `build_messages` returns
  `[{"role": "system", ...}, {"role": "user", ...}]`. Gemini wants the system message hoisted into
  `types.GenerateContentConfig(system_instruction=...)`, and the rest as `contents` with roles `user` and
  **`model`** — not `assistant`. Passing the list through unchanged either errors or silently drops the system
  prompt, and a dropped system prompt means **no sentinel instruction and no citation instruction**, which
  disables stage 2 of the gate without any test noticing.
- **GOTCHA**: The hosted profile is single-turn, so `contents` is one user message today. Write the
  translation to handle history anyway — `prompts._trim_history` is ported and tested, and a translation that
  only works for the single-turn case is a trap for whoever enables multi-turn.
- **GOTCHA**: Yield only non-empty text. A chunk with no text part is normal and must not yield `""`, which
  would confuse `filter_sentinel`'s buffer accounting.
- **GOTCHA**: `stream` is a plain `def` that returns a `TokenStream`, not an `async def`. Re-read the port
  docstring if this looks wrong.
- **VALIDATE**: `uv run pytest tests/unit/test_gemini.py -v -k generat` — covers the system hoist, the
  `assistant`→`model` mapping, empty-chunk skipping, and error mapping.
- **SATISFIES**: AC #2

### 6. CREATE `src/rag_adapters/groq.py` — `GroqGenerator`

- **IMPLEMENT**: `GenerationProvider` over `AsyncGroq`. `await client.chat.completions.create(messages=...,
  model=..., stream=True)`, yield `chunk.choices[0].delta.content` when non-empty, wrap in a `TokenStream`.
  Map `RateLimitError` / `APIConnectionError` / `APIStatusError` to `ProviderUnavailable`.
- **PATTERN**: `GeminiGenerator` from Task 5 for structure; `SRC/rag/generation.py:42-45` for delta extraction.
- **GOTCHA**: Groq is OpenAI-compatible, so `build_messages` output passes through **unchanged** — no
  translation, unlike Gemini. That asymmetry is worth a comment; the next reader will assume both need the
  same treatment.
- **GOTCHA**: Naming the module `groq.py` alongside `import groq` looks like a shadowing bug and is not —
  Python 3 uses absolute imports, so `import groq` inside `rag_adapters/groq.py` resolves to the installed
  SDK. Chosen for consistency with `postgres.py`. Add a test importing both to pin it, because the next person
  to see it will otherwise "fix" it.
- **GOTCHA**: `delta.content` is `None` on the final chunk and on role-only chunks. Skip those; do not yield
  `None` into a `str` stream.
- **GOTCHA**: The `create(...)` call is awaited, then the result is iterated. Awaiting inside the async
  generator means a rate limit raises on first `__anext__`, not at `stream()` call time — which is exactly
  what D3 needs, since it is still before any token.
- **VALIDATE**: `uv run pytest tests/unit/test_groq.py -v`
- **SATISFIES**: AC #2

### 7. CREATE `src/rag_adapters/failover.py` — `FailoverGenerator`

- **IMPLEMENT**: A `GenerationProvider` decorating two others. On `stream()`: try the primary; on a
  retryable error **before the first token**, retry it once; if it fails again, fall through to the secondary.
  If the secondary also fails, raise `AllProvidersUnavailable`. Set `TokenStream.served_by` to the `model_id`
  of whichever produced the first token.
- **PATTERN**: `src/rag_core/errors.py`'s `AllProvidersUnavailable` docstring — it already states the required
  behaviour ("this is the end of the chain and must produce an explicit service message. Never an ungrounded
  answer").
- **GOTCHA**: **The whole design turns on "before the first token" (D3).** Buffer nothing; the moment a token
  is yielded, the provider is committed and a later failure propagates as an error the shell reports as
  truncated. Attempting to fail over mid-stream would either duplicate text or require retracting what the
  reader has already seen.
- **GOTCHA**: One retry on the primary, then the secondary — three attempts total, and no delay between them.
  This sits inside a 2.5s p50 budget.
- **GOTCHA**: A non-retryable error must **not** consume the fallback. A malformed prompt fails identically on
  both providers, and burning the secondary on it turns one clear error into two and doubles the latency of
  the failure. Distinguish `ProviderUnavailable` (retry, then fall through) from `ProviderProtocolError`
  (raise immediately).
- **GOTCHA**: `model_id` on the decorator itself should be the primary's, since that is what will serve the
  overwhelming majority of requests. `served_by` on the stream is the authoritative per-request answer.
- **VALIDATE**: `uv run pytest tests/unit/test_failover.py -v` — primary succeeds; primary rate-limited once
  then succeeds; primary fails twice then secondary serves and `served_by` reports the secondary; both fail →
  `AllProvidersUnavailable`; a non-retryable error raises without touching the secondary; a mid-stream failure
  propagates rather than failing over.
- **SATISFIES**: AC #3

### 8. UPDATE `src/rag_adapters/profile.py`

- **IMPLEMENT**: `_build_hosted` uses `GeminiEmbedder` and a `FailoverGenerator` over
  `GroqGenerator`/`GeminiGenerator`, resolved from `cfg.generation.primary_provider` /
  `secondary_provider`. Remove the "Placeholder until TICKET-3" comment.
- **PATTERN**: `_build_hosted` as TICKET-2 left it, and its `_REGISTRY` seam.
- **GOTCHA**: A small provider-name → class map keeps ADR-004's "no conditional provider logic anywhere below
  the composition root" true. An `if provider == "groq"` inside the failover would violate exactly that.
- **GOTCHA**: Construction must not perform I/O and must not require a key to be present. Building a hosted
  profile with no API key has to succeed; the failure belongs at the first call. This mirrors the pool
  decision in TICKET-2 and keeps `test_building_the_hosted_profile_does_not_connect` meaningful.
- **GOTCHA**: An unknown provider name raises at startup naming the valid ones — same rule as
  `build_profile`'s unknown-profile error, and for the same reason.
- **VALIDATE**: `uv run pytest tests/unit/test_profile.py -v`
- **SATISFIES**: AC #5

### 9. UPDATE `tests/contract/test_port_contract.py`, CREATE `tests/contract/test_live_providers.py`, UPDATE CI

- **IMPLEMENT**: Add the new adapters to `EMBEDDERS` and `GENERATORS` — each as a builder returning an
  instance with an injected fake client, so the shared suite runs against them with no network. Then a
  separate `live`-marked file that exercises the real APIs: an embedding of the right dimension and unit norm,
  a streamed answer, and both models emitting the exact sentinel when given no relevant context. CI gains no
  live step.
- **PATTERN**: `tests/contract/conftest.py`'s `pg_pool` fixture for the skip-readably shape.
- **GOTCHA**: TICKET-1 claimed adding an adapter here would be one line, and TICKET-2 found that false for
  stores. **For embedders and generators it is actually true** — they have no lifecycle and no shared state,
  which is exactly why TICKET-2 left those two registries alone. If you find yourself restructuring them,
  stop and check why.
- **GOTCHA**: The live tests cost real quota. Deselected by default, never in CI, and documented in the README
  as a manual check. ADR-004 action item 3 (validate the prompt against both models on the evaluation set) is
  TICKET-8's; this is the cheap smoke-test half.
- **GOTCHA**: `test_embedder_declared_dimension_matches_the_index_schema` asserts `dimension == 768` against
  `DIMENSIONS` in `fakes.py`. The Gemini embedder must report 768 too, from config — not its native 3072.
- **VALIDATE**: `uv run pytest tests/contract -q` (no keys, no network) then, with keys set,
  `uv run pytest -m live -v`
- **SATISFIES**: AC #4, AC #6

### 10. UPDATE `docs/ARCHITECTURE.md`, `docs/PRD.md`, `README.md`

- **IMPLEMENT**: Tick ADR-004 action items 1 and 2 (failover implemented; active provider surfaced — note
  that the UI half lands in TICKET-5/7). Leave 3 and 4 open, attributed to TICKET-8 and TICKET-6. **Answer PRD
  open question 1** with what the SDK documents and what this ticket does. Record D1–D3 in ADR-004. README
  gains the API-key prerequisite and how to run the live tests.
- **PATTERN**: TICKET-1 and TICKET-2's doc reconciliation — amend and date, never silently rewrite.
- **GOTCHA**: PRD open question 1 asked "does truncation to 768 need renormalisation, and does skipping it
  measurably hurt cosine ranking? Assume yes; verify." Half is now answered from documentation: it needs
  renormalisation. **Whether skipping it measurably hurts is still unmeasured** — say so rather than implying
  the whole question is closed. TICKET-8 could measure it cheaply while sweeping τ.
- **GOTCHA**: Do not claim retrieval parity anywhere. That is TICKET-9's measurement and this ticket changes
  the embedder, which is the variable it will be measuring.
- **VALIDATE**: `grep -n "REPLACE_ME" .env.example src/rag_core/config.py` returns nothing for the embedding
  model, and `grep -n "open question 1" docs/PRD.md` finds the answer.
- **SATISFIES**: AC #6

---

## TESTING STRATEGY

### Unit Tests

Every adapter takes an injected client, so the whole suite runs with no key and no network — the same property
`rag_core` has and for the same reason.

- `test_gemini.py` — task types applied correctly; renormalisation to unit length; count and dimension guards;
  zero-vector rejection; empty batch short-circuit; batching at `batch_size`; backoff on rate limit; the
  message translation (system hoisted, `assistant`→`model`); error mapping.
- `test_groq.py` — streaming deltas; `None` delta skipping; messages passed through unchanged; error mapping.
- `test_failover.py` — the six paths listed in Task 7.
- `test_backoff.py` — the five paths listed in Task 3.

### Contract Tests

The existing suite, with the new adapters added to `EMBEDDERS` and `GENERATORS`. Same assertions, three
implementations each.

### Live Tests

`tests/contract/test_live_providers.py`, `live`-marked, skipped without keys, never in CI. Real embeddings at
the right dimension and unit norm, a real stream from each generator, and the sentinel check.

### Edge Cases

- `embed_documents([])` → `[]`, no API call
- A batch larger than `batch_size` → multiple calls, order preserved across batch boundaries
- Provider returns fewer vectors than inputs → raises, naming both counts
- Provider returns a 3072-length vector despite `output_dimensionality=768` → raises
- A zero vector → raises rather than producing a NaN cosine distance downstream
- Rate limit on the embedder → backs off and succeeds
- Rate limit on the primary generator → one retry, then secondary
- Both generators unavailable → `AllProvidersUnavailable`
- Non-retryable error on the primary → raises without consuming the secondary
- Primary fails **after** the first token → propagates; no failover, no duplicate text
- Gemini given `[system, user, assistant, user]` → system hoisted, roles mapped, order preserved
- `served_by` read before iteration → `None`, not a lie

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

### Level 3: Contract & Integration Tests

```bash
# No keys, no network, no database — must stay green
uv run pytest -q

# With a database (TICKET-2's suite must not regress)
docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
uv run pytest -m postgres -q
```

### Level 4: Manual Validation

```bash
# Purity holds with two more SDKs installed
uv run pytest tests/unit/test_core_purity.py -v
uv run python -c "
import sys, rag_core.pipeline, rag_core.ports, rag_core.contracts
loaded = {m.split('.')[0] for m in sys.modules}
for banned in ('google','groq','asyncpg','pgvector'):
    assert banned not in loaded, f'{banned} was pulled in by importing rag_core'
print('rag_core still pulls in no SDK')
"

# The module name is not shadowing the SDK
uv run python -c "
from rag_adapters.groq import GroqGenerator
import rag_adapters.groq as m
assert m.AsyncGroq.__module__.startswith('groq'), 'module shadowed the SDK'
print('no shadowing')
"

# Live, with keys — costs quota
export GEMINI_API_KEY=... GROQ_API_KEY=...
uv run pytest -m live -v

# Real embedding: right width, unit norm
uv run python -c "
import asyncio, math, os
from rag_core.config import load_config
from rag_adapters.gemini import GeminiEmbedder
cfg = load_config(env=dict(os.environ))
async def main():
    e = GeminiEmbedder(cfg)
    v = await e.embed_query('What is the adult starting dose of metformin?')
    print('dim :', len(v))
    print('norm:', math.sqrt(sum(x*x for x in v)))
    assert len(v) == 768
    assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-6
    print('embedding OK')
asyncio.run(main())
"

# Failover, observed: revoke the primary key and confirm the secondary reports itself
GROQ_API_KEY=invalid uv run python -c "
import asyncio, os
from rag_core.config import load_config
from rag_adapters.profile import build_profile
cfg = load_config(env={**os.environ, 'RAG_PROFILE': 'hosted'})
p = build_profile(cfg)
async def main():
    stream = p.generator.stream([
        {'role': 'system', 'content': 'Answer in one short sentence.'},
        {'role': 'user', 'content': 'What is 2 + 2?'},
    ])
    text = ''.join([t async for t in stream])
    print('served_by:', stream.served_by)
    print('text     :', text.strip()[:80])
    assert stream.served_by and 'groq' not in stream.served_by.lower()
    print('failover OK — secondary served and reported itself')
asyncio.run(main())
"

docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
# No key reached the public repository
git diff --cached | grep -inE "AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}" || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-3, plus the standard bar:

- [ ] **AC #1** — Contract suite passes for both generation adapters and the embedder
- [ ] **AC #2** — Primary rate-limited → exactly one retry → secondary serves → response reports the secondary. Proven with a fake client, not by revoking a real key
- [ ] **AC #3** — Both providers failing raises `AllProvidersUnavailable`, and never returns partial text as if it were an answer
- [ ] **AC #4** — An embedder returning the wrong count or wrong dimension raises, with a message naming both numbers
- [ ] **AC #5** — No provider SDK is importable from `rag_core` outside `rag_adapters`
- [ ] **AC #6** — Embeddings are unit-norm after truncation to 768, pinned by a test
- [ ] All validation commands pass with zero errors
- [ ] `mypy --strict` clean; TICKET-1's vendored-port exclusions and mypy override unchanged
- [ ] `uv run pytest` stays green with no keys, no network and no database
- [ ] No API key in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 10 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full suite green with and without keys, with and without a database
- [ ] TICKET-2's Postgres suite still passes unchanged
- [ ] CI green
- [ ] Acceptance criteria all met
- [ ] TICKET-4 can embed a corpus; TICKET-5 can stream an answer and read `served_by`

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 `gemini-embedding-001` truncated to 768 and
renormalised; D2 Groq primary, Gemini secondary; D3 failover only before the first token.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — the port gains `TokenStream` so provenance can be expressed. This is the fourth port
   amendment across three tickets, all from the same cause. Worth a moment's thought about whether the ports
   were designed too early — though the counterfactual (designing them after three adapters existed) would
   have meant no parallel work at all.
2. **Assumed** — `served_by` is `None` until the first token. The alternative, eagerly probing the primary
   with a throwaway request, doubles cost and latency to answer a question the first token answers for free.
3. **Assumed** — a `providers` extra separate from `postgres`, and `dependencies` stays `[]`.
4. **Assumed** — live tests exist but never run in CI. They cost quota, and a red build caused by someone
   else's rate limit trains people to ignore CI.
5. **Assumed** — `groq.py` as a module name is safe under absolute imports. Pinned by a test because it looks
   wrong.
6. **Assumed** — `task_type` is not recorded in `IndexManifest`. It is fixed by the adapter rather than
   configurable, so it cannot drift independently of `model_id`. If it ever becomes configurable, the manifest
   needs it, because a query-task index and a document-task index are silently incompatible in exactly the way
   the manifest exists to prevent.
7. **Open, deferred** — whether skipping renormalisation *measurably* hurts ranking is unmeasured. PRD open
   question 1 asked; documentation answers the first half. TICKET-8 can measure the second half cheaply while
   sweeping τ.

---

## NOTES (open canvas)

### The prefix problem, twice

The single most useful thing SRC teaches this ticket is in a docstring:

> `nomic-embed-text` is a prefixed model: indexed text needs `search_document: ` and queries need
> `search_query: `. Omitting them degrades retrieval silently, so the prefixes are applied here and are not
> callable parameters.

Gemini has the same asymmetry under a different name — `task_type=RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY` —
and the same failure mode: no error, no exception, just worse ranking that looks like the model being
mediocre. SRC's answer was to make the prefixes *not callable parameters*, so no caller could forget them.
This adapter should do the same: the task type is chosen by which method you call, never passed in.

### Why the port keeps growing

Three tickets, four amendments: `count()`, the manifest pair, `open`/`close`, and now `TokenStream`. Every one
came from the same place — the fakes were the only implementation when the ports were written, and a fake has
no connection, no schema, no lifecycle and no uncertainty about who served a request.

That is not an argument against having written the ports early; the parallelism they bought is real, and
TICKET-2 and TICKET-3 could not have been planned independently otherwise. But it *is* the honest answer to
ADR-001's stated worry about "over-abstracting ports that only ever get two implementations." The abstraction
was not too rich. It was too poor, and each real adapter has been paying a small instalment to fix it.

Worth a line in ADR-001's Consequences when TICKET-3 lands.

### Alternatives weighed and rejected

**A `contextvars.ContextVar` for provenance.** Idiomatic for async, no port change, correct under concurrency.
Rejected because it is invisible: nothing in the type signature says the value exists, and TICKET-5 would have
to know to look. `TokenStream` makes it a value you cannot miss.

**A mutable `last_served_by` on the generator.** Simplest to write, and a race the moment two requests overlap
— which is precisely when a demo is being evaluated. It would report the wrong provider and no test would
catch it.

**Buffering the whole answer to make failover total.** Rejected in D3 with the user. Worth recording that it
also breaks the telemetry strip's time-to-first-token, which is one of the numbers the strip exists to show.

**`text-embedding-004` to dodge renormalisation.** Rejected in D1. The renormalisation is four lines and a
test; model retirement during the demo's life is not recoverable in four lines.

**Retrying inside `FailoverGenerator` with backoff.** The embedder backs off because it runs offline where a
two-second sleep costs nothing. On the request path the same sleep is most of the time-to-first-token budget,
so the chain retries immediately and then moves on.

### Sequencing risk

This ticket and TICKET-2 both touch `profile.py`'s `_REGISTRY`, `pyproject.toml`'s extras, and the contract
suite. TICKET-2 is on an open PR. Branch from it rather than from `main`, or wait for the merge — a three-way
conflict in the composition root is a tedious way to start.

After this lands, TICKET-4 (ingestion) and TICKET-5 (API shell) both unblock, and they are genuinely parallel
with each other.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
