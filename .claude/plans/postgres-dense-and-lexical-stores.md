# Feature: TICKET-2 — Postgres schema and the dense + lexical store adapter

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for ported logic:** `/Users/jsonse/Documents/development/interview/medical-rag/backend/` — referred to as **SRC**. A different repository; read from it, never write to it.

---

## Feature Description

One Postgres database serving both retrieval legs — pgvector with an HNSW index for dense, a generated
`tsvector` column with a GIN index for lexical — plus the manifest that makes serving an index built by a
different embedding model impossible rather than merely unlikely.

This is the first *real* adapter. TICKET-1 defined four ports against a set of in-memory fakes, and the fakes
under-specified them: a fake needs no connection, no lifecycle, and no schema. Implementing a store that has
all three is what reveals what the ports actually need. Three small amendments to TICKET-1's surface follow
from that and are part of this ticket, each justified at its task.

The structural win this ticket collects is ADR-002's: dense and lexical rows are **the same row**. The local
build's split between ChromaDB and SQLite FTS5 made partial-ingestion states possible where a chunk was
searchable lexically but not densely. That entire class of bug disappears here, and with it the compensating
deletes and the `reconcile_vectors` command that existed to repair it.

## User Story

As the author of the Medical RAG project
I want dense and lexical retrieval backed by one Postgres database behind the existing ports
So that the pipeline can run against real data on managed infrastructure, and so that the two retrieval legs cannot disagree about what has been ingested.

## Problem Statement

`rag_core` is complete and has nothing real to talk to. Neither `DenseStore` nor `LexicalStore` has an
implementation that survives a process restart, and the hosted profile cannot exist without one:
ChromaDB and SQLite FTS5 both assume a writable persistent filesystem, and serverless has neither.

Downstream, TICKET-4 (ingestion) cannot write anywhere and TICKET-5's startup manifest check has nothing to
read.

## Solution Statement

A migration defining `document`, `chunk` and `index_manifest`; `PostgresDenseStore` and
`PostgresLexicalStore` implementing their ports over a shared `asyncpg` pool; and a port of the local
build's query tokeniser translated from FTS5 syntax to `to_tsquery`.

Four decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **`asyncpg`** | Fastest async driver, first-class pgvector support, the common pairing with Neon. |
| D2 | **Port the tokeniser; OR-join into `to_tsquery`** | Preserves the OR semantics the gate's `lexical_support` signal and its stopword list were tuned around. `websearch_to_tsquery` ANDs by default, which would change lexical recall and make TICKET-9 measure two changes at once. |
| D3 | **CI service container + a `postgres` marker** | Mirrors SRC's `markers = ollama` / `addopts = -m "not ollama"`. `uv run pytest` stays green with no database at all. |
| D4 | **`ts_rank_cd`, not `ts_rank`** | Inherited from ADR-002 and Architecture §7. PRD open question 2 is answered by measurement in TICKET-9, not by argument here. |

## Out of Scope / Non-Goals

- **Not included: the ingestion job.** This ticket implements `upsert`/`index`; the CLI that calls them, the
  corpus builder, batching and backoff are TICKET-4.
- **Not included: the startup manifest comparison.** This ticket provides `read_manifest`/`write_manifest`.
  Comparing the manifest against the configured embedder and refusing to serve is TICKET-5.
- **Not included: any provider adapter.** No Gemini, no Groq. Embeddings in tests come from `FakeEmbedder`.
- **Not included: Neon provisioning.** No account setup, no branch creation, no production `DATABASE_URL`.
  That is TICKET-10. This ticket runs against a local or CI Postgres.
- **Not included: query-time HNSW tuning.** `hnsw.ef_search` is left at its default. At three documents the
  planner will sequential-scan anyway, and tuning against a corpus this size would be measuring noise.
- **Not changing:** anything in `rag_core` except the three amendments named in Tasks 2, 3 and 9. In
  particular the gate, fusion, chunking, prompts and sentinel modules are vendored ports and stay untouched.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium — the SQL is small and the port surface is four methods; the complexity is in the lifecycle, the tsquery translation, and restructuring the contract suite to admit an adapter that needs setup.
**Primary Systems Affected**: `src/rag_adapters/`, `db/`, `tests/contract/`, `src/rag_core/ports.py` + `contracts.py` (amendments), CI
**Dependencies**: `asyncpg`, `pgvector` (Python package, for `register_vector`); Postgres 14+ with the `vector` extension

## Related Work

**Implements**: TICKET-2 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`, decisions D1–D6 in the ticket breakdown

**Back-references**:

- `.claude/plans/port-rag-core-and-scaffold-repo.md` — defines the ports, `Scored`, `Chunk`, the composition
  root and the contract suite this ticket implements against and amends.

**Forward-references**:

- TICKET-4 — calls `upsert` and `write_manifest` from the ingestion CLI
- TICKET-5 — calls `read_manifest` at startup, and `retrieve()` against these stores per request
- TICKET-9 — measures the `ts_rank_cd` vs BM25 difference this ticket introduces

**Sequencing:** TICKET-1 is on **PR #1, not yet merged**. Merge it before branching, or branch from
`feature/port-rag-core-and-scaffold-repo` and retarget. Branching from an un-merged `main` gets you a
repository with no `rag_core`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

**This repository:**

- `src/rag_core/ports.py` — Why: the two Protocols being implemented. **Read the SCORE DIRECTION paragraphs
  in both docstrings before writing a single query.** Dense returns cosine *distance* ascending; lexical
  returns rank *descending*. They run in opposite directions, on purpose.
- `src/rag_core/contracts.py` — Why: `Chunk`, `EmbeddedChunk`, `Scored`, `make_chunk_id`, `split_chunk_id`.
  Note `Scored` carries the native score untransformed (ADR-003) and `Chunk.anchor` is text.
- `src/rag_adapters/fakes.py` — Why: `FakeDenseStore` and `FakeLexicalStore` are the reference semantics.
  Where behaviour is ambiguous, match them; the contract suite runs both against the same assertions.
- `src/rag_adapters/profile.py` — Why: the composition root and its `_REGISTRY` seam. This ticket adds the
  `hosted` entry and the lifecycle amendment.
- `tests/contract/test_port_contract.py` — Why: the suite this adapter must satisfy, and the file this ticket
  restructures. Read all of it; the score-direction and idempotence tests are the ones that matter.
- `src/rag_core/config.py` — Why: `EmbeddingConfig.dimension` is compared against the schema. This ticket adds
  a `database` section.
- `pyproject.toml` — Why: the vendored-port exclusions and the mypy override. Do not widen either; add the
  `postgres` marker alongside the existing pytest config.

**Source repository (port from, do not modify):**

- `SRC/rag/lexical.py` (42 lines, read in full) — Why: `build_fts_query`, its `TOKEN_RE`, `STOPWORDS`,
  `MIN_TERM_LENGTH` and `MAX_TERMS`. The whole file ports; only the join operator changes. **Read the two
  comments**: the decimal-with-unit token rule (`0.5mg` must not split into `0` and `5mg` — that turned a
  paediatric dose question into an adult one) and the stopword rationale (function words OR-joined would leave
  `lexical_support` permanently True and collapse the gate's middle band).
- `SRC/tests/unit/test_lexical.py` (81 lines) — Why: the tokeniser's test suite. Ports with its assertions
  adjusted for the new join operator.
- `SRC/chat/lexical_search.py` (31 lines) — Why: the query shape, and the `limit <= 0` guard.
- `SRC/documents/migrations/0002_fts5.py` — Why: the FTS5 setup being replaced. Note `tokenize='porter
  unicode61'`. Postgres `to_tsvector('english', …)` is also Porter-stemmed, so tokenisation is close to
  like-for-like; it is the *ranking function* that differs, which is exactly what ADR-002 says.
- `SRC/rag/vectorstore.py` (lines 14-18, 52-71) — Why: `VectorHit.distance`, the `similarity = 1 - distance`
  semantics, and the reasoning for not clamping `n_results` with a `count()` round trip.

### New Files to Create

```
db/
├── migrations/
│   └── 001_initial.sql              # extension, three tables, two indexes
├── migrate.py                       # ~60-line runner, applied-migrations table
└── README.md                        # how to point it at a database
src/rag_adapters/
├── postgres.py                      # pool, both stores, manifest
└── tsquery.py                       # ported tokeniser, FTS5 -> to_tsquery
tests/
├── contract/conftest.py             # store-pair fixtures, pg_pool, postgres marker
└── unit/test_tsquery.py             # ported tokeniser tests
```

Modified: `src/rag_core/ports.py`, `src/rag_core/contracts.py`, `src/rag_adapters/fakes.py`,
`src/rag_adapters/profile.py`, `tests/contract/test_port_contract.py`, `pyproject.toml`, `.env.example`,
`.github/workflows/test.yml`, `tests/unit/test_profile.py`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [pgvector — indexing and operators](https://github.com/pgvector/pgvector#indexing)
  - Specific: `<=>` is cosine distance; HNSW with `vector_cosine_ops`
  - Why: getting the operator/opclass pair wrong builds an index the planner silently ignores.
- [pgvector-python — asyncpg](https://github.com/pgvector/pgvector-python#asyncpg)
  - Specific: `from pgvector.asyncpg import register_vector; await register_vector(conn)`
  - Why: **must run on every connection in the pool**, via `asyncpg.create_pool(init=…)`. Miss it and vectors
    come back as strings with no error until something downstream does arithmetic on them.
- [asyncpg — connection pools](https://magicstack.github.io/asyncpg/current/api/index.html#connection-pools)
  - Specific: `create_pool(..., init=...)`, `pool.acquire()`, `await pool.close()`
  - Why: the pool must be created inside a running event loop, which drives the lifecycle amendment in Task 3.
- [Postgres — text search controls](https://www.postgresql.org/docs/current/textsearch-controls.html#TEXTSEARCH-RANKING)
  - Specific: `ts_rank_cd(tsvector, tsquery [, normalization])`, default normalization `0`
  - Why: `ts_rank_cd` is coverage density, not BM25 (ADR-002). Normalization stays at the default; chunks are
    near-uniform in length so document-length normalisation would add a knob with nothing to tune against.
- [Postgres — generated columns](https://www.postgresql.org/docs/current/ddl-generated-columns.html)
  - Why: a generated column's expression must be `IMMUTABLE`. **`to_tsvector('english', content)` (two
    argument, literal config) is immutable; `to_tsvector(content)` is not** — it depends on
    `default_text_search_config`. The one-argument form fails at `CREATE TABLE`.
- [Neon — connection pooling](https://neon.com/docs/connect/connection-pooling)
  - Specific: protocol-level prepared statements are supported (PgBouncer ≥ 1.22.0); SQL-level `PREPARE`/
    `EXECUTE` are not
  - Why: the widely-repeated `statement_cache_size=0` workaround for asyncpg on pooled endpoints is **no
    longer required**. Do not add it as a cargo-culted precaution. If `prepared statement "s0" already exists`
    ever appears, that is the symptom to search on.

### Patterns to Follow

**Module docstrings state the failure the module prevents**, and cite `ARCHITECTURE.md §N` or an ADR — never
`spec N.N`, which refers to a document that does not exist in this repository. From `src/rag_core/ports.py`:

```python
"""Full-text retrieval.

SCORE DIRECTION: `search` returns a relevance rank, DESCENDING — best
first. Opposite to DenseStore, because the underlying measures run in
opposite directions. Fusion only ever reads position, so the two never need
to be comparable (ARCHITECTURE.md §7) — but each must be ordered correctly
before RRF sees it.
"""
```

**`from __future__ import annotations` on every module.** Present in all 11 `rag_core` modules and both
`rag_adapters` modules.

**Frozen dataclasses for value types**, and tests that assert the frozenness
(`tests/unit/test_contracts.py::test_scored_is_frozen`).

**Guards fail closed and explain themselves.** From `src/rag_adapters/fakes.py`:

```python
async def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]:
    if k <= 0:
        return []
```

**Test names are full sentences.** `test_dense_results_are_ordered_by_ascending_distance`,
`test_lexical_query_that_sanitises_to_nothing_returns_empty_not_an_error`.

**Anti-patterns to avoid:** string-interpolated SQL (always parameterise, even where the input is already
alphanumeric); normalising a score inside an adapter (ADR-003); `SELECT *`; catching bare `Exception`;
copying SRC's double-checked locking from `documents/services.py` — that existed for a Django threadpool race
this codebase does not have.

---

## IMPLEMENTATION PLAN

### Phase 1: Schema and the migration runner

Standalone and testable with `psql` alone, before any Python adapter exists.

**Tasks:** 1 (migration), 10 (runner) — see note on Task 10's position below.

### Phase 2: Amend the ports for what a real store needs

**Depends on:** nothing in this ticket; do it early so the adapter is written against the final surface.

The fakes under-specified the ports in three ways. Fixing that before writing the adapter avoids writing it
twice.

**Tasks:** 2 (manifest on `DenseStore`), 3 (`Profile` lifecycle).

### Phase 3: The tokeniser

**Independent of:** Phases 1 and 2 — pure string processing, no database, no ports. Could be done in parallel.

**Tasks:** 4.

### Phase 4: The adapter

**Depends on:** Phases 1–3.

**Tasks:** 5 (pool), 6 (dense), 7 (lexical), 8 (manifest), 9 (profile registration).

### Phase 5: Contract suite and CI

**Depends on:** Phase 4.

**Tasks:** 10 (runner, if not already done), 11 (suite restructure), 12 (CI + marker), 13 (docs).

---

## STEP-BY-STEP TASKS

### 1. CREATE `db/migrations/001_initial.sql`

- **IMPLEMENT**: The schema from Architecture §5, with the `document` table that section now defines:

```sql
create extension if not exists vector;

create table document (
  id            text primary key,
  title         text not null,
  source_set_id text,
  ingested_at   timestamptz not null default now()
);

create table chunk (
  id          text primary key,
  document_id text not null references document(id) on delete cascade,
  ordinal     int  not null,
  anchor      text not null,
  content     text not null,
  embedding   vector(768) not null,
  tsv         tsvector generated always as (to_tsvector('english', content)) stored,
  unique (document_id, ordinal)
);

create index chunk_embedding_hnsw on chunk using hnsw (embedding vector_cosine_ops);
create index chunk_tsv_gin        on chunk using gin  (tsv);

create table index_manifest (
  id                 int  primary key default 1,
  embedding_model_id text not null,
  dimension          int  not null,
  ingested_at        timestamptz not null,
  constraint index_manifest_is_a_singleton check (id = 1)
);
```

- **PATTERN**: Architecture §5 verbatim, plus `document` as amended by TICKET-1.
- **GOTCHA**: `to_tsvector('english', content)` — the **two-argument** form. The one-argument form is `STABLE`,
  not `IMMUTABLE`, because it reads `default_text_search_config`, and Postgres rejects it in a generated
  column. The error names immutability, not the argument count, so it is easy to misread.
- **GOTCHA**: `vector(768)` is hardcoded while `EmbeddingConfig.dimension` is configurable. That is
  deliberate — the schema is the authority (Architecture §5: "the embedding model is part of the schema, not
  a runtime setting"). A mismatch must fail at insert. Do not template the migration off config.
- **GOTCHA**: The `check (id = 1)` is an addition. Architecture §5's `id int primary key default 1` permits
  rows 2, 3, … which would let two manifests coexist and make "the" manifest ambiguous. Note it in the file.
- **GOTCHA**: `on delete cascade` is an addition, so deleting a document cannot strand its chunks. Without it
  a delete raises and the caller has to remember the order.
- **VALIDATE**: `psql "$DATABASE_URL" -f db/migrations/001_initial.sql` against a scratch database, then
  `psql "$DATABASE_URL" -c "\d chunk"` and confirm `tsv` shows `generated always as (...) stored`.
- **SATISFIES**: AC #1

### 2. UPDATE `src/rag_core/ports.py` + `contracts.py` + `fakes.py` — manifest on `DenseStore`

- **IMPLEMENT**: `IndexManifest` frozen dataclass in `contracts.py` (`embedding_model_id: str`,
  `dimension: int`, `ingested_at: datetime`). Add to the `DenseStore` Protocol:

```python
async def read_manifest(self) -> IndexManifest | None: ...
async def write_manifest(self, manifest: IndexManifest) -> None: ...
```

  Implement both on `FakeDenseStore` (in-memory, `None` until written).
- **PATTERN**: the existing `count()` method on `DenseStore` and its docstring, which explains why a method
  not in Architecture §4 earned its place.
- **GOTCHA**: This is amendment 1 of 3 to TICKET-1's surface. **Put it on `DenseStore` rather than adding a
  fifth port.** ADR-001 caps the port count at four and refuses a fifth "without a third implementation
  demanding it". The manifest describes the dense index — which embedding model built it — so it belongs
  there. A separate concrete manifest class would work for the hosted profile but would force TICKET-5's
  startup check to branch on profile, which ADR-001 forbids below the composition root.
- **GOTCHA**: `read_manifest` returns `None` for "no manifest written", distinguishable from a manifest whose
  fields happen to be empty. TICKET-5 treats `None` as refuse-to-serve.
- **GOTCHA**: `datetime` in `contracts.py` — the module currently imports nothing beyond stdlib, and
  `datetime` is stdlib, so purity holds. Do not reach for a third-party date type.
- **VALIDATE**: `uv run mypy && uv run pytest tests/unit -q`
- **SATISFIES**: AC #5

### 3. UPDATE `src/rag_adapters/profile.py` — open/close lifecycle

- **IMPLEMENT**: `async def open(self) -> None` and `async def close(self) -> None` on `Profile`, defaulting
  to no-ops. The hosted builder returns a `Profile` whose `open()` creates the asyncpg pool and whose
  `close()` disposes it. `build_profile` stays **synchronous**.
- **PATTERN**: `src/rag_adapters/profile.py`'s existing frozen `Profile` container and `_REGISTRY` seam.
- **GOTCHA**: Amendment 2 of 3. An asyncpg pool must be created inside a running event loop, so a purely
  synchronous `build_profile` cannot produce a connected profile. Making `build_profile` async instead would
  push an `await` into every caller including the sync composition root; splitting construction from
  connection keeps resolution synchronous and gives TICKET-5's FastAPI shell an obvious lifespan hook.
- **GOTCHA**: `Profile` is a frozen dataclass. Holding a mutable pool means either dropping frozen or storing
  the pool on the store objects rather than on `Profile`. **Store it on the stores** — `Profile.open()`
  delegates. Keep `Profile` frozen; its frozenness is asserted in `tests/unit/test_profile.py`.
- **GOTCHA**: `close()` must be idempotent and safe to call when `open()` never ran. A shell that fails
  during startup will still run its shutdown path.
- **VALIDATE**: `uv run pytest tests/unit/test_profile.py -v` — the fake profile's `open`/`close` are no-ops
  and calling `close()` twice, or without `open()`, does not raise.
- **SATISFIES**: AC #5

### 4. CREATE `src/rag_adapters/tsquery.py` + `tests/unit/test_tsquery.py`

- **IMPLEMENT**: Port `SRC/rag/lexical.py` — `TOKEN_RE`, `STOPWORDS`, `MIN_TERM_LENGTH`, `MAX_TERMS` and the
  term-extraction loop, **unchanged**. Rename the function `build_tsquery` and change only the join:
  `" | ".join(terms)` instead of `" OR ".join(f'"{t}"' for t in terms)`. Port
  `SRC/tests/unit/test_lexical.py` with its assertions adjusted for the new output shape.
- **PATTERN**: `SRC/rag/lexical.py` in full. Carry both comments verbatim — the `0.5mg` tokenisation rule and
  the stopword rationale each record a specific bug.
- **GOTCHA**: The FTS5 version wrapped each term in double quotes to neutralise reserved words like `NEAR`.
  `to_tsquery` has no such reserved words, and quoting would be wrong there — but the sanitising that made
  quoting sufficient is still doing the real work, because `to_tsquery` **raises a syntax error** on `&`,
  `|`, `!`, `(`, `)`, `:` and `*`. `TOKEN_RE` already reduces everything to alphanumerics, so the output is
  safe by construction. Keep the tokeniser exactly; it is load-bearing for a different reason now.
- **GOTCHA**: An empty result must stay an empty string, and the caller must check it. `to_tsquery('english',
  '')` raises rather than matching nothing.
- **GOTCHA**: This module lives in `rag_adapters`, not `rag_core`. It encodes one backend's query syntax, so
  it is adapter-layer by definition. Putting it in the core would be a provider decision leaking below the
  port boundary, and `test_core_purity` would not catch it — the check is import-based, and this module
  imports nothing suspicious. That makes placement a judgement the purity test cannot make for you.
- **VALIDATE**: `uv run pytest tests/unit/test_tsquery.py -v` — covers `"metformin dose"` → `metformin | dose`,
  every punctuation case from SRC's parametrised list, single-character terms dropped, `0.5mg` kept whole,
  `MAX_TERMS` capping, and an all-stopword question yielding `""`.
- **SATISFIES**: AC #3

### 5. CREATE `src/rag_adapters/postgres.py` — pool construction

- **IMPLEMENT**: `create_pool(dsn: str, min_size: int, max_size: int) -> asyncpg.Pool` wrapping
  `asyncpg.create_pool(dsn, init=_init_connection, ...)`, where `_init_connection` calls
  `await register_vector(conn)` from `pgvector.asyncpg`. Add a `DatabaseConfig` section to
  `rag_core/config.py` (`dsn`, `min_size`, `max_size`) read from `DATABASE_URL`, `DB_POOL_MIN`, `DB_POOL_MAX`,
  and add those keys to `.env.example`.
- **PATTERN**: `rag_core/config.py`'s `_s`/`_i` coercion helpers and one-dataclass-per-concern structure.
- **GOTCHA**: `register_vector` **must** run via the pool's `init=` hook so it applies to every connection,
  including ones the pool creates later to grow. Registering once on a single acquired connection appears to
  work in a one-connection test and then fails intermittently under concurrency — the worst way to find out.
- **GOTCHA**: Do **not** set `statement_cache_size=0`. Neon's PgBouncer supports protocol-level prepared
  statements since 1.22.0, so the widely-copied workaround is obsolete and costs real performance.
- **GOTCHA**: `min_size` should be small (1–2). Serverless invocations are short-lived and a large minimum
  pool means opening connections that the invocation never uses and Neon still counts.
- **GOTCHA**: `DATABASE_URL` is a secret and this repository is public. It goes in `.env.example` as a name
  with a placeholder value only, and `.env` is already gitignored — verify before committing.
- **VALIDATE**: `uv run mypy && uv run pytest tests/unit/test_config.py -v`
- **SATISFIES**: AC #2

### 6. ADD `PostgresDenseStore` to `src/rag_adapters/postgres.py`

- **IMPLEMENT**: `DenseStore` over the pool.
  - `search(vector, k)` → `if k <= 0: return []`, then

    ```sql
    select c.id, c.document_id, c.ordinal, c.anchor, c.content, d.title,
           c.embedding <=> $1 as distance
    from chunk c join document d on d.id = c.document_id
    order by c.embedding <=> $1
    limit $2
    ```

    returning `Scored(item=Chunk(...), score=distance)` — **raw distance, ascending**.
  - `upsert(chunks)` → one statement, `on conflict (id) do update set` for every non-generated column.
  - `count()` → `select count(*) from chunk`.
- **PATTERN**: `FakeDenseStore` in `src/rag_adapters/fakes.py` for the exact semantics; `SRC/rag/vectorstore.py`
  lines 52-61 for why `k` is not clamped with a preliminary `count()`.
- **GOTCHA**: `<=>` is cosine distance and pairs with `vector_cosine_ops`. Using `<->` (L2) against a
  `vector_cosine_ops` index makes the planner ignore the index *and* returns a different metric, so the gate's
  τ silently stops meaning what it says. This single character is the highest-consequence typo in the ticket.
- **GOTCHA**: The `ORDER BY` expression must match the indexed expression exactly for HNSW to be used. Order
  by `c.embedding <=> $1`, not by the `distance` alias.
- **GOTCHA**: Cosine distance against a **zero vector** is `NaN` in pgvector. The gate fails closed on
  non-finite similarity (`rag_core/gate.py`), so this degrades correctly — but assert it rather than trusting
  it, because the path from "embedder returned zeros" to "gate refuses" runs through three modules.
- **GOTCHA**: `upsert` must write `document` rows too, or the foreign key rejects every chunk. Either upsert
  the parent document first in the same transaction, or document that TICKET-4 does it. **Do it here, in one
  transaction** — a store whose write method half-works depending on call order is a trap for TICKET-4.
- **GOTCHA**: Inserting a vector whose length ≠ 768 raises from Postgres. That is correct and wanted; add a
  test so the behaviour is pinned rather than incidental.
- **VALIDATE**: `uv run pytest tests/contract -m postgres -v -k dense`
- **SATISFIES**: AC #2, AC #4

### 7. ADD `PostgresLexicalStore` to `src/rag_adapters/postgres.py`

- **IMPLEMENT**: `LexicalStore` over the same pool.
  - `search(query, k)` → `if k <= 0: return []`; `expression = build_tsquery(query)`;
    `if not expression: return []`; then

    ```sql
    select c.id, c.document_id, c.ordinal, c.anchor, c.content, d.title,
           ts_rank_cd(c.tsv, q) as rank
    from chunk c join document d on d.id = c.document_id,
         to_tsquery('english', $1) q
    where c.tsv @@ q
    order by rank desc
    limit $2
    ```

    returning `Scored(item=Chunk(...), score=rank)` — **descending**.
  - `index(chunks)` → **verify-and-no-op**: check every chunk id exists; raise if any does not, naming
    `DenseStore.upsert`.
- **PATTERN**: `SRC/chat/lexical_search.py` for the query shape and the `limit <= 0` guard;
  `FakeLexicalStore` for the ordering contract.
- **GOTCHA**: `index()` having nothing to do **is the point of ADR-002**. `tsv` is
  `GENERATED ALWAYS … STORED`, so it populates itself on the same insert that writes the chunk — dense and
  lexical rows are literally the same row and cannot disagree. Write that in the docstring; a bare `pass`
  reads like an unfinished method.
- **GOTCHA**: Make it *verify*, not silently return. A silent no-op means TICKET-4 could call only `index()`,
  see success, and ship an empty index. Raising with a message that names `upsert` turns a subtle
  data-loss bug into an immediate, self-explaining error.
- **GOTCHA**: The empty-tsquery guard must come **before** the query. `to_tsquery('english', '')` raises a
  syntax error, so an all-stopword question like "what is it?" would 500 instead of returning no lexical
  hits. SRC has exactly this guard at `chat/lexical_search.py:27`.
- **GOTCHA**: `ts_rank_cd` returns `real` (float4), not float8. Cast in Python (`float(row["rank"])`), which
  the `Scored.score: float` annotation requires anyway.
- **GOTCHA**: Leave the normalization argument off (default `0`, no length normalisation). Chunks are
  near-uniform at ~1000 characters, so there is nothing for it to correct, and adding it now means TICKET-9
  measures two changes at once.
- **VALIDATE**: `uv run pytest tests/contract -m postgres -v -k lexical`
- **SATISFIES**: AC #3, AC #4

### 8. ADD manifest read/write to `PostgresDenseStore`

- **IMPLEMENT**: `read_manifest()` → `select ... from index_manifest where id = 1`, returning `IndexManifest`
  or `None`. `write_manifest(m)` → `insert ... values (1, ...) on conflict (id) do update set ...`.
- **PATTERN**: Task 2's `FakeDenseStore` implementation for the `None`-when-absent semantics.
- **GOTCHA**: Reading when the table is empty returns `None`, not a zero-valued manifest. TICKET-5 maps `None`
  to refuse-to-serve, and a manifest full of empty strings would compare unequal to the configured embedder
  and produce a confusing mismatch message instead of a clear "no manifest" one.
- **GOTCHA**: `ingested_at` is `timestamptz`. asyncpg returns a timezone-aware `datetime`; keep it aware
  end-to-end rather than stripping tzinfo, or round-tripping shifts the value.
- **VALIDATE**: `uv run pytest tests/contract -m postgres -v -k manifest`
- **SATISFIES**: AC #5

### 9. UPDATE `src/rag_adapters/profile.py` — register the hosted profile

- **IMPLEMENT**: `_build_hosted(cfg)` returning a `Profile` with both Postgres stores sharing one pool, and
  `FakeGenerator` as a placeholder until TICKET-3 (with a comment saying so). Register as `"hosted"`.
- **PATTERN**: the existing `_build_fake` and `_REGISTRY` entry — this should be one dict entry plus one
  builder function.
- **GOTCHA**: Amendment 3's blast radius check — `build_profile(load_config(env={"RAG_PROFILE": "hosted"}))`
  must now succeed, so `tests/unit/test_profile.py::test_an_unknown_profile_raises_and_names_the_valid_options`
  currently uses `"hosted"` as its *unknown* name and will start passing for the wrong reason. Change it to a
  name that stays unregistered.
- **GOTCHA**: `_build_hosted` must not connect. Construction is synchronous; connecting happens in
  `Profile.open()` (Task 3). Building a hosted profile with an unreachable database must succeed.
- **VALIDATE**: `uv run pytest tests/unit/test_profile.py -v`
- **SATISFIES**: AC #5

### 10. CREATE `db/migrate.py` and `db/README.md`

- **IMPLEMENT**: ~60 lines. Create `schema_migration (filename text primary key, applied_at timestamptz)`,
  read `db/migrations/*.sql` sorted by filename, skip applied ones, apply each inside a transaction, record
  it. Entry point `uv run python db/migrate.py`, DSN from `DATABASE_URL`.
- **PATTERN**: none in this repository — new. Keep it boring and dependency-free beyond asyncpg.
- **GOTCHA**: No Alembic. There is one schema with no history to migrate and no branching; Alembic's
  autogenerate would add a dependency and a second source of truth for a table definition that is already
  written out in Architecture §5.
- **GOTCHA**: Each migration runs in its own transaction so a failure leaves no half-applied file. Postgres
  supports transactional DDL — use it.
- **GOTCHA**: Applying an already-applied migration must be a no-op, not an error. The contract-suite fixture
  will call this repeatedly.
- **VALIDATE**: `uv run python db/migrate.py` twice against a scratch database — second run reports zero
  applied and exits 0.
- **SATISFIES**: AC #1

### 11. UPDATE `tests/contract/` — restructure for adapters that need setup

- **IMPLEMENT**: Create `tests/contract/conftest.py` with a `StorePair` holding `dense`, `lexical` and an
  async `seed(embedded_chunks)`; fixtures `fake_stores` and `pg_stores` (the latter `postgres`-marked,
  migrating and truncating per test); and a `stores` fixture resolving indirect parametrisation via
  `request.getfixturevalue`. Rewrite `test_port_contract.py`'s store sections to take `stores` and seed
  through `seed()` rather than through a constructor. Leave the embedder and generator sections alone.

```python
STORE_PAIRS = [
    "fake_stores",
    pytest.param("pg_stores", marks=pytest.mark.postgres),
]


@pytest.fixture
def stores(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("stores", STORE_PAIRS, indirect=True)
async def test_dense_results_are_ordered_by_ascending_distance(stores): ...
```

- **PATTERN**: the existing `test_port_contract.py` assertions — **keep every one of them**; only the way a
  store is obtained and seeded changes.
- **GOTCHA**: TICKET-1 claimed a new adapter would be "one line in the parametrisation, and if TICKET-2 has to
  restructure this file, the seam was built wrong." **The seam was built wrong**, and this task fixes it. The
  registry held classes and tests called `builder()` / `builder(CORPUS)`, which only works for a store with no
  dependencies and no lifecycle. Do not preserve the old shape out of deference to the comment — update the
  comment.
- **GOTCHA**: Seeding through a constructor was a fake-only shortcut that skipped the write path entirely.
  Seeding through `seed()` means every contract run now exercises `upsert`, which is strictly more coverage.
- **GOTCHA**: The Postgres fixture must isolate tests. `truncate document, chunk, index_manifest restart
  identity cascade` between tests is simpler and faster here than a transaction-rollback fixture, and the
  suite is small.
- **GOTCHA**: `pg_stores` must skip cleanly — not error — when no `DATABASE_URL` is set, so that someone
  running `uv run pytest -m postgres` without a database gets a skip with a readable reason.
- **VALIDATE**: `uv run pytest tests/contract -v` (fakes only, postgres deselected) then
  `DATABASE_URL=... uv run pytest tests/contract -m postgres -v` — **the same assertions pass for both**.
- **SATISFIES**: AC #4

### 12. UPDATE `pyproject.toml` and `.github/workflows/test.yml`

- **IMPLEMENT**: Add `asyncpg` and `pgvector` to `[project] dependencies` — an **optional** group if you want
  `rag_core` installable without them; otherwise plain dependencies with a comment that `rag_core` still
  imports neither. Register the marker and default deselection:

```toml
markers = ["postgres: requires a live Postgres with pgvector (deselected by default)"]
addopts = '-m "not postgres"'
```

  In CI, add a `services:` block running `pgvector/pgvector:pg17` with a health check, and a second test step
  `uv run pytest -m postgres` with `DATABASE_URL` pointing at it.
- **PATTERN**: `SRC/pytest.ini`'s `markers` + `addopts = -m "not ollama"` — the same idea, same reasoning.
- **GOTCHA**: `dependencies = []` in `pyproject.toml` is asserted by
  `tests/unit/test_core_purity.py::test_core_declares_no_runtime_dependencies`. Adding runtime dependencies
  **breaks that test**. Either move them to an optional group and leave the base empty, or update the test to
  assert what it actually cares about — that `rag_core` imports nothing outside stdlib, which the four
  import-scanning tests already cover. **Prefer the optional group**; keeping the base empty is a stronger and
  more legible claim.
- **GOTCHA**: `addopts` with `-m` is overridden, not merged, by a command-line `-m`. `uv run pytest -m postgres`
  therefore works as intended — verify it actually selects them rather than assuming.
- **GOTCHA**: Do not widen the vendored-port exclusions or the mypy override while editing this file. They are
  load-bearing for TICKET-1's parity claim.
- **VALIDATE**: `uv run pytest -q` (postgres deselected, still green with no database) and a pushed CI run
  showing both test steps pass.
- **SATISFIES**: AC #4, AC #6

### 13. UPDATE `docs/ARCHITECTURE.md` and `README.md`

- **IMPLEMENT**: Tick ADR-002's action item 1 (schema with `vector(768)`, HNSW cosine, generated `tsvector`,
  GIN). Leave items 2 and 3 open — they are TICKET-9's. Note in §5 that `index_manifest` is constrained to a
  single row. In the README, add the database prerequisite and how to run migrations and the `postgres` tests.
- **PATTERN**: TICKET-1's doc reconciliation — amend and date, never silently rewrite.
- **GOTCHA**: Do not claim `ts_rank_cd` parity with BM25 anywhere. ADR-002 commits to *measuring* the
  difference, and TICKET-9 does the measuring. An optimistic sentence here is exactly the kind of claim the
  epic exists to avoid.
- **GOTCHA**: `ruff` has `extend-exclude = ["docs"]`; editing markdown will not be reformatted, which is
  intended. Do not remove that exclusion.
- **VALIDATE**: `grep -n "001_initial\|pgvector" README.md docs/ARCHITECTURE.md`
- **SATISFIES**: AC #6

---

## TESTING STRATEGY

### Unit Tests

`tests/unit/test_tsquery.py` — the ported tokeniser, no database. Carries SRC's parametrised punctuation
cases, the `0.5mg` rule, stopwords, `MIN_TERM_LENGTH`, `MAX_TERMS`, and the empty-result case.

`tests/unit/test_config.py`, `tests/unit/test_profile.py` — extended for `DatabaseConfig`, the hosted profile,
and the `open`/`close` no-ops.

### Contract Tests

`tests/contract/test_port_contract.py`, parametrised over `fake_stores` and `pg_stores`. **The same
assertions run against both.** The Postgres run is `postgres`-marked and deselected by default.

The highest-value assertions remain the score-direction ones: dense ascending, lexical descending. They are
what stands between a helpfully-normalising adapter and a gate whose thresholds have quietly stopped meaning
anything.

### Integration Tests

`tests/integration/test_pipeline_against_postgres.py` — `rag_core.pipeline.retrieve()` end to end against a
real database with `FakeEmbedder`. This is the first time the pipeline runs against anything real, and it is
where a wrong score direction or a broken `join` shows up as a wrong *answer* rather than a wrong row.

### Edge Cases

- `k <= 0`, and `k` larger than the corpus (fewer rows, not an error)
- Empty database: `search` returns `[]`, `count()` returns 0, `read_manifest()` returns `None`
- A question that sanitises to an empty tsquery ("what is it?") → `[]`, not a syntax error
- A question of pure punctuation → `[]`
- `upsert` twice with identical chunks → identical row count and identical ids
- `upsert` with changed content → content and `tsv` both updated, no duplicate row
- A vector of the wrong dimension → raises
- A zero vector → `NaN` distance → gate fails closed
- `index()` called for a chunk id that was never upserted → raises, naming `upsert`
- `write_manifest` twice → one row, second wins
- `close()` without `open()`, and `close()` twice → no error
- A document id containing underscores (`some_drug_name_3`) → round-trips through `split_chunk_id`

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
# Fakes only — must stay green with no database at all
uv run pytest -q

# Start a database
docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py

# The same contract assertions, against real Postgres
uv run pytest -m postgres -v
```

### Level 4: Manual Validation

```bash
# The schema is what Architecture §5 says it is
psql "$DATABASE_URL" -c "\d chunk"      # tsv generated always as (...) stored
psql "$DATABASE_URL" -c "\di chunk*"    # hnsw on embedding, gin on tsv

# Migrations are idempotent
uv run python db/migrate.py && uv run python db/migrate.py   # second run: 0 applied, exit 0

# Score directions are opposite, against real data
uv run python -c "
import asyncio, os
from rag_core.config import load_config
from rag_adapters.profile import build_profile
cfg = load_config(env={**os.environ, 'RAG_PROFILE': 'hosted'})
p = build_profile(cfg)

async def main():
    await p.open()
    try:
        v = await p.embedder.embed_query('metformin dose')
        d = await p.dense.search(v, 5)
        l = await p.lexical.search('metformin dose', 5)
        print('dense  :', [round(h.score, 4) for h in d])
        print('lexical:', [round(h.score, 4) for h in l])
        assert [h.score for h in d] == sorted(h.score for h in d), 'dense must ascend'
        assert [h.score for h in l] == sorted((h.score for h in l), reverse=True), 'lexical must descend'
        print('score directions OK')
    finally:
        await p.close()
asyncio.run(main())
"

# The full pipeline, against real Postgres
uv run pytest tests/integration -m postgres -v

# An all-stopword question does not 500
uv run python -c "
from rag_adapters.tsquery import build_tsquery
assert build_tsquery('what is it?') == ''
assert build_tsquery('metformin dose') == 'metformin | dose'
print('tsquery OK')
"

docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
# No secret reached the public repository
git diff --cached | grep -inE "postgres(ql)?://[^ ]*:[^ ]*@" | grep -v "localhost" || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-2, plus the standard bar:

- [ ] **AC #1** — Contract suite passes against fakes **and** against a real Postgres, from the same assertions
- [ ] **AC #2** — Re-running `upsert` with the same chunks changes no row count and no id
- [ ] **AC #3** — Dense search returns raw cosine distance; a test pins `1 - distance` against a known vector pair
- [ ] **AC #4** — Lexical search returns results for a multi-word clinical query and empty (not an error) for a query that sanitises to nothing
- [ ] **AC #5** — Reading the manifest when none has been written returns a distinguishable "absent", not a crash
- [ ] **AC #6** — CI runs both suites; `uv run pytest` alone stays green with no database
- [ ] All validation commands pass with zero errors
- [ ] `mypy --strict` clean; the TICKET-1 vendored-port exclusions and mypy override are unchanged
- [ ] `test_core_purity` still passes — `rag_core` imports neither `asyncpg` nor `pgvector`
- [ ] No credential in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 13 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full suite green with and without a database
- [ ] The same contract assertions pass for both store implementations
- [ ] CI green, both test steps
- [ ] Acceptance criteria all met
- [ ] TICKET-4 can call `upsert` and `write_manifest`; TICKET-5 can call `read_manifest`

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 `asyncpg`; D2 port the tokeniser and OR-join into
`to_tsquery`; D3 CI service container plus a `postgres` marker.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — `LexicalStore.index()` is a verify-and-no-op on this adapter. The generated `tsv` column
   means there is no separate lexical index to maintain, which is ADR-002's structural win. The alternative
   readings are that `index()` should write text-only rows (impossible: `embedding` is `NOT NULL`) or that it
   should be removed from the port (diverges from Architecture §4, which lists it). **This is the single
   design decision in the ticket most worth a reviewer's disagreement.**
2. **Assumed** — the manifest goes on `DenseStore` rather than a fifth port. Keeps ADR-001's four-port cap and
   avoids TICKET-5 branching on profile.
3. **Assumed** — `Profile` gains `open()`/`close()` and `build_profile` stays synchronous.
4. **Assumed** — `asyncpg` and `pgvector` become an **optional** dependency group so `dependencies = []`
   stays true and `test_core_purity` keeps its package-level claim.
5. **Assumed** — `pgvector/pgvector:pg17` in CI. Any Postgres 14+ with the extension works; pg17 is current
   and matches what Neon offers.
6. **Assumed** — `upsert` writes the parent `document` row in the same transaction, rather than requiring
   callers to order their writes.
7. **Assumed** — the contract suite is restructured rather than contorted to fit the old builder shape,
   accepting that this contradicts a comment TICKET-1 wrote.
8. **Open, deferred** — `ts_rank_cd` normalization stays at the default `0`. If TICKET-9 finds length bias in
   the lexical leg, option `1` or `2` is the knob, and changing it there keeps the measurement clean.

---

## NOTES (open canvas)

### The fakes under-specified the ports, and that is the useful finding

ADR-001 worried about "over-abstracting ports that only ever get two implementations." The risk landed in the
opposite direction: the ports were *under*-specified, because the only implementation had no connection, no
lifecycle and no schema. Three things a real store needs were missing — a way to persist the manifest, a way
to open and close, and a contract suite that admits a fixture rather than a constructor.

This is the normal shape of a second implementation and it is worth saying out loud in review, because it
looks like scope creep and is not. The alternative — bending Postgres to fit a shape derived from a
dictionary — produces a worse adapter and a contract suite that tests less.

### Why `index()` doing nothing is the whole point

The local build's ingestion has a comment explaining that vectors are written *before* the SQLite transaction,
because an orphaned vector is invisible while an orphaned row makes a document appear `ready` but
unsearchable. It has a compensating-delete path, a `_safe_cleanup` that swallows exceptions so a failing
cleanup cannot strand a document in `processing`, and a whole `reconcile_vectors` management command to repair
the states that still get through.

All of that exists because two stores can disagree. Here they cannot: `tsv` is generated from `content` in the
same row, in the same insert. So `index()` has nothing to do, and every one of those mechanisms is deleted
rather than ported. That is the return on ADR-002, and it is much larger than the `ts_rank_cd` cost it paid.

The risk is that a no-op looks like an unfinished method. Hence verify-and-raise rather than `pass`.

### Alternatives weighed and rejected

**`websearch_to_tsquery` on the raw question.** Less code, no injection surface, handles quoted phrases. But
it ANDs terms, and the local build ORs them — and the stopword list exists *because* OR-joined function words
would leave `lexical_support` permanently True and collapse the gate's middle band. Switching to AND would
change lexical recall and the gate's behaviour simultaneously, right before TICKET-9 tries to attribute a
difference to `ts_rank_cd`.

**A fifth `ManifestStore` port.** Cleaner separation, but ADR-001 caps the count at four and refuses a fifth
without a third implementation demanding one. The manifest describes the dense index; `DenseStore` is where it
belongs.

**Making `build_profile` async.** Simpler than an `open()`/`close()` pair, but it pushes `await` into every
caller and makes the composition root itself asynchronous, for the sake of one adapter that needs a pool.

**Alembic.** One schema, no history, no branches. It would add a dependency and a second source of truth for a
table definition that Architecture §5 already writes out in full.

**A transaction-rollback fixture instead of `truncate`.** More elegant and genuinely faster at scale, but it
fights `asyncpg` pooling (every test must hold the same connection) and the suite is small enough that
`truncate` is both simpler and quick.

### Sequencing risk

TICKET-1 is on an open PR. Branching this ticket from an unmerged `main` gets a repository with no `rag_core`
in it at all — the failure is immediate and obvious, but it wastes a setup cycle. Merge PR #1 first.

TICKET-3 (providers) is genuinely parallel with this one: different files, different ports, no shared code
except `profile.py`'s `_REGISTRY`, where each adds one entry. The two will conflict on that one line and on
`pyproject.toml`'s dependency list. Both are trivial merges; worth knowing before running them in parallel
worktrees.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
