# Implementation Report — TICKET-1: Repo scaffold and `rag_core` port

**Plan**: `.claude/plans/port-rag-core-and-scaffold-repo.md`
**Branch**: `feature/port-rag-core-and-scaffold-repo`
**Repository**: https://github.com/JSON-FX/medical-rag-hosted-version (public)
**Status**: COMPLETE

## Summary

Stood up the hosted repository and ported the retrieval pipeline out of the local Django build into a
framework-free `rag_core` package sitting behind four async provider ports, with fake adapters as the second
implementation of each. The pure modules — chunking, fusion, gate, prompts, sentinel — are copied unchanged
and carry their original test suites, which under the standalone-repo decision are the parity harness: three
of the ported test files are byte-identical to source apart from an import prefix, and that identity is
verified by `diff` rather than asserted.

Three things improved in the port, all consequences of both retrieval legs now living in one store rather
than choices made here: hydration became a dict lookup instead of a database query, the corpus-empty count
moved off the hot path, and the purity test now enforces all of "no web framework and no provider SDKs"
instead of grepping for `django` alone.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | Repo scaffold and tooling | `pyproject.toml`, `.gitignore`, `.env.example` (CREATE) |
| 2 | GitHub repo + CI | `.github/workflows/test.yml` (CREATE); repo created public, `main` pushed |
| 3 | Verbatim port of three pure modules + tests | `src/rag_core/{chunking,fusion,gate}.py`, `tests/unit/test_{chunking,fusion,gate}.py` (CREATE) |
| 4 | Provider-agnostic error family | `src/rag_core/errors.py` (CREATE) |
| 5 | Sentinel filter split out of the source's `generation.py` | `src/rag_core/sentinel.py`, `tests/unit/test_sentinel.py` (CREATE) |
| 6 | `prompts.py` with rewritten copy | `src/rag_core/prompts.py`, `tests/unit/test_prompts.py` (CREATE) |
| 7 | Data model and wire contract | `src/rag_core/contracts.py`, `tests/unit/test_contracts.py` (CREATE) |
| 8 | Config | `src/rag_core/config.py`, `tests/unit/test_config.py` (CREATE) |
| 9 | The four ports | `src/rag_core/ports.py` (CREATE) |
| 10 | Fake adapters | `src/rag_adapters/fakes.py` (CREATE) |
| 11 | Pipeline | `src/rag_core/pipeline.py`, `tests/unit/test_pipeline.py`, `tests/conftest.py` (CREATE) |
| 12 | Composition root | `src/rag_adapters/profile.py`, `tests/unit/test_profile.py` (CREATE) |
| 13 | Shared port contract suite | `tests/contract/test_port_contract.py` (CREATE) |
| 14 | Purity test | `tests/unit/test_core_purity.py` (CREATE) |
| 15 | Doc reconciliation | `docs/ARCHITECTURE.md`, `docs/PRD.md` (UPDATE), `README.md` (CREATE) |

2,657 lines across `src/` and `tests/`.

## Tests added

**155 tests, all passing.**

| File | Tests | Provenance |
|---|---|---|
| `tests/unit/test_gate.py` | 17 | Ported — **byte-identical to source** |
| `tests/unit/test_fusion.py` | 8 | Ported — **byte-identical to source** |
| `tests/unit/test_chunking.py` | 10 | Ported — **byte-identical to source** |
| `tests/unit/test_sentinel.py` | 15 | Ported (subset of source `test_generation.py`) |
| `tests/unit/test_prompts.py` | 18 | Ported, 2 assertions replaced + 1 new (copy rewrite) |
| `tests/unit/test_contracts.py` | 23 | New |
| `tests/unit/test_pipeline.py` | 17 | New |
| `tests/unit/test_config.py` | 13 | New (source shape, new assertions) |
| `tests/unit/test_core_purity.py` | 6 | New (replaces the source's single `django` check) |
| `tests/unit/test_profile.py` | 5 | New |
| `tests/contract/test_port_contract.py` | 23 | New — parametrised, one line per future adapter |

Notable coverage: chunk ids splitting on the *last* underscore (a slug like `some_drug_name_3`); score
direction for both stores; `count()` staying off the hot path; concurrent retrieval legs; hydration
asserting rather than silently dropping; NaN surviving JSON encoding; and both gate conditions reported
independently.

## Validation results

| Check | Command | Result |
|---|---|---|
| Format | `uv run ruff format --check .` | PASS — 18 files formatted, 10 vendored files excluded |
| Lint | `uv run ruff check .` | PASS — all checks passed |
| Types | `uv run mypy` | PASS — no issues in 14 source files |
| Tests | `uv run pytest` | PASS — 155 passed in 0.27s |
| Clean env | `rm -rf .venv && uv sync && uv run pytest` | PASS |
| Purity, negative | inject `import sqlite3` into `gate.py` | FAILS correctly, naming `gate.py: import sqlite3` |
| Port faithfulness | `diff` vs source for 3 test files | IDENTICAL |
| Copy | no `upload` in any decline string, `SYSTEM_TEMPLATE`, or `FALLBACK_DECLINE` | PASS |
| AC #4 round trip | retrieve → gate → prompt → sentinel against fakes | PASS — **0.8 ms**, budget 1 s |
| Secrets | scan of staged diff | clean |
| CI | `gh run list` | **green** on `feature/port-rag-core-and-scaffold-repo` |

All five acceptance criteria met.

`rag_core` imports nothing outside the standard library, and this is now enforced three ways: per-module by
the purity test, at the package level by `dependencies = []`, and incidentally by the fact that an SDK import
cannot even resolve in the environment.

## Deviations from the plan

**1. mypy `--strict` carves out one axis for two vendored files.** `gate.py` and `prompts.py` use bare
`dict` annotations, which `--strict` rejects. Widening them to `dict[str, Any]` is a null change at runtime
but a real change to the diff that the parity claim depends on. Rather than weaken the port or drop mypy, a
per-module override disables `disallow_any_generics` for exactly those two modules; every other strict check
still applies to them. Recorded in `pyproject.toml` with the reasoning and a note to delete it when a ticket
modifies either module anyway.

**2. Vendored files excluded from `ruff format` and three lint rules.** The plan anticipated the
`line-length` problem and set 100 to prevent it, but the formatter still wanted to rewrite the ported files,
and `ruff check --fix` did re-sort an import in `test_chunking.py` before the ignores were in place (caught
by the `diff` check and reverted). The tooling is now configured to leave the ten vendored files alone
rather than relying on discipline — which is strictly better, since the plan itself flagged
"boredom-driven drift" as the largest unlisted risk.

**3. `extend-exclude = ["docs"]` added to ruff.** Not in the plan. `ruff format` reformats Python code
blocks inside Markdown, and it silently rewrote the port definitions in `ARCHITECTURE.md` — a doc whose code
blocks are quoted specification. This was the cause of the CI failure on the initial `main` commit.

**4. CI triggers on every branch, not just `main` and PRs.** Not in the plan. As written, a feature branch
with no PR yet ran no checks at all, so the first signal that the tree was broken would have arrived at PR
time. Added as its own commit.

**5. `split_chunk_id` rejects an empty document id.** The plan specified splitting on the last underscore
and validating the ordinal. A parametrised test caught that `"_3"` also passes both of those and yields an
empty document id. Implementation fixed, not the test.

**6. `config.py` written complete rather than as a stub.** The plan had Task 3 create a two-class stub and
Task 8 finish it. Writing it once was simpler with no downside.

**7. Sentinel test count is 15, not 17.** The plan estimated 17 from the source line ranges; the actual
count of portable sentinel tests is 15. No tests were dropped beyond the four Ollama-transport ones the plan
already excluded.

## Issues encountered

**The CI run on `main` is red.** It is the initial scaffold commit, which predates the `docs` exclusion in
deviation 3. The fix is on this branch and CI is green here; merging the PR turns `main` green. Worth
knowing before anyone reads the badge.

**`docs/medical-rag-system-flow.html` is referenced by `ARCHITECTURE.md` §2 but does not exist** in this
repository. Not in scope for this ticket and left alone — flagging it because the link is now public.

**Two test bugs, both mine, both caught by the tests themselves.** `test_a_dense_only_hit_is_still_delivered`
asserted every delivered chunk was a metformin one, which is false when `per_leg=10` over a 3-chunk corpus
returns everything; the meaningful assertion is that metformin ranks first. And the first draft of the
hydration-assertion test tried to provoke the failure through `retrieve()`, which cannot reach that state by
construction — it now tests `_hydrate` directly, which is the honest way to test an invariant guard.

## Ready for the next step

All changes committed and pushed. CI green on the branch.

Next: `piv-create-pr` to open the PR (this report fills the body), then `piv-review-pr`.

The contract that unblocks the rest of the epic is in place — TICKET-2 (Postgres stores), TICKET-3 (Gemini,
Groq, failover), TICKET-5 (FastAPI shell) and TICKET-7 (frontend) can now proceed in parallel worktrees, as
the ticket breakdown's Wave 2 describes.
