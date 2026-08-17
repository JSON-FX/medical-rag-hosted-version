# Implementation Report — TICKET-6: Rate limiting, health, and the failure surface

**Plan**: `.claude/plans/rate-limiting-health-and-failure-surface.md`
**Branch**: `feature/rate-limiting-health-and-failure-surface`
**Status**: COMPLETE

## Summary

A per-IP limit in front of the paths that cost something, a health endpoint that can be asked how deep to
look, and a weekly check that exercises the failover secondary on purpose.

Verified live: the probe found Gemini returning a transient 503 on first call and succeeding on retry —
which is exactly the kind of false alarm a weekly red badge would train people to ignore, and led to the one
unplanned change in this ticket.

## Tasks completed

| # | Task | Files |
|---|---|---|
| 1 | Deps + `RateLimitConfig` | `pyproject.toml`, `src/rag_core/config.py`, `.env.example` (UPDATE) |
| 2 | The limiter seam | `src/rag_api/ratelimit.py` (CREATE) |
| 3 | Wiring to the expensive paths | `src/rag_api/{chat,errors,main}.py` (UPDATE) |
| 4 | `FailoverGenerator.providers` | `src/rag_adapters/failover.py` (UPDATE) |
| 5 | The probe + deep health | `src/rag_api/probe.py` (CREATE), `health.py` (UPDATE) |
| 6 | The weekly workflow | `.github/workflows/provider-check.yml` (CREATE) |
| 7 | Tests | `tests/unit/test_{ratelimit,probe}.py`, `tests/integration/test_ratelimit_api.py` (CREATE) |
| 8 | Docs | `docs/ARCHITECTURE.md`, `README.md` (UPDATE) |

## Tests added

**481 tests: 408 without a database, 65 Postgres, 8 live. All passing.**

| File | Tests | Note |
|---|---|---|
| `tests/unit/test_ratelimit.py` | 19 | Both windows, the selection rule, durations-not-timestamps, and **fail-open pinned** |
| `tests/integration/test_ratelimit_api.py` | 18 | 429 shape and copy, `Retry-After`, per-IP independence, `x-forwarded-for` parsing, health exemption |
| `tests/unit/test_probe.py` | 11 | Both chain halves probed individually, dead-secondary reporting, timeouts, store reachability |

## Validation results

| Check | Result |
|---|---|
| Default suite (no database, no keys, no Upstash) | **408 passed**, 2.34s |
| Postgres suite (all tickets) | **65 passed**, no regressions |
| Types | `mypy --strict` clean, 35 source files |
| Lint + format | `ruff` clean, 71 files |
| Purity | `rag_core` pulls in neither Upstash SDK |

**Live, against real providers:**

```
probe          ok   primary    llama-3.3-70b-versatile   590ms  "4"
               ok   secondary  gemini-flash-latest      3455ms  "4"   exit=0

health         serviceable: True | index built by: gemini-embedding-001        200
health?deep=1  store: True (71 chunks) · primary: True · secondary: True       200
```

No-credentials path logs `rate limiting is DISABLED: ... This deployment is unprotected against quota
exhaustion.` and allows everything, as designed.

AC #1, #2, #4 and #6 are new here. **AC #3 and AC #5 were already satisfied by TICKET-5** and were re-verified
rather than rebuilt.

## Deviations from the plan

**1. The probe retries once on failure.** Not in the plan, and found by running it: `gemini-flash-latest`
returned `503 UNAVAILABLE — currently experiencing high demand` on the first call and answered on the second.
A weekly check that goes red for a transient blip is a check people learn to ignore, which would defeat the
whole point. A retired model 404s every time, so one retry sharpens the signal rather than hiding a real
death. Recorded in the module with the observation that prompted it.

**2. Rate limiting runs *before* the serviceability check** in `chat.py`. An unserviceable deployment being
hammered should still be limited — otherwise the cheapest way to bypass the limiter is to attack a broken
deployment.

**3. The shared `state` fixture lives in `tests/integration/conftest.py`.** The plan implied reusing
`test_chat_api.py`'s `build_state`; importing helpers across test modules is worse than a fixture, so the
fixture is in conftest where both suites can reach it without an import.

**4. `_worst()` is a shared free function** rather than duplicated selection logic in each limiter. My first
draft inlined it in `InMemoryLimiter` and the branching was wrong; extracting it made both implementations
provably identical and gave the rule its own three tests.

## Issues encountered

**Gemini's free tier returns transient 503s.** Seen twice — once during TICKET-4's failover check and again
here. It is the reason for deviation 1, and worth knowing before TICKET-10: an availability target that
assumes every Gemini call succeeds first time is optimistic.

**The local-development gap from TICKET-5 is still open, and it bit the manual validation.** There is no
profile pairing Postgres storage with a fake embedder, so `RAG_PROFILE=fake` serves an in-memory store with no
manifest and correctly 503s. Demonstrating the limiter end to end therefore required re-ingesting with real
Gemini. TICKET-7 will want this fixed; `register_profile` is the seam.

**The 429 path is proven through the real ASGI app but not against Upstash itself.** No credentials exist
until TICKET-10 provisions them. The 18 integration tests exercise the full request path over
`InMemoryLimiter`, and `UpstashLimiter` is covered by unit tests with a faked client — including one asserting
it holds no local counter state, which is what AC #2 actually rests on. Said plainly rather than claiming
end-to-end coverage that does not exist yet.

## Outstanding — needs the repository owner

**The weekly workflow needs `GEMINI_API_KEY` and `GROQ_API_KEY` as repository secrets.** Only you can add
them (Settings → Secrets and variables → Actions). Until then the scheduled run fails with a message naming
the missing variable — deliberate, because a check that skips itself when unconfigured is a check that
silently never runs, but it does mean a red weekly run until they are set.

The first scheduled run happens after merge: `schedule` only fires on the default branch. `workflow_dispatch`
is enabled so it can be triggered by hand before then.

## Ready for the next step

All work committed. CI needs a push.

Next: `piv-create-pr`. TICKET-7 (frontend) is unblocked and independent of everything remaining.
