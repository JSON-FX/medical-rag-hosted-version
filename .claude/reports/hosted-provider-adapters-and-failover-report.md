# Implementation Report — TICKET-3: Hosted provider adapters and the failover chain

**Plan**: `.claude/plans/hosted-provider-adapters-and-failover.md`
**Branch**: `feature/hosted-provider-adapters-and-failover` (stacked on `feature/postgres-dense-and-lexical-stores`)
**Status**: COMPLETE

## Summary

The last two ports have real implementations. `GeminiEmbedder` produces 768-dimensional unit vectors from
`gemini-embedding-001`, `GroqGenerator` and `GeminiGenerator` stream answers, and `FailoverGenerator` retries
the primary once before falling through to a different vendor — reporting which one actually served.

`AllProvidersUnavailable` finally has a producer, and the `hosted` profile no longer wires fakes for
inference. Everything below the API shell is now production code.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | Provider extras, `live` marker, config | `pyproject.toml`, `src/rag_core/config.py`, `.env.example` (UPDATE) |
| 2 | `TokenStream` + port amendment | `src/rag_core/{contracts,ports}.py`, `src/rag_adapters/fakes.py` (UPDATE) |
| 3 | Backoff helper | `src/rag_adapters/_backoff.py` (CREATE) |
| 4 | `GeminiEmbedder` | `src/rag_adapters/gemini.py` (CREATE) |
| 5 | `GeminiGenerator` + message translation | `src/rag_adapters/gemini.py` |
| 6 | `GroqGenerator` | `src/rag_adapters/groq.py` (CREATE) |
| 7 | `FailoverGenerator` | `src/rag_adapters/failover.py` (CREATE) |
| 8 | Hosted profile wiring | `src/rag_adapters/profile.py` (UPDATE), `src/rag_adapters/_client.py` (CREATE) |
| 9 | Contract registration + live tests | `tests/contract/{providers,test_live_providers}.py` (CREATE), `test_port_contract.py` (UPDATE) |
| 10 | Docs | `docs/ARCHITECTURE.md`, `docs/PRD.md`, `README.md` (UPDATE) |

## Tests added

**303 tests: 257 without keys or a database, 38 Postgres, 8 live. All passing or skipping as designed.**

| File | Tests | Note |
|---|---|---|
| `tests/unit/test_gemini.py` | 23 | Task types, renormalisation, guards, batching, backoff, message translation |
| `tests/unit/test_failover.py` | 16 | Every failure shape, including two concurrency tests |
| `tests/unit/test_groq.py` | 11 | Streaming, `None` deltas, pass-through, error mapping, module shadowing |
| `tests/unit/test_backoff.py` | 7 | Retry policy, delay doubling, exclusions |
| `tests/contract/test_port_contract.py` | 29 → **40** | Two embedders and three generators now run the same assertions |
| `tests/contract/test_live_providers.py` | 8 | `live`-marked; skip readably without keys |
| `tests/unit/test_config.py` | +3 | Model/dimension pinned together, distinct vendors, keys not read behind config's back |

## Validation results

| Check | Result |
|---|---|
| Default suite (no keys, no database) | **257 passed**, 46 deselected, 0.71s |
| Postgres suite (TICKET-2 regression check) | **38 passed**, no regressions |
| Live suite without keys | 8 skipped with a readable reason |
| Types | `mypy --strict` clean, 21 source files |
| Lint + format | `ruff` clean, 43 files |
| Purity | `rag_core` pulls in none of `google`, `groq`, `asyncpg`, `pgvector`; `dependencies = []` still true |
| Module shadowing | `rag_adapters.groq` → SDK resolves to `groq` |
| Failover, observed | primary up → `groq/llama-3.3-70b`; primary down → `gemini/2.0-flash`, same text |

All six acceptance criteria met.

## Deviations from the plan

**1. Lazy SDK clients — `_client.py` was not in the plan.** The plan's Task 8 required that building a hosted
profile succeed without an API key. It did not, because `genai.Client(api_key="")` raises immediately, which
broke three existing profile tests. `LazyClient` defers construction to first use. Groq's constructor accepts
an empty key, but it uses the same seam for symmetry.

**2. SDK errors are mapped to the provider family at the call site, not filtered by SDK type.** The plan had
`_RETRYABLE = (ServerError,)`. That is wrong: **a 429 is a `ClientError`, not a `ServerError`**, so the
embedder would not have retried the one failure it exists to survive. Errors are now mapped to
`ProviderUnavailable` / `ProviderProtocolError` at the boundary and the retry rule is expressed over our own
types.

**3. `FailoverGenerator` uses a per-call closure, not instance state.** My first draft stored the in-flight
`TokenStream` on `self._current` — the exact race `TokenStream` exists to prevent, since one generator instance
serves concurrent requests and an async generator's body does not run until first `__anext__`. Two concurrent
calls would have clobbered each other. Two concurrency tests now cover it.

**4. A `sleep` seam on `GeminiEmbedder`.** Not in the plan. The retry tests were sleeping through real backoff
delays — 7.5s for one file. Injecting the sleeper mirrors the existing client seam and brought it to 0.66s.

**5. `except GroqError`, not `except Exception`.** The plan's anti-patterns list forbids bare `Exception`, and
catching it here would report a programming error in the adapter as a provider outage and quietly fail over to
hide it.

**6. Provider registries live in `tests/contract/providers.py`.** The plan said "add to `EMBEDDERS` and
`GENERATORS`", which are module-level in the test file; the real adapters need SDK doubles, so both registries
moved next to those doubles, matching how `STORE_PAIRS` lives in `stores.py`. **TICKET-1's "one line per
adapter" claim holds for providers** — it was only stores that needed the rebuild.

**7. `test_the_embedding_model_id_is_an_obvious_placeholder` was replaced.** It asserted `REPLACE_ME` was still
in the model id, which this ticket resolves. Replaced by a test pinning the model and dimension *together*,
since they are one decision.

## Issues encountered

**A silent no-op edit nearly shipped a false claim.** My first attempt to wire the registries into
`test_port_contract.py` did not match (ruff had reformatted the import block), so the file kept its local
one-element registries. The suite went green at 29 tests — unchanged — and only a collected-ids check caught
it. Had I trusted the green, this report would have claimed three generators were under test while one was.

**8.76s of test time was real sleeping.** Found via `--durations`, fixed by deviation 4.

**Nothing about the adapters themselves needed a second attempt.** The three highest-risk items the plan named
— the Gemini message translation, the renormalisation, and the `task_type` asymmetry — were specific enough to
be built right the first time.

## Ready for the next step

All changes committed. CI needs a push.

Next: `piv-commit`, then `piv-create-pr` (base `feature/postgres-dense-and-lexical-stores`), then
`piv-review-pr`.

TICKET-4 (ingestion) and TICKET-5 (API shell) both unblock after this, and they are parallel with each other.
Before either, someone with keys should run `uv run pytest -m live` — it is the only check that the vendors
behave as documented, particularly that `output_dimensionality=768` really returns 768 values.
