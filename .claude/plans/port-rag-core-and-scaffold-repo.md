# Feature: TICKET-1 — Repo scaffold and `rag_core` port

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for every port in this plan:**
`/Users/jsonse/Documents/development/interview/medical-rag/backend/`
Referred to throughout as **SRC**. It is a *different repository* — read from it, never write to it.

**Target repository:** `/Users/jsonse/Documents/development/medical-rag-hosted-version/`
Currently contains only `docs/`. Not yet a git repository. Referred to as **DST**.

---

## Feature Description

Stand up the `medical-rag-hosted-version` repository and move the retrieval pipeline out of Django into a
framework-agnostic package with four provider ports and a set of fake adapters.

The local build already keeps its retrieval logic in a Django-free package (`SRC/rag/`, 735 lines, pinned by
`SRC/tests/unit/test_rag_purity.py`). So this is not an extraction from tangled code — it is a **port** of an
already-clean package into a new repo, plus the port boundary (`ports.py`), the composition root
(`profile.py`), the wire contract (`contracts.py`), and the Django-free rewrite of the one orchestration
module that *is* coupled (`SRC/chat/retrieval.py`).

Under the standalone-repo decision (D1 in the epic), the ported unit tests are the **parity harness**. They
are what turns "the hosted pipeline behaves like the local one" from a claim into a failing test when it
stops being true.

## User Story

As the author of the Medical RAG project
I want the retrieval pipeline running as a standalone, framework-free package with provider ports and fakes
So that the hosted adapters, the API shell and the frontend can all be built in parallel against one stable contract, and so that divergence from the local build shows up as a red test rather than a surprise.

## Problem Statement

There is no hosted codebase yet — only two planning documents. Every other ticket in the epic (TICKET-2
through TICKET-7) is blocked on a package boundary that does not exist: the four ports, the data contracts,
the NDJSON frame schema, and fakes to build against. Meanwhile the logic those tickets need is sitting in
another repository, coupled to Django in exactly four places (`chat/retrieval.py`, `chat/lexical_search.py`,
`documents/ingestion.py`, `documents/services.py`) and to Ollama in three (`rag/embeddings.py`,
`rag/generation.py`, `rag/vectorstore.py`).

## Solution Statement

Create the repository with `src/rag_core/` (pure) and `src/rag_adapters/` (impure), enforce the dependency
direction with a purity test, port the five pure modules with their test suites, define four async Protocols,
and rewrite `retrieve()` against those Protocols instead of the Django ORM.

Three things get *better* in the port rather than being carried across:

1. **Hydration stops being I/O.** Architecture §4 has both stores return `list[Scored[Chunk]]` — full chunks,
   not bare ids. RRF only ranks ids that appeared in at least one leg, so hydration becomes a dict lookup
   over the union of both legs' results. `SRC/chat/retrieval.py:26-54`'s ORM round-trip disappears, and with
   it the "orphaned vector: drop it silently" branch at line 45 — there is no second store to orphan from.
2. **`count()` leaves the hot path.** `SRC/chat/retrieval.py:58` calls `store.count()` on *every* query. The
   corpus can only be empty if both legs returned nothing, so the count moves behind that condition — zero
   round trips on the happy path, identical gate behaviour.
3. **The purity test gets teeth.** `SRC/tests/unit/test_rag_purity.py` greps for `django` only. The new one
   enforces Architecture §3 in full: no web framework, no provider SDK, no database driver, and no import of
   `rag_adapters` from `rag_core`.

## Out of Scope / Non-Goals

- **Not included: any real adapter.** No Postgres, no Gemini, no Groq. Only fakes. (TICKET-2, TICKET-3.)
- **Not included: the FastAPI app.** `contracts.py` defines the frame *schema*; `api/` is TICKET-5.
- **Not included: `build_fts_query`.** `SRC/rag/lexical.py` is pure and tempting, but its output is FTS5
  syntax (`"term" OR "term"`). Postgres needs `websearch_to_tsquery`. It ports in TICKET-2, where it can be
  translated and tested against a real database.
- **Not included: ingestion.** `chunk_pages` ports here; the job that calls it is TICKET-4.
- **Not included: the τ re-sweep.** `tau_abstain` / `tau_strong` are carried across *marked invalid*.
  Measuring them is TICKET-8.
- **Not changing:** the algorithms. Chunking boundaries, RRF, gate thresholds logic, sentinel buffering and
  prompt structure are ported byte-identical. If a ported test needs editing to pass, that is a port bug —
  stop and fix the port, do not edit the test. The two deliberate exceptions are named in Task 6 and Task 8.

## Feature Metadata

**Feature Type**: Refactor (cross-repository port) + New Capability (ports, contracts, composition root)
**Estimated Complexity**: Medium — high task count, low per-task risk; the async conversion of `retrieve()` is the only genuinely new logic
**Primary Systems Affected**: `src/rag_core/`, `src/rag_adapters/`, `tests/`, repository tooling, `docs/`
**Dependencies**: Python ≥3.12, `uv`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`. No runtime dependencies at all — `rag_core` imports nothing outside the standard library.

## Related Work

**Implements**: TICKET-1 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`, with slicing decisions D1–D6 recorded in `docs/tickets/medical-rag-hosted-version.md`

**Back-references**: none — this is the first ticket in the epic.

**Forward-references** (this ticket's contract is what unblocks them):

- TICKET-2 — implements `DenseStore` + `LexicalStore` against `src/rag_core/ports.py`
- TICKET-3 — implements `EmbeddingProvider` + `GenerationProvider` against the same
- TICKET-5 — consumes `pipeline.retrieve()`, `contracts.Telemetry`, `contracts.FRAME_TYPES`
- TICKET-7 — consumes the NDJSON frame schema from `contracts.py`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

All paths under **SRC** = `/Users/jsonse/Documents/development/interview/medical-rag/backend/`.

**Port verbatim (read in full — the comments encode measured behaviour, carry them across):**

- `SRC/rag/chunking.py` (88 lines) — Why: ports unchanged. Note the size contract in the `chunk_pages`
  docstring (lines 68-78): effective max is `size + overlap`, not `size`.
- `SRC/rag/fusion.py` (28 lines) — Why: ports unchanged. The `(-score, chunk_id)` sort key at line 27 is
  load-bearing — it makes ties deterministic.
- `SRC/rag/gate.py` (69 lines) — Why: ports unchanged. Lines 52-57 (NaN fails closed) and lines 17-20
  (`_jsonable`) are guards against specific, documented failures.
- `SRC/rag/prompts.py` (112 lines) — Why: logic ports unchanged; **the user-facing copy does not** (Task 6).
- `SRC/rag/generation.py` lines 18-19, 48-99 — Why: `BUFFER_CHARS`, `PREAMBLE_TOLERANCE`, `_is_sentinel`,
  `filter_sentinel` become `sentinel.py`. Lines 22-46 (`_http_stream`, `stream_chat`) are Ollama transport
  and do **not** port.
- `SRC/rag/ollama.py` lines 9-25 — Why: the error family and, critically, the docstring at lines 18-25
  explaining why `ProtocolError` subclasses both the transport base and `ValueError`.

**Rewrite against ports (read, then reimplement — do not copy):**

- `SRC/chat/retrieval.py` (107 lines) — Why: this is `pipeline.retrieve()`. Read all of it. The 20-line
  comment at lines 73-92 explaining the `mean_similarity` population, and the 7-line comment at lines 98-104
  explaining why `lexical_support` is measured across all delivered chunks rather than `top_ids[0]`, both
  port verbatim. The ORM code at lines 26-54 does not.
- `SRC/rag/config.py` (77 lines) — Why: structure and the `load_config(env)` signature port; `OllamaConfig`
  is replaced. Note the comment at lines 34-38 recording where the thresholds came from.
- `SRC/rag/vectorstore.py` lines 14-18 — Why: `VectorHit.distance` and the `similarity = 1 - distance`
  comment. This is the semantic the whole gate depends on.
- `SRC/chat/streaming.py` (12 lines) — Why: the NDJSON `frame()` helper and the reason it is safe
  (`json.dumps` escapes newlines, so answer text cannot split a frame).
- `SRC/documents/models.py` lines 36-39 — Why: the `vector_id` shape `f"{document_id}_{chunk_index}"`.

**Port the tests (these are the parity harness):**

- `SRC/tests/unit/test_chunking.py` (77 lines) — Why: ports with zero edits. Imports only `rag.chunking`, `rag.config`.
- `SRC/tests/unit/test_fusion.py` (47 lines) — Why: ports with zero edits.
- `SRC/tests/unit/test_gate.py` (113 lines) — Why: ports with zero edits. Read `test_mean_similarity_does_not_affect_the_decision` (lines 59-66) and `test_nan_similarity_fails_closed_rather_than_open` (lines 92-97) — both are deliberate pins.
- `SRC/tests/unit/test_prompts.py` (133 lines) — Why: ports with **5 assertions changed** (Task 6). Lines 41-47, 42, 45-47 assert on upload-specific copy.
- `SRC/tests/unit/test_generation.py` (141 lines) — Why: lines 12-74 and 93-114 port as `test_sentinel.py`. Lines 76-90 (`test_stream_chat_*`) and 117-141 (`test_malformed_json_line_*`) do **not** port.
- `SRC/tests/unit/test_config.py` (38 lines) — Why: the *shape* ports (defaults / typed-overrides / frozen); the assertions change with the config.
- `SRC/tests/unit/test_rag_purity.py` (13 lines) — Why: the idea ports, the implementation is replaced (Task 14).
- `SRC/tests/conftest.py` — Why: `FakeEmbedder`'s orthogonal-axis design (known terms → fixed axes, everything else → an "unrelated" axis, width 768) is the model for `rag_adapters/fakes.py`.

**Epic context:**

- `docs/ARCHITECTURE.md` §3 (components), §4 (ports, verbatim Protocol definitions), §7 (query path), ADR-001, ADR-003
- `docs/PRD.md` F5–F13, non-goals in §4
- `docs/tickets/medical-rag-hosted-version.md` — TICKET-1 body, and decisions D1–D6

### New Files to Create

```
DST/
├── pyproject.toml                          # uv project, ruff, mypy, pytest config
├── .gitignore
├── .env.example
├── README.md                               # skeleton; TICKET-10 writes the real one
├── .github/workflows/test.yml
├── src/
│   ├── rag_core/                           # PURE — stdlib only
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── contracts.py
│   │   ├── errors.py
│   │   ├── chunking.py                     # verbatim port
│   │   ├── fusion.py                       # verbatim port
│   │   ├── gate.py                         # verbatim port
│   │   ├── prompts.py                      # verbatim logic, rewritten copy
│   │   ├── sentinel.py                     # split out of SRC/rag/generation.py
│   │   ├── ports.py
│   │   └── pipeline.py
│   └── rag_adapters/                       # IMPURE — may import anything
│       ├── __init__.py
│       ├── fakes.py
│       └── profile.py
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_chunking.py                # verbatim
    │   ├── test_fusion.py                  # verbatim
    │   ├── test_gate.py                    # verbatim
    │   ├── test_prompts.py                 # copy assertions updated
    │   ├── test_sentinel.py                # subset of SRC test_generation.py
    │   ├── test_config.py                  # rewritten for the new shape
    │   ├── test_contracts.py               # new
    │   ├── test_pipeline.py                # new
    │   └── test_core_purity.py             # replaces test_rag_purity.py
    └── contract/
        └── test_port_contract.py           # shared suite; fakes only in this ticket
```

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [uv — project structure and layout](https://docs.astral.sh/uv/concepts/projects/layout/)
  - Specific section: src layout and `[build-system]`
  - Why: `src/` layout is what keeps `import rag_core` resolving to the installed package rather than the
    working directory, which is exactly what makes the purity test meaningful.
- [pytest-asyncio — configuration reference](https://pytest-asyncio.readthedocs.io/en/stable/reference/configuration.html)
  - Specific sections: `asyncio_mode`, `asyncio_default_fixture_loop_scope`
  - Why: `asyncio_mode = "auto"` lets async tests run without decorating each one. Leaving
    `asyncio_default_fixture_loop_scope` unset emits a deprecation warning on every run — set it to
    `"function"`.
- [typing.Protocol](https://docs.python.org/3/library/typing.html#typing.Protocol)
  - Why: the four ports. Also read `runtime_checkable` and understand why we are *not* using it (below).
- [asyncio.gather](https://docs.python.org/3/library/asyncio-task.html#asyncio.gather)
  - Why: the concurrent dense + lexical legs. Note `return_exceptions` defaults to `False` — one leg failing
    cancels the other, which is the behaviour we want (Architecture §8: embedding/store failure fails the request).
- [Ruff — configuration](https://docs.astral.sh/ruff/configuration/)
  - Why: lint + format config in `pyproject.toml`.
- [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv)
  - Why: the CI workflow.

### Patterns to Follow

These come from SRC and are the house style. Match them.

**Module docstrings state the *why*, not the *what*.** Every module in `SRC/rag/` opens with a short
docstring naming the failure the module prevents. Keep this, but replace the `(spec 6.2)` references — they
point at a spec that does not exist in this repo. Point at `docs/ARCHITECTURE.md §N` or the relevant ADR instead.

```python
"""Reciprocal rank fusion.

RRF compares only positions in ranked lists, never raw scores. This matters
because the two legs produce incompatible scales — cosine distance in [0, 2]
and ts_rank_cd in arbitrary positive units — with no principled normalisation
between them (ARCHITECTURE.md §7).
"""
```

**`from __future__ import annotations` on every module.** Present in all 11 files in `SRC/rag/`.

**Frozen dataclasses for every value type.** `GateSignals`, `GateDecision`, `FusedHit`, `ChunkDraft`,
`PageText`, `ContextChunk`, `VectorHit`, and every config class. `SRC/tests/unit/test_gate.py:76-80` asserts
frozenness explicitly — carry that habit.

**Comments record rejected alternatives and measured facts.** This is the most distinctive thing about the
codebase and it is why the ported comments must survive. Example from `SRC/rag/vectorstore.py:52-61`:

```python
def query(self, embedding: list[float], n_results: int) -> list[VectorHit]:
    """Nearest neighbours, closest first.

    No count() guard or clamp here deliberately. Verified against chromadb
    1.5.9: querying an empty collection returns [[]] ... Clamping with count()
    previously added two backend round-trips per query and a TOCTOU window
    that could drive n_results to zero and crash.
    """
```

**Guards fail closed and say why.** From `SRC/rag/gate.py:52-57`:

```python
# A non-finite similarity must fail CLOSED. NaN compares False against every
# threshold, so without this guard it falls through every check and reaches
# `ok` — the most permissive outcome from the most degenerate input, in the
# one component whose job is to decline when uncertain.
if not math.isfinite(signals.top_similarity):
    return GateDecision(False, "off_domain", payload)
```

**Test naming is a full sentence describing the behaviour**, not `test_gate_1`:
`test_nan_similarity_fails_closed_rather_than_open`, `test_an_oversized_turn_does_not_discard_smaller_older_turns`,
`test_lexical_support_cannot_rescue_below_tau_abstain`.

**Regression tests carry the story.** From `SRC/tests/unit/test_generation.py:93-97`:

```python
def test_preamble_split_across_deltas_near_the_buffer_boundary_still_declines():
    """Regression: the decision once fired at 40 chars while a tolerated
    preamble plus the sentinel needs 44, locking in a false negative and
    leaking the raw sentinel to the user."""
```

**Anti-patterns to avoid** (all absent from SRC, keep them absent): unittest-style test classes; `assert True`
placeholder tests; bare `except:`; provider names leaking into `rag_core` identifiers; config read from the
environment anywhere except `load_config()`.

---

## IMPLEMENTATION PLAN

### Phase 1: Repository foundation

Get a green, empty test run before porting anything. Every later task's `VALIDATE` command depends on this
working.

**Tasks:** git init, `pyproject.toml` with all four tools configured, `src/` package skeleton, CI workflow,
GitHub repo created and pushed.

### Phase 2: Verbatim ports

**Depends on:** Phase 1.

The mechanical half. No design decisions — copy the module, rewrite the imports, copy the test, run it.
A failing ported test means the port is wrong.

**Tasks:** `chunking.py`, `fusion.py`, `gate.py` and their three test files; `errors.py`; `sentinel.py`;
`prompts.py` (the one port with a deliberate deviation).

### Phase 3: The new contract surface

**Depends on:** Phase 2 (`contracts.py` imports `ContextChunk` from `prompts.py`; `config.py` holds
`GateConfig`, which `gate.py` already needs).

The design half — data model, config, and the four Protocols.

**Tasks:** `contracts.py`, `config.py`, `ports.py`.

### Phase 4: Fakes, pipeline, composition root

**Depends on:** Phase 3.

**Tasks:** `rag_adapters/fakes.py`, `pipeline.py`, `rag_adapters/profile.py`, the shared port contract suite.

### Phase 5: Guardrails and documentation

**Depends on:** Phase 4 (the purity test must scan the finished package).
**Independent of:** nothing in this ticket — but Task 15 (doc reconciliation) touches only `docs/` and could
be done at any point, including in parallel by a second loop.

**Tasks:** `test_core_purity.py`, doc reconciliation, README skeleton.

---

## STEP-BY-STEP TASKS

Execute in order, top to bottom. Run each task's `VALIDATE` before moving on.

### 1. CREATE repository scaffold and tooling

- **IMPLEMENT**: `git init` in DST (on `main`). Create `pyproject.toml` declaring a `uv` project,
  `requires-python = ">=3.12"`, hatchling build backend with `packages = ["src/rag_core", "src/rag_adapters"]`,
  **zero runtime dependencies**, and a dev group of `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
  Configure all three tools in the same file:
  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  python_files = "test_*.py"
  asyncio_mode = "auto"
  asyncio_default_fixture_loop_scope = "function"

  [tool.ruff]
  line-length = 100          # SRC's effective width; matches the ported files

  [tool.mypy]
  files = ["src"]
  strict = true
  ```
  Create `.gitignore` (`.venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.env`, `*.egg-info`),
  `.env.example` (every key `load_config` reads, no values), and the four `__init__.py` files.
- **PATTERN**: `SRC/pyproject.toml` for the `[dependency-groups] dev` shape; `SRC/pytest.ini` for
  `python_files` / `testpaths`.
- **IMPORTS**: n/a
- **GOTCHA**: `line-length = 100` is not the ruff default (88). The ported files were written at ~95 columns —
  the wide explanatory comments in `gate.py` and `retrieval.py` will be reflowed by the formatter at 88, which
  produces a large, meaningless diff against SRC and makes real divergence harder to spot later. Set it before
  you format anything.
- **GOTCHA**: `mypy strict = true` will reject the ported modules until they are fully annotated. They already
  are — SRC annotates throughout — but `chunk_pages` and friends use `list[str]` style that needs
  `from __future__ import annotations` to parse on 3.12. It is already in every file; do not drop it.
- **VALIDATE**: `cd DST && uv sync && uv run pytest --collect-only && uv run ruff check .`
- **SATISFIES**: AC #1 (toolchain half)

### 2. CREATE the GitHub repository and CI workflow

- **IMPLEMENT**: `.github/workflows/test.yml` — on push and PR: checkout, `astral-sh/setup-uv`, `uv sync`,
  then `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run pytest`.
  Then `gh repo create medical-rag-hosted-version --public --source=. --remote=origin --push`.
- **PATTERN**: none in SRC (it has no CI) — this is new.
- **IMPORTS**: n/a
- **GOTCHA**: **The repo is public from the first commit** (confirmed decision). Before the first push, confirm
  `.gitignore` excludes `.env` and that no key, connection string or token exists anywhere in the tree.
  `.env.example` carries key *names* only. Once pushed, a secret is public and rotating it is the only fix.
- **GOTCHA**: `gh repo create` is the one outward-facing step in this ticket. It publishes. Run it only after
  the tree is clean.
- **VALIDATE**: `gh repo view medical-rag-hosted-version --json visibility,url` and confirm the first CI run
  goes green: `gh run list --limit 1`
- **SATISFIES**: AC #1 (CI half)

### 3. PORT `chunking.py`, `fusion.py`, `gate.py` and their tests — verbatim

- **IMPLEMENT**: Copy each module from `SRC/rag/` to `src/rag_core/`, changing **only** the relative imports
  (`from .config import ChunkConfig` still resolves — the package-relative form is unchanged) and the
  `(spec N.N)` references in docstrings, which become `docs/ARCHITECTURE.md §N` / ADR references.
  Copy `SRC/tests/unit/test_{chunking,fusion,gate}.py` to `tests/unit/` and change `from rag.X` → `from rag_core.X`.
  Nothing else changes in any of the six files.
- **PATTERN**: n/a — this *is* the pattern source.
- **IMPORTS**: `chunking` imports `.config.ChunkConfig`; `gate` imports `.config.GateConfig`. `fusion` imports
  nothing from the package. Task 8 creates `config.py` — until then these will not import. **Create a minimal
  `config.py` stub containing just `ChunkConfig` and `GateConfig` now** (verbatim from `SRC/rag/config.py:18-38`)
  and complete it in Task 8.
- **GOTCHA**: `test_gate.py` uses `GateConfig(tau_abstain=0.30, tau_strong=0.45)` — its own values, not the
  defaults. It is therefore immune to the threshold invalidation in Task 8. Do not "helpfully" update it.
- **GOTCHA**: Do not reformat. If `ruff format` wants to change these files, your `line-length` is wrong
  (Task 1). Diffability against SRC is the point.
- **VALIDATE**: `uv run pytest tests/unit/test_chunking.py tests/unit/test_fusion.py tests/unit/test_gate.py -v`
  — expect all to pass with **zero edits to the test bodies**. Then confirm the port is faithful:
  `diff <(sed 's/^from rag\./from rag_core./' SRC/tests/unit/test_gate.py) tests/unit/test_gate.py`
- **SATISFIES**: AC #3, AC #4 (partial)

### 4. CREATE `src/rag_core/errors.py`

- **IMPLEMENT**: The provider-agnostic error family, mirroring `SRC/rag/ollama.py:9-25`:
  `ProviderError(RuntimeError)` (base), `ProviderUnavailable(ProviderError)`,
  `ProviderProtocolError(ProviderError, ValueError)`, `AllProvidersUnavailable(ProviderError)`,
  `ManifestMismatch(RuntimeError)`.
- **PATTERN**: `SRC/rag/ollama.py:17-25` — port that docstring's reasoning almost verbatim. It explains why
  the protocol error subclasses both the transport base *and* `ValueError`, and that reasoning is unchanged
  by the provider swap.
- **IMPORTS**: stdlib only.
- **GOTCHA**: `AllProvidersUnavailable` and `ManifestMismatch` have no consumer in this ticket — TICKET-3 and
  TICKET-5 raise them. Define them here anyway so the shells import one error module rather than three.
  `ManifestMismatch` is deliberately *not* a `ProviderError`: it is a deployment fault, not a transport fault,
  and Architecture §8 maps them to different behaviours (refuse to serve vs. fail the request).
- **VALIDATE**: `uv run python -c "from rag_core.errors import ProviderProtocolError as E; assert issubclass(E, ValueError) and issubclass(E, __import__('rag_core.errors', fromlist=['x']).ProviderError)"`
- **SATISFIES**: AC #1

### 5. PORT `sentinel.py` and its tests

- **IMPLEMENT**: Create `src/rag_core/sentinel.py` from `SRC/rag/generation.py` lines 18-19 and 48-99 —
  `BUFFER_CHARS`, `PREAMBLE_TOLERANCE`, `_is_sentinel`, `filter_sentinel`. Import `SENTINEL` from
  `.prompts`. Write a module docstring explaining the buffering (adapt `SRC/rag/generation.py:1-6`: the
  sentinel cannot be streamed and then retracted, so output is buffered until the decision is conclusive).
  Create `tests/unit/test_sentinel.py` from `SRC/tests/unit/test_generation.py` lines 1-74 and 93-114.
- **PATTERN**: `SRC/rag/generation.py:64-99`.
- **IMPORTS**: `from .prompts import SENTINEL`. Task 6 creates `prompts.py` — do Task 6 first if you prefer;
  they are otherwise independent.
- **GOTCHA**: **Do not port** `test_stream_chat_extracts_message_content_deltas`,
  `test_stream_chat_ignores_lines_without_content` (SRC lines 76-90) or
  `test_malformed_json_line_becomes_ollama_unavailable` (SRC lines 117-141). All three test the Ollama HTTP
  transport, which does not exist here. TICKET-3 writes the equivalents for Gemini and Groq.
- **GOTCHA**: `filter_sentinel` is a **sync** generator over a sync iterable, and it stays that way — it is
  pure string processing. TICKET-5 will feed it from an async token stream; adapting the iteration is the
  shell's job, not the core's. Do not make this async.
- **GOTCHA**: The `threshold = max(buffer_chars, PREAMBLE_TOLERANCE + len(sentinel))` line
  (`SRC/rag/generation.py:77`) looks redundant — it is not. `test_preamble_split_across_deltas_near_the_buffer_boundary_still_declines`
  is the regression test for exactly the version without it.
- **VALIDATE**: `uv run pytest tests/unit/test_sentinel.py -v` — 17 tests, all passing, bodies unedited.
- **SATISFIES**: AC #3, AC #4 (partial)

### 6. PORT `prompts.py` with rewritten user-facing copy

- **IMPLEMENT**: Copy `SRC/rag/prompts.py`. Keep `SENTINEL`, `ContextChunk`, `format_context`,
  `_trim_history`, `build_messages`, and the `DECLINE_COPY` / `FALLBACK_DECLINE` *structure* — all logic
  byte-identical. **Rewrite the four `DECLINE_COPY` strings, `FALLBACK_DECLINE`, and `SYSTEM_TEMPLATE`'s
  first two lines** for a fixed, pre-ingested corpus. Every one of them currently references uploading, which
  does not exist in this profile:
  | key | current (SRC) | must become |
  |---|---|---|
  | `empty_corpus` | "No documents have been uploaded yet. Upload a medical reference document…" | a deployment-fault message — the corpus is pre-ingested, so this state means the index is unpopulated |
  | `off_domain` | "…grounded in the medical documents you've uploaded, and this question doesn't relate to them." | "…grounded in the drug labels in this corpus, and this question isn't covered by them." |
  | `weak_unsupported` | "…Try rephrasing, or upload a document that covers it." | "…Try rephrasing." — drop the upload suggestion, there is no remedy the reader can take |
  | `insufficient_context` | "Your uploaded documents cover this topic…" | "This corpus covers this topic, but doesn't contain enough detail…" |
  | `SYSTEM_TEMPLATE` L36-39 | "for the user's uploaded medical documents" / "drawn from documents the user has uploaded" | "for a fixed corpus of public drug labels" / "drawn from that corpus" |
  Then update `tests/unit/test_prompts.py`: `test_empty_corpus_copy_mentions_uploading` becomes an assertion
  about the corpus wording, and any other assertion that greps for upload vocabulary moves with it.
- **PATTERN**: `SRC/rag/prompts.py`. The module docstring's point — "Declines are generated by the server,
  never by the model — that is what makes them consistent, testable, and identical between the eval harness
  and the UI" — is unchanged and ports as-is.
- **IMPORTS**: stdlib only (`dataclasses`).
- **GOTCHA**: `_trim_history` and the five history tests **port verbatim** (confirmed decision) even though
  the hosted shell always passes `history=[]`. This keeps `prompts.py` diffable against SRC and leaves the
  deferred multi-turn work already built and tested. Do not delete it as dead code.
- **GOTCHA**: Keep the copy *distinct per reason* — `test_decline_copy_is_distinct_per_reason` asserts the
  values are unique, and it is there because indistinguishable refusals make the telemetry strip useless.
- **GOTCHA**: Do not name the three drugs in the copy. Rejected at slicing time — it hardcodes corpus
  contents into `rag_core`, and the corpus is TICKET-4's concern.
- **GOTCHA**: `SYSTEM_TEMPLATE`'s sentinel instruction (lines 41-43: "nothing before it, nothing after it, no
  greeting") is pinned by `test_sentinel_instruction_forbids_any_surrounding_text`. Detection is
  `startswith`-based. Do not soften that wording while editing the surrounding lines.
- **VALIDATE**: `uv run pytest tests/unit/test_prompts.py -v` and
  `uv run python -c "from rag_core.prompts import DECLINE_COPY, SYSTEM_TEMPLATE; assert not any('upload' in v.lower() for v in DECLINE_COPY.values()); assert 'upload' not in SYSTEM_TEMPLATE.lower()"`
- **SATISFIES**: AC #3 (with the documented deviation), AC #5

### 7. CREATE `src/rag_core/contracts.py`

- **IMPLEMENT**: The data model and wire contract.
  - `Vector = list[float]`, `Token = str`
  - `Chunk` (frozen): `id: str`, `document_id: str`, `ordinal: int`, `anchor: str`, `content: str`,
    `document_title: str`. `id` is `f"{document_id}_{ordinal}"` — provide a `make_chunk_id(document_id, ordinal)`
    helper and a `split_chunk_id(chunk_id) -> tuple[str, int]` that splits on the **last** underscore.
  - `EmbeddedChunk` (frozen): `chunk: Chunk`, `embedding: Vector`
  - `Scored[T]` (frozen, generic): `item: T`, `score: float` — the provider's native score, untransformed
    (Architecture §4, ADR-003).
  - `to_context_chunk(chunk: Chunk) -> ContextChunk` — bridges the storage model to `prompts.ContextChunk`.
  - `FRAME_TYPES = ("meta", "token", "sources", "error", "done")` and `frame(kind, **fields) -> str`,
    ported from `SRC/chat/streaming.py`.
  - `Telemetry` (frozen): `retrieval_ms`, `ttft_ms: float | None`, `total_tokens`, `provider: str | None`,
    `gate_proceed`, `gate_reason`, `similarity_ok: bool`, `lexical_support: bool`, `top_similarity: float | None`,
    `fused_scores: list[float]`, plus `as_dict()`.
  - `explain_gate(decision: GateDecision, cfg: GateConfig) -> tuple[bool, bool]` — returns
    `(similarity_ok, lexical_support)` so the two gate conditions can be reported **independently**
    (ADR-003 action item 3).
- **PATTERN**: `SRC/rag/gate.py:30-36` for the `as_dict()` + `_jsonable` approach — `Telemetry.as_dict()`
  must produce valid JSON for non-finite floats too, for exactly the reason `_jsonable` exists.
- **IMPORTS**: `dataclasses`, `typing` (`Generic`, `TypeVar`), `json`; `from .prompts import ContextChunk`;
  `from .gate import GateDecision`; `from .config import GateConfig`.
- **GOTCHA**: `split_chunk_id` must split on the **last** underscore, not the first.
  `SRC/chat/retrieval.py:30` uses `partition("_")` because the local `document_id` is an integer. Here it is a
  text slug (`metformin`, and a future `some_drug_name` is entirely plausible). Getting this wrong does not
  raise — `_hydrate`-style code drops unresolvable ids silently, so every retrieved chunk would vanish and
  the system would refuse everything. Write the test for `metformin_12` **and** `some_drug_name_3`.
- **GOTCHA**: `ContextChunk.page_number` is an `int` while `Chunk.anchor` is `str` (Architecture §5).
  `to_context_chunk` does the conversion. This is safe only because the anchor is always a page number for
  this corpus (D4) — assert it and let it raise loudly if that ever changes, rather than silently formatting
  "p. section-3".
- **GOTCHA**: `Scored` must not normalise. ADR-003's whole argument is that the gate needs raw magnitude, and
  a helpful `score = 1 - distance` inside the adapter is precisely the bug the ADR exists to prevent.
- **VALIDATE**: `uv run pytest tests/unit/test_contracts.py -v` — cover round-tripping `make_chunk_id` /
  `split_chunk_id` for slugs with underscores, `frame()` output being one line of valid JSON with an embedded
  newline in the payload, `Telemetry.as_dict()` surviving `json.dumps` with a NaN similarity, and
  `explain_gate` returning `(False, _)` for an off-domain decision and `(True, False)` for `weak_unsupported`.
- **SATISFIES**: AC #1, and unblocks TICKET-5 / TICKET-7

### 8. CREATE `src/rag_core/config.py` and its tests

- **IMPLEMENT**: Complete the stub from Task 3. Keep `ChunkConfig` (1000/150), `RetrievalConfig` (10/4/60),
  `GateConfig`, and the `load_config(env: Mapping[str,str] | None = None) -> RagConfig` shape with its `_f`/`_i`/`_s`
  coercion helpers. Replace `OllamaConfig` with:
  - `EmbeddingConfig`: `model_id`, `dimension: int = 768`, `batch_size`, `request_timeout_s`
  - `GenerationConfig`: `primary_model_id`, `secondary_model_id`, `request_timeout_s`
  - `RagConfig`: `embedding`, `generation`, `chunk`, `retrieval`, `gate`, `profile: str`
  Drop `max_upload_mb` (no uploads). Keep `history_messages = 4` (Task 6 keeps the history code).
  Rewrite `tests/unit/test_config.py` for the new shape, keeping all three test *kinds*: defaults are pinned,
  env overrides are typed, config is frozen.
- **PATTERN**: `SRC/rag/config.py` in full — same dataclass-per-concern structure, same `load_config` shape.
- **IMPORTS**: `os`, `dataclasses`, `typing.Mapping`.
- **GOTCHA**: **The thresholds are invalid.** Carry `tau_abstain=0.70` / `tau_strong=0.75` across so the
  system runs, but replace `SRC/rag/config.py:33-38`'s comment with one that says plainly they were measured
  against `nomic-embed-text` and are meaningless for a different embedding model, and points at TICKET-8.
  ADR-003's consequence — "τ from the local build is meaningless here… the old value must be discarded, not
  carried over" — is the reason. A carried-over number with a stale provenance comment is worse than no
  number, because it reads as measured.
- **GOTCHA**: Do not give `EmbeddingConfig.model_id` a confident default. The spike verifies which Gemini
  model emits 768 dimensions; until then the default should be an obviously-placeholder value that
  `.env.example` overrides, not a guess that looks authoritative.
- **GOTCHA**: `dimension` belongs in config **and** in `index_manifest` (Architecture §5). They are compared
  at startup in TICKET-5 — this is the field that comparison reads.
- **VALIDATE**: `uv run pytest tests/unit/test_config.py -v`
- **SATISFIES**: AC #1

### 9. CREATE `src/rag_core/ports.py`

- **IMPLEMENT**: The four `Protocol`s from Architecture §4, async:
  ```python
  class EmbeddingProvider(Protocol):
      model_id: str
      dimension: int
      async def embed_documents(self, texts: list[str]) -> list[Vector]: ...
      async def embed_query(self, text: str) -> Vector: ...

  class GenerationProvider(Protocol):
      model_id: str
      def stream(self, messages: list[dict]) -> AsyncIterator[Token]: ...

  class DenseStore(Protocol):
      async def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]: ...
      async def upsert(self, chunks: list[EmbeddedChunk]) -> None: ...
      async def count(self) -> int: ...

  class LexicalStore(Protocol):
      async def search(self, query: str, k: int) -> list[Scored[Chunk]]: ...
      async def index(self, chunks: list[Chunk]) -> None: ...
  ```
  Document the **score direction** for each store in the Protocol docstrings — this is the contract the
  adapters must honour and the contract suite tests:
  - `DenseStore.search` → cosine **distance**, ascending (closest first). `similarity = 1 - score`.
  - `LexicalStore.search` → relevance rank, **descending** (best first).
- **PATTERN**: Architecture §4 gives the signatures verbatim; `SRC/rag/vectorstore.py:14-18` gives the
  distance semantics.
- **IMPORTS**: `typing.Protocol`, `typing.AsyncIterator`; `from .contracts import Chunk, EmbeddedChunk, Scored, Token, Vector`.
- **GOTCHA**: `GenerationProvider.stream` is declared `def`, **not** `async def`. An async generator
  (`async def` + `yield`) returns an `AsyncIterator` when *called*, without awaiting. Declaring the Protocol
  method `async def` would require implementations to be awaited before iterating, which no async generator
  satisfies. This trips people up; the annotation above is correct.
- **GOTCHA**: Architecture §4 does **not** list `count()` on `DenseStore`. It is added because `retrieve()`
  needs the `corpus_empty` gate reason (`SRC/chat/retrieval.py:58-60`) and that reason has distinct
  user-facing copy. Recorded as a deviation in OPEN QUESTIONS. Task 11 keeps it off the hot path.
- **GOTCHA**: Do **not** use `@runtime_checkable`. It only checks method *names*, never signatures, so it
  would pass an adapter with the wrong arity and give false confidence. The contract suite in Task 13 is the
  real check; mypy is the compile-time one.
- **VALIDATE**: `uv run mypy` — clean.
- **SATISFIES**: AC #1

### 10. CREATE `src/rag_adapters/fakes.py`

- **IMPLEMENT**: In-memory implementations of all four ports, deterministic, zero network.
  - `FakeEmbedder`: orthogonal-axis scheme from `SRC/tests/conftest.py` — known terms map to fixed axes so
    cosine distances are predictable and "france" is maximally far from "metformin"; everything unrecognised
    lands on the unrelated axis. Width **768**, matching `dimension`. Counts batches so tests can assert
    batching. Add `ExplodingEmbedder` for failure-path tests.
  - `FakeDenseStore`: in-memory list, brute-force cosine **distance**, ascending. Honours the score direction
    contract exactly.
  - `FakeLexicalStore`: naive term-overlap score, descending.
  - `FakeGenerator`: yields a scripted token list; constructible to raise, and to emit the sentinel.
- **PATTERN**: `SRC/tests/conftest.py` in full — the docstring explains why the axis scheme exists ("width
  matches the real model (768) so tests cannot pass against a dimensionality the production path would reject").
- **IMPORTS**: `math`, `dataclasses`; `from rag_core.contracts import ...`; `from rag_core.errors import ...`.
- **GOTCHA**: These live in `src/rag_adapters/`, **not** in `tests/`. They are the second implementation of
  each port — the thing that justifies the ports existing at all under ADR-001's "delete a port that only ever
  has one implementation" rule — and TICKET-5's integration tests import them.
- **GOTCHA**: `FakeDenseStore` must return **distance**, not similarity. It is the reference implementation
  every real adapter is checked against; if the fake has the direction backwards, the contract suite
  enshrines the bug.
- **VALIDATE**: `uv run pytest tests/contract/test_port_contract.py -v` (written in Task 13 — run this task's
  validation after that one, or write a throwaway smoke test now).
- **SATISFIES**: AC #1, AC #4

### 11. CREATE `src/rag_core/pipeline.py` and its tests

- **IMPLEMENT**: `async def retrieve(question, embedder, dense, lexical, cfg) -> RetrievalResult`, a
  Django-free rewrite of `SRC/chat/retrieval.py`:
  1. `await embedder.embed_query(question)`
  2. Issue both legs concurrently: `await asyncio.gather(dense.search(vec, per_leg), lexical.search(question, per_leg))`
  3. `similarities = [1.0 - hit.score for hit in dense_hits]`; `top_similarity = max(...) if ... else 0.0`
  4. `reciprocal_rank_fusion([[dense ids], [lexical ids]], k=cfg.retrieval.rrf_k)`; take `top_k`
  5. `mean_similarity` over **delivered** chunks only — port the 20-line comment at
     `SRC/chat/retrieval.py:73-92` verbatim
  6. `lexical_support = bool(set(top_ids) & set(lexical_ids))` — port the 7-line comment at
     `SRC/chat/retrieval.py:98-104` verbatim
  7. Corpus-empty check: **only if both legs returned zero results**, `await dense.count()`; if 0, return the
     `corpus_empty` decision
  8. `evaluate_gate(signals, cfg.gate)`; hydrate from the union of both legs' chunks if `decision.proceed`
  Hydration is `{c.item.id: c.item for c in dense_hits + lexical_hits}` then map `top_ids` through
  `to_context_chunk`.
- **PATTERN**: `SRC/chat/retrieval.py` — same order of operations, same signal computation, same dataclass
  (`RetrievalResult(decision, chunks)`).
- **IMPORTS**: `asyncio`, `dataclasses`; `from .fusion import reciprocal_rank_fusion`;
  `from .gate import GateDecision, GateSignals, evaluate_gate`; `from .config import RagConfig`;
  `from .contracts import Scored, Chunk, to_context_chunk`; `from .ports import ...`.
- **GOTCHA**: The corpus-empty reordering (step 7) is a deliberate improvement over SRC, which counts on every
  query. It is behaviour-identical — if either leg returned a result the corpus is not empty — but it must be
  *tested* as such: a test asserting `count()` is never awaited when either leg returns a hit, and a test
  asserting the `corpus_empty` reason still fires when both are empty and count is 0.
- **GOTCHA**: Every `top_id` is now guaranteed present in the union, because RRF only ranks ids that came from
  a leg. SRC's `if row is None: continue` (line 45) has no equivalent condition here. **Assert** instead of
  skipping — a missing id would mean a fusion bug, and silently dropping it is how a system ends up refusing
  everything for a reason nobody can find.
- **GOTCHA**: `asyncio.gather` with default `return_exceptions=False` cancels the sibling leg when one fails
  and propagates. That is correct per Architecture §8 (an embedding or store failure fails the request; there
  is no meaningful degraded retrieval), but it means a lexical failure now also kills a perfectly good dense
  result. If you want the other behaviour, that is a change to Architecture §8, not a local decision.
- **GOTCHA**: `mean_similarity` is recorded but the gate **must not read it**. `test_mean_similarity_does_not_affect_the_decision`
  in the ported `test_gate.py` pins this. Do not wire it in.
- **VALIDATE**: `uv run pytest tests/unit/test_pipeline.py -v` — cover: both legs concurrent (assert with an
  ordering probe or timing, not a sleep race), a dense-only hit, a lexical-only hit, agreement across both,
  empty corpus, gate decline yielding zero chunks, and `top_similarity` derived as `1 - distance`.
- **SATISFIES**: AC #4

### 12. CREATE `src/rag_adapters/profile.py`

- **IMPLEMENT**: The composition root. `build_profile(cfg: RagConfig) -> Profile` reads the profile env var
  **once** and returns a frozen container of the four adapters. Ship with `"fake"` wired. Provide an explicit
  registration seam — a `_REGISTRY: dict[str, Callable[[RagConfig], Profile]]` — so TICKET-2 and TICKET-3 each
  add one entry rather than editing a growing if/elif.
- **PATTERN**: `SRC/documents/services.py` for the singleton idea, but **without** the double-checked locking
  — that exists because Django's sync views run in a threadpool and race chromadb's tenant init. Resolving
  once at startup under FastAPI does not have that problem, and copying the lock would be cargo-culting a fix
  for a bug this repo does not have.
- **IMPORTS**: `os`, `dataclasses`; `from rag_core.ports import ...`; `from .fakes import ...`.
- **GOTCHA**: This module lives in `rag_adapters`, not `rag_core` — it imports concrete adapters, and
  Architecture §3 forbids that inside the core. The purity test in Task 14 enforces the direction.
- **GOTCHA**: Architecture §4: "Profile selection is one environment variable read at startup, resolved once
  into a container of adapters. No conditional provider logic anywhere below that." An unknown profile name
  must raise at startup with the valid names listed, not fall back to a default.
- **VALIDATE**: `uv run pytest tests/unit/test_profile.py -v` — the fake profile resolves; an unknown name
  raises and names the valid options.
- **SATISFIES**: AC #1

### 13. CREATE `tests/contract/test_port_contract.py`

- **IMPLEMENT**: One parametrised suite per port, run against every registered implementation. In this ticket
  only the fakes are registered; TICKET-2 and TICKET-3 add theirs to the same parametrisation and inherit the
  whole suite. Assert the behaviours that adapters get wrong:
  - `DenseStore.search` returns results ordered by **ascending** score; `k` larger than the corpus returns
    fewer rows rather than raising; `k <= 0` returns `[]`; an empty store returns `[]`.
  - `LexicalStore.search` returns **descending** score; a query that sanitises to nothing returns `[]`.
  - `upsert`/`index` are idempotent on `chunk.id`.
  - `EmbeddingProvider.embed_documents` returns exactly one vector per input, each of width `dimension`.
  - `GenerationProvider.stream` is iterable without awaiting the call itself, and `model_id` is non-empty.
- **PATTERN**: `SRC/tests/contract/test_ollama_contract.py` for the marker-based structure. Note
  `SRC/pytest.ini`'s `markers` + `addopts = -m "not ollama"` — the same idea applies here: mark the tests that
  need a live external service so they deselect by default. In this ticket none do.
- **IMPORTS**: `pytest`; `from rag_adapters.fakes import ...`.
- **GOTCHA**: Architecture §11: "every adapter runs the same suite against its port, so local and hosted are
  held to one specification." Write this suite so a new implementation is **one line** in the parametrisation.
  If TICKET-2 has to restructure it, this task was done wrong.
- **GOTCHA**: The score-direction assertions are the highest-value tests in this file. They are the only thing
  standing between a helpfully-normalising adapter and a gate whose thresholds silently stop meaning anything.
- **VALIDATE**: `uv run pytest tests/contract/ -v`
- **SATISFIES**: AC #1, AC #4

### 14. CREATE `tests/unit/test_core_purity.py`

- **IMPLEMENT**: Replace `SRC/tests/unit/test_rag_purity.py`'s single django check with four, scanning every
  `.py` under `src/rag_core/`:
  1. No web framework: `django`, `fastapi`, `flask`, `starlette`
  2. No provider SDK: `google`, `groq`, `openai`, `anthropic`, `chromadb`, `ollama`, `httpx`, `requests`
  3. No database driver: `psycopg`, `asyncpg`, `sqlalchemy`, `sqlite3`
  4. No `rag_adapters` import — the dependency direction only ever points inward
  Report every offender with file and matched module, as SRC does.
- **PATTERN**: `SRC/tests/unit/test_rag_purity.py` — same `rglob` + regex approach, same
  `assert offenders == []` with a message naming the files.
- **IMPORTS**: `pathlib`, `re`.
- **GOTCHA**: Anchor the path off `__file__` (`parents[2] / "src" / "rag_core"`), as SRC does. A hardcoded
  relative path breaks when pytest is invoked from elsewhere and the test silently scans nothing — a purity
  test that passes vacuously is worse than none.
- **GOTCHA**: Match the module *root* (`^\s*(import|from)\s+google\b`), not a substring. A bare substring
  match on `re` or `json` would hit half the standard library.
- **GOTCHA**: This test must scan `rag_core` only. `rag_adapters` is *supposed* to import provider SDKs.
- **VALIDATE**: `uv run pytest tests/unit/test_core_purity.py -v`, then prove it can fail: temporarily add
  `import httpx` to `src/rag_core/gate.py`, confirm red, revert.
- **SATISFIES**: AC #1, AC #2

### 15. UPDATE `docs/ARCHITECTURE.md` and `docs/PRD.md`; CREATE `README.md`

- **IMPLEMENT**: Apply the six corrections listed under **Corrections to the source docs** in
  `docs/tickets/medical-rag-hosted-version.md`:
  - **ADR-001** — rewrite Decision and Consequences for the standalone-repo call (D1). Keep the Options
    analysis; its warning that two copies drift within weeks is now a risk this repo manages rather than one
    it avoided. Record the mitigation: the pure modules are a verbatim port carrying their own test suite.
  - **ADR-003** — correct the Context. It claims the local build gates on the fused RRF score; it does not.
    `SRC/chat/retrieval.py:1-6` and `SRC/rag/gate.py:59-67` already implement ADR-003's chosen Option C.
    Mark action item 4 ("backport the fix to the local profile") as **not applicable — already correct there**.
    Keep everything about re-sweeping τ; that part is live.
  - **§2** — SSE → NDJSON (D3). **§6** — the chunking spec → the shipped char-based, page-bounded chunker (D4).
    **§7** — add the stage-2 sentinel gate (D6). **§5** — note that TICKET-2 defines the missing `document` table.
  - **PRD G2 and success criterion 5** — restate. "Both profiles run from the same `rag_core` package" is
    unsatisfiable across two repositories. It becomes: the pure core is a verbatim port carrying its own test
    suite, so behavioural divergence fails a test.
  Create a README skeleton: what the project is, how to run the tests, the repo layout, and a placeholder for
  the numbers TICKET-8/9 will publish.
- **PATTERN**: match the existing ADR structure — Status / Context / Decision / Options considered /
  Trade-off analysis / Consequences / Action items.
- **IMPORTS**: n/a
- **GOTCHA**: Amend the ADRs; do not delete them. An ADR that records a decision and its later reversal is
  more useful than one that pretends the first decision never happened. Add
  `**Status:** Amended · <date>` and say what changed.
- **GOTCHA**: The README is public from commit one (Task 2). Do not put placeholder numbers in it — an empty
  "measured numbers pending TICKET-8" is honest; a fabricated table is not.
- **VALIDATE**: `grep -ni "sse\|byte-for-byte" docs/ARCHITECTURE.md docs/PRD.md` returns nothing stale, and
  `grep -n "not applicable" docs/ARCHITECTURE.md` finds the ADR-003 correction.
- **SATISFIES**: AC #5

---

## TESTING STRATEGY

### Unit Tests

`pytest`, function-style, no test classes, descriptive sentence names. Two distinct categories:

**Ported (the parity harness)** — `test_chunking`, `test_fusion`, `test_gate`, `test_sentinel`, and
`test_prompts` (minus the copy assertions). These run against ported modules and **their bodies must not be
edited**. A failure here means the port is wrong, not the test. Verify faithfulness with a `diff` against SRC
after normalising the import prefix.

**New** — `test_contracts`, `test_config`, `test_pipeline`, `test_profile`, `test_core_purity`. These cover
code that has no counterpart in SRC.

### Integration Tests

None in this ticket — there is nothing to integrate with yet. `test_pipeline.py` exercises the full
`retrieve → gate` path against fakes, which is as close as this ticket gets and is the right level for it.

### Contract Tests

`tests/contract/test_port_contract.py`, parametrised over implementations. Fakes only here. The structural
requirement is that TICKET-2 and TICKET-3 add an implementation with a one-line change.

### Edge Cases

- Chunk id containing underscores in the document slug (`some_drug_name_3`) — splits on the **last** underscore
- Both retrieval legs empty, corpus genuinely empty → `corpus_empty`
- Both legs empty, corpus populated → `off_domain`, and `count()` awaited exactly once
- Either leg non-empty → `count()` **never** awaited
- Dense-only hit, lexical-only hit, and agreement across both → different `lexical_support` values
- `top_similarity` NaN → fails closed (pinned by the ported gate test)
- Sentinel split across single-character deltas with a 20–24 char preamble (the SRC regression)
- `Telemetry.as_dict()` with a non-finite similarity → survives `json.dumps`
- `frame()` with a payload containing `\n` → still exactly one output line
- Unknown profile name → raises at startup, listing valid names

---

## VALIDATION COMMANDS

Run every command. Zero errors, zero skips other than deliberately-marked ones.

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

### Level 3: Contract Tests

```bash
uv run pytest tests/contract -v
```

### Level 4: Manual Validation

```bash
# The full suite, from a clean environment
rm -rf .venv && uv sync && uv run pytest

# Purity holds — no framework, SDK, driver or adapter import in the core
uv run pytest tests/unit/test_core_purity.py -v

# The purity test can actually fail (temporarily add `import httpx` to src/rag_core/gate.py first)
uv run pytest tests/unit/test_core_purity.py -v   # expect RED, then revert

# rag_core imports with nothing installed beyond the stdlib
uv run python -c "import rag_core.pipeline, rag_core.gate, rag_core.prompts; print('core imports clean')"

# Full round trip against fakes, under a second (AC #4)
uv run python -c "
import asyncio, time
from rag_core.config import load_config
from rag_adapters.profile import build_profile
from rag_core.pipeline import retrieve
cfg = load_config(env={'RAG_PROFILE': 'fake'})
p = build_profile(cfg)
t = time.perf_counter()
r = asyncio.run(retrieve('What is the adult starting dose of metformin?', p.embedder, p.dense, p.lexical, cfg))
print(r.decision.reason, len(r.chunks), f'{time.perf_counter()-t:.3f}s')
assert time.perf_counter() - t < 1.0
"

# Ported tests are faithful to SRC
SRC=/Users/jsonse/Documents/development/interview/medical-rag/backend
diff <(sed 's/^from rag\./from rag_core./' $SRC/tests/unit/test_gate.py) tests/unit/test_gate.py
diff <(sed 's/^from rag\./from rag_core./' $SRC/tests/unit/test_fusion.py) tests/unit/test_fusion.py
diff <(sed 's/^from rag\./from rag_core./' $SRC/tests/unit/test_chunking.py) tests/unit/test_chunking.py

# No upload vocabulary survives in user-facing copy
uv run python -c "
from rag_core.prompts import DECLINE_COPY, SYSTEM_TEMPLATE, FALLBACK_DECLINE
bad = [k for k,v in DECLINE_COPY.items() if 'upload' in v.lower()]
assert not bad, bad
assert 'upload' not in SYSTEM_TEMPLATE.lower()
assert 'upload' not in FALLBACK_DECLINE.lower()
print('copy clean')
"

# CI is green and the repo is public
gh run list --limit 1
gh repo view medical-rag-hosted-version --json visibility,url
```

### Level 5: Additional Validation

```bash
# No secret reached the public repo
git log -p | grep -inE "api[_-]?key|secret|password|postgres://|postgresql://" || echo "clean"
```

---

## ACCEPTANCE CRITERIA

From TICKET-1, plus the standard bar:

- [ ] **AC #1** — `uv run pytest` green, with no Django, no provider SDK, and no network reachable from `rag_core`
- [ ] **AC #2** — `test_core_purity` passes against the new package layout, and demonstrably fails when purity is violated
- [ ] **AC #3** — the ported unit tests pass **unmodified** against the ported modules — same assertions, same numbers (except the documented `test_prompts` copy deviation)
- [ ] **AC #4** — a full `retrieve → gate → prompt → sentinel` round trip runs against fakes in under a second
- [ ] **AC #5** — `ARCHITECTURE.md` and `PRD.md` reflect D1–D6; ADR-003's stale premise is corrected, not carried forward
- [ ] All validation commands pass with zero errors
- [ ] `mypy strict` clean across `src/`
- [ ] Code follows the SRC conventions documented above (docstrings state the why; frozen dataclasses; comments record measured facts)
- [ ] The four ports each have at least one implementation (fakes) exercised by the contract suite
- [ ] No secret of any kind in the public repository's history

---

## COMPLETION CHECKLIST

- [ ] All 15 tasks completed in order
- [ ] Each task's `VALIDATE` command passed immediately after that task
- [ ] All validation commands executed successfully
- [ ] Full test suite passes (unit + contract)
- [ ] No linting, formatting or type errors
- [ ] `diff` against SRC confirms the three verbatim test files are faithful
- [ ] Acceptance criteria all met
- [ ] CI green on `main`
- [ ] TICKET-2, TICKET-3, TICKET-5 and TICKET-7 can each start against a stable contract

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered):

1. Decline copy and the system prompt are **rewritten in TICKET-1** for a fixed corpus, with `test_prompts.py`'s
   assertions moving with them. Not deferred to the frontend.
2. `_trim_history` and its five tests **port verbatim**; the shell passes `history=[]`. Keeps `prompts.py`
   diffable against SRC.
3. **All four ports are async**, uniformly. The ingest CLI (TICKET-4) wraps its entry point in `asyncio.run`.
4. The GitHub repo is **public from the first commit**. Task 2's secret-hygiene gotcha exists because of this.

**Assumptions this plan makes** — confirm before execution if any looks wrong:

5. **Assumed** — `src/` layout with two distributed packages (`rag_core`, `rag_adapters`) rather than a flat
   root layout. This is what makes the purity test meaningful, since `import rag_core` then resolves to the
   installed package rather than the working directory.
6. **Assumed** — `mypy --strict` is added even though SRC has no type checker. Protocols without a type
   checker are documentation, not contracts, and the ports are this ticket's main deliverable. If you would
   rather not carry mypy, say so — the plan works without it, but Task 9's validation weakens to "it imports".
7. **Assumed** — `DenseStore.count()` is added to Architecture §4's port definition. `retrieve()` needs it for
   the `corpus_empty` gate reason, which has its own user-facing copy. The alternative — inferring emptiness
   from "both legs returned nothing" — conflates an unpopulated index with an off-domain question, which are
   different faults with different remedies. Task 11 keeps it off the hot path.
8. **Assumed** — `Chunk.anchor` is always a page number for this corpus (D4), so `to_context_chunk` can
   `int()` it. Asserted rather than silently handled, so a future section-based anchor fails loudly.
9. **Assumed** — `EmbeddingConfig.model_id` ships as an obvious placeholder overridden by `.env.example`,
   because the spike has not yet confirmed which Gemini model emits 768 dimensions. If the spike has already
   run, use its answer.
10. **Assumed** — `asyncio.gather` default (`return_exceptions=False`) is correct: one leg failing fails the
    request. This follows Architecture §8 but means a lexical failure discards a good dense result. Changing
    it is an architecture decision, not a local one.

---

## NOTES (open canvas)

### Why the ported tests matter more here than they would normally

Under the shared-package design in ADR-001, parity between profiles was structural — one implementation,
therefore no drift possible. The standalone-repo decision (D1) removes that guarantee and replaces it with a
behavioural one: the same tests, with the same numbers, running against both copies. That only works if the
tests are ported *unedited*. The moment someone "fixes" a ported assertion to make a port compile, the parity
claim silently becomes unfalsifiable. Hence the `diff`-against-SRC validation, and hence Task 3's instruction
to treat a red ported test as a port bug rather than a test bug.

The two deliberate exceptions — the decline copy (Task 6) and the config shape (Task 8) — are both places
where the hosted profile genuinely differs, and both are documented at the point of change.

### Three things the port makes better, and why they're safe

| Change | vs SRC | Why it's safe |
|---|---|---|
| Hydration is a dict lookup, not a query | `SRC/chat/retrieval.py:26-54` hits the ORM | RRF only ranks ids that came from a leg, so the union always contains every `top_id` — by construction, not by luck |
| `count()` only when both legs are empty | `SRC/chat/retrieval.py:58` counts every query | If either leg returned a row the corpus isn't empty. Same gate outcome, zero round trips on the happy path |
| Purity test checks four things | SRC checks `django` only | Architecture §3 asks for "no web framework and no provider SDKs"; the original test enforced a third of that |

The orphan-drop branch (`SRC/chat/retrieval.py:45`) disappearing is the one to watch. In SRC it was load-bearing
because Chroma and SQLite could disagree; here there is no second store, so an unresolvable id would mean a
fusion bug. Task 11 asserts rather than skips — a silent drop is how you end up with a system that refuses
everything for a reason nobody can find.

### Alternatives weighed and rejected

**Sync ports with an injected concurrency seam.** Would have kept `pipeline.retrieve()` byte-comparable to
SRC's. Rejected: the shell would have to supply a threadpool executor to get parallel legs under FastAPI,
which is more machinery than `asyncio.gather` and puts the concurrency decision in the wrong layer.

**`Scored.score` normalised to similarity in the adapter.** Tempting — it removes the `1 - distance`
conversion from the pipeline and the direction confusion from the contract suite. Rejected because ADR-003 is
explicit that the gate needs raw magnitude, and a normalising adapter is exactly the failure the ADR was
written to prevent. The contract suite's direction assertions are the substitute.

**Porting `build_fts_query` now.** It is pure and it would tick the "no Django" box. Rejected: its output is
FTS5 syntax and Postgres needs `websearch_to_tsquery`. Porting it here means shipping a function no caller can
use, then rewriting it in TICKET-2 against a real database — where the translation can actually be tested.

**`@runtime_checkable` on the Protocols.** Rejected: it validates method names only, never signatures, so it
would happily accept an adapter with the wrong arity. That is worse than no check, because it looks like one.

### Risk that isn't in the task list

The largest one is boredom-driven drift. Fourteen of the fifteen tasks are mechanical, and mechanical work
invites small "improvements" — tidying a comment, renaming a variable, reflowing a line. Every one of those
weakens the `diff` that makes this port verifiable. The `line-length = 100` setting in Task 1 exists entirely
to stop the formatter doing this automatically; the rest is discipline.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
