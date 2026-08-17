# Implementation Report — TICKET-4: Offline ingestion job

**Plan**: `.claude/plans/offline-ingestion-job.md`
**Branch**: `feature/offline-ingestion-job` (stacked on `feature/hosted-provider-adapters-and-failover`)
**Status**: **PARTIAL** — 7 of 8 tasks complete. Task 7 (the real Gemini run, decision D3) is blocked on an
API key not available in this session. See *Outstanding* below.

## Summary

`src/ingest/` holds the corpus and the job that loads it: three public-domain FDA drug labels, 71 chunks,
chunked and embedded and upserted with the index manifest written last. Re-running embeds nothing; resuming
after an interruption costs only the embeddings not yet made; a shortened document converges rather than
leaving orphans.

There is no PDF anywhere in the path, and that is the ticket's main finding rather than a simplification.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | Package + fixtures | `src/ingest/__init__.py`, `src/ingest/fixtures/*.json` (CREATE), `pyproject.toml` (UPDATE) |
| 2 | Corpus assembler | `src/ingest/corpus.py` (CREATE) |
| 3 | Absence scan + tests | `src/ingest/axes.py`, `tests/unit/test_corpus.py` (CREATE) |
| 4 | The ingest pass | `src/ingest/run.py` (CREATE) |
| 5 | The CLI | `src/ingest/run.py` |
| 6 | Integration tests | `tests/integration/test_ingestion.py` (CREATE) |
| 7 | **The real run** | **OUTSTANDING — needs `GEMINI_API_KEY`** |
| 8 | Docs | `docs/ARCHITECTURE.md`, `docs/PRD.md`, `README.md` (UPDATE) |

The four fixture files are byte-identical to source, verified with `cmp`.

## Tests added

**369 tests: 302 without a database, 59 Postgres, 8 live. All passing.**

| File | Tests | Note |
|---|---|---|
| `tests/unit/test_corpus.py` | 45 | Assembly order, pagination bounds and determinism, chunk/page containment, the absence scan, and the mojibake regression guard |
| `tests/integration/test_ingestion.py` | 21 | `postgres`-marked: full pass, idempotence, resumability, convergence, citation anchors, manifest |

Page counts are pinned at 9 / 15 / 13 (metformin / atenolol / amoxicillin) — changing them moves every chunk
id, so it should be a deliberate act.

## Validation results

| Check | Result |
|---|---|
| Default suite (no database, no keys) | **302 passed**, 67 deselected, 0.63s |
| Postgres suite (all tickets) | **59 passed**, no regressions |
| Types | `mypy --strict` clean, 25 source files |
| Lint + format | `ruff` clean, 51 files |
| CLI, first run | 71 chunks across 3 documents, 71 embedded |
| CLI, second run | 71 chunks, **0 embedded, 71 skipped** — idempotence at the CLI level |
| `pypdf` | **not a dependency** |
| Secrets | clean |

AC #1–#5 met. **AC #6 (the real run) is outstanding.**

## Deviations from the plan

**1. `_record_source_ids` is a separate `update` after upsert, not a widened `upsert`.** The plan offered
either. `PostgresDenseStore.upsert` is shared with the query path's contract suite, and `source_set_id` is
corpus metadata only this job knows; widening the adapter for one caller's benefit is the coupling ADR-001
warns about. One extra statement per document, on an offline job.

**2. `src/ingest/axes.py` gets an `E501` per-file ignore.** It is a vendored port whose comments record two
real bugs, and reflowing them to 100 columns would break the diff against source for no benefit — the same
treatment the other vendored files already have.

**3. `test_every_anchor_names_the_page_the_text_came_from` matches on the chunk's tail, not the whole chunk.**
Overlap prepends up to 150 characters of the *previous* chunk, so a chunk is not a literal substring of its
page. The last 120 characters are unambiguously this chunk's own text, and that is what proves the anchor is
right.

**4. `test_corpus.py` asserts pinned page counts.** Not in the plan. Page counts determine chunk ids, which
determine what every stored citation points at — worth failing loudly on.

## Issues encountered

**The PDF round-trip corrupts the corpus, and the local build shipped it.** Measured during planning and
re-confirmed here:

| drug | pages | non-ASCII chars | pages corrupted |
|---|---|---|---|
| metformin | 9 | 3 | 2 |
| atenolol | 15 | 4 | 4 |
| amoxicillin | 13 | 21 | 7 |

Every one of the 28 corrupts. The local build's shipped corpus contains `Î²-lactamase`, so it embedded that,
indexed that, and a visitor asking about β-lactamase gets no lexical match. `make_fixture_pdf.py` escapes `\`,
`(` and `)` — the characters that would corrupt the *file* — but emits text as raw UTF-8 into a stream
declared `/Helvetica` with no encoding.

Two consequences are recorded in `docs/ARCHITECTURE.md` §6: this profile skips the PDF entirely, and
**TICKET-9 must not bank the resulting improvement** as though it came from pgvector or `ts_rank_cd`.

Worth reporting upstream to the local build. The fix there is a WinAnsi or CID font encoding, and it would
move their published eval numbers.

**Nothing else needed a second attempt.** The 21 integration tests passed on their first run, including the
anchor check that the plan flagged as the likely off-by-one.

## Outstanding

**Task 7 / AC #6 — the real ingest against Gemini.** You chose this explicitly (D3), so I have not silently
substituted the fake-backed run for it. `GEMINI_API_KEY` is set neither in the shell nor in `.env`.

To finish it, put the key in `.env` (already gitignored) and say so:

```bash
echo 'GEMINI_API_KEY=your-key-here' >> .env
```

Then the remaining work is one command plus verification — real 768-dim unit-norm vectors, a manifest naming
`gemini-embedding-001`, and a live `retrieve()` against the real index. A local pgvector container is already
running on port 55432 with the fake-embedded corpus in it, ready to be truncated and re-ingested.

If you would rather not spend the quota, say so and I will mark AC #6 as deliberately deferred to TICKET-10,
where the corpus has to be ingested into Neon anyway.

## Ready for the next step

Work committed. CI needs a push.

Next: finish Task 7, then `piv-create-pr`. TICKET-5 (API shell) unblocks either way — it needs the manifest
and a populated index, both of which exist now under the fake profile.
