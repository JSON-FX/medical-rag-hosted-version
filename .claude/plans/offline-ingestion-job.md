# Feature: TICKET-4 — Offline ingestion job

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for ported logic:** `/Users/jsonse/Documents/development/interview/medical-rag/backend/` — referred to as **SRC**. A different repository; read from it, never write to it.

---

## Feature Description

The job that puts the corpus in the database: three public-domain FDA drug labels, chunked, embedded, and
upserted, with the index manifest written last.

It runs on a laptop or in CI and never in a request handler (ARCHITECTURE.md §3). On a free embedding quota a
full pass may need to span more than one day, so it is resumable — and because `upsert` is idempotent on chunk
id, resuming needs no checkpoint file of its own: the rows already in the database *are* the progress marker.

This is the ticket that makes the demo answerable. Every prior ticket built machinery with nothing in it.

## User Story

As the author of the Medical RAG project
I want a repeatable, resumable job that ingests the fixed corpus
So that the demo has something to retrieve, and so that re-running it after a fixture edit converges instead of duplicating.

## Problem Statement

The database schema exists and the embedder works, but nothing connects them. There is no corpus in this
repository at all — the fixtures live in SRC, and the code that assembles them into documents is entangled with
a Django ingestion path that assumes uploaded PDFs.

Downstream, TICKET-5 has an empty index to serve (the gate would return `empty_corpus` to every question),
TICKET-8 cannot sweep τ without real vectors, and TICKET-9 has nothing to measure recall against.

## Solution Statement

A `src/ingest/` package: the three fixtures and their manifest, a pure corpus assembler ported from SRC, the
near-miss absence scan, and a CLI that runs the whole pass.

Three decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **No PDF round-trip.** JSON → assemble → paginate → `PageText` → chunk. | Measured, not assumed: SRC's `make_fixture_pdf.py` writes UTF-8 bytes into a PDF string literal and pypdf decodes them as Latin-1. **All 28 non-ASCII characters in the corpus corrupt on the round-trip** — `β-lactamase` → `Î²-lactamase`, `patient's` → `patientâ€™s`. 6 of 13 amoxicillin pages are affected. The local build embedded that corrupted text. Skipping the PDF is lossless and drops the `pypdf` dependency. |
| D2 | **Synthetic pages as the citation anchor**, ~1200 characters, exactly as SRC paginates. | Keeps `chunk_pages`' page-boundary guarantee, keeps `to_context_chunk`'s numeric-anchor assertion true, and keeps citations resolving to a stable location. A section-name anchor would break a contract landed in TICKET-2 and change the prompt format. |
| D3 | **Run it for real** against a local Postgres with real Gemini embeddings, in addition to the fake-backed tests. | The fake embedder cannot exercise dimension, `task_type`, or real batching against a live quota. TICKET-8's τ sweep needs real vectors to measure. |

**D1 has a consequence TICKET-9 must carry:** the local baseline numbers were measured on corrupted text, so a
hosted-vs-local retrieval comparison is no longer strictly like-for-like. The hosted corpus is *better*, not
merely different, and that difference is not attributable to pgvector or `ts_rank_cd`. TICKET-9 must say so
rather than quietly banking the improvement.

## Out of Scope / Non-Goals

- **Not included: fetching new fixtures.** SRC has `fetch_fixtures.py`; the three labels are ported as-is,
  pinned by their `set_id`. Re-fetching would change the text and invalidate the labelled question set.
- **Not included: the evaluation harness.** `questions.yaml`, `collect.py`, `metrics.py`, `sweep.py` are
  TICKET-8. This ticket ports only `axes.py`, because the corpus's own absence claims need verifying here.
- **Not included: the startup manifest comparison.** This writes the manifest; TICKET-5 reads it and refuses
  to serve on mismatch.
- **Not included: production ingestion.** The real run targets a **local** Postgres. Ingesting into Neon is
  TICKET-10, where the production `DATABASE_URL` first exists.
- **Not included: uploads, or any on-demand ingestion path.** PRD §4 non-goal. The corpus is fixed.
- **Not included: a fifth port method.** See the note in Task 4 on why the job reaches for the Postgres
  adapter directly instead.
- **Not changing:** the vendored port, the chunker, the schema, or any adapter.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium — the assembly and chunking are ported and pure; the complexity is resumability, stale-chunk convergence, and being honest about what a "page" is.
**Primary Systems Affected**: `src/ingest/` (new), `pyproject.toml`, `tests/integration/`, docs
**Dependencies**: none new — **`pypdf` is deliberately not added** (D1)

## Related Work

**Implements**: TICKET-4 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`

**Back-references**:

- `.claude/plans/postgres-dense-and-lexical-stores.md` — the store this writes to, `IndexManifest`, and the `postgres` marker pattern the tests follow.
- `.claude/plans/hosted-provider-adapters-and-failover.md` — `GeminiEmbedder`, its batching and backoff, and the `live` marker precedent.

**Forward-references**:

- TICKET-5 — reads the manifest this writes, at startup
- TICKET-8 — sweeps τ against the vectors this produces; also inherits `axes.py` and the fixtures
- TICKET-9 — measures recall against this corpus, and must carry D1's consequence

**Sequencing:** TICKET-3 is on **PR #3, not yet merged**, and this needs `GeminiEmbedder`. Branch from
`feature/hosted-provider-adapters-and-failover`, or wait for the merge.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

**This repository:**

- `src/rag_core/chunking.py` — Why: `chunk_pages(pages: list[PageText], cfg: ChunkConfig) -> list[ChunkDraft]`.
  **`ChunkDraft.chunk_index` is global across pages, not per-page** — it becomes `ordinal` directly. Read the
  size contract in the docstring: effective max is `size + overlap`, 1150 characters.
- `src/rag_core/contracts.py` — Why: `Chunk`, `EmbeddedChunk`, `IndexManifest`, `make_chunk_id`. Note
  `Chunk.anchor` is text and `to_context_chunk` asserts it parses as an int.
- `src/rag_adapters/postgres.py` — Why: `PostgresPool`, `PostgresDenseStore.upsert` (idempotent on id, writes
  the parent `document` row in the same transaction), `write_manifest`.
- `src/rag_adapters/profile.py` — Why: `build_profile` and `Profile.open()/close()`. The job gets its embedder
  from here so `RAG_PROFILE=fake` swaps it for tests.
- `db/migrate.py` — Why: the CLI shape this mirrors — DSN from the environment, a `main() -> int`,
  `raise SystemExit(asyncio.run(main()))`, and progress printed per unit of work.
- `db/migrations/001_initial.sql` — Why: `document(id, title, source_set_id, ingested_at)` is what
  `source_set_id` gets written from, and `chunk.embedding` is `vector(768) NOT NULL`.
- `tests/contract/conftest.py` — Why: the `pg_pool` fixture pattern, including skipping readably without
  `DATABASE_URL` and truncating between tests.
- `pyproject.toml` — Why: `[tool.hatch.build.targets.wheel] packages` needs `src/ingest` adding. Do not widen
  the vendored-port exclusions or the mypy override.

**Source repository (port from, do not modify):**

- `SRC/evals/corpus.py` (63 lines, read in full) — Why: `SECTION_TITLES` ordering, `_paginate`,
  `assemble_text`, `corpus_text`, `load_manifest`, `load_drug`. Everything except `build_pdf` ports. Read the
  docstring's claim that pagination "exercises the page-aware chunker the way an uploaded document would" —
  still true, and now the *only* reason pagination exists.
- `SRC/evals/axes.py` (39 lines, read in full) — Why: `NEAR_MISS_AXES` and `verified_absent_axes`. **Read the
  comment on stems**: literal matching failed twice, and "a false ABSENCE is the dangerous direction — it ships
  a near-miss that is actually answerable, counted as a false decline at every operating point."
- `SRC/evals/fixtures/{manifest,metformin,atenolol,amoxicillin}.json` — Why: the corpus itself. `manifest.json`
  carries `set_id`, `included_sections`, `withheld_sections`, `included_chars`, `verified_absent` per drug.
- `SRC/documents/ingestion.py` (127 lines) — Why: **mostly as a list of what NOT to port.** Its ordering
  comment, `cleanup_document`, `_safe_cleanup` and the `destroyed_previous` dance all exist because two stores
  could disagree. One row cannot (ADR-002), so none of it ports. What *does* port is the principle in its
  comment at lines 64-66: everything failure-prone happens before stored data is touched.
- `SRC/tests/fixtures/make_fixture_pdf.py` — Why: **read it to understand the bug, then do not port it.**
  `_escape` handles `\`, `(` and `)` but the content stream carries raw UTF-8 bytes with a `/Helvetica` font
  and no encoding declaration, so pypdf decodes them as Latin-1.

### New Files to Create

```
src/ingest/
├── __init__.py
├── corpus.py            # fixtures loader, section assembly, pagination
├── axes.py              # ported near-miss absence scan
├── run.py               # the CLI
└── fixtures/
    ├── manifest.json
    ├── metformin.json
    ├── atenolol.json
    └── amoxicillin.json
tests/unit/test_corpus.py            # assembly, pagination, absence scan — no database
tests/integration/test_ingestion.py  # postgres-marked: full run, idempotence, resume, convergence
```

Modified: `pyproject.toml`, `README.md`, `docs/ARCHITECTURE.md`, `docs/PRD.md`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [PostgreSQL — `DELETE`](https://www.postgresql.org/docs/current/sql-delete.html)
  - Why: stale-chunk convergence in Task 4 deletes by `ordinal >=`, which needs the same transaction as the upsert.
- [Gemini API — embeddings rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
  - Why: the free tier's requests-per-minute is what makes the run resumable rather than a single pass. The
    adapter already backs off; this informs `EMBED_BATCH_SIZE`.
- `.claude/reports/hosted-provider-adapters-and-failover-report.md`
  - Why: records that `GeminiEmbedder` batches at `cfg.embedding.batch_size`, retries `ProviderUnavailable`
    with doubling delays, and renormalises. The job should not re-implement any of it.

### Patterns to Follow

**A CLI that reports per unit of work and exits with a status.** From `db/migrate.py`:

```python
async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    ...

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

**Module docstrings state the failure the module prevents**, citing `ARCHITECTURE.md §N` or an ADR — never
`spec N.N`, which refers to a document that does not exist in this repository.

**Comments record measured facts and rejected alternatives.** This ticket has an unusually good example
available: D1's corruption is measurable, and the comment explaining why there is no PDF should say what was
measured rather than "we don't need PDFs".

**Marker-gated tests that skip readably.** From `tests/contract/conftest.py`:

```python
dsn = os.environ.get("DATABASE_URL", "")
if not dsn:
    pytest.skip("DATABASE_URL is not set; start a pgvector container to run these")
```

**Anti-patterns to avoid:** a bare `except Exception`; re-implementing batching or backoff that
`GeminiEmbedder` already does; adding a port method for something only the offline job needs; deleting the
whole document and re-inserting (it destroys resumability); reading `os.environ` outside `load_config` or a
CLI entry point.

---

## IMPLEMENTATION PLAN

### Phase 1: The corpus, as pure code

No database, no network. Everything here is deterministic and testable in milliseconds.

**Tasks:** 1 (package + fixtures), 2 (corpus assembly), 3 (absence scan + tests).

### Phase 2: The job

**Depends on:** Phase 1.

**Tasks:** 4 (the ingest pass), 5 (the CLI).

### Phase 3: Proof

**Depends on:** Phase 2.

**Tasks:** 6 (integration tests), 7 (the real run), 8 (docs).

---

## STEP-BY-STEP TASKS

### 1. CREATE `src/ingest/` package with the fixtures

- **IMPLEMENT**: `src/ingest/__init__.py` and `src/ingest/fixtures/` holding the four JSON files copied
  byte-for-byte from `SRC/evals/fixtures/`. Add `"src/ingest"` to
  `[tool.hatch.build.targets.wheel] packages`.
- **PATTERN**: `src/rag_adapters/` — same `src/` layout, same package registration.
- **GOTCHA**: A real package under `src/`, **not** a top-level script directory like `db/`. TICKET-8 and
  TICKET-9 both import the corpus loader, and `db/migrate.py`'s `sys.path.insert` dance is already repeated in
  three test files. Making this importable stops that spreading.
- **GOTCHA**: Copy the fixtures verbatim. They are pinned by `set_id` and the labelled question set in
  TICKET-8 refers to their exact contents.
- **VALIDATE**: `uv sync && uv run python -c "import ingest, pathlib; print(sorted(p.name for p in (pathlib.Path(ingest.__file__).parent / 'fixtures').glob('*.json')))"`
- **SATISFIES**: AC #1

### 2. CREATE `src/ingest/corpus.py`

- **IMPLEMENT**: Port from `SRC/evals/corpus.py`, dropping `build_pdf` and the `make_pdf` import:
  `SECTION_TITLES`, `CHARS_PER_PAGE = 1200`, `_paginate`, `assemble_text`, `corpus_text`, `load_manifest`,
  `load_drug`. Add `paginate_document(slug) -> list[PageText]` returning `rag_core.chunking.PageText` with
  1-based page numbers.
- **PATTERN**: `SRC/evals/corpus.py` in full. Keep `corpus_text`'s docstring intent — it is "the exact string"
  the corpus contains, and now nothing transforms it afterwards.
- **GOTCHA**: **No `build_pdf`, and no `pypdf` dependency.** The reason is measurable and belongs in the module
  docstring: SRC's PDF writer emits UTF-8 into a content stream with no encoding declaration, pypdf decodes it
  as Latin-1, and every one of the corpus's 28 non-ASCII characters corrupts — `β-lactamase` becomes
  `Î²-lactamase`. The local build ingested that. Write the finding down; a future reader will otherwise assume
  the PDF step was dropped for convenience.
- **GOTCHA**: `SECTION_TITLES` ordering is load-bearing — it fixes the order sections appear in, which fixes
  pagination, which fixes page numbers, which fixes citations. Do not sort it or use the JSON's key order.
- **GOTCHA**: `_paginate` splits on whitespace and rejoins with single spaces, so it normalises internal
  whitespace. That is existing behaviour and the manifest's absence claims were computed against it — do not
  "improve" it.
- **VALIDATE**: `uv run python -c "
from ingest.corpus import load_manifest, paginate_document
for slug in load_manifest()['drugs']:
    pages = paginate_document(slug)
    print(slug, len(pages), 'pages,', sum(len(p.text) for p in pages), 'chars')
    assert [p.page_number for p in pages] == list(range(1, len(pages) + 1))
"` — expect metformin 9, atenolol 15, amoxicillin 13.
- **SATISFIES**: AC #1, AC #5

### 3. CREATE `src/ingest/axes.py` and `tests/unit/test_corpus.py`

- **IMPLEMENT**: Port `SRC/evals/axes.py` unchanged — `NEAR_MISS_AXES`, `verified_absent_axes`. Then tests
  covering: the manifest's `verified_absent` claim matches a fresh scan for all three drugs; pagination is
  deterministic and 1-based; no page exceeds `CHARS_PER_PAGE`; section order is stable; and **no mojibake
  survives into the assembled text**.
- **PATTERN**: `SRC/evals/axes.py`. Carry the stem comment verbatim — it records two real failures and names
  the dangerous direction.
- **GOTCHA**: The absence scan is what makes TICKET-8's near-miss questions legitimate. A near-miss question
  is only fair if the corpus genuinely lacks the answer, and withholding a *section* does not guarantee that —
  metformin's `dosage_and_administration` discusses paediatric dosing even with `pediatric_use` withheld.
- **GOTCHA**: The claims **do** hold on clean assembled text — verified during planning for all three drugs.
  If the test fails, a fixture changed, not the scan.
- **GOTCHA**: `manifest.json`'s `included_chars` is ~110 lower per drug than `len(assemble_text(...))`, because
  it counts section bodies without the titles `assemble_text` inserts. Systematic and consistent across all
  three; do not assert equality against it.
- **VALIDATE**: `uv run pytest tests/unit/test_corpus.py -v`
- **SATISFIES**: AC #1

### 4. CREATE `src/ingest/run.py` — the ingest pass

- **IMPLEMENT**: `async def ingest_document(pool, embedder, slug, cfg) -> IngestResult` doing, per document:
  1. `paginate_document(slug)` → `chunk_pages(pages, cfg.chunk)` → drafts
  2. Build the intended `Chunk` list: `id=make_chunk_id(slug, draft.chunk_index)`, `ordinal=chunk_index`,
     `anchor=str(draft.page_number)`, `content=draft.text`, `document_title=slug.title()`
  3. Read what is already stored: `select id, content from chunk where document_id = $1`
  4. Embed **only** chunks whose content differs from stored, or that do not exist
  5. `upsert` the newly embedded ones
  6. **Converge**: `delete from chunk where document_id = $1 and ordinal >= $2` with the new chunk count
  Return counts of chunks total / embedded / skipped / deleted.
- **PATTERN**: `SRC/documents/ingestion.py:64-66` for the principle — everything failure-prone happens before
  stored data is touched. The rest of that file does **not** port.
- **GOTCHA**: **Resumability needs no checkpoint.** `upsert` is idempotent on chunk id, so the rows already in
  the database are the progress marker. Step 3 is the entire resume mechanism, and it is why an interrupted
  run costs only the embeddings it had not yet made — which matters when a full pass can span more than one day
  on a free quota (ARCHITECTURE.md §6).
- **GOTCHA**: Compare **content**, not just id. A fixture edit leaves the ids identical while the text changes;
  skipping on id alone would leave stale vectors that retrieve the old text forever, with nothing to notice.
- **GOTCHA**: Step 6 exists because a shortened document leaves orphan high-ordinal chunks that `upsert` will
  never touch. Do **not** solve it by deleting the document first and re-inserting — that destroys resumability
  and re-spends the entire quota on every run.
- **GOTCHA**: This job talks to `PostgresDenseStore` and the pool **directly**, not through `DenseStore`.
  Step 3 needs "what is already stored", which no port method exposes and which nothing on the query path
  wants. Adding a fifth method for an offline script's benefit is exactly the over-abstraction ADR-001 warns
  about; the job is hosted-profile-specific by design (ARCHITECTURE.md §3: it "writes to the same stores the
  query path reads").
- **GOTCHA**: Do not re-implement batching or backoff. `GeminiEmbedder.embed_documents` already batches at
  `cfg.embedding.batch_size` and retries with doubling delays. Hand it the full list.
- **VALIDATE**: `uv run pytest tests/integration/test_ingestion.py -m postgres -v -k "single_document"`
- **SATISFIES**: AC #2, AC #3, AC #4

### 5. ADD the CLI to `src/ingest/run.py`

- **IMPLEMENT**: `main()` — resolve config via `load_config()`, build the profile, `await profile.open()`,
  ingest every drug in `load_manifest()["drugs"]` in sorted order, print per-document counts, then
  **write the manifest last** with the embedder's actual `model_id` and `dimension` and a UTC timestamp.
  `finally: await profile.close()`. Entry point `uv run python -m ingest.run`.
- **PATTERN**: `db/migrate.py` — same DSN-from-environment check, same `main() -> int`, same
  `raise SystemExit(asyncio.run(main()))`, same per-unit progress output.
- **GOTCHA**: **The manifest is written last, once, after every document succeeds.** An interrupted run
  therefore leaves no manifest, and TICKET-5's startup check refuses to serve a partially-populated index. That
  is a designed property, not an accident — say so in a comment.
- **GOTCHA**: Write the manifest from `embedder.model_id` and `embedder.dimension`, **not** from
  `cfg.embedding.*`. AC #4 is that the manifest records what actually ran; reading config would record what was
  *asked for*, which is the same value right up until the moment it matters.
- **GOTCHA**: `source_set_id` from `manifest.json` goes on the `document` row. `PostgresDenseStore.upsert`
  currently writes `document` with `id` and `title` only — either extend it or write the document row here. If
  extending, keep it in the same transaction.
- **GOTCHA**: Never print the API key or the DSN. This repository is public.
- **VALIDATE**: `RAG_PROFILE=fake DATABASE_URL=... uv run python -m ingest.run` against a local container —
  prints three documents with chunk counts and exits 0.
- **SATISFIES**: AC #1, AC #4

### 6. CREATE `tests/integration/test_ingestion.py`

- **IMPLEMENT**: `postgres`-marked, using `FakeEmbedder` and a real pool:
  - a full pass stores chunks for all three documents, and `count()` matches the draft total
  - re-running changes no row count and no ids (AC #2), and embeds **zero** chunks the second time
  - interrupting after one document and resuming completes with no duplicates and no gaps (AC #3)
  - editing a chunk's content re-embeds only that chunk
  - shortening a document deletes the orphaned high-ordinal chunks
  - every chunk's `anchor` is the page its text came from, checked against `paginate_document` (AC #5)
  - the manifest matches the embedder that ran (AC #4)
  - an embedder failure part-way leaves no manifest
- **PATTERN**: `tests/integration/test_pipeline_against_postgres.py` for the `pg_dsn` fixture and the
  migrate-then-truncate setup.
- **GOTCHA**: AC #5 is the one that catches an off-by-one between `ChunkDraft.page_number` and `anchor`.
  Assert the chunk's text is a substring of the page its anchor names — that is what makes a citation true.
- **GOTCHA**: `ExplodingEmbedder` from `rag_adapters.fakes` already exists for the failure case; do not write
  another.
- **VALIDATE**: `uv run pytest tests/integration -m postgres -v`
- **SATISFIES**: AC #2, AC #3, AC #4, AC #5

### 7. RUN the real ingest (D3)

- **IMPLEMENT**: Against a local pgvector container with a real `GEMINI_API_KEY`:
  `RAG_PROFILE=hosted uv run python -m ingest.run`. Then verify: 768-dimensional vectors stored, all unit
  norm, manifest naming `gemini-embedding-001`, and a real `retrieve()` returning sensible chunks for
  "What is the adult starting dose of metformin?".
- **PATTERN**: the Level 4 manual validation in `.claude/plans/postgres-dense-and-lexical-stores.md`.
- **GOTCHA**: **This needs the user's `GEMINI_API_KEY` in the session.** Ask for it rather than assuming one is
  set, and never echo it. If it is unavailable, stop and report the ticket as PARTIAL rather than silently
  falling back to fakes — D3 was an explicit decision and quietly skipping it would misreport what was proven.
- **GOTCHA**: This costs quota. ~37 pages, a few hundred chunks, one pass. If it rate-limits, the backoff
  handles it and a resumed run costs only the remainder — which also happens to demonstrate AC #3 for real.
- **GOTCHA**: Do not commit the resulting database or any key. Local container only.
- **VALIDATE**: the manual block in VALIDATION COMMANDS Level 4.
- **SATISFIES**: AC #1, AC #4, and the D3 decision

### 8. UPDATE docs

- **IMPLEMENT**: README gains a "Corpus" section and the ingest command. ARCHITECTURE §6 gains the D1 note:
  no PDF round-trip, and why. **PRD open question 3** ("is one shared corpus enough to make the refusal path
  feel natural") is answerable now — the three labels with their `verified_absent` axes are exactly the narrow
  corpus with obvious edges the PRD hoped for; record that. Add a note for TICKET-9 that the local baseline was
  measured on corrupted text.
- **PATTERN**: prior tickets' doc reconciliation — amend and date, never silently rewrite.
- **GOTCHA**: State the corruption finding plainly in ARCHITECTURE §6. It is a real defect in the sibling
  project, and the reason this repository's corpus differs from it. A future reader comparing the two will
  otherwise conclude the chunker changed.
- **GOTCHA**: Do not claim retrieval parity or improvement anywhere. TICKET-9 measures; this ticket only makes
  the measurement possible and flags that the baselines differ.
- **VALIDATE**: `grep -n "pypdf" pyproject.toml uv.lock | head` returns nothing in `[project]`, and
  `grep -n "open question 3" docs/PRD.md` finds the answer.
- **SATISFIES**: AC #1

---

## TESTING STRATEGY

### Unit Tests

`tests/unit/test_corpus.py` — no database, no network. Assembly order, pagination determinism and bounds, the
absence scan against the manifest's claims, and an assertion that no mojibake sequence appears in the assembled
text (the regression guard for D1).

### Integration Tests

`tests/integration/test_ingestion.py`, `postgres`-marked, `FakeEmbedder` + a real pool. The eight cases in
Task 6. These are where idempotence, resumability and convergence are actually proven.

### Manual / Live

The Task 7 real run. Not a test — a one-time operation whose output is recorded in the implementation report.

### Edge Cases

- Re-running immediately → zero embeddings made, zero rows changed
- Resuming after one document → the remaining two ingest, the first is untouched
- A fixture edited to change one chunk's text → exactly one chunk re-embedded
- A fixture edited to be shorter → orphaned high-ordinal chunks deleted
- A fixture edited to be longer → new chunks appended, existing ones untouched
- Embedder fails on document two → documents one's chunks persist, **no manifest is written**
- An empty `included` section map → the document produces no chunks, and the run says so rather than silently succeeding
- Every chunk's `anchor` names a page whose text contains that chunk
- Chunk ids round-trip through `split_chunk_id`

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
uv run pytest -m postgres -v
```

### Level 4: Manual Validation

```bash
# The corpus assembles to the expected shape
uv run python -c "
from ingest.corpus import load_manifest, paginate_document, assemble_text, load_drug
from ingest.axes import verified_absent_axes
for slug, meta in load_manifest()['drugs'].items():
    text = assemble_text(load_drug(slug)['included'])
    pages = paginate_document(slug)
    scanned = verified_absent_axes(text)
    print(f'{slug:12} {len(pages):3} pages  {len(text):6} chars  absent={scanned}')
    assert scanned == meta['verified_absent'], slug
print('corpus OK')
"

# No mojibake survives (the D1 regression guard)
uv run python -c "
import re
from ingest.corpus import assemble_text, load_drug, load_manifest
for slug in load_manifest()['drugs']:
    text = assemble_text(load_drug(slug)['included'])
    assert not re.search(r'[ÂÃÎ][\x80-\xbf]|â€', text), slug
    assert 'β-lactamase' in text or slug != 'amoxicillin'
print('no mojibake')
"

# A fake-backed full run, twice — the second must embed nothing
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
RAG_PROFILE=fake uv run python -m ingest.run
RAG_PROFILE=fake uv run python -m ingest.run   # expect: 0 embedded, 0 changed

# --- THE REAL RUN (D3) — needs GEMINI_API_KEY ---
docker exec medrag-pg psql -U postgres -c "truncate document, chunk, index_manifest cascade"
export GEMINI_API_KEY=...        # supplied by the user; never echoed
RAG_PROFILE=hosted uv run python -m ingest.run

# Real vectors: right width, unit norm, manifest matches
uv run python -c "
import asyncio, math, os
from rag_adapters.postgres import PostgresPool
async def main():
    pool = PostgresPool(os.environ['DATABASE_URL']); await pool.open()
    try:
        n = await pool.pool.fetchval('select count(*) from chunk')
        row = await pool.pool.fetchrow('select embedding from chunk limit 1')
        v = list(row['embedding'])
        m = await pool.pool.fetchrow('select * from index_manifest where id = 1')
        print('chunks   :', n)
        print('dimension:', len(v))
        print('norm     :', math.sqrt(sum(x*x for x in v)))
        print('manifest :', m['embedding_model_id'], m['dimension'])
        assert len(v) == 768 and m['dimension'] == 768
        assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-5
    finally:
        await pool.close()
asyncio.run(main())
"

# End to end against the real index
uv run python -c "
import asyncio, os
from rag_core.config import load_config
from rag_core.pipeline import retrieve
from rag_adapters.profile import build_profile
cfg = load_config(env={**os.environ, 'RAG_PROFILE': 'hosted'})
p = build_profile(cfg)
async def main():
    await p.open()
    try:
        r = await retrieve('What is the adult starting dose of metformin?', p.embedder, p.dense, p.lexical, cfg)
        print('gate  :', r.decision.reason, r.decision.signals['top_similarity'])
        for c in r.chunks: print('  ', c.chunk_id, 'p.', c.page_number, '-', c.text[:70])
    finally:
        await p.close()
asyncio.run(main())
"

docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
git diff --cached | grep -inE "AIza[0-9A-Za-z_-]{20,}|postgres(ql)?://[^ ]*:[^ ]*@" | grep -v localhost || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-4, plus the standard bar:

- [ ] **AC #1** — A full ingest of all three labels completes and reports chunk counts per document
- [ ] **AC #2** — Re-running produces identical row count and identical chunk ids, and embeds nothing (PRD F4)
- [ ] **AC #3** — Interrupting mid-run and resuming completes without duplicate or missing chunks
- [ ] **AC #4** — The manifest matches the embedder that actually ran, and is written only on full success
- [ ] **AC #5** — A chunk's `anchor` resolves to the page its text came from
- [ ] **AC #6** — The real run (D3) completed against real Gemini, with 768-dim unit-norm vectors stored
- [ ] All validation commands pass with zero errors
- [ ] `mypy --strict` clean; vendored-port exclusions and mypy override unchanged
- [ ] `uv run pytest` stays green with no database and no keys
- [ ] `pypdf` is not a dependency
- [ ] No key or DSN in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 8 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full suite green with and without a database
- [ ] Prior tickets' suites unchanged
- [ ] CI green
- [ ] Acceptance criteria all met
- [ ] TICKET-5 has a populated index and a manifest to check; TICKET-8 has real vectors to sweep

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 no PDF round-trip; D2 synthetic pages as the citation
anchor; D3 run it for real against local Postgres and real Gemini.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — `src/ingest/` is a real package rather than a script directory like `db/`, because TICKET-8
   and TICKET-9 both import the corpus loader and the `sys.path.insert` pattern is already in three test files.
2. **Assumed** — the job reaches for the Postgres adapter directly rather than gaining a fifth port method for
   "what is already stored". The offline job is hosted-specific by design.
3. **Assumed** — content comparison, not id comparison, decides what to re-embed. Costs one `select` per
   document and catches the fixture-edit case that id comparison misses silently.
4. **Assumed** — convergence by deleting `ordinal >= new_count`, not by delete-then-reinsert, which would
   destroy resumability and re-spend the whole quota every run.
5. **Assumed** — `document_title` is `slug.title()` (`"Metformin"`), matching what SRC does. The fixtures carry
   no display title of their own.
6. **Assumed** — the manifest's `included_chars` is not asserted against, since it counts section bodies
   without titles and is ~110 lower per drug by definition.
7. **Open, carried to TICKET-9** — the local baseline was measured on mojibake-corrupted text, so a
   hosted-vs-local retrieval comparison is not strictly like-for-like. TICKET-9 must state this rather than
   bank the improvement as though it came from the storage change.
8. **Needs the user at execution time** — Task 7 requires `GEMINI_API_KEY`. If it is not available, the ticket
   is PARTIAL, not COMPLETE.

---

## NOTES (open canvas)

### The finding that shaped this ticket

SRC's eval corpus is built by rendering fixture JSON into a PDF and reading it back with pypdf. The writer
escapes `\`, `(` and `)` — the characters that would corrupt the *file* — but emits the text as raw UTF-8 bytes
into a content stream declared with `/Helvetica` and no encoding. pypdf decodes those bytes as Latin-1.

Measured across the three labels during planning:

| drug | pages | non-ASCII chars | pages corrupted |
|---|---|---|---|
| metformin | 9 | 3 | 2 |
| atenolol | 15 | 4 | 4 |
| amoxicillin | 13 | 21 | 7 |

Every one of the 28 non-ASCII characters corrupts. The shipped corpus contains `Î²-lactamase`, not
`β-lactamase` — so the local build embedded that string, indexed that string, and a visitor asking about
β-lactamase would get no lexical match at all.

Two things follow. First, the PDF step should go: it is a lossy round-trip that buys fidelity to a document
format this profile never actually ingests. Second — and this is the part worth carrying forward — the local
build's published retrieval numbers were measured on corrupted text. That does not invalidate them, but it does
mean TICKET-9's hosted-vs-local comparison has a third variable in it alongside pgvector and `ts_rank_cd`.

It is also worth telling the sibling project. The fix there is a WinAnsi or CID font encoding in
`make_fixture_pdf.py`, and it would change their eval numbers.

### Why resumability is nearly free here

The obvious design is a checkpoint file recording how far the last run got. It is unnecessary: `upsert` is
idempotent on chunk id, so the rows in the database already encode exactly what has been done. Resume is a
`select`, and the only thing an interrupted run costs is the embeddings it had not yet made.

The one subtlety is that "already there" must mean "already there *with this content*". A fixture edit keeps
every id and changes the text, so an id-only check would leave stale vectors pointing at text that no longer
exists — retrieving the old wording forever, with nothing anywhere to notice. One `select id, content` per
document closes that, and at three documents the cost is irrelevant.

### Alternatives weighed and rejected

**Delete the document, then re-insert.** Simplest possible convergence, and it destroys the resumability the
free quota makes necessary — every run would re-embed everything.

**A content hash column.** Cheaper comparison than shipping content back, and it needs a schema migration for
a corpus of a few hundred rows. Revisit if the corpus grows by two orders of magnitude.

**A fifth port method for "existing chunks".** Would let the job run against fakes end to end. Rejected: it
widens a port shared by the request path for the benefit of an offline script, which is the over-abstraction
ADR-001 explicitly warns about. The job is allowed to know it targets Postgres.

**Fixing the PDF encoding and keeping the round-trip.** Offered as an option and not chosen. It is real work —
WinAnsi or a CID font — in a ticket that is otherwise about batching and idempotence, and the PDF buys nothing
this profile uses.

### Sequencing

TICKET-3 is on PR #3 and this needs `GeminiEmbedder`; branch from it. After this lands, TICKET-5 (API shell)
has a populated index to serve and a manifest to check at startup, and TICKET-8 has real vectors to sweep τ
against — which is the first point where the gate's thresholds stop being provisional.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
