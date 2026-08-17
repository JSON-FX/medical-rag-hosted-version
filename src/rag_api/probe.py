"""Exercising the providers deliberately (ADR-004 action item 4).

    uv run python -m rag_api.probe

The failover secondary is called only when the primary fails, which — if the
demo is healthy — may be never, right up until the moment it is needed.

This is not a hypothetical concern. The first time the chain was exercised
against real providers the secondary was **already dead**: a pinned
`gemini-2.0-flash` the vendor had retired. The primary was healthy, every test
passed, and the demo would have worked perfectly until the first rate limit
sent a visitor to a 404. ADR-004 predicted it — "if the secondary is never
exercised in ninety days, test it deliberately rather than assuming it works" —
and this is that test.

Run weekly in CI against the providers themselves, not against a deployed URL:
a deployment check goes red for reasons unrelated to whether the secondary
model still exists, which is the only question being asked here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

from rag_adapters.profile import Profile, build_profile
from rag_core.config import load_config
from rag_core.ports import GenerationProvider

logger = logging.getLogger(__name__)

# A probe should cost almost nothing and still prove the model answers.
PROBE_MESSAGES = [
    {"role": "system", "content": "Reply with a single digit and nothing else."},
    {"role": "user", "content": "What is 2 + 2?"},
]
TIMEOUT_S = 20.0

# One retry, because a free tier returns transient 503s under load and the
# question being asked is "does this model still exist", not "is it busy right
# now". Observed during implementation: gemini-flash-latest returned
# "currently experiencing high demand" on the first call and answered on the
# second. A retired model 404s every time, so a retry sharpens the signal
# rather than hiding a real death — and a weekly check that goes red for a
# transient blip is a check people learn to ignore.
RETRY_DELAY_S = 2.0


@dataclass(frozen=True)
class ProbeResult:
    name: str
    model_id: str
    ok: bool
    detail: str
    latency_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "model_id": self.model_id,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
        }


def _generators(profile: Profile) -> list[tuple[str, GenerationProvider]]:
    """Each provider individually, unwrapping the failover chain if present."""
    generator = profile.generator
    providers = getattr(generator, "providers", None)
    if providers is None:
        return [("generator", generator)]
    names = ("primary", "secondary")
    return list(zip(names, providers, strict=True))


async def _probe_one(name: str, provider: GenerationProvider) -> ProbeResult:
    result = await _attempt(name, provider)
    if result.ok:
        return result
    await asyncio.sleep(RETRY_DELAY_S)
    return await _attempt(name, provider)


async def _attempt(name: str, provider: GenerationProvider) -> ProbeResult:
    started = time.perf_counter()
    try:

        async def run() -> str:
            stream = provider.stream(list(PROBE_MESSAGES))
            return "".join([token async for token in stream])

        text = await asyncio.wait_for(run(), timeout=TIMEOUT_S)
        elapsed = (time.perf_counter() - started) * 1000
        if not text.strip():
            return ProbeResult(name, provider.model_id, False, "returned no tokens", elapsed)
        return ProbeResult(name, provider.model_id, True, text.strip()[:40], elapsed)
    except TimeoutError:
        elapsed = (time.perf_counter() - started) * 1000
        return ProbeResult(name, provider.model_id, False, f"timed out after {TIMEOUT_S}s", elapsed)
    except Exception as exc:  # noqa: BLE001 - the point is to report, not to become the failure
        # A probe must NAME what failed and why. The local build's health
        # endpoint records the lesson: a check reporting a capability present
        # when it is not "converts a clear failure into an unexplained one
        # later". "gemini-2.0-flash is no longer available" is the message that
        # would have saved a ticket.
        elapsed = (time.perf_counter() - started) * 1000
        return ProbeResult(
            name, provider.model_id, False, f"{type(exc).__name__}: {exc}"[:200], elapsed
        )


async def probe_generators(profile: Profile) -> list[ProbeResult]:
    """Every generation provider, individually. Never raises."""
    return list(await asyncio.gather(*(_probe_one(name, p) for name, p in _generators(profile))))


async def probe_store(profile: Profile) -> ProbeResult:
    """Is the index reachable and populated."""
    started = time.perf_counter()
    try:
        count = await asyncio.wait_for(profile.dense.count(), timeout=TIMEOUT_S)
        elapsed = (time.perf_counter() - started) * 1000
        return ProbeResult("dense_store", "postgres", count > 0, f"{count} chunks", elapsed)
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        elapsed = (time.perf_counter() - started) * 1000
        return ProbeResult(
            "dense_store", "postgres", False, f"{type(exc).__name__}: {exc}"[:200], elapsed
        )


async def main() -> int:
    """Weekly CI entry point. Non-zero if any provider is unreachable."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config()

    missing = [
        name
        for name, value in (
            ("GEMINI_API_KEY", cfg.providers.gemini_api_key),
            ("GROQ_API_KEY", cfg.providers.groq_api_key),
        )
        if not value
    ]
    if missing:
        # Deliberately an error rather than a skip. A check that skips itself
        # when unconfigured is a check that silently never runs.
        print(f"cannot probe: {', '.join(missing)} not set", file=sys.stderr)
        print("add them as repository secrets for the scheduled workflow", file=sys.stderr)
        return 1

    profile = build_profile(load_config(env={**os.environ, "RAG_PROFILE": "hosted"}))
    results = await probe_generators(profile)

    for result in results:
        status = "ok  " if result.ok else "FAIL"
        print(
            f"  {status} {result.name:10} {result.model_id:28} "
            f"{result.latency_ms:7.0f}ms  {result.detail}"
        )

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} providers unreachable", file=sys.stderr)
        return 1
    print(f"\nall {len(results)} generation providers reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
