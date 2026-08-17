# Medical RAG — Hosted Version

A publicly reachable version of the Medical RAG system. It answers clinical questions grounded in a fixed
corpus of public-domain FDA drug labels, with a two-stage confidence gate that refuses to answer when
retrieval is weak.

> **Not a clinical tool.** This is a portfolio and engineering demonstration. Nothing it produces is medical
> advice, and it must never be presented as such.

## Status

Under construction. This repository currently contains `rag_core` — the framework-agnostic retrieval
pipeline — and its fake adapters. The Postgres store, the hosted providers, the API shell and the frontend
land in subsequent tickets. See [`docs/tickets/medical-rag-hosted-version.md`](docs/tickets/medical-rag-hosted-version.md).

## Why a second version exists

The original system runs entirely on-device — Django, Next.js, Ollama, ChromaDB, SQLite FTS5 — because
patient data cannot leave the machine. That is the right architecture for its use case and the wrong
architecture for a link someone can click.

This version keeps the retrieval pipeline and swaps only inference and storage: Ollama becomes Gemini and
Groq, ChromaDB plus SQLite FTS5 becomes one Postgres database with `pgvector` and a generated `tsvector`.
The reasoning behind that boundary is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Layout

```
src/rag_core/       pure — chunking, fusion, gate, prompts, sentinel, ports, pipeline.
                    Imports nothing outside the standard library. Enforced by a test.
src/rag_adapters/   impure — provider and store implementations, plus the composition root.
tests/unit/         unit tests. Several are ported unedited from the local build (see below).
tests/contract/     one suite every adapter must satisfy, per port.
docs/               PRD, architecture and ADRs, ticket breakdown.
```

The dependency direction only ever points inward: `rag_adapters` imports `rag_core`, never the reverse.

## Running the tests

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

No network, no database and no API key is required. That is a deliberate property of `rag_core`, not a
convenience — the whole pipeline is testable against fakes in milliseconds.

The store adapters are held to the same contract suite as the fakes, which does need a database. Those
tests are marked `postgres` and deselected by default:

```bash
docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
uv run pytest -m postgres
```

See [`db/README.md`](db/README.md).

The provider adapters are held to that same suite using SDK doubles, so they need no key
either. A separate set of `live`-marked tests calls the real APIs and costs quota — they
never run in CI:

```bash
export GEMINI_API_KEY=... GROQ_API_KEY=...
uv run pytest -m live
```

## Corpus

Three FDA drug labels — metformin, atenolol, amoxicillin — public domain, pinned to exact label revisions by
`set_id`. Around 71 chunks. Deliberately narrow: each drug has axes measured *absent* from its text, so a
visitor can find the edges of what the system knows in under a minute, and the refusal path is easy to trigger
on purpose rather than by accident.

Ingestion is offline and idempotent. Re-running converges instead of duplicating, and resuming after an
interruption costs only the embeddings that had not yet been made:

```bash
export DATABASE_URL="postgresql://..." GEMINI_API_KEY=...
uv run python db/migrate.py
RAG_PROFILE=hosted uv run python -m ingest.run
```

There is no PDF anywhere in that path, and the reason is worth knowing: the local build round-trips the same
fixtures through a generated PDF, which corrupts every non-ASCII character in the corpus — `β-lactamase`
becomes `Î²-lactamase`. See [`docs/ARCHITECTURE.md` §6](docs/ARCHITECTURE.md).

## Providers

Embeddings come from `gemini-embedding-001`, reduced from its native 3072 dimensions to the
schema's 768. That reduction truncates rather than re-projects, which breaks the unit norm
cosine similarity depends on, so the adapter renormalises — and pins it with a test.

Generation runs Groq first and Gemini second, behind a failover chain. Groq leads because
time-to-first-token is the number that matters for a demo; Gemini is a different company
with a different quota and a different outage, which is the point. Every response reports
which one served it.

Failover happens only before the first token. Once text has reached the reader it cannot be
retracted, so a mid-stream failure is reported as truncated rather than silently replaced.

## Running the API

```bash
export DATABASE_URL="postgresql://..."
RAG_PROFILE=hosted uv run uvicorn rag_api.main:app --port 8000
```

```bash
curl -s localhost:8000/api/health | python3 -m json.tool

curl -N -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What is the adult starting dose of metformin?"}'
```

The response is NDJSON — one JSON object per line — as `meta`, then `token`s, then `sources`, then `done`.
Telemetry arrives in two halves: the gate decision and retrieval latency on `meta`, the timings and serving
provider on `done`. A refusal therefore has its telemetry fully populated *before* the decline text, which is
what makes the refusal path read as deliberate rather than broken.

If the index was built by a different embedding model than the one configured, every query returns 503 naming
both — querying it would return plausible-looking garbage, which is the worst failure mode available.

## On parity with the local build

The pure modules — `chunking`, `fusion`, `gate`, `prompts`, `sentinel` — are ported from the local
repository unchanged, and they carry their original test suites with them. Those tests are the parity
harness: if the two implementations ever diverge, a test goes red rather than the difference going unnoticed.

## Measured results

Retrieval quality (recall@k, MRR) and gate calibration numbers are published here once TICKET-8 and
TICKET-9 have measured them. They are not filled in yet, and no placeholder stands in for them.

## Limitations

The full list lives in [`docs/ARCHITECTURE.md` §10](docs/ARCHITECTURE.md) and is repeated here at deploy.
The short version: the lexical ranking differs from the local build, there is no re-ranker, the gate
measures retrieval strength rather than source correctness, it is single-turn only, and it runs on free
tiers with no availability guarantee.
