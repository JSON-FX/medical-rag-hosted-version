# Medical RAG — Hosted Version

**Architecture**
Status: Draft · Last updated: 17 Aug 2026 · Companion to [PRD.md](./PRD.md)

---

## 1. Context

The original system answers questions over clinical documents entirely on-device: Django, Next.js, Ollama running Llama 3.1 8B, ChromaDB for vectors, SQLite FTS5 for lexical search, `nomic-embed-text` for embeddings, hybrid retrieval fused with reciprocal rank fusion, and a confidence gate ahead of generation. The whole design follows from one requirement — patient data cannot leave the machine.

That requirement makes the system undemonstrable to anyone who is not sitting at the machine. This document describes a second deployment profile that runs on managed infrastructure against a public corpus, sharing one implementation with the local build.

The forcing question for the whole design: **what is genuinely different between the two profiles, and what only looks different?** Inference and storage are genuinely different. Chunking, retrieval strategy, fusion, gating, citation, and prompt construction are not. The architecture draws the boundary exactly there.

## 2. System overview

```
┌───────────────────────────────────────────────────────────┐
│  Next.js frontend            (Vercel, static + edge)      │
└───────────────────────────┬───────────────────────────────┘
                            │  NDJSON
┌───────────────────────────▼───────────────────────────────┐
│  API shell — FastAPI (hosted) │ Django (local)            │
│  thin: HTTP, streaming, rate limiting, telemetry          │
└───────────────────────────┬───────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────┐
│  rag_core                    ← all the logic lives here   │
│  chunking · retrieval · RRF · gate · prompt · citations   │
│                                                            │
│  ports:  EmbeddingProvider  GenerationProvider            │
│          DenseStore         LexicalStore                  │
└───────┬───────────────────────────────────┬───────────────┘
        │                                   │
   ┌────▼──────────────┐          ┌─────────▼─────────────┐
   │ LOCAL adapters    │          │ HOSTED adapters       │
   │ Ollama · Chroma   │          │ Gemini · Groq         │
   │ SQLite FTS5       │          │ Postgres pgvector+FTS │
   └───────────────────┘          └───────────────────────┘
```

Visual walkthrough of the request path: [`medical-rag-system-flow.html`](./medical-rag-system-flow.html).

## 3. Components

**`rag_core`** — a plain Python package with no web framework and no provider SDKs imported at module level. Everything provider-specific enters through a port. This package is the deliverable; the two HTTP shells are packaging.

**API shell** — owns transport only. Request validation, NDJSON framing, rate limiting, telemetry assembly, error mapping. If a behaviour would be identical over gRPC, it belongs in `rag_core`, not here.

The transport is **NDJSON**, one JSON object per line, not SSE. An earlier draft of this document said SSE. The local build ships `application/x-ndjson` with a tested browser-side parser, and reusing that frame vocabulary makes the frontend a re-skin rather than a rewrite. `json.dumps` escapes embedded newlines, so answer text containing line breaks cannot split a frame. The frame types are `meta`, `token`, `sources`, `error`, `done`, defined in `rag_core/contracts.py` so the shell and the frontend can be built against one definition.

**Frontend** — a single question box, a streaming answer pane with inline citations, and a telemetry strip showing gate decision, fused scores, retrieval latency, time to first token, and which provider served the request. The telemetry strip is a product feature, not debug output; it is the part that shows engineering rather than describing it.

**Ingestion job** — runs on a developer machine or in CI, never in a request handler. Writes to the same stores the query path reads.

## 4. Ports

Four interfaces. Two implementations each.

```python
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int
    def embed_documents(self, texts: list[str]) -> list[Vector]: ...
    def embed_query(self, text: str) -> Vector: ...

class GenerationProvider(Protocol):
    model_id: str
    def stream(self, prompt: Prompt) -> Iterator[Token]: ...

class DenseStore(Protocol):
    def search(self, vector: Vector, k: int) -> list[Scored[Chunk]]: ...
    def upsert(self, chunks: list[EmbeddedChunk]) -> None: ...

class LexicalStore(Protocol):
    def search(self, query: str, k: int) -> list[Scored[Chunk]]: ...
    def index(self, chunks: list[Chunk]) -> None: ...
```

`Scored` carries the provider's native score untransformed. The gate needs the raw magnitude and normalising early would destroy it — see ADR-003.

Profile selection is one environment variable read at startup, resolved once into a container of adapters. No conditional provider logic anywhere below that.

## 5. Data model

Hosted profile, single Postgres database. Dense and lexical retrieval hit the same table, which is the main structural win over the local split between ChromaDB and SQLite.

The `document` table below was referenced but never defined in the first draft of this section; `chunk.document_id` had a foreign key pointing at nothing. It is defined here, and TICKET-2 implements it.

```sql
create extension if not exists vector;

create table document (
  id            text primary key,       -- slug, e.g. 'metformin'
  title         text not null,
  source_set_id text,                   -- openFDA set_id, pinning the exact label revision
  ingested_at   timestamptz not null
);

create table chunk (
  id            text primary key,
  document_id   text not null references document(id),
  ordinal       int  not null,
  anchor        text not null,          -- page or section, for citation resolution
  content       text not null,
  embedding     vector(768) not null,
  tsv           tsvector generated always as (to_tsvector('english', content)) stored,
  unique (document_id, ordinal)
);

create index on chunk using hnsw (embedding vector_cosine_ops);
create index on chunk using gin (tsv);

create table index_manifest (
  id                  int primary key default 1,
  embedding_model_id  text not null,
  dimension           int  not null,
  ingested_at         timestamptz not null
);
```

`index_manifest` is constrained to a single row (`check (id = 1)`). The `default 1` above permits rows 2, 3, … which would make "the" manifest ambiguous; there is exactly one index, so there is exactly one manifest. `chunk.document_id` cascades on delete, so removing a document cannot strand its chunks.

`index_manifest` exists because the embedding model is part of the schema, not a runtime setting. On startup the service compares the configured embedder against the manifest and refuses to serve if they disagree. Silently querying an index built by a different model returns plausible-looking garbage, which is the worst failure mode available.

`ordinal` and the unique constraint make ingestion idempotent: re-running upserts in place rather than duplicating.

## 6. Ingestion path

1. Parse source documents, preserving page structure.
2. Chunk with the local build's page-aware recursive splitter, ported unchanged: **1000 characters with 150 characters of overlap, never spanning a page boundary**, splitting on `\n\n`, `\n`, `. `, then ` `. The effective maximum length is `size + overlap` — 1150 characters, roughly 300 tokens.
3. Embed in batches sized to the provider's quota, with backoff.
4. Upsert chunks with vectors and let the generated `tsvector` column populate itself.
5. Write the manifest.

An earlier draft specified ~800 tokens with 15% overlap and section-first splitting. That was replaced with the shipped chunker for two reasons: every measured number in the local build's evaluation was produced by this splitter, so changing it invalidates the baseline the parity claim rests on; and never spanning a page boundary is what gives every chunk an exact page number, which is what the schema's `anchor` column stores and what citations resolve to.

Run offline. On free embedding quotas a full corpus pass may need to span more than one day, which is fine for a fixed corpus and is exactly why on-demand upload is out of scope.

## 7. Query path

**Embed query** with the manifest-matched embedder.

**Retrieve in parallel.** Dense by cosine distance, lexical by `ts_rank_cd`, each returning its own top-k with native scores. On the hosted profile both are queries against one database and can be issued concurrently.

**Fuse** with reciprocal rank fusion:

```
score(d) = Σ  1 / (k + rank_i(d))        k = 60
         i∈{dense, lexical}
```

RRF is deliberately rank-only. It needs no score calibration between two retrievers whose scores are not comparable, which is exactly why it works here — and exactly why it cannot be used as the gate input.

**Gate.** Evaluated on raw retrieval scores, not the fused score. See ADR-003. Two conditions, both required:

- the best raw cosine similarity across retrieved chunks clears τ
- at least one chunk appears in both retrievers' top-k (agreement)

Failing either returns a refusal. No prompt is built, no model is called.

**Generate.** Build the prompt from the fused top-n chunks, each labelled with its chunk ID, and instruct the model to cite by ID and to decline anything not present in the context. Stream tokens through.

**Gate again — stage 2.** The gate above is stage 1, and it is not the whole refusal path. The system prompt instructs the model to answer with the exact string `INSUFFICIENT_CONTEXT` and nothing else when the retrieved context does not support an answer; the stream is buffered until that decision is conclusive, and a refusal is replaced with server-authored copy rather than being passed through.

This document originally described only stage 1. Stage 2 is not an implementation detail: on the local build's evaluation set it accounted for 9 of 25 correct declines — the near-miss questions where retrieval finds the right *document* but the specific fact is absent. Dropping it would have quietly halved the refusal path. The buffering is what makes it possible; a sentinel cannot be streamed to the browser and then retracted, and the buffer costs a few tokens of latency.

**Cite.** Resolve chunk IDs in the output to source document and anchor on the way out. An unresolvable citation is a bug and is logged as one.

## 8. Failure handling

| Failure | Behaviour |
|---|---|
| Primary generator rate-limited or errors | Retry once, then fall to secondary; response reports which served it |
| All generators unavailable | Explicit service message; never an ungrounded answer |
| Embedding provider down | Fail the request; there is no meaningful degraded retrieval without a query vector |
| Manifest mismatch at startup | Refuse to serve, log loudly |
| Per-IP rate limit exceeded | 429 with a plain-language message and retry hint |

The fallback chain is a hard requirement rather than polish, because free tiers change quota without notice and a single-provider demo is a demo with an expiry date nobody told you about.

## 9. Deployment

Frontend and API on Vercel; Postgres with pgvector on Neon; rate-limit counters in Upstash. Ingestion runs from a laptop or a GitHub Actions job, never in a request handler.

Vercel Hobby permits non-commercial personal use only, which covers a portfolio piece and would not cover a client deployment. Function duration on Hobby is generous relative to a single RAG request; the binding constraints are the absence of a filesystem and of any long-lived process, both of which the design already accommodates.

---

## ADR-001: A standalone repository with a verbatim-ported core

**Status:** Amended · **Date:** 17 Aug 2026 · Originally "Extract `rag_core` rather than fork the Django app"

### Context

The local system is a Django application with retrieval logic already isolated in a framework-free `rag/` package — 735 lines, no Django imports, pinned by a purity test. The coupling to Django is confined to the orchestration around it. The hosted profile needs different providers and a deployment target Django does not suit well.

This ADR originally chose Option B below: one repository containing `rag_core` with both a Django and a FastAPI shell over it. That is not what was built.

### Decision

**Amended.** The hosted profile lives in its own repository. The pure modules — chunking, fusion, gate, prompts, sentinel — are ported into it **unchanged**, and they bring their existing test suites with them. The four provider ports are defined in the new repository; the Django application is left untouched.

The Options analysis below is unchanged, because its reasoning still holds and its warning is now a live risk rather than one this design avoided.

### Why the amendment

A shared package would have made parity structural: one implementation, therefore no drift possible. Two repositories cannot offer that guarantee, so it is replaced with a behavioural one — **the same tests, asserting the same numbers, running against both copies.** The ported test files are byte-identical to the originals apart from an import prefix, and that identity is verified rather than asserted:

```bash
diff <(sed 's/^from rag\./from rag_core./' $SRC/tests/unit/test_gate.py) tests/unit/test_gate.py
```

This is genuinely weaker than a shared package. It catches behavioural divergence, not the slow structural rot of two codebases growing apart, and it only holds as long as nobody edits a ported test to make a port compile. The guard against that is that a red ported test is treated as a port bug, never a test bug.

Two consequences follow and are accepted:

- **PRD G2 and success criterion 5 cannot hold as written.** "Both profiles run from the same `rag_core` package" is false across two repositories. Restated in the PRD.
- **The vendored files are excluded from the formatter and from three lint rules** (`pyproject.toml`). A formatter rewrite would destroy the diff that makes this decision defensible, so the tooling is configured to leave them alone rather than relying on discipline.

### Options considered

**Option A — Fork the Django app**

| Dimension | Assessment |
|---|---|
| Complexity | Low up front |
| Cost | High over time |
| Team familiarity | Highest |

Pros: fastest path to a running demo; no refactor risk.
Cons: two copies of the retrieval logic that drift within weeks; destroys the claim that both profiles run the same pipeline, which is the entire point of the exercise.

**Option B — Extract a shared core**

| Dimension | Assessment |
|---|---|
| Complexity | Medium up front |
| Cost | Low over time |
| Team familiarity | High |

Pros: one implementation, provable parity, testable without a web framework or a network; the abstraction is itself the thing worth showing.
Cons: a real refactor before any new feature; risk of over-abstracting ports that only ever get two implementations.

### Trade-off analysis

Forking is faster to a demo and fatally undermines the argument the demo exists to make. Two divergent copies of a retrieval pipeline is a worse artifact than no hosted version at all, because it invites the question of which one is real.

The over-abstraction risk is bounded by holding the port count to four and refusing to add a fifth without a third implementation demanding it.

### Consequences

- Easier: testing retrieval with fake providers, at speed, with no network.
- Easier: adding a third profile later, which is what a client would actually ask for.
- Harder: the first day is refactoring rather than building.
- **Harder, post-amendment:** the two copies can now drift, and only behaviour is checked. A change to the local build's chunker will not fail anything here.
- Revisit: if a port only ever has one real implementation, delete it.

On that last point — in this repository each port has exactly one real adapter plus a fake. The fake is the second implementation and it is not merely test scaffolding: it is what lets the entire pipeline run in milliseconds with nothing installed and nothing reachable, which is the stated payoff of the abstraction. That is the justification, and it is recorded here rather than left implicit.

### Action items

1. [x] Port chunking, fusion, gate, prompt assembly and the sentinel filter into `rag_core`
2. [x] Define the four ports; no provider SDK imported inside the package
3. [x] Fake adapters — in `rag_adapters`, not in `tests/`
4. [x] Carry the pure modules' test suites across unedited, and verify the diff
5. [x] ~~Reduce the Django views to shell calls~~ — not applicable under the amendment; the Django application is untouched

---

## ADR-002: Postgres for both dense and lexical retrieval

**Status:** Accepted · **Date:** 17 Aug 2026

### Context

The local profile splits retrieval across ChromaDB (vectors, on disk) and SQLite FTS5 (lexical, on disk). Neither survives serverless: both assume a writable persistent filesystem, and there isn't one.

### Decision

On the hosted profile, put both halves in one Postgres database — pgvector with an HNSW index for dense, a generated `tsvector` column with a GIN index for lexical.

### Options considered

**Option A — Managed vector DB plus a separate search service**

| Dimension | Assessment |
|---|---|
| Complexity | High — two services, two clients, two free tiers |
| Cost | Free tiers exist but stack two sets of limits |
| Scalability | Best at large corpus sizes |

Pros: each component is purpose-built; the dense side scales furthest.
Cons: two failure domains and two quotas for a corpus small enough to fit comfortably in one database; consistency between the two indexes becomes a real problem during ingestion.

**Option B — Postgres for both**

| Dimension | Assessment |
|---|---|
| Complexity | Low — one connection, one migration path |
| Cost | One free tier |
| Scalability | Adequate to roughly six figures of chunks |

Pros: dense and lexical rows are the same row, so they cannot disagree; both retrievals are one round trip to one host; ordinary SQL tooling applies.
Cons: HNSW in pgvector trails dedicated vector engines at large scale; `ts_rank_cd` is not BM25 and ranks somewhat differently.

### Trade-off analysis

The scalability advantage of a dedicated vector store is real and irrelevant at this corpus size. The consistency advantage of colocating both indexes in one row is immediate and permanent: the local build's split store makes partial-ingestion states possible where a chunk is searchable lexically but not densely, and single-table storage removes that class of bug outright.

The `ts_rank_cd` substitution is the honest cost of the move. It is a coverage-density ranking rather than BM25's term-frequency saturation model, so lexical ordering will differ from the local profile. This is measured on the evaluation set rather than assumed away.

### Consequences

- Easier: transactional ingestion, one client, one quota to monitor.
- Easier: filtering by document metadata in the same query as the vector search.
- Harder: lexical ranking is no longer strictly comparable to the local profile.
- Revisit: at roughly 10⁵–10⁶ chunks, or if recall measurably suffers.

### Action items

1. [x] Schema with `vector(768)`, HNSW cosine index, generated `tsvector`, GIN index — `db/migrations/001_initial.sql`
2. [ ] Measure `ts_rank_cd` against the FTS5 baseline on the evaluation set (TICKET-9)
3. [ ] Document the ranking difference in the README rather than hiding it (TICKET-9)

---

## ADR-003: Gate on raw retrieval scores, not the RRF score

**Status:** Amended · **Date:** 17 Aug 2026 · Originally claimed to supersede the local build's gate input

### Context

**Correction.** This ADR was written on the premise that "the local implementation evaluates the confidence gate on the fused hybrid score." That is not true, and reading the code before porting is what surfaced it. The local build already gates on the raw vector-leg `top_similarity` plus cross-retriever agreement — Option C below, the option this ADR goes on to choose. Its own source says so at the top of `chat/retrieval.py`:

> The gate reads `top_similarity` from the VECTOR leg directly, not from fused output: RRF deliberately discards score magnitude, so fused scores carry no similarity information.

and the behaviour is pinned by `test_mean_similarity_does_not_affect_the_decision` in its gate tests. So this ADR supersedes nothing; it *documents* a design the local build already has. The reasoning below is still worth keeping, because it is the argument for that design — it simply is not a correction to anything.

What survives as live work is the note in Consequences about re-tuning τ. That part is real and it is the highest-likelihood risk in the PRD.

Reciprocal rank fusion consumes ranks and discards magnitudes. Every non-empty result set produces a top-1 with the same fused score — `2/(60+1)` when both retrievers agree on first place — whether the best chunk is a near-exact match or entirely unrelated. A threshold over RRF output therefore measures *how many retrievers agreed on an ordering*, not *whether anything relevant was found*. It is very nearly a constant.

### Decision

Gate on the raw scores. Require the best cosine similarity to clear τ, and require at least one chunk to appear in both retrievers' top-k. Keep RRF for ordering the context window, which is what it is good at.

### Options considered

**Option A — Threshold on the fused RRF score** (~~the current local behaviour~~ — never implemented anywhere; see the correction above)

Pros: one number, already implemented.
Cons: the number is scale-free and nearly constant; the gate does not do what its name says.

**Option B — Threshold on raw dense similarity alone**

Pros: directly measures semantic closeness; interpretable.
Cons: blind to lexical-only matches, which for clinical text means exact drug and code matches with mediocre cosine scores get refused.

**Option C — Raw similarity plus cross-retriever agreement** *(chosen)*

Pros: catches the case where one retriever finds something the other missed entirely, which is the signature of a weak match; each condition is separately interpretable in telemetry.
Cons: two parameters to tune instead of one; agreement is a coarse signal at small k.

### Trade-off analysis

Option A is cheapest and is measuring the wrong quantity, which is worse than measuring nothing because it produces a confident-looking number. Option B is correct but incomplete for a domain where exact terminology matching carries real weight. Option C costs one extra tuning parameter and covers both failure shapes.

Being able to say in the telemetry strip *which* condition failed is worth the second parameter on its own.

### Consequences

- Easier: explaining what the gate measures, to a reviewer or to yourself in six months.
- Easier: tuning, since each condition moves independently.
- Harder: two parameters now need re-tuning whenever the embedding model changes.
- Note: τ from the local build is meaningless here. A different embedding model has a different similarity distribution; the old value must be discarded, not carried over.

### Action items

1. [x] ~~Change the gate input to raw scores~~ — already the case; the ported gate is unchanged and keeps RRF for context ordering
2. [ ] **Sweep τ against the evaluation set**, plotting refusal rate versus answer quality (TICKET-8). The carried-over 0.70 / 0.75 are marked invalid in `rag_core/config.py` — they were measured against a different embedding model and say nothing about this one
3. [x] Report both conditions separately in telemetry — `contracts.explain_gate` returns them independently and `Telemetry.as_dict` never collapses them into one number
4. [x] ~~Backport the fix to the local profile — the flaw is in both~~ — **not applicable.** There is no flaw to backport; the local profile was already correct

---

## ADR-004: Two generation providers with a fallback chain

**Status:** Accepted · **Date:** 17 Aug 2026

### Context

Free inference tiers are rate-limited, are not covered by any SLA, and change terms without notice. A demo needs to work in an interview months from now, on a day nobody is watching the quota.

Trial-credit providers were considered and rejected outright: cheap per token is irrelevant when the free allocation expires on a fixed date. A demo that dies quietly after thirty days is worse than one that was never built, because you will link to it without checking.

### Decision

Primary and secondary generation providers behind one port, with automatic failover and the serving provider reported in every response.

### Trade-off analysis

Two providers means two prompt behaviours to validate and two sets of quirks. The alternative is a single point of failure entirely outside your control, on an artifact whose only job is to work when someone clicks it. Reporting the active provider in the telemetry strip converts the failover from hidden infrastructure into a visible design decision, which is the outcome worth having.

### Consequences

- Easier: surviving a provider outage or a quota change without touching code.
- Harder: prompt behaviour must be checked against both models, not one.
- Revisit: if the secondary is never exercised in ninety days, test it deliberately rather than assuming it works.

### Action items

1. [ ] Implement failover on rate limit and error, with one retry before falling through
2. [ ] Surface the active provider in the response payload and the UI
3. [ ] Validate the prompt against both models on the evaluation set
4. [ ] Health check that exercises the secondary path on a schedule

---

## 10. Limitations

Stated plainly, because a system whose limits are unstated has not been understood.

- **`ts_rank_cd` is not BM25.** Lexical ranking differs from the local profile. The difference is measured, not assumed to be negligible.
- **No re-ranker.** A cross-encoder between fusion and gate would likely improve precision at top-k. Omitted for cost and latency; it is the first thing to add.
- **The gate cannot detect a confidently wrong corpus.** It measures retrieval strength, not source correctness. If the corpus contains an outdated guideline, the system will cite it with full confidence.
- **Single-turn only.** Follow-up questions carrying pronouns will retrieve badly. Query rewriting is the fix and is not implemented.
- **Free-tier reliability is best-effort.** Concurrent load will hit rate limits. This is a demo, not a service.
- **Not a clinical tool** and it must never be presented as one, in the interface or in conversation about it.

## 11. Testing

The point of `rag_core` having no framework and no SDKs is that most of this runs in milliseconds with no network.

- **Unit** — chunking boundaries and overlap; RRF against hand-computed rankings; gate decisions across a score matrix including both failure conditions.
- **Contract** — every adapter runs the same suite against its port, so local and hosted are held to one specification.
- **Retrieval evaluation** — a fixed question set with known relevant chunks, scored on recall@k and MRR, run against both profiles. These are the numbers published in the README.
- **Gate calibration** — a labelled set of answerable and unanswerable questions; report false-refusal and false-answer rates at candidate τ values rather than picking a threshold by feel.
- **Failover** — revoke the primary key in a test environment and assert a correct answer still returns with the secondary reported.
