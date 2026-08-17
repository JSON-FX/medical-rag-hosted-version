# Implementation Report — TICKET-7: Frontend, question box, streaming answer, telemetry strip

**Plan**: `.claude/plans/frontend-question-box-and-telemetry-strip.md`
**Branch**: `feature/frontend-question-box-and-telemetry-strip`
**Status**: COMPLETE

## Summary

The part a visitor sees: one question box, an answer that streams with citations that resolve to a real page,
a refusal that reads as a decision, and the telemetry strip that shows the gate deciding. Built in `web/` as a
Next.js app that fetches `/api/chat` same-origin, because the frontend and the API deploy as two services in
one Vercel project.

The plan named its own weakest point — no browser-level verification — so the ten manual checks were performed
by driving a real browser rather than by eye. **That found three defects no unit test in this plan would have
caught**, two of them in code the plan expected to port unchanged.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | D3 `local` profile + D4 one-shape `done` | `rag_adapters/{profile,fakes}.py`, `rag_api/streaming.py` (UPDATE) |
| 2 | The `web/` scaffold | `web/{package.json,next.config.ts,tsconfig.json,vitest.config.mts,eslint.config.mjs,postcss.config.mjs}` (CREATE) |
| 3 | Ported parser | `web/lib/ndjson.ts`, `ndjson.test.ts` (CREATE, byte-identical) |
| 4 | The frame contract | `web/lib/types.ts` (CREATE) |
| 5 | API client | `web/lib/api.ts` (CREATE) |
| 6 | Reducer | `web/lib/chatReducer.ts` (CREATE) |
| 7 | Citations | `web/components/AnswerText.tsx` (CREATE) |
| 8 | The page | `web/app/{page,layout,icon.svg,globals.css}`, `components/{QuestionBox,TurnView,SourceList}.tsx` (CREATE) |
| 9 | The strip | `web/components/TelemetryStrip.tsx` (CREATE) |
| 10 | Refusal, disclaimer, a11y | `web/components/{ExampleQuestions,Disclaimer}.tsx`, `web/lib/copy.ts` (CREATE) |
| 11 | CI, `vercel.json`, docs | `.github/workflows/test.yml`, `vercel.json`, `README.md`, `docs/{ARCHITECTURE,PRD}.md` |

## Tests added

**Web: 83 tests, 5 files.** **Python: +9 (7 default, 2 Postgres).**

| File | Tests | Note |
|---|---|---|
| `web/lib/ndjson.test.ts` | 8 | **Ported byte-identical**, passed unedited |
| `web/lib/chatReducer.test.ts` | 30 | Two-half telemetry merge, `meta` never arriving, sources-before-tokens, transcript accumulation |
| `web/lib/api.test.ts` | 13 | 429/400/503 → synthetic error frame, `Retry-After` fallback, abort yields nothing |
| `web/components/AnswerText.test.tsx` | 15 | `sources[n-1]`, out-of-range logs, **grouped `[1, 2]`**, partial marker while streaming |
| `web/components/TelemetryStrip.test.tsx` | 17 | Which condition *decided*, meta-only and done-only strips, nulls as dashes |
| `tests/integration/test_chat_api.py` | +4 | All five `done` paths share one key set; CORS by profile |
| `tests/unit/test_profile.py` | +3 | `local` is real storage with fake inference, and is not `fake` |
| `tests/integration/test_ingestion.py` | +2 | Switching embedder re-embeds; same embedder still costs nothing |

The `done`-shape test asserts each of the five paths was *actually exercised* before comparing keys, and the
embedder-switch test was confirmed to fail without its fix (`assert 71 == 0`) — both because a test that
passes for the wrong reason is worse than no test.

## Validation results

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` | clean, 75 files |
| `mypy --strict` | clean, 35 source files |
| `pytest` (no database) | **415 passed** |
| `pytest -m postgres` | **67 passed** |
| `tsc --noEmit` / `eslint` | clean |
| `vitest` | **83 passed**, 5 files |
| `next build` | compiled, 4 static pages |

### The ten manual checks (Level 4), performed in a real browser against real providers

| # | Check | Result |
|---|---|---|
| 1 | Progressive rendering | **Measured**: answer text grew 46 → 171 chars over 14 distinct increments across 1.3 s |
| 2 | Citations resolve | Clicking `[1]` moves focus to source 1 and highlights it; title and page shown |
| 3 | Console clean | 0 errors, 0 warnings on a full load + answer + refusal cycle |
| 4 | The refusal | "Outside this corpus", server copy verbatim, rationale line, no sources, `tokens: 0`, `served by: —` |
| 5 | Which condition failed | `similarity: not met ← decided this` beside `lexical support: met` — the two genuinely disagreeing |
| 6 | Keyboard | All 5 controls + every citation reachable, each carrying `focus-visible:ring` |
| 7 | Reduced motion | Rule verified in the compiled CSS: `.streaming-caret { animation: … none }` |
| 8 | Screen reader | `aria-live="polite"` + `aria-busy` toggling verified structurally. **VoiceOver itself was not run** |
| 9 | Rate limit | Renders "Rate limited" + the plain-language wait, no "429"; box re-enables. See deviation 8 |
| 10 | 375 px | No horizontal scroll, `scrollWidth == 375`, strip reflows to two columns |

Live behaviour of all four example questions against Groq + Gemini:

```
metformin starting dose      proceed=True   sim=0.762  lex=True    4 sources  llama-3.3-70b-versatile
atenolol contraindicated     proceed=True   sim=0.771  lex=True    4 sources  llama-3.3-70b-versatile
atenolol in pregnancy        proceed=False  sim=0.691  lex=True    off_domain  no model called
capital of France            proceed=False  sim=0.481  lex=False   off_domain  no model called
```

Both refusing examples genuinely refuse, so both labels are honest.

## Deviations from the plan

**1. The manual checks were driven through a real browser (Playwright MCP), not by eye.** The plan's
assumption #4 accepted that AC #1 and AC #5 would rest on unverifiable manual checks and called it "the
weakest part of the plan". No Playwright dependency, e2e suite or CI job was added — the repo is unchanged —
but the checks were executed and their measurements recorded above rather than asserted. **This is what found
deviations 2, 3 and 4.**

**2. `AnswerText`'s marker regex was widened to `[1, 2]`.** Llama 3.3 answers a two-source question with a
single grouped marker, which `\[(\d+)\]` does not match at all — so a fully-cited answer rendered **zero**
citations and AC #2 failed against the real model while every ported unit test passed. Each number in a group
now resolves independently, so `[1, 9]` keeps the working citation. The prompt was *not* changed:
`rag_core/prompts.py` is a verbatim port under ADR-001 shared with the eval harness, and editing it to suit a
UI would break the parity that makes the port defensible.

**3. The strip names which condition *decided* a refusal, rather than colouring both pass/fail.** The gate
consults lexical support only in the middle band (`gate.py:60`), so an answered turn can legitimately show it
unmet — and a bare "not met" beside "answered" reads as the system overriding its own gate. It now reads "not
required here" when the gate proceeded, and marks the deciding condition on a refusal. This is a stronger
reading of AC #4, not a weaker one: it says which condition *caused* the outcome, which is what ADR-003
actually wanted.

**4. The ingest job now re-embeds when the embedder changes.** Resume compares chunk *content*, which cannot
see which model produced a vector, so ingesting under a second profile skipped all 71 chunks and then rewrote
the manifest to name the new embedder — leaving fake vectors labelled as Gemini's and defeating the startup
manifest check exactly where it matters. Pre-existing since TICKET-4, latent until D3 made switching profiles
routine. Out of TICKET-7's stated scope, but shipping the thing that makes it reachable without the fix was
not defensible.

**5. `FakeGenerator` gained `delay_s`, defaulting to `0.0`.** The `local` profile sets 0.05 s. Without it the
whole answer arrives in one event-loop tick, and a frontend built against that cannot tell progressive
rendering from a spinner — the one thing the profile exists to check. Default zero, so no existing test slows.

**6. CORS is narrowed to non-`hosted` profiles rather than deleted.** Same-origin means production needs none;
a direct `curl` or tool pointed at port 8000 in development is the one genuinely cross-origin case. Two tests
pin both halves.

**7. `vitest` runs `jsdom` with `@testing-library/react`.** The source repo's config is node-only with
components explicitly out of scope. Two acceptance criteria here are about what a component renders, and
`AnswerText`'s mapping is the single assumption the citation feature rests on.

**8. Manual check #9 injected the 429 at the browser boundary.** `build_limiter` returns `NoLimiter` without
Upstash credentials — TICKET-6's deliberate design — so a local API cannot emit a 429 no matter what
`RATE_LIMIT_PER_MINUTE` is set to, which the plan's step assumed it could. The exact body and `Retry-After`
that `errors.py:rate_limited(90)` produces were served to the page instead. The API half already has 18
integration tests; what was unverified was the UI, and that is what was verified.

**9. `vercel.json` uses the schema the live docs specify, not the plan's sketch.** `services` is an object
keyed by name and a rewrite's `destination` is an object `{"service": …}` — checked against
`vercel.com/docs/services/routing` rather than written from memory, since TICKET-10 builds on this file.

**10. `ndjson.test.ts` is byte-identical and therefore still says "Django".** One comment names the source
repository's backend. Editing it would break the `diff` that makes the port defensible, which is ADR-001's
whole convention; both ported files are excluded from lint for the same reason. Recorded rather than silently
fixed.

## Issues encountered

**`FakeEmbedder` makes the stage-1 gate inert, not strict.** My first `local` docstring claimed a question
naming no drug scores 0.0 and refuses. Measured in the browser: "What is the capital of France?" is *answered*
under `local`, because chunks that name no drug land on the same unrelated axis as the query. Corrected in the
docstring and the README — `local` exercises transport, citations and the strip, and does not exercise the
gate at all.

**The Postgres suite leaves its own manifest behind.** Running `-m postgres` ingests with test embedders, so a
`hosted` API started afterwards correctly refuses to serve until re-ingested. Not a bug — the manifest check
doing its job — but worth knowing before someone reads that 503 as a regression.

**Gemini's free tier stayed healthy this session**, unlike TICKET-6. Groq served every generation; the
failover secondary was not exercised here, which is what `probe.py` is for.

## Still outstanding — needs the repository owner

Unchanged from TICKET-6: the weekly `provider-check` workflow needs `GEMINI_API_KEY` and `GROQ_API_KEY` as
**repository secrets**. Only you can add them.

## Ready for the next step

All work committed across three commits. CI needs a push; the new `web` job has not run on a runner yet.

Next: `piv-create-pr`. TICKET-8 (τ sweep) and TICKET-9 (retrieval parity) are unblocked and independent of
this; TICKET-10 activates the `vercel.json` written here.
