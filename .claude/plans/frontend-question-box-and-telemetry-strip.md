# Feature: TICKET-7 — Frontend: question box, streaming answer, telemetry strip

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

**Source repository for ported logic:** `/Users/jsonse/Documents/development/interview/medical-rag/frontend/` — referred to as **SRC**. A different repository; read from it, never write to it.

---

## Feature Description

The part a visitor actually sees. One question box, a streaming answer with inline citations that resolve to
real pages, and a telemetry strip that shows the gate's reasoning.

The strip is the point. Architecture §3: *"The telemetry strip is a product feature, not debug output; it is
the part that shows engineering rather than describing it."* Six tickets have produced a two-stage confidence
gate, a failover chain and a manifest check that nobody can see. This makes them visible.

The second thing that has to land is the refusal. PRD G5 wants it discoverable in under a minute by someone
who was not told about it, and PRD success criterion 2 wants it to *"read as deliberate, not broken"* — which
is a design problem more than an engineering one.

## User Story

As a technical evaluator with ten minutes, probably on a phone
I want to ask a clinical question and watch a cited answer stream back — and to easily find the case where it refuses
So that I can tell in under a minute whether the engineering behind it is serious.

## Problem Statement

The API works and nothing can reach it but `curl`. PRD success criterion 1 is *"a stranger with the URL
reaches a cited answer without instructions"*, and there is currently no URL and no interface.

Everything that makes this project an argument rather than a script — the gate's two conditions, which
provider served, the fact that a refusal is a decision rather than a failure — exists only as JSON on a wire.

## Solution Statement

A Next.js app in `web/`, deployed alongside the API as a **Vercel Service** so both share one project, one
domain and one set of environment variables. The frontend calls `/api/chat` same-origin; there is no CORS and
no API base URL to configure.

The hard-won logic is ported from the local build; the chrome is not.

Four decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **Port the logic, build the page fresh.** `ndjson.ts`, `chatReducer.ts`, `AnswerText.tsx` and their tests come across; `AppShell`, `StatusBar`, `EvidencePanel`, `ChatWindow` do not. | The parser's comments record two real bugs — frames split across chunk boundaries, multi-byte characters straddling reads — and its tests pin them. The chrome exists to manage uploads this profile does not have, and Architecture §3 asks for "a single question box, a streaming answer pane with inline citations, and a telemetry strip". |
| D2 | **Keep a transcript of the session.** Each question and answer appends. | Lets an evaluator put a grounded answer and a refusal side by side, which is what makes the refusal read as deliberate. Still single-turn — no history is sent to the model, so nothing about the pipeline changes. Purely a rendering choice. |
| D3 | **Add a `local` profile** pairing Postgres stores with `FakeEmbedder`/`FakeGenerator`. | Third ticket to hit this gap. Frontend work needs a running API with real chunks and citations; burning Gemini quota on every page reload to get it is the wrong trade. ~15 lines through the existing `register_profile` seam. |
| D4 | **Fix the API so `done` has one shape.** | The retrieval-failure path emits a bare `{"type":"done","truncated":true}` while every other `done` carries `telemetry`, `was_declined` and `decline_reason`. Two shapes for one frame type is a contract bug every future client inherits. |

## Out of Scope / Non-Goals

- **Not included: deployment.** No Vercel project, no domain, no production environment variables. TICKET-10.
  This ticket writes the `vercel.json` service config so the layout is right, and TICKET-10 activates it.
- **Not included: uploads, a document list, or any admin surface.** PRD §4 non-goal — the corpus is fixed.
  `DocumentUploader.tsx`, `DocumentTable.tsx` and `app/documents/page.tsx` do **not** port.
- **Not included: multi-turn.** D2 renders a transcript; the request still sends `{"question": ...}` and
  nothing else. PRD §4 non-goal.
- **Not included: accounts, sessions or persistence.** Reloading the page loses the transcript, and that is
  correct — there is nothing to persist and no one to persist it for.
- **Not included: the published evaluation numbers.** The README placeholder stays until TICKET-8/9 measure
  them. Do not invent numbers for the UI.
- **Not included: end-to-end browser tests.** Playwright would be the honest way to prove progressive
  rendering in a real browser; the frontend logic is covered by vitest and the visual and accessibility
  criteria are verified manually. Flagged in Open Questions rather than silently skipped.
- **Not changing:** `rag_core`, the adapters, the schema, or the API's behaviour beyond D4's frame fix and
  D3's profile registration.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium-High — the logic is ported and small, but this is the repo's first frontend, and the refusal and accessibility criteria are design work rather than code.
**Primary Systems Affected**: `web/` (new), `src/rag_api/streaming.py` (D4), `src/rag_adapters/profile.py` (D3), CI
**Dependencies**: Next.js 16, React 19, Tailwind 4, vitest

## Related Work

**Implements**: TICKET-7 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`

**Back-references**:

- `.claude/plans/fastapi-shell-streaming-and-telemetry.md` — the frame contract this renders, and the telemetry split (`meta` before generation, `done` after) that the strip depends on.
- `.claude/plans/rate-limiting-health-and-failure-surface.md` — the 429 shape and `Retry-After` this must handle.
- `.claude/plans/offline-ingestion-job.md` — the corpus whose absent axes supply the refusal examples.

**Forward-references**:

- TICKET-10 — activates the `vercel.json` service config this ticket writes

**Sequencing:** TICKET-6 is on **PR #6, not yet merged**, and this needs its 429 shape. Branch from
`feature/rate-limiting-health-and-failure-surface`, or wait for the merge.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

**This repository — the contract being rendered:**

- `src/rag_api/streaming.py` — Why: **every frame this app parses**. Read all of it. Note the exact order
  (`meta` → `token`* → `sources` → `done`), that `sources` sits immediately *before* the first token, and the
  retrieval-failure path at lines 66-69 that D4 fixes.
- `src/rag_api/telemetry.py` — Why: the two payload shapes. `meta_payload` gives `gate` (with
  `similarity_ok` and `lexical_support` **separately**), `latency.retrieval_ms`, `fused_scores`.
  `done_payload` gives `latency.ttft_ms`, `total_tokens`, `provider`, `truncated`. The strip fills in two
  stages and must render a partial one.
- `src/rag_core/contracts.py` — Why: `FRAME_TYPES`, and `Telemetry.as_dict()`'s nesting, which is what
  `types.ts` mirrors. Note `_jsonable` turns non-finite floats into `null`.
- `src/rag_api/errors.py` — Why: every `code` the UI may receive: `rate_limited`, `index_unavailable`,
  `all_providers_unavailable`, `provider_unavailable`, `provider_error`, `invalid_request`, `internal_error`.
- `src/rag_core/prompts.py` — Why: `DECLINE_COPY` — the four refusal messages, server-authored. The UI
  renders them; it must not write its own.
- `src/rag_adapters/profile.py` — Why: `_REGISTRY` and `register_profile`, the seam D3 uses.
- `src/ingest/fixtures/manifest.json` — Why: each drug's `verified_absent` axes. The refusal examples come
  from here — a near-miss question is only a fair example if the corpus genuinely lacks the answer.

**Source repository (port from, do not modify):**

- `SRC/lib/ndjson.ts` (50 lines, **read in full, port verbatim**) — Why: its two comments record real bugs.
  Chunks do not respect line boundaries, so the buffer keeps whatever follows the last newline; and
  `TextDecoder` runs in streaming mode because a multi-byte character can straddle a chunk boundary. The
  `finally` block cancels the reader so a consumer that `break`s does not leave the connection held.
- `SRC/lib/ndjson.test.ts` (116 lines) — Why: **ports unmodified**. It is the parity harness for the parser.
- `SRC/lib/chatReducer.ts` (166 lines) — Why: the state machine. Same frame kinds, different payloads.
  `isTerminalFrame`, `patchLast` and the turn-kind transitions all port; the payload fields change.
- `SRC/lib/chatReducer.test.ts` (270 lines) — Why: the assertions port with their payloads updated.
- `SRC/components/AnswerText.tsx` (68 lines, **read in full**) — Why: the citation renderer, and the clearest
  statement of AC #2's logic anywhere: *"`format_context` numbers chunks from 1 in the same order
  `_sources_payload` serialises them, so `[n]` is always `sources[n-1]`. That mapping is the only reason this
  component can exist."* Also already carries `focus-visible:ring` styling, which AC #5 needs.
- `SRC/lib/copy.ts` (43 lines) — Why: user-facing strings kept out of components. The strings themselves
  mostly do not port (they mention uploading), but the pattern does.
- `SRC/app/globals.css`, `SRC/components/ui/button.tsx` — Why: the Tailwind 4 token setup and the
  `class-variance-authority` pattern, if you want them. Optional.

### New Files to Create

```
web/
├── package.json, next.config.ts, tsconfig.json, postcss.config.mjs, vitest.config.ts
├── app/
│   ├── layout.tsx          # disclaimer, fonts, globals
│   ├── page.tsx            # the whole app
│   └── globals.css
├── components/
│   ├── QuestionBox.tsx
│   ├── Transcript.tsx      # the list of turns
│   ├── TurnView.tsx        # one question + answer/refusal + sources + strip
│   ├── AnswerText.tsx      # ported
│   ├── SourceList.tsx
│   ├── TelemetryStrip.tsx
│   ├── ExampleQuestions.tsx
│   └── Disclaimer.tsx
└── lib/
    ├── ndjson.ts / ndjson.test.ts        # ported verbatim
    ├── types.ts                          # rewritten for this contract
    ├── chatReducer.ts / chatReducer.test.ts
    ├── copy.ts
    └── api.ts
vercel.json
```

Modified: `src/rag_api/streaming.py` (D4), `src/rag_adapters/profile.py` (D3),
`tests/integration/test_chat_api.py`, `.github/workflows/test.yml`, `README.md`, `docs/ARCHITECTURE.md`,
`docs/PRD.md`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [Vercel — Services](https://vercel.com/docs/services)
  - Specific: *"deploy multiple backends and frontends within a single Vercel project ... such as Next.js,
    FastAPI, or Go, to exist in the same repository and share routing, environment variables, and a unique
    domain"*
  - Why: this is why there is **no CORS and no API base URL**. The frontend fetches `/api/chat` relatively.
- [Vercel — Services routing](https://vercel.com/docs/services/routing)
  - Specific: the `services` + `rewrites` block, with `root` and `entrypoint` per service
  - Why: Task 10 writes exactly this shape so TICKET-10 only has to activate it.
- [MDN — `aria-live`](https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-live)
  - Specific: `polite` vs `assertive`, and that content changes are announced
  - Why: a streaming answer is invisible to a screen reader without it. AC #5 is not satisfied by focus rings alone.
- [MDN — `prefers-reduced-motion`](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion)
  - Why: PRD §7 requires it. The streaming cursor is the only animation, and it is exactly the kind that
    triggers discomfort.
- [Next.js — rewrites](https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites)
  - Why: local development runs Next on 3000 and the API on 8000. A rewrite proxies `/api/*` so the code path
    is identical in development and production — no `NEXT_PUBLIC_API_URL` branching.
- [MDN — `fetch` streaming with `ReadableStream`](https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams)
  - Why: `response.body` is what `readFrames` consumes. Note it is `null` for a non-streaming error response —
    Task 5's error path depends on that.

### Patterns to Follow

**Comments record the bug, not the behaviour.** From `SRC/lib/ndjson.ts`:

```ts
// The trailing element is either an incomplete frame or "" — either way
// it is not ready to parse, so it stays in the buffer.
buffer = lines.pop() ?? "";
```

**User-facing strings live in `copy.ts`, not in components.** Same reason the Python side keeps decline copy
in `rag_core/prompts.py`: it makes the wording reviewable in one place.

**A dead affordance is worse than no affordance.** From `SRC/components/AnswerText.tsx`:

```
A marker pointing past the end of `sources` renders as plain text rather
than a dead button. Small models do occasionally invent a citation number,
and an affordance that does nothing is worse than no affordance.
```

**Naming:** `PascalCase.tsx` for components, `camelCase.ts` for lib modules, matching SRC.

**Anti-patterns to avoid:** writing refusal copy in the frontend (it is server-authored, and the eval harness
depends on it being identical); an `NEXT_PUBLIC_API_URL` (same origin — see D-note above); `dangerouslySetInnerHTML`
on model output; a loading spinner instead of streamed tokens; inventing evaluation numbers for the UI.

---

## IMPLEMENTATION PLAN

### Phase 1: Make the backend developable

**Tasks:** 1 (the `local` profile and the `done` frame fix).

Doing this first means every later task can be checked against a running API with real chunks.

### Phase 2: The contract, in TypeScript

**Tasks:** 2 (scaffold), 3 (ported parser), 4 (types), 5 (api client), 6 (reducer).

### Phase 3: The page

**Depends on:** Phase 2.

**Tasks:** 7 (answer and citations), 8 (page, question box, transcript), 9 (telemetry strip), 10 (refusal discoverability, disclaimer, accessibility).

### Phase 4: Wiring

**Tasks:** 11 (CI, `vercel.json`, docs).

---

## STEP-BY-STEP TASKS

### 1. UPDATE `src/rag_adapters/profile.py` and `src/rag_api/streaming.py`

- **IMPLEMENT**: **(D3)** a `local` profile: Postgres dense and lexical stores, `FakeEmbedder`,
  `FakeGenerator`, registered in `_REGISTRY`. **(D4)** make the retrieval-failure `done` frame carry the same
  keys as every other one — `telemetry` (with `truncated: true`), `was_declined: false`,
  `decline_reason: null`. Add a test asserting **every** `done` frame has the same key set.
- **PATTERN**: `_build_hosted` in `profile.py`, and the other three `frame("done", ...)` call sites in
  `streaming.py`.
- **GOTCHA**: The `local` profile must write a manifest matching `FakeEmbedder` or the startup check refuses
  to serve. `RAG_PROFILE=local uv run python -m ingest.run` then `RAG_PROFILE=local uvicorn ...` — document
  that sequence, because getting it backwards produces a 503 that looks like a bug.
- **GOTCHA**: The `done` fix changes a contract three tickets old. Run the whole API suite; if a test asserts
  the bare shape, the test is now wrong.
- **GOTCHA**: `local` is not `fake`. `fake` keeps in-memory stores and stays the zero-dependency test profile;
  `local` is "real storage, fake inference". Both are needed.
- **VALIDATE**: `uv run pytest tests/integration/test_chat_api.py -v` and
  `RAG_PROFILE=local uv run python -c "from rag_adapters.profile import build_profile; from rag_core.config import load_config; print(build_profile(load_config()).name)"`
- **SATISFIES**: AC #1, AC #2

### 2. CREATE the `web/` scaffold

- **IMPLEMENT**: Next.js 16 App Router, React 19, TypeScript, Tailwind 4, vitest. `next.config.ts` with a
  rewrite proxying `/api/:path*` to `http://127.0.0.1:8000/api/:path*` **in development only**.
- **PATTERN**: `SRC/package.json`, `SRC/next.config.ts`, `SRC/tsconfig.json`, `SRC/vitest.config.ts`.
- **GOTCHA**: The rewrite is what keeps the code path identical in development and production. In production
  the Vercel service routing already sends `/api/*` to the backend, so the app always fetches `/api/chat`
  relatively and **there is no `NEXT_PUBLIC_API_URL`**. An env-var base URL is the thing to avoid here.
- **GOTCHA**: Guard the rewrite on `process.env.NODE_ENV === "development"`, or production requests get
  proxied to a localhost that does not exist.
- **GOTCHA**: `SRC/frontend/AGENTS.md` warns that this Next version has breaking changes versus training data
  and to read `node_modules/next/dist/docs/`. Heed it — the App Router API moves.
- **VALIDATE**: `cd web && npm install && npm run build && npm test`
- **SATISFIES**: AC #1

### 3. PORT `web/lib/ndjson.ts` and its test verbatim

- **IMPLEMENT**: Copy both files unchanged apart from the `Frame` import path.
- **PATTERN**: `SRC/lib/ndjson.ts` — this *is* the pattern.
- **GOTCHA**: **Do not "simplify" the buffering.** The trailing-element handling exists because a frame can
  arrive split across two reads, and the streaming `TextDecoder` exists because a multi-byte character can
  straddle a chunk boundary. Both are recorded bugs.
- **GOTCHA**: The `finally` block's `reader.cancel()` matters more here than in the local build: a visitor who
  navigates away mid-answer would otherwise hold a serverless function open, and the function-duration budget
  is real.
- **GOTCHA**: The test ports **unmodified**. If it needs editing to pass, the port is wrong.
- **VALIDATE**: `cd web && npm test -- ndjson` — every ported case green with no edits.
- **SATISFIES**: AC #6

### 4. CREATE `web/lib/types.ts`

- **IMPLEMENT**: The frame union for *this* API, mirroring `rag_api/telemetry.py`:

```ts
export interface Source { chunk_id: string; title: string; page: number; snippet: string; }

export interface GateTelemetry {
  proceed: boolean; reason: string;
  similarity_ok: boolean; lexical_support: boolean;   // separate, never merged
  top_similarity: number | null;                       // null when non-finite
}
export interface MetaTelemetry { gate: GateTelemetry; latency: { retrieval_ms: number | null }; fused_scores: (number | null)[]; }
export interface DoneTelemetry { latency: { ttft_ms: number | null }; total_tokens: number; provider: string | null; truncated: boolean; }

export type Frame =
  | { type: "meta"; telemetry: MetaTelemetry }
  | { type: "token"; text: string }
  | { type: "sources"; items: Source[] }
  | { type: "done"; telemetry: DoneTelemetry; was_declined: boolean; decline_reason: string | null }
  | { type: "error"; code: string; message: string };
```

- **PATTERN**: `SRC/lib/types.ts` for the discriminated-union shape.
- **GOTCHA**: **`similarity_ok` and `lexical_support` stay separate all the way to the pixel.** ADR-003 chose
  a two-condition gate specifically so telemetry could say *which* one failed — "worth the second parameter on
  its own". Collapsing them into a single "confidence" in the UI throws away the entire reason the gate has
  two parameters.
- **GOTCHA**: `top_similarity` and every latency can be `null` — `_jsonable` converts non-finite floats,
  because a bare `NaN` is not valid JSON. The types must admit it and the strip must render it.
- **GOTCHA**: There is **no `document_id`** on `Source`, unlike the local build. Do not port that field.
- **VALIDATE**: `cd web && npx tsc --noEmit`
- **SATISFIES**: AC #4

### 5. CREATE `web/lib/api.ts`

- **IMPLEMENT**: `askQuestion(question, onFrame, signal)` — POST `/api/chat`, and on a 200 iterate
  `readFrames(response.body)`. On a non-200, parse the JSON error body and surface `{code, message}` as a
  synthetic `error` frame so callers have one path.
- **PATTERN**: `SRC/lib/api.ts`.
- **GOTCHA**: A 429 is a **non-streaming JSON body**, not a frame stream. So is a 400 and a 503. `response.body`
  exists but is not NDJSON, and `readFrames` would throw on the first `JSON.parse`. Branch on `response.ok`
  first.
- **GOTCHA**: The 429 carries a `Retry-After` header. Surface it — a rate-limit message without the wait is
  the raw 429 PRD F15 explicitly asks not to return.
- **GOTCHA**: Pass an `AbortSignal` so a new question cancels an in-flight one. Without it, two overlapping
  streams interleave tokens into the same turn.
- **VALIDATE**: `cd web && npm test -- api`
- **SATISFIES**: AC #1

### 6. PORT and adapt `web/lib/chatReducer.ts` + tests

- **IMPLEMENT**: The state machine from SRC: `Turn` with a kind of `pending | answer | decline | error`,
  `patchLast`, `isTerminalFrame`, and a transcript (D2) rather than a single turn. Update the payload handling
  for the new frames: `meta` stores `MetaTelemetry`, `done` merges `DoneTelemetry` and sets the kind from
  `was_declined`.
- **PATTERN**: `SRC/lib/chatReducer.ts` — structure ports, payloads change.
- **GOTCHA**: `meta` **may never arrive**. On the retrieval-failure path the API emits `error` then `done`
  with no `meta` at all, so the reducer must not assume telemetry exists before rendering.
- **GOTCHA**: Telemetry arrives in **two halves**. The strip is partial after `meta` and complete after
  `done` — and on a refusal it is complete *before* the decline text, which is the whole point of the split.
  Merge, do not replace.
- **GOTCHA**: `sources` arrives immediately *before* the first token, not after the answer. A reducer that
  waits for `done` to attach sources will render an answer whose citations are dead for its entire duration.
- **VALIDATE**: `cd web && npm test -- chatReducer`
- **SATISFIES**: AC #1, AC #3

### 7. PORT `web/components/AnswerText.tsx`

- **IMPLEMENT**: Copy it, keeping the marker regex, the `sources[n-1]` mapping, the plain-text fallback and
  the `focus-visible:ring` classes. **Add**: `console.error` when a marker points past the end of `sources`.
- **PATTERN**: `SRC/components/AnswerText.tsx` in full.
- **GOTCHA**: Architecture §7 says *"An unresolvable citation is a bug and is logged as one."* The ported
  component degrades gracefully but **silently**, which satisfies the user-facing half and not the logging
  half. Log it; do not add a visible error, because a model occasionally inventing a number is not something
  to shout at a reader about.
- **GOTCHA**: The `[n] → sources[n-1]` mapping only holds because `prompts.format_context` numbers chunks in
  the same order `_sources_payload` serialises them. That coupling is invisible from the frontend and is the
  single assumption this component rests on — keep the comment that says so.
- **VALIDATE**: `cd web && npm test -- AnswerText` — a marker within range renders a button, one past the end
  renders plain text and logs.
- **SATISFIES**: AC #2

### 8. CREATE the page: `app/page.tsx`, `QuestionBox`, `Transcript`, `TurnView`, `SourceList`

- **IMPLEMENT**: A single page. Question box at the top, transcript below, each turn showing the question, the
  streaming answer (or refusal), its sources, and its telemetry strip.
- **PATTERN**: `SRC/components/ChatWindow.tsx` for the submit/stream wiring only — not its layout.
- **GOTCHA**: **Render tokens as they arrive.** A spinner until `done` would pass every functional test and
  destroy the demo — the same class of failure the API side guarded against with a timing test.
- **GOTCHA**: Sources render as citation targets, showing title and page. AC #2 is that a citation *resolves*;
  a source list nobody can get to from the text is not resolution.
- **GOTCHA**: Disable the submit control while a request is in flight, or cancel the previous one. Both are
  fine; silently interleaving two streams is not.
- **GOTCHA**: The question box is the first thing a stranger sees and PRD criterion 1 is that they reach a
  cited answer *without instructions*. Autofocus it.
- **VALIDATE**: `cd web && npm run build && npx tsc --noEmit`, then the manual check in Level 4.
- **SATISFIES**: AC #1, AC #2

### 9. CREATE `web/components/TelemetryStrip.tsx`

- **IMPLEMENT**: Visible by default (PRD open question 4, resolved toward visible in the ticket). Shows: the
  gate decision and **both conditions separately**, `top_similarity`, fused scores, retrieval latency, TTFT,
  token count, and which provider served. Renders a partial strip gracefully.
- **PATTERN**: `SRC/components/StatusBar.tsx` for the compact-metrics layout idea only.
- **GOTCHA**: **On a refusal, the strip must say which condition failed.** That is AC #4 and it is the reason
  ADR-003 accepted a second tuning parameter. `similarity_ok: false` and `lexical_support: true` is a
  different story from both false, and the strip is where that story gets told.
- **GOTCHA**: Fused scores will look nearly constant — `2/(60+1)` when both retrievers agree on first place.
  That is not a bug to hide: shown beside `top_similarity` it is ADR-003's argument made visible, which is
  exactly what a strip that "shows engineering rather than describing it" is for. Label it so the near-constancy
  reads as intentional.
- **GOTCHA**: Every number can be `null`. Render a dash, not `NaN` or `null`.
- **GOTCHA**: Architecture §3 calls this "a product feature, not debug output". It should look designed, not
  like a console dump.
- **VALIDATE**: `cd web && npm test -- TelemetryStrip`
- **SATISFIES**: AC #4

### 10. CREATE `ExampleQuestions.tsx`, `Disclaimer.tsx`, and the accessibility pass

- **IMPLEMENT**: Three or four clickable example questions, **at least one that refuses**. A persistent,
  non-dismissable "not a clinical tool" disclaimer. Then: `aria-live="polite"` on the answer region, visible
  focus throughout, full keyboard traversal, and `prefers-reduced-motion` honoured.
- **PATTERN**: `SRC/lib/copy.ts` for keeping strings out of components.
- **GOTCHA**: **The refusal examples must genuinely refuse**, and `src/ingest/fixtures/manifest.json` is how
  you know. Each drug lists `verified_absent` axes measured over the assembled text — atenolol has
  `pediatric` and `pregnancy`. A question on an absent axis is a fair near-miss; one you guessed at may well
  be answerable, and an example labelled "this one refuses" that answers is worse than no example.
- **GOTCHA**: PRD G5 is that the refusal is discoverable in under a minute **by someone who was not told about
  it**. An example that is clearly labelled is the mechanism; burying it is failing the criterion.
- **GOTCHA**: `aria-live` is the accessibility requirement most likely to be missed. Focus rings satisfy the
  visible half of AC #5; a streaming answer that a screen reader never announces fails the rest.
- **GOTCHA**: Use `aria-live="polite"`, not `assertive` — an assertive region interrupts on every token.
- **GOTCHA**: The disclaimer is non-dismissable by requirement (PRD risk: "Someone reads the demo as medical
  advice"). Do not add a close button.
- **VALIDATE**: Keyboard-only traversal reaching every control with visible focus; the manual checks in
  Level 4.
- **SATISFIES**: AC #3, AC #5

### 11. UPDATE CI, `vercel.json`, and the docs

- **IMPLEMENT**: A frontend job in `.github/workflows/test.yml` running `npm ci`, `tsc --noEmit`, `npm test`,
  `npm run build`. A `vercel.json` with the Services block routing `/api/(.*)` to the Python service and
  `/(.*)` to `web/`. README gains how to run both halves locally. Record in ARCHITECTURE §2/§3 that the two
  deploy as one project, and answer **PRD open question 4** (telemetry visible by default) with the reasoning.
- **PATTERN**: the existing workflow's `astral-sh/setup-uv` job; Vercel's Services routing example.
- **GOTCHA**: Same-origin means **TICKET-5's permissive CORS middleware is now unnecessary**. Remove it or
  narrow it to development, and say which in the report — leaving `allow_origins=["*"]` on a public API for no
  reason is the kind of thing a reviewer should not have to ask about.
- **GOTCHA**: `vercel.json` here is configuration, not deployment. TICKET-10 creates the project and sets the
  environment variables; this only makes the layout correct so that ticket does not restructure the repo.
- **GOTCHA**: Do not fill in the README's measured-results placeholder. TICKET-8 and TICKET-9 own those
  numbers and inventing them is the one thing this project's argument cannot survive.
- **VALIDATE**: `cd web && npm ci && npx tsc --noEmit && npm test && npm run build`, and a pushed CI run with
  both jobs green.
- **SATISFIES**: AC #6

---

## TESTING STRATEGY

### Unit Tests (vitest, in `web/`)

`ndjson.test.ts` — **ported unmodified**. Split frames, multi-byte boundaries, missing trailing newline,
early `break`.

`chatReducer.test.ts` — ported, payloads updated. Every frame kind; `meta` never arriving; telemetry merging
across two frames; `sources` before the first token; terminal-frame handling; transcript accumulation.

`api.test.ts` — a 200 streams frames; a 429/400/503 becomes a synthetic `error` frame with the code intact;
`Retry-After` is surfaced; an abort stops iteration.

`AnswerText.test.tsx` — in-range marker renders a button; out-of-range renders plain text **and logs**; text
without markers renders unchanged.

`TelemetryStrip.test.tsx` — both gate conditions rendered separately; a refusal shows which failed; nulls
render as a dash; a partial (meta-only) strip renders.

### Python-side

`tests/integration/test_chat_api.py` — extended for D4: **every** `done` frame carries the same keys.

### Manual (Level 4)

Progressive rendering, the refusal reading as deliberate, keyboard traversal, focus visibility, reduced
motion, and the citation click-through. These are the criteria a unit test cannot honestly claim.

### Edge Cases

- A question submitted while one is in flight
- Navigating away mid-stream (the reader must be cancelled)
- An answer citing `[3]` when only two sources exist → plain text, logged
- An answer with no citation markers at all
- A refusal — no sources, telemetry complete before the text
- `top_similarity: null` (non-finite from the gate)
- A 429 with a multi-hour wait → phrased in hours
- `index_unavailable` (503) → a clear "this deployment cannot serve" state, not a generic error
- A mid-stream `error` frame after partial text → text stays, marked truncated
- Zero-token answer → `done` with `total_tokens: 0`, no crash
- Very long answer → the transcript scrolls, the question box stays reachable

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
cd web && npx tsc --noEmit && npx eslint .
```

### Level 2: Unit Tests

```bash
uv run pytest tests/unit -q
cd web && npm test
```

### Level 3: Integration Tests

```bash
uv run pytest -q

docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
RAG_PROFILE=local uv run python -m ingest.run     # the D3 profile: real storage, fake inference
uv run pytest -m postgres -q
cd web && npm run build
```

### Level 4: Manual Validation

```bash
# Both halves, no API keys needed (D3)
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
RAG_PROFILE=local uv run uvicorn rag_api.main:app --port 8000 &
cd web && npm run dev &
sleep 5
open http://localhost:3000
```

Then, in the browser:

1. **Progressive rendering** — ask "What is the adult starting dose of metformin?" and watch tokens appear
   incrementally. A block appearing at once is the failure.
2. **Citations resolve** — click a `[1]`; it should highlight a source showing a title and page number.
3. **An invented citation** — not reproducible on demand; verify via the unit test and check the console
   stays clean on normal answers.
4. **The refusal** — click the labelled refusal example. It should read as a decision: the telemetry strip
   already populated, the gate reason shown, no sources, and copy that explains rather than apologises.
5. **Which condition failed** — the strip shows `similarity_ok` and `lexical_support` separately, not merged.
6. **Keyboard only** — unplug the mouse. Tab to the question box, type, submit, tab to a citation, activate
   it. Every stop must have a visible focus ring.
7. **Reduced motion** — enable it in the OS and reload. The streaming cursor animation must stop; text still
   streams.
8. **Screen reader** — VoiceOver (⌘F5). The answer must be announced as it arrives, not silently.
9. **Rate limit** — restart the API with `RATE_LIMIT_PER_MINUTE=2`, ask three times. The third shows the
   plain-language wait, not a raw 429.
10. **Small viewport** — 375px wide. The strip must remain readable; PRD §3 says "probably on a phone".

```bash
kill %1 %2; docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
git diff --cached | grep -inE "AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}|postgres(ql)?://[^ ]*:[^ ]*@" | grep -v localhost || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-7, plus the standard bar:

- [ ] **AC #1** — Tokens render progressively, not as one block at the end
- [ ] **AC #2** — Every citation resolves to a source with title and page anchor; an unresolvable one renders as plain text **and is logged**
- [ ] **AC #3** — A refusal renders as a deliberate, designed state — not an error, not an empty answer
- [ ] **AC #4** — The telemetry strip shows **which gate condition failed** on a refusal, with the two conditions never merged
- [ ] **AC #5** — Keyboard-only traversal reaches every control with visible focus; `aria-live` announces the streaming answer; reduced motion is honoured
- [ ] **AC #6** — The ported `ndjson` and `chatReducer` tests pass **unmodified** (ndjson) / with only payload updates (chatReducer)
- [ ] Every `done` frame from the API carries the same key set (D4)
- [ ] `RAG_PROFILE=local` serves the real corpus with no API keys (D3)
- [ ] All validation commands pass; `mypy --strict` and `tsc --noEmit` clean
- [ ] No invented evaluation numbers anywhere in the UI or README
- [ ] No secret in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 11 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full Python suite green with and without a database
- [ ] `web` suite green; build succeeds
- [ ] CI green, both jobs
- [ ] The ten manual checks in Level 4 performed and recorded in the report
- [ ] Acceptance criteria all met

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 port the logic and build the page fresh; D2 keep a
session transcript; D3 add a `local` profile; D4 fix the `done` frame.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — same-origin, so no CORS and no API base URL. Vercel Services puts Next.js and FastAPI in one
   project sharing a domain; local development uses a Next rewrite so the code path is identical.
2. **Assumed** — the telemetry strip is visible by default, answering PRD open question 4. The ticket already
   leaned this way; the audience is a technical evaluator and the strip is the argument.
3. **Assumed** — `console.error` satisfies "an unresolvable citation is logged as a bug". A visible error
   would shout at a reader about a model quirk they cannot act on.
4. **Assumed** — no Playwright. The frontend logic is unit-tested and the visual and accessibility criteria
   are verified by hand. **This is the weakest part of the plan**: AC #1 and AC #5 rest on manual checks, and
   a browser-level regression would not be caught. Worth revisiting if the UI grows.
5. **Assumed** — TICKET-5's permissive CORS middleware is removed or narrowed to development, since
   same-origin makes it unnecessary.
6. **Assumed** — the transcript is in-memory only; reloading clears it. Nothing to persist and no one to
   persist it for.

---

## NOTES (open canvas)

### The strip is the deliverable

It is tempting to treat the telemetry strip as a nice-to-have beside the answer. Architecture §3 is explicit
that it is the opposite: *"the part that shows engineering rather than describing it."*

Six tickets of work are invisible without it. The two-condition gate, the failover chain reporting which
provider served, retrieval latency separate from time-to-first-token, the fused scores that demonstrate
ADR-003's point about RRF discarding magnitude — all of that is a paragraph in a README unless a reader can
watch it happen. A visitor who asks one question and one refusal should be able to infer the whole design
from what the strip shows them.

Which is why the two gate conditions must never be merged into one "confidence" number in the interface. That
merge is precisely what ADR-003 rejected, and doing it in the UI would undo the decision at the last possible
moment, where nobody would think to look.

### Why the refusal needs design work, not code

PRD success criterion 2 is that the refusal *"reads as deliberate, not broken"*. Every mechanism for that
already exists — server-authored copy, a gate reason, both conditions, a fast path with no model call. What
does not exist is the presentation.

A refusal that renders in the same grey box as an error, with an empty source list and a blank strip, reads as
a failure regardless of how carefully the backend decided it. The split telemetry from TICKET-5 exists exactly
so that a refusal arrives *explained first*: the strip is fully populated before the decline text renders. The
frontend has to actually use that ordering rather than waiting for `done` like it would for an answer.

### The example questions carry real weight

PRD G5 — discoverable in under a minute by someone not told about it — is met by a labelled example, and the
label has to be honest. `manifest.json` records which axes are *measured absent* from each drug's text, and a
near-miss question drawn from those is genuinely unanswerable. A question invented on the theme of "something
the corpus probably lacks" may well be answerable, and an example promising a refusal that produces an answer
is worse than having no examples at all.

### Alternatives weighed and rejected

**A `NEXT_PUBLIC_API_URL`.** Standard for a split frontend and backend, and unnecessary here: Vercel Services
gives one origin, and a dev-only rewrite covers local development. An env-var base URL would add a
configuration axis whose only failure mode is silent (pointing at the wrong deployment).

**Porting the workbench shell.** Rejected in D1. Faster to something that looks finished, at the cost of
carrying a three-pane layout designed around uploads that no longer exist.

**A visible "unresolved citation" marker.** Rejected: it surfaces a model quirk to a reader who cannot act on
it. The console is where a bug belongs.

**Rendering the answer only when complete.** Simpler state handling, and it throws away the streaming demo —
the same failure the API side spent a whole timing test guarding against.

### Sequencing

TICKET-6 is on PR #6; branch from it. After this, only TICKET-8 (τ sweep), TICKET-9 (retrieval parity) and
TICKET-10 (deploy) remain. TICKET-8 and TICKET-9 are sequential with each other and independent of this.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
