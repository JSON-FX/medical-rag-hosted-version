# Implementation Report — TICKET-4: Offline ingestion job

**Plan**: `.claude/plans/offline-ingestion-job.md`
**Branch**: `feature/offline-ingestion-job` (stacked on `feature/hosted-provider-adapters-and-failover`)
**Status**: **COMPLETE** — all 8 tasks. The real run (D3) executed against live Gemini once keys were supplied.

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
| 7 | The real run | executed live — 71 chunks, `gemini-embedding-001` |
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

AC #1–#6 all met.

### The real run

| Check | Result |
|---|---|
| Ingest | 71 chunks across 3 documents in **6.4s**, all embedded |
| Dimensions | all 768 |
| Unit norm | min 0.999999988, max 1.000000013 |
| Manifest | `gemini-embedding-001`, dim 768 |
| `source_set_id` | populated for all three documents |
| Live provider suite | **8 passed** — both models stream, both emit the exact sentinel |

End-to-end retrieval against the real index:

| question type | top_similarity | lexical | gate |
|---|---|---|---|
| answerable (metformin dose) | 0.7623 | yes | `ok` |
| answerable (atenolol contraindications) | 0.7708 | yes | `ok` |
| near-miss (paediatric atenolol) | 0.7355 | yes | `ok` → stage 2's job |
| off-corpus medical (ibuprofen) | 0.6212 | yes | `off_domain` |
| off-domain (capital of France) | 0.4813 | no | `off_domain` |

Five questions is an anecdote, not a sweep — τ stays provisional until TICKET-8 measures it over the 40-question
set. But the distribution is encouragingly close in shape to the local build's under `nomic-embed-text`, and the
near-miss landing between the answerable and off-corpus bands is exactly the middle band the two-stage gate exists for.

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

**The live run found two production bugs that no fake-driven test could have.**

*The failover secondary was dead.* `gemini-2.0-flash` returned 404 "no longer available". The replacement I
picked, `gemini-2.5-flash`, returned 404 "no longer available **to new users**" — listed by the models API,
which reports availability generally rather than per-key entitlement. Two pinned defaults retired inside one
session, which flipped the pinning argument: ADR-004 exists to survive provider change *without touching code*,
and a pinned model that retires requires exactly that. The secondary is now `gemini-flash-latest`, with the
`live` sentinel test as the guard against the alias drifting somewhere that behaves differently.

*An invalid API key did not fail over.* TICKET-3 classified every non-429 4xx as a bad request, reasoning that
"the secondary would reject it identically". True of a malformed request and false of a credential — the
secondary is a different vendor holding a different key. So a revoked key raised `ProviderProtocolError` and
skipped the fallback, against **PRD success criterion 4**, which names this case outright. 401 and 403 now map
to `ProviderUnavailable` in both adapters, pinned by parametrised tests. The fake tests could not have caught
it: they validated my classification against itself.

After the fix, observed live: `primary healthy → served_by=llama-3.3-70b-versatile`; `primary key revoked →
served_by=gemini-flash-latest`, same answer. Criterion 4 demonstrated rather than asserted.

**Nothing else needed a second attempt.** The 21 integration tests passed on their first run, including the
anchor check that the plan flagged as the likely off-by-one.

## Outstanding

None.

## Ready for the next step

All work committed. CI green.

Next: `piv-create-pr`. TICKET-5 (API shell) unblocks — it has a real manifest to check at startup and a real
index to serve. TICKET-8 has real vectors to sweep τ against.

**Two follow-ups worth tracking**, both surfaced by the live run rather than by any test:

1. **ADR-004 action item 4 (TICKET-6's scheduled health check) is now evidence-backed, not precautionary.**
   The secondary generator was dead — a pinned `gemini-2.0-flash` that had been retired — and nothing would
   have discovered it until the failover was actually needed. The ADR anticipated this: "if the secondary is
   never exercised in ninety days, test it deliberately rather than assuming it works."
2. **Nothing loads `.env` automatically.** `load_config` reads `os.environ` only, so the documented commands
   need the values exported. Worth either a loader or a clearer README line before TICKET-10.
