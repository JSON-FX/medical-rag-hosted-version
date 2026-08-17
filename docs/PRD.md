# Medical RAG — Hosted Version

**Product Requirements Document**
Status: Draft · Owner: Jayson · Last updated: 17 Aug 2026

---

## 1. Summary

A publicly reachable version of the Medical RAG system, answering clinical questions grounded in a corpus of public medical literature, with a confidence gate that refuses to answer when retrieval is weak.

The existing system runs entirely on-device via Ollama. That is the right architecture for its stated use case and the wrong architecture for a link someone can click. This version keeps the retrieval pipeline byte-for-byte identical and swaps only the inference providers, so both deployments run from one codebase.

## 2. Why this exists

Two goals, in priority order.

**Make the strongest project in the portfolio demonstrable.** The local build cannot be shown without a screen share and a machine with Ollama running. Reviewers skim; a dead or unrunnable demo is worth less than a mediocre live one.

**Turn the local-vs-hosted tension into the argument rather than losing it.** The original data-residency justification is the best design reasoning in the project. Shipping a hosted version by deleting that reasoning would be a downgrade. Shipping it as a second profile behind one interface preserves the argument and adds a new one about abstraction boundaries.

A useful secondary outcome: a hosted deployment forces the retrieval stack onto managed infrastructure, which surfaces real constraints (no filesystem, no persistent process, rate-limited providers) that the local build never had to answer for.

## 3. Audience

**Primary — technical evaluator.** A CTO or senior engineer, ten minutes, probably on a phone, deciding whether to spend an hour on an interview. They need to reach a grounded answer with citations inside sixty seconds, and see the refusal path without hunting for it.

**Secondary — the author, in an interview.** Needs to drive the system live while narrating trade-offs, and needs it to not fall over on a hotel wifi connection.

**Not an audience: clinicians.** This is not a clinical decision tool and must not read like one.

## 4. Goals and non-goals

### Goals

- G1 — Public URL, no login, cold-start to first token under five seconds.
- G2 — One codebase, two profiles, selected by environment variable. No forked logic.
- G3 — Retrieval quality equal to or better than the local build, measured on a fixed question set.
- G4 — Runs at zero recurring cost on provider free tiers.
- G5 — The refusal path is discoverable in under a minute by someone who was not told about it.
- G6 — Every generated claim carries a citation that resolves to a retrievable source chunk.

### Non-goals

- Not multi-tenant. One shared corpus, no accounts, no per-user state.
- Not a document upload product. The corpus is fixed and ingested offline.
- Not handling PHI or any real patient data, in any form, ever.
- Not production-grade availability. Free tiers have no SLA and the demo says so.
- Not a chat product. Single-turn question and answer; conversation memory is out of scope for v1.

## 5. Constraints

| Constraint | Consequence |
|---|---|
| Vercel Hobby is non-commercial only | Fine for a portfolio piece; a client deployment would need Pro |
| No persistent filesystem on serverless | SQLite FTS5 cannot come along; lexical search must move to Postgres |
| Function duration ceiling of 300s | Generous for a single request; batch ingestion must run elsewhere |
| Free LLM tiers are rate-limited and can change without notice | A single-provider design will break; fallback is mandatory, not optional |
| Free embedding tiers are quota-limited per day | Corpus ingestion must be a one-time offline job, not on-demand |
| Public corpus only | Rules out anything resembling the original clinical dataset |

## 6. Functional requirements

Numbered for traceability into tests.

**Ingestion (offline, run once)**

- F1 — Ingest a fixed corpus of public medical documents into a dense index and a lexical index.
- F2 — Chunk documents with overlap, preserving a stable chunk ID, source document reference, and section or page anchor.
- F3 — Record the embedding model identifier and vector dimension alongside the index, and refuse to serve queries if the runtime embedder does not match.
- F4 — Ingestion is idempotent and re-runnable without duplicating chunks.

**Query**

- F5 — Accept a natural-language question and return a grounded answer or an explicit refusal.
- F6 — Embed the query with the same provider and dimension used for the corpus.
- F7 — Retrieve candidates from the dense index by cosine similarity and from the lexical index by rank, in parallel.
- F8 — Fuse the two rankings into one ordered list via reciprocal rank fusion.
- F9 — Evaluate the confidence gate on raw retrieval scores before any generation call.
- F10 — On gate failure, return a refusal that names what was searched and lists the closest sources found, without calling a language model.
- F11 — On gate pass, generate an answer constrained to the retrieved chunks, streamed token by token.
- F12 — Attach a citation to every factual claim, resolving to a chunk ID and its source anchor.
- F13 — Expose per-request telemetry in the response: retrieval latency, time to first token, total tokens, provider used, gate decision and score.

**Reliability**

- F14 — Fall back to a secondary generation provider on rate limit or error, and surface which provider served the request.
- F15 — Rate-limit public endpoints per IP, returning a clear message rather than a raw 429.
- F16 — Degrade to a stated "service unavailable" message when all providers fail, never to an ungrounded answer.

## 7. Non-functional requirements

| Dimension | Target | Notes |
|---|---|---|
| Time to first token | < 2.5s p50, < 5s p95 | Retrieval budget ~600ms of that |
| Retrieval latency | < 400ms p50 | Two parallel queries against one database |
| Refusal latency | < 800ms | No model call, so this should be fast and visibly so |
| Cost | $0/month recurring | Free tiers only; a spend above zero is a bug |
| Availability | Best effort | Explicitly stated on the page |
| Accessibility | Keyboard navigable, visible focus, reduced motion respected | |

## 8. Success criteria

The build is done when all of these hold.

1. A stranger with the URL reaches a cited answer without instructions.
2. The refusal path triggers on an out-of-corpus question and reads as deliberate, not broken.
3. Retrieval scores on the fixed evaluation set are at or above the local baseline, with numbers published in the README.
4. Killing the primary generation provider's key produces a working answer from the fallback, with the swap visible in the telemetry.
5. Both profiles run from the same `rag_core` package with no branching outside the adapter layer.
6. Thirty days after deploy, the demo still works and has cost nothing.

Criterion 6 is the one most likely to fail. Trial credits that expire are disqualifying regardless of how cheap the provider is.

## 9. Scope

### In scope for v1

- Fixed public corpus, ingested offline
- Single-turn question and answer with streaming
- Hybrid retrieval, RRF, confidence gate
- Provider abstraction with two profiles
- Fallback chain and rate limiting
- Telemetry panel showing gate decision, scores, latency, provider
- Evaluation set with published before/after retrieval numbers

### Deferred

- Multi-turn conversation and query rewriting
- Re-ranking model between fusion and gate
- User-uploaded documents
- Answer feedback capture

### Explicitly rejected

- Any handling of real patient data
- Login or accounts
- Anything that presents output as clinical advice

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Free tier limits tighten or the provider changes terms | High | Demo breaks silently | Fallback chain across two independent providers; a health check that fails loudly |
| Embedding swap degrades retrieval | Medium | Core claim of parity fails | Measure on the fixed question set before committing; keep the local index for comparison |
| Gate threshold mis-tuned after the embedding change | High | Refuses everything or nothing | Re-tune τ against the evaluation set; treat the old value as invalid |
| Python runtime streaming on Vercel behaves differently than expected | Medium | Loses the streaming demo | Spike this first; fallback is to host the API on Hugging Face Spaces and keep the frontend on Vercel |
| Someone reads the demo as medical advice | Low | Reputational | Persistent, non-dismissable disclaimer; refusal-first framing throughout the copy |
| A scraper burns the daily quota | Medium | Demo dead for the day | IP rate limit in front of every endpoint from day one |

## 11. Milestones

**Spike (2 hours, before anything else)** — Confirm the deployment target streams correctly from a Python handler, and confirm the embedding provider can emit 768-dimension vectors. Both are load-bearing; if either fails, the plan changes.

**Day 1** — Extract `rag_core` from the Django app with provider interfaces. Implement hosted adapters. Migrate the schema to Postgres with pgvector and tsvector. Run ingestion. Verify retrieval parity on the evaluation set.

**Day 2** — API shell with streaming and telemetry. Frontend. Fallback chain and rate limiting. Re-tune the gate. Deploy. Write the README with published numbers and a limitations section.

## 12. Open questions

1. Does the chosen embedding model's truncation to 768 dimensions need renormalization, and does skipping it measurably hurt cosine ranking? Assume yes; verify.
2. Should the lexical half use `ts_rank` or `ts_rank_cd`? Coverage density may matter more than frequency for clinical terminology.
3. Is one shared corpus enough to make the refusal path feel natural, or does the demo need a deliberately narrow corpus so out-of-scope questions are easy for a visitor to invent?
4. Should the telemetry panel be visible by default, or behind a toggle? Visible by default makes the engineering legible but adds noise for a non-technical reader.

Question 3 is a product question disguised as a technical one, and it decides how well the demo lands. A corpus narrow enough that its edges are obvious is probably better than a broad one that rarely refuses.
