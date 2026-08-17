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
                            │  SSE
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

**API shell** — owns transport only. Request validation, SSE framing, rate limiting, telemetry assembly, error mapping. If a behaviour would be identical over gRPC, it belongs in `rag_core`, not here.

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

```sql
create extension if not exists vector;

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

`index_manifest` exists because the embedding model is part of the schema, not a runtime setting. On startup the service compares the configured embedder against the manifest and refuses to serve if they disagree. Silently querying an index built by a different model returns plausible-looking garbage, which is the worst failure mode available.

`ordinal` and the unique constraint make ingestion idempotent: re-running upserts in place rather than duplicating.

## 6. Ingestion path

1. Parse source documents, preserving section structure where it exists.
2. Chunk to roughly 800 tokens with about 15% overlap, splitting on section boundaries first and only falling back to fixed windows inside oversized sections.
3. Embed in batches sized to the provider's quota, with backoff.
4. Upsert chunks with vectors and let the generated `tsvector` column populate itself.
5. Write the manifest.

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

## ADR-001: Extract `rag_core` rather than fork the Django app

**Status:** Accepted · **Date:** 17 Aug 2026

### Context

The local system is a Django application with retrieval logic distributed across views, services, and management commands. The hosted profile needs different providers and a deployment target Django does not suit well. The obvious move is a fork.

### Decision

Extract all retrieval logic into a framework-agnostic `rag_core` package with provider ports. Django and FastAPI become thin shells over it.

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
- Revisit: if a port only ever has one real implementation, delete it.

### Action items

1. [ ] Extract chunking, retrieval, fusion, gate, prompt assembly into `rag_core`
2. [ ] Define the four ports; no provider SDK imported inside the package
3. [ ] Fake adapters for tests
4. [ ] Reduce the Django views to shell calls

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

1. [ ] Schema with `vector(768)`, HNSW cosine index, generated `tsvector`, GIN index
2. [ ] Measure `ts_rank_cd` against the FTS5 baseline on the evaluation set
3. [ ] Document the ranking difference in the README rather than hiding it

---

## ADR-003: Gate on raw retrieval scores, not the RRF score

**Status:** Accepted · **Date:** 17 Aug 2026 · **Supersedes** the local build's gate input

### Context

The local implementation evaluates the confidence gate on the fused hybrid score. Reviewing that for the hosted port surfaced a flaw worth correcting rather than porting.

Reciprocal rank fusion consumes ranks and discards magnitudes. Every non-empty result set produces a top-1 with the same fused score — `2/(60+1)` when both retrievers agree on first place — whether the best chunk is a near-exact match or entirely unrelated. A threshold over RRF output therefore measures *how many retrievers agreed on an ordering*, not *whether anything relevant was found*. It is very nearly a constant.

### Decision

Gate on the raw scores. Require the best cosine similarity to clear τ, and require at least one chunk to appear in both retrievers' top-k. Keep RRF for ordering the context window, which is what it is good at.

### Options considered

**Option A — Threshold on the fused RRF score** (the current local behaviour)

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

1. [ ] Change the gate input to raw scores; keep RRF for context ordering
2. [ ] Sweep τ against the evaluation set, plotting refusal rate versus answer quality
3. [ ] Report both conditions separately in telemetry
4. [ ] Backport the fix to the local profile — the flaw is in both

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
