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
