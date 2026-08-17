# Feature: TICKET-6 — Rate limiting, health, and the failure surface

The following plan should be complete, but it's important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files etc.

---

## Feature Description

Everything that keeps a free-tier demo alive and makes its failures legible: a per-IP limit in front of the
expensive endpoint, a health endpoint that can be asked how deep to look, and a scheduled check that exercises
the failover secondary on purpose rather than hoping.

That last one is not speculative. During TICKET-4 the secondary generator turned out to be **dead** — a pinned
`gemini-2.0-flash` that had been retired — and nothing would have discovered it until the failover was
actually needed. ADR-004 anticipated exactly this: *"if the secondary is never exercised in ninety days, test
it deliberately rather than assuming it works."* This ticket is that test.

## User Story

As the owner of a demo that has to still work months from now, unattended
I want a limit in front of the expensive path and a scheduled check behind it
So that one scraper cannot burn the day's quota, and so that a provider dying tells me rather than waiting to be discovered by a visitor.

## Problem Statement

The API is reachable and unprotected. A single script can exhaust the daily embedding and generation
allowances, which PRD's risk table rates *Medium* likelihood with *"Demo dead for the day"* as the impact and
names the mitigation: *"IP rate limit in front of every endpoint from day one."*

Separately, the failover secondary has no exercise. It is called only when the primary fails, which — if the
demo is healthy — may be never, right up until the moment it is needed and is found to be broken.

## Solution Statement

A rate-limit seam in the shell with an Upstash implementation and an in-memory one; a `?deep=1` mode on
`/api/health` that probes both generation providers individually; and a weekly GitHub Actions workflow that
runs that probe and fails loudly.

Four decisions were taken at planning time and are settled:

| # | Decision | Why |
|---|---|---|
| D1 | **Fail open.** If Upstash is unreachable, allow the request and log loudly. | Rate limiting is a third free tier with no SLA. Making the demo's availability depend on three rather than two defeats PRD criterion 6 — that it still works thirty days later, which the PRD calls the criterion most likely to fail. The cost is that quota protection is absent during an Upstash outage; the scheduled check surfaces a limiter that stays broken. |
| D2 | **Health is shallow by default; `?deep=1` probes the providers.** | A monitor polling a provider-probing endpoint every minute would burn the quota the limiter exists to protect. Shallow stays free and fast; deep is what the scheduled check calls, and what answers "is the secondary alive *right now*" on demand — the question whose absence let a dead model go unnoticed. |
| D3 | **Weekly, and a failed workflow is the alarm.** | Well inside ADR-004's ninety-day concern, two generations a week, and GitHub emails the repo owner on scheduled-workflow failure by default. No issue-dedup machinery and no badge people learn to ignore. |
| D4 | **10 requests/minute, 100/day, per IP.** | PRD §3 describes the evaluator as spending ten minutes asking a handful of questions, so 10/min is generous for real use and stops a script cold. The daily cap is the one that actually protects quota: a minute limit alone still permits 14,400 requests a day from one address. |

## Out of Scope / Non-Goals

- **Not included: the all-providers-unavailable service message.** Already built and tested in TICKET-5
  (`rag_api/errors.py::ALL_PROVIDERS`, `test_all_providers_unavailable_returns_the_service_message`). This
  ticket's AC #5 is already satisfied; re-verify, do not rebuild.
- **Not included: the manifest-mismatch health behaviour.** Also TICKET-5 — `/api/health` already returns 503
  with the reason. AC #3 is satisfied; this ticket only adds store and provider reachability alongside it.
- **Not included: deploying anything.** The scheduled check runs the probe **in CI against the providers
  directly**, not against a deployed URL — there is no deployment until TICKET-10, and probing the providers
  is the point rather than probing the deployment.
- **Not included: authentication, accounts or per-user quotas.** PRD §4 non-goal — no login, no accounts.
- **Not included: rate limiting `/api/health` (shallow).** It must stay reachable for diagnosis and costs
  nothing. Limit by cost, not by URL — see Task 3.
- **Not included: retry/backoff on the client side.** The 429 carries `Retry-After`; what a client does with
  it is TICKET-7's.
- **Not changing:** `rag_core` at all. Rate limiting is transport (ARCHITECTURE.md §3 lists it under the API
  shell explicitly), so nothing here belongs one layer down.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium-Low — three small pieces, but the failure semantics matter more than the code.
**Primary Systems Affected**: `src/rag_api/`, `src/rag_adapters/failover.py` (one small addition), CI
**Dependencies**: `upstash-ratelimit`, `upstash-redis`

## Related Work

**Implements**: TICKET-6 in `docs/tickets/medical-rag-hosted-version.md`
**Epic**: `docs/ARCHITECTURE.md` + `docs/PRD.md`

**Back-references**:

- `.claude/plans/fastapi-shell-streaming-and-telemetry.md` — the app, `AppState`, the error table and the health endpoint this extends.
- `.claude/plans/hosted-provider-adapters-and-failover.md` — `FailoverGenerator`, whose secondary this ticket exercises.
- `.claude/reports/offline-ingestion-job-report.md` — records the dead secondary that makes ADR-004's action item 4 evidence-backed rather than precautionary.

**Forward-references**:

- TICKET-7 — renders the 429 copy and the `Retry-After` hint
- TICKET-10 — provisions Upstash and sets the production credentials

**Sequencing:** every prior ticket is merged; branch from `main`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `src/rag_api/main.py` — Why: `create_app`, the lifespan, and the injected-state seam. Rate limiter
  construction goes alongside profile construction; **read the comment on why a pre-set `app.state.rag` is
  left alone** — the same reasoning applies to an injected limiter.
- `src/rag_api/health.py` — Why: the endpoint being extended. It already reports serviceability, the manifest
  and the configured embedder, and already 503s on mismatch. Its docstring says "ADR-004's scheduled exercise
  of the secondary provider is TICKET-6" — this is that ticket.
- `src/rag_api/errors.py` — Why: the `Failure` dataclass and the exception→response table. The 429 joins it.
  Note the module docstring's rule: **never return a provider's message verbatim.**
- `src/rag_api/chat.py` — Why: where the dependency attaches, and the existing 400/503 response shape the 429
  must match.
- `src/rag_api/state.py` — Why: `AppState`. Whether the limiter lives here or beside it is Task 2's call.
- `src/rag_adapters/failover.py` — Why: `_primary` and `_secondary` are private. Task 4 exposes them, because
  probing *both* is the entire point of ADR-004 item 4 and a probe through the chain only ever exercises the
  primary.
- `src/rag_adapters/fakes.py` — Why: `FakeGenerator(fail_with=...)` is the seam the probe tests drive, and the
  fakes are the model for the in-memory limiter.
- `src/rag_core/config.py` — Why: the section-per-concern pattern and `load_config` as the single
  environment boundary. Both SDKs read `UPSTASH_REDIS_REST_*` from the environment themselves — do not let
  them (see Task 1).
- `tests/integration/test_chat_api.py` — Why: `build_state`, `app_for`, `frames_for`, and the
  `httpx.ASGITransport` pattern the limiter tests reuse. **Note the finding recorded at the top: ASGITransport
  buffers**, which is irrelevant here but explains why the streaming tests look unusual.
- `.github/workflows/test.yml` — Why: the workflow shape, the `uv` setup, and the service-container block the
  scheduled workflow mirrors minus the database.
- `pyproject.toml` — Why: the extras pattern (`postgres`, `providers`, `api`) and the marker + `addopts`
  pattern. Do not widen the vendored-port exclusions or the mypy override.

### New Files to Create

```
src/rag_api/
├── ratelimit.py          # the seam: protocol, in-memory, Upstash
└── probe.py              # exercises each generation provider individually
tests/unit/test_ratelimit.py
tests/integration/test_ratelimit_api.py
tests/unit/test_probe.py
.github/workflows/provider-check.yml
```

Modified: `src/rag_api/{main,chat,health,errors,state}.py`, `src/rag_adapters/failover.py`,
`src/rag_core/config.py`, `pyproject.toml`, `.env.example`, `README.md`, `docs/ARCHITECTURE.md`.

### Relevant Documentation — YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [upstash-ratelimit — getting started](https://upstash.com/docs/redis/sdks/ratelimit-py/gettingstarted)
  - Specific: `Ratelimit(redis=..., limiter=FixedWindow(max_requests, window), prefix=...)`, and
    `response.allowed / limit / remaining / reset`
  - Why: `reset` is a **unix timestamp in seconds**, which is what the `Retry-After` hint is derived from.
- [upstash-ratelimit — usage](https://upstash.com/docs/redis/sdks/ratelimit-py/gettingstarted#usage)
  - Specific: *"For asynchronous usage, import the asyncio-based variant from the `upstash_ratelimit.asyncio`
    module."*
  - Why: the shell is async throughout; the sync variant would block the event loop on every request.
- [upstash-ratelimit — overview](https://upstash.com/docs/redis/sdks/ratelimit-py/overview)
  - Specific: connectionless, HTTP over TCP, built for AWS Lambda / Vercel Serverless
  - Why: this is why it works on a platform with no long-lived process, and why no pooling is needed.
- [upstash-ratelimit — algorithms](https://upstash.com/docs/redis/sdks/ratelimit-py/algorithms)
  - Specific: fixed window vs sliding window
  - Why: Task 2's choice. Fixed window is one round trip and permits a 2× burst at a window boundary; sliding
    window is smoother and costs more.
- [Vercel — request headers](https://vercel.com/docs/edge-network/headers/request-headers)
  - Specific: `x-forwarded-for`, `x-real-ip`
  - Why: Task 3 identifies the client from these. Getting it wrong buckets every visitor together.
- [GitHub Actions — schedule](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)
  - Specific: cron syntax, and that scheduled workflows email the repo owner on failure
  - Why: D3's alarm mechanism is exactly that default.

### Patterns to Follow

**A seam with a fake, so tests need no service.** Every adapter in this codebase takes its client, and the
fakes are first-class rather than test scaffolding. From `src/rag_adapters/fakes.py`:

```python
"""In-memory implementations of all four ports. Deterministic, no network.

These are not test scaffolding that happens to live in the source tree. They
are the second implementation of each port ...
"""
```

**Module docstrings state the failure the module prevents**, citing `ARCHITECTURE.md §N`, an ADR, or a PRD
requirement.

**Failures carry a code and a human message, never a vendor string.** From `src/rag_api/errors.py`:

```python
@dataclass(frozen=True)
class Failure:
    status: int
    code: str
    message: str
```

**Config is the single environment boundary.** From `src/rag_core/config.py`:

```python
# Empty by default so the fake profile needs nothing. Both SDKs will read
# these from the environment themselves if not passed a key — which is
# exactly why they are read here instead.
```

**Anti-patterns to avoid:** a bare `except Exception`; module-level mutable state (one process, concurrent
requests); reading `os.environ` outside `load_config`; letting a limiter failure become a request failure
(D1); rate-limiting an endpoint whose cost does not warrant it.

---

## IMPLEMENTATION PLAN

### Phase 1: The limiter

**Tasks:** 1 (config + deps), 2 (the seam), 3 (wiring it to the expensive path).

### Phase 2: The probe

**Independent of Phase 1** — different files, no shared code. Genuinely parallelisable.

**Tasks:** 4 (expose the chain's providers), 5 (the probe + deep health).

### Phase 3: The alarm and proof

**Depends on:** Phases 1 and 2.

**Tasks:** 6 (the weekly workflow), 7 (tests), 8 (docs).

---

## STEP-BY-STEP TASKS

### 1. UPDATE `pyproject.toml`, `src/rag_core/config.py`, `.env.example`

- **IMPLEMENT**: Add `upstash-ratelimit>=1.0` and `upstash-redis>=1.2` to the `api` extra. Add a
  `RateLimitConfig` section: `redis_url: str = ""`, `redis_token: str = ""`, `per_minute: int = 10`,
  `per_day: int = 100`, read from `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`,
  `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_DAY`. Add to `RagConfig`. Document all four in `.env.example` with
  the secret warning.
- **PATTERN**: `ProvidersConfig` in `src/rag_core/config.py` — same empty-by-default shape and the same
  comment about SDKs reading the environment behind config's back.
- **GOTCHA**: `Redis.from_env()` exists and must **not** be used. `config.py`'s docstring says it is "the
  single boundary between os.environ and the pipeline", and an SDK reaching around it makes the config a lie
  and the tests non-hermetic. Pass the URL and token explicitly.
- **GOTCHA**: Empty credentials mean **no limiter**, not a broken one — that is the local-development path,
  and it must log that limiting is disabled rather than failing silently.
- **GOTCHA**: `dependencies` stays `[]`. `tests/unit/test_core_purity.py` asserts it.
- **VALIDATE**: `uv sync && uv run pytest tests/unit/test_config.py tests/unit/test_core_purity.py -q`
- **SATISFIES**: AC #1, AC #2

### 2. CREATE `src/rag_api/ratelimit.py` — the seam

- **IMPLEMENT**: A `RateLimiter` Protocol with `async def check(identifier: str) -> Decision`, where
  `Decision` is a frozen dataclass of `allowed: bool`, `limit: int`, `remaining: int`, `retry_after: int`.
  Three implementations:
  - `NoLimiter` — always allows. Used when credentials are absent.
  - `InMemoryLimiter` — fixed windows in a dict. For tests and local development.
  - `UpstashLimiter` — two `upstash_ratelimit.asyncio.Ratelimit` instances (minute and day), both consulted;
    denies if **either** denies, and reports the longer `retry_after`.
- **PATTERN**: `src/rag_adapters/fakes.py` for the fake-is-first-class stance;
  `src/rag_api/errors.py::Failure` for the frozen-dataclass-of-a-decision shape.
- **GOTCHA**: **D1 — fail open.** Wrap the Upstash calls; on any `Exception` from the SDK, log at warning and
  return `allowed=True`. A third free tier with no SLA must not be able to take the demo down. Log the failure
  loudly enough that a persistently-broken limiter is visible, because failing open silently is how you
  discover months later that you were never limited at all.
- **GOTCHA**: Use `upstash_ratelimit.asyncio`, not the sync module. The sync variant issues blocking HTTP on
  the event loop, which would stall every concurrent request behind it.
- **GOTCHA**: Two limiters means two round trips per request. Upstash REST is typically tens of milliseconds,
  so this sits inside the 2.5s p50 TTFT budget — but it is the reason the limiter guards only the expensive
  endpoint (Task 3), not everything.
- **GOTCHA**: `Response.reset` is a **unix timestamp in seconds**, not a duration. `retry_after` is
  `max(1, ceil(reset - now))`; returning the raw timestamp would tell a client to wait 55 years.
- **GOTCHA**: `InMemoryLimiter` is per-process and therefore useless on serverless — that is the point of
  AC #2. Say so in its docstring so nobody promotes it to production.
- **VALIDATE**: `uv run pytest tests/unit/test_ratelimit.py -v`
- **SATISFIES**: AC #1, AC #2

### 3. UPDATE `src/rag_api/{main,chat,errors}.py` — wire it to the expensive path

- **IMPLEMENT**: Build the limiter in `create_app`/lifespan and hang it on `app.state` beside `rag`. A FastAPI
  dependency on `POST /api/chat` that calls `check(identifier)` and raises a 429 carrying a `Retry-After`
  header and a `Failure` body. Add `RATE_LIMITED` to `errors.py`. Identify the client from
  `x-forwarded-for` (leftmost entry), then `x-real-ip`, then `request.client.host`, then a constant
  `"unknown"` bucket.
- **PATTERN**: `src/rag_api/chat.py`'s existing serviceability guard — same early return shape, same
  `{"code": ..., "message": ...}` body.
- **GOTCHA**: **Guard `/api/chat` and `/api/health?deep=1`, not shallow health.** PRD F15 says "public
  endpoints", but the cost is what matters: shallow health is free and must stay reachable for diagnosis,
  while a monitor polling it every minute would blow a 100/day cap by itself. Limiting by cost rather than by
  URL is the decision; record it in a comment.
- **GOTCHA**: `x-forwarded-for` is a **comma-separated list** and the leftmost entry is the client; the rest
  are proxies. Taking the last one buckets every visitor behind Vercel's edge into a single identifier, which
  would rate-limit the whole world together and look like an outage.
- **GOTCHA**: A dependency, not global middleware. Middleware would need path filtering to avoid the health
  exemption, and the filtering is the thing most likely to be got wrong later.
- **GOTCHA**: The 429 body must be plain language with the wait, not a raw status (PRD F15: "returning a clear
  message rather than a raw 429"). Something a non-technical reader understands.
- **VALIDATE**: `uv run pytest tests/integration/test_ratelimit_api.py -v`
- **SATISFIES**: AC #1

### 4. ADD `providers` to `src/rag_adapters/failover.py`

- **IMPLEMENT**: A `providers` property returning `(primary, secondary)`.
- **PATTERN**: the existing `model_id` attribute — the chain already exposes something about what it wraps.
- **GOTCHA**: **This is the whole reason ADR-004 item 4 needs it.** A probe *through* the chain exercises only
  the primary, because that is what the chain is for. Exercising the secondary deliberately means reaching
  past the abstraction — which is exactly what the ADR asks for: "test it deliberately rather than assuming it
  works."
- **GOTCHA**: Do not add a `probe()` method to the chain itself. Health-checking is not generation, and the
  `GenerationProvider` port has no business growing a diagnostic method that the request path never calls.
- **VALIDATE**: `uv run pytest tests/unit/test_failover.py -v`
- **SATISFIES**: AC #4

### 5. CREATE `src/rag_api/probe.py` and extend `src/rag_api/health.py`

- **IMPLEMENT**: `async def probe_generators(profile) -> list[ProbeResult]` — for each provider in the chain
  (or the single generator if it is not a chain), send a trivial prompt, consume the stream, and record
  `{name, model_id, ok, detail, latency_ms}`. `async def probe_store(profile) -> ProbeResult` — a `count()`
  against the dense store. Then `?deep=1` on `/api/health` includes both; shallow includes only the store
  reachability check, which is cheap.
- **PATTERN**: `src/rag_api/health.py`'s existing body shape and its 200/503 rule.
- **GOTCHA**: **A probe must name which provider failed and why**, not report a boolean. The local build's
  health endpoint records the lesson: a check that reports a capability present when it is not "converts a
  clear failure into an unexplained one later". `gemini-2.0-flash is no longer available` is the message that
  would have saved a ticket.
- **GOTCHA**: Deep health returns 503 when **any** probe fails, including a healthy-primary /
  dead-secondary case. A working demo with a dead fallback is not healthy; it is one rate limit away from
  broken, and that is precisely the state that went unnoticed.
- **GOTCHA**: Each probe needs its own timeout so one hung provider cannot hang the endpoint. `asyncio.wait_for`
  around each, a few seconds.
- **GOTCHA**: Probe failures must not raise out of the endpoint. Catch per-provider, record, continue — the
  point is to report the state, not to become the failure.
- **VALIDATE**: `uv run pytest tests/unit/test_probe.py -v`
- **SATISFIES**: AC #3, AC #4

### 6. CREATE `.github/workflows/provider-check.yml`

- **IMPLEMENT**: `schedule: cron` weekly, plus `workflow_dispatch` so it can be run on demand. Checks out,
  `uv sync`, runs `uv run python -m rag_api.probe` with `GEMINI_API_KEY` and `GROQ_API_KEY` from repo
  secrets. The module's `__main__` exits non-zero if any provider is unreachable.
- **PATTERN**: `.github/workflows/test.yml` — same checkout, same `astral-sh/setup-uv`, same `uv sync`.
- **GOTCHA**: **This runs the probe in CI against the providers directly, not against a deployed URL.** There
  is no deployment until TICKET-10, and probing the providers is the point — a deployment check would also go
  red for reasons that have nothing to do with whether the secondary model still exists.
- **GOTCHA**: **The workflow needs `GEMINI_API_KEY` and `GROQ_API_KEY` as repository secrets, which only the
  repository owner can add.** Until they exist the weekly run fails with a message naming the missing secret.
  That is deliberate — a check that skips itself when unconfigured is a check that silently never runs — but
  flag it prominently in the report so it is added rather than ignored into background noise.
- **GOTCHA**: Never echo a secret. The probe prints model ids and error categories, never keys.
- **GOTCHA**: `schedule` only fires on the default branch. It will not run from a feature branch; verify with
  `workflow_dispatch` instead, and note that the first scheduled run happens after merge.
- **VALIDATE**: `uv run python -m rag_api.probe` locally with keys exported (exits 0), and
  `gh workflow list` showing the workflow registered after push.
- **SATISFIES**: AC #4

### 7. CREATE the test suites

- **IMPLEMENT**: `tests/unit/test_ratelimit.py` (the seam, no network),
  `tests/integration/test_ratelimit_api.py` (the endpoint, over `InMemoryLimiter`),
  `tests/unit/test_probe.py` (the probe over fakes). Cases in the Testing Strategy below.
- **PATTERN**: `tests/integration/test_chat_api.py`'s `build_state` / `app_for` helpers.
- **GOTCHA**: **AC #2 cannot be proven with `InMemoryLimiter`** — it is per-process by construction. What the
  tests can prove is that `UpstashLimiter` holds no local counter state and that the identifier is the only
  thing distinguishing callers. Prove the rest by inspection and say so, rather than writing a test whose name
  claims more than it checks.
- **GOTCHA**: Re-verify TICKET-5's `test_all_providers_unavailable_returns_the_service_message` still passes
  and reference it for AC #5 rather than duplicating it.
- **GOTCHA**: A test that a limiter failure **allows** the request is the one that pins D1. Without it,
  someone tightening the error handling later turns fail-open into fail-closed and nothing notices until an
  Upstash blip takes the demo down.
- **VALIDATE**: `uv run pytest tests/unit tests/integration -q`
- **SATISFIES**: AC #1, AC #2, AC #4, AC #5

### 8. UPDATE docs

- **IMPLEMENT**: Tick ADR-004 action item 4 and record what the check does. Add the limits and the fail-open
  decision to ARCHITECTURE §8's table. README gains the rate-limit and Upstash environment variables, and how
  to run the probe manually. Note that ADR-004's action items are now complete except item 3's evaluation-set
  half, which stays with TICKET-8.
- **PATTERN**: prior tickets' doc reconciliation — amend and date, never silently rewrite.
- **GOTCHA**: State the fail-open trade-off plainly in §8. It is a real weakening of the quota protection
  under a specific condition, and a reader deserves to find that in the architecture rather than in a comment.
- **VALIDATE**: `grep -n "fail open\|429\|provider-check" docs/ARCHITECTURE.md README.md | head`
- **SATISFIES**: AC #1, AC #4

---

## TESTING STRATEGY

### Unit Tests

`test_ratelimit.py` — `InMemoryLimiter` allows under the limit and denies over it; the window resets;
identifiers are independent; `retry_after` is a positive duration, never a timestamp; both limits are
enforced and the longer wait is reported; `NoLimiter` always allows; **a raising Upstash client results in
`allowed=True`** (D1).

`test_probe.py` — a healthy chain reports both providers ok; a dead secondary reports `ok=False` with the
provider named and the reason included; a hung provider times out rather than hanging; a probe failure does
not raise; the store probe reports reachability.

### Integration Tests

`test_ratelimit_api.py` — the 11th request in a minute returns 429; the body is plain language, carries a
`Retry-After` header, and names the wait; requests under the limit are unaffected and still stream;
shallow `/api/health` is never limited; `?deep=1` is; two different `x-forwarded-for` values are limited
independently; a comma-separated `x-forwarded-for` uses the leftmost entry.

`test_chat_api.py` (existing) — must still pass unchanged. It builds apps without a limiter, so the default
must be permissive.

### Edge Cases

- No Upstash credentials → `NoLimiter`, logged once, everything allowed
- Upstash raises → allowed, warning logged
- `x-forwarded-for: 1.2.3.4, 10.0.0.1, 10.0.0.2` → limited as `1.2.3.4`
- No forwarding headers and no `request.client` → the `"unknown"` bucket, still limited
- `reset` already in the past → `retry_after` is at least 1, never 0 or negative
- Minute limit hit but day limit has room → 429 with the shorter wait
- Day limit hit → 429 with a wait measured in hours, phrased in hours not seconds
- Deep health with a healthy primary and a dead secondary → 503, both states reported
- Deep health when the store is unreachable → 503 naming the store
- The probe with no keys configured → non-zero exit naming the missing variable

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

### Level 2: Unit Tests

```bash
uv run pytest tests/unit -v
```

### Level 3: Integration Tests

```bash
uv run pytest -q            # no database, no keys — must stay green

docker run -d --name medrag-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg17
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
uv run python db/migrate.py
RAG_PROFILE=fake uv run python -m ingest.run
uv run pytest -m postgres -v
```

### Level 4: Manual Validation

```bash
# Purity unaffected by two more SDKs
uv run python -c "
import sys, rag_core.pipeline, rag_core.contracts
loaded = {m.split('.')[0] for m in sys.modules}
for banned in ('fastapi','upstash_ratelimit','upstash_redis','google','groq','asyncpg'):
    assert banned not in loaded, f'{banned} pulled in by rag_core'
print('rag_core still clean')
"

# The limiter, end to end, against the running app
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
RAG_PROFILE=fake RATE_LIMIT_PER_MINUTE=3 uv run uvicorn rag_api.main:app --port 8000 &
sleep 3

for i in 1 2 3 4 5; do
  printf 'request %d -> ' "$i"
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/chat \
    -H 'content-type: application/json' -H 'x-forwarded-for: 203.0.113.9' \
    -d '{"question":"What is the metformin dose?"}'
done     # expect 200 200 200 429 429

# The 429 is readable and carries the hint
curl -s -D- -o /dev/null -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' -H 'x-forwarded-for: 203.0.113.9' \
  -d '{"question":"hi"}' | grep -i "retry-after\|HTTP/"
curl -s -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' -H 'x-forwarded-for: 203.0.113.9' \
  -d '{"question":"hi"}' | python3 -m json.tool

# A different IP is unaffected
curl -s -o /dev/null -w 'other IP -> %{http_code}\n' -X POST localhost:8000/api/chat \
  -H 'content-type: application/json' -H 'x-forwarded-for: 198.51.100.4' \
  -d '{"question":"What is the metformin dose?"}'

# Shallow health is never limited
for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w '%{http_code} ' localhost:8000/api/health \
  -H 'x-forwarded-for: 203.0.113.9'; done; echo

kill %1

# --- the probe, against real providers (needs keys) ---
export GEMINI_API_KEY=... GROQ_API_KEY=...
uv run python -m rag_api.probe          # expect exit 0, both providers named

# And with the primary key revoked, it should still report the secondary alive
GROQ_API_KEY=invalid uv run python -m rag_api.probe; echo "exit=$?"   # expect non-zero, groq named

docker rm -f medrag-pg
```

### Level 5: Additional Validation

```bash
git diff --cached | grep -inE "AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}|AQ\.[A-Za-z0-9_-]{20,}|upstash\.io" | grep -v example || echo clean
```

---

## ACCEPTANCE CRITERIA

From TICKET-6, plus the standard bar:

- [ ] **AC #1** — Exceeding the limit returns 429 with human-readable copy and a retry hint; under the limit is unaffected
- [ ] **AC #2** — Rate-limit state lives in Upstash, not process memory (`UpstashLimiter` holds no counters; `InMemoryLimiter` is documented as unfit for serverless)
- [ ] **AC #3** — Health reports a manifest mismatch as unhealthy, not a passing check with a warning *(already true from TICKET-5; re-verified here)*
- [ ] **AC #4** — The scheduled check exercises the **secondary** provider and fails loudly when it cannot
- [ ] **AC #5** — All generators unavailable → the service message, never an ungrounded answer *(already true from TICKET-5; re-verified here)*
- [ ] **AC #6** — A limiter failure allows the request (D1), pinned by a test
- [ ] All validation commands pass with zero errors
- [ ] `mypy --strict` clean; vendored-port exclusions and mypy override unchanged
- [ ] `uv run pytest` stays green with no database, no keys and no Upstash
- [ ] `rag_core` imports neither Upstash SDK
- [ ] No secret in the repository or its history

---

## COMPLETION CHECKLIST

- [ ] All 8 tasks completed in order
- [ ] Each task's `VALIDATE` passed before the next began
- [ ] Full suite green with and without a database
- [ ] Prior tickets' suites unchanged
- [ ] CI green
- [ ] Acceptance criteria all met
- [ ] The repository secrets needed by the weekly check are flagged in the report

---

## OPEN QUESTIONS / ASSUMPTIONS

**Resolved before planning** (asked and answered): D1 fail open; D2 shallow health with `?deep=1`; D3 weekly,
failed workflow is the alarm; D4 10/minute and 100/day per IP.

**Assumptions — confirm before execution if any looks wrong:**

1. **Assumed** — the limiter guards `/api/chat` and `/api/health?deep=1`, not shallow health. Limiting by cost
   rather than by URL; shallow health must stay reachable for diagnosis and a monitor would otherwise exhaust
   the daily cap by itself.
2. **Assumed** — fixed window rather than sliding. One round trip per limiter, and the 2× boundary burst it
   permits is irrelevant at these limits. Revisit if the limits tighten.
3. **Assumed** — `FailoverGenerator` gains a `providers` property rather than a `probe()` method. Health
   checking is not generation, and the port should not grow a diagnostic the request path never calls.
4. **Assumed** — the weekly check runs the probe **in CI against the providers**, not against a deployed URL.
   There is no deployment until TICKET-10.
5. **Assumed** — the `"unknown"` identifier bucket is shared across all callers with no resolvable IP. In
   practice `request.client.host` always resolves locally, so this is a degenerate case.
6. **Needs the user after merge** — `GEMINI_API_KEY` and `GROQ_API_KEY` must be added as **repository
   secrets** for the weekly workflow. Until then it fails with a message naming the missing variable. Only the
   repository owner can do this.
7. **Open, deferred** — Upstash provisioning itself is TICKET-10. Until then the limiter runs as `NoLimiter`
   locally and `InMemoryLimiter` in tests, and AC #2 rests on inspection plus the absence of local state.

---

## NOTES (open canvas)

### Why the scheduled check earns its place

ADR-004's fourth action item reads like ordinary diligence: *"Health check that exercises the secondary path
on a schedule."* It stopped being ordinary during TICKET-4.

The first time the failover chain was exercised against real providers, the secondary was **already dead** —
`gemini-2.0-flash`, pinned months earlier, retired by the vendor. The primary was healthy, every test passed,
and the demo would have worked perfectly right up until the moment Groq rate-limited a visitor, at which point
the fallback would have 404'd and the whole ADR-004 argument would have evaporated in front of the person it
was meant to impress.

The replacement pin died too — `gemini-2.5-flash`, "no longer available to new users" — which is why the
secondary is now an alias. An alias that moves is a different risk, and it is the risk this check is for.

So the check is not proving the chain works. It is proving the *far half* of the chain still exists, which is
the half nobody ever calls.

### On failing open

D1 is the uncomfortable decision in this ticket, and it should be recorded as uncomfortable rather than
obvious.

Failing open means: if Upstash is down, the demo is unprotected, and the exact scenario the PRD risk table
warns about — *"A scraper burns the daily quota → Demo dead for the day"* — becomes possible again. That is a
real hole.

The argument for it anyway: the demo already depends on Neon and two inference providers, all on free tiers
with no SLA. Adding a fourth dependency whose failure takes the whole thing down trades a *likely* failure
(Upstash hiccup, demo dead) for an *unlikely* one (scraper during that hiccup). PRD criterion 6 — that it
still works thirty days later, and the PRD's own note that this is the criterion most likely to fail — points
the same way.

What makes it defensible rather than lazy is the logging: a limiter that fails open silently is
indistinguishable from no limiter at all. Loud warnings plus the weekly check are what keep "we are currently
unprotected" from becoming a permanent, invisible state.

### Alternatives weighed and rejected

**Global middleware instead of a route dependency.** Simpler to attach, and it would need path filtering to
exempt shallow health — filtering that is easy to get subtly wrong and hard to notice, since the failure is
"health got rate-limited during an incident".

**A `probe()` method on `GenerationProvider`.** Would make probing uniform across adapters. Rejected: it grows
the port with a method the request path never calls, for the benefit of one diagnostic endpoint. `providers`
on the chain is a smaller change with a narrower blast radius.

**Sliding-window limiting.** Smoother, no boundary burst. At 10/minute the burst is 20 in a moment, which
matters not at all, and it costs extra round trips on the request path.

**Checking a deployed URL from the scheduled workflow.** More end-to-end. Rejected until TICKET-10 exists, and
even then the probe belongs against the providers — a deployment check goes red for reasons unrelated to
whether the secondary model still exists, which is the question being asked.

### Sequencing

Everything is merged; branch from `main`. After this, only TICKET-7 (frontend), TICKET-8/9 (evaluation) and
TICKET-10 (deploy) remain, and TICKET-7 is independent of all of them.

---

## AMENDMENTS

<!-- Newest at the bottom. Append entries here after this plan has been executed. -->
