"""Per-IP rate limiting (PRD F15).

The PRD's risk table rates a scraper burning the daily quota as *Medium*
likelihood with "Demo dead for the day" as the impact, and names the
mitigation: "IP rate limit in front of every endpoint from day one".

FAILING OPEN, AND WHY IT IS UNCOMFORTABLE
-----------------------------------------
If Upstash is unreachable this allows the request. That is a real hole: during
an Upstash outage the demo is unprotected and the risk above becomes live
again.

The argument for it anyway is that the demo already depends on Neon and two
inference providers, all free tiers with no SLA. A fourth dependency whose
failure takes the whole thing down trades a *likely* failure (Upstash hiccup,
demo dead) for an *unlikely* one (a scraper arriving during that hiccup). PRD
success criterion 6 — that it still works thirty days later, which the PRD
itself calls the criterion most likely to fail — points the same way.

What makes that defensible rather than lazy is the logging. A limiter that
fails open silently is indistinguishable from no limiter at all, so every
failure is logged at warning, and the weekly provider check is what stops
"currently unprotected" becoming a permanent invisible state.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

from rag_core.config import RateLimitConfig

logger = logging.getLogger(__name__)

# Fixed window rather than sliding: one round trip per limiter, and the 2x
# burst it permits at a window boundary is irrelevant at 10 requests a minute.
# Revisit if the limits ever tighten enough for that burst to matter.
MINUTE = 60
DAY = 60 * 60 * 24


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    # SECONDS TO WAIT, not a timestamp. Upstash returns `reset` as a unix
    # timestamp in seconds; handing that to a client as Retry-After would tell
    # it to wait fifty-five years.
    retry_after: int


def _retry_after(reset_at: float) -> int:
    """Convert an absolute reset time into a duration, never below one second."""
    return max(1, math.ceil(reset_at - time.time()))


def _worst(decisions: list[Decision]) -> Decision:
    """The decision that actually governs.

    Any denial wins, and among denials the longest wait — telling someone to
    retry in 30 seconds when it was the daily cap that stopped them is worse
    than useless. With no denials, report the tightest remaining budget.
    """
    denials = [d for d in decisions if not d.allowed]
    if denials:
        return max(denials, key=lambda d: d.retry_after)
    return min(decisions, key=lambda d: d.remaining)


class RateLimiter(Protocol):
    async def check(self, identifier: str) -> Decision: ...


class NoLimiter:
    """Allows everything. Used when no credentials are configured.

    Distinct from a limiter that happens to be failing open: the shell logs
    once at startup that limiting is disabled, so an unprotected deployment is
    a stated fact rather than something to be inferred.
    """

    async def check(self, identifier: str) -> Decision:
        return Decision(allowed=True, limit=0, remaining=0, retry_after=0)


class InMemoryLimiter:
    """Fixed windows in a dict. For tests and local development ONLY.

    Per-process, and therefore useless on serverless — every invocation would
    start with an empty counter, which is precisely the failure the Upstash
    implementation exists to avoid. Never promote this to production; that is
    what AC #2 is about.
    """

    def __init__(self, per_minute: int = 10, per_day: int = 100) -> None:
        self._limits = ((per_minute, MINUTE), (per_day, DAY))
        self._hits: dict[tuple[str, int], list[float]] = {}

    async def check(self, identifier: str) -> Decision:
        now = time.time()
        decisions: list[Decision] = []

        for max_requests, window in self._limits:
            key = (identifier, window)
            start = math.floor(now / window) * window
            hits = [t for t in self._hits.get(key, []) if t >= start]
            hits.append(now)
            self._hits[key] = hits

            used = len(hits)
            decisions.append(
                Decision(
                    allowed=used <= max_requests,
                    limit=max_requests,
                    remaining=max(0, max_requests - used),
                    retry_after=_retry_after(start + window),
                )
            )

        return _worst(decisions)


class UpstashLimiter:
    """Two fixed windows in Upstash Redis, reached over HTTP.

    Connectionless by design, which is what makes it work on a platform with no
    long-lived process — there is nothing to pool and nothing to keep warm.

    Holds NO local counter state. The only thing distinguishing one caller from
    another is the identifier passed in, so the limit survives across
    invocations and across instances (AC #2).
    """

    def __init__(self, cfg: RateLimitConfig, client: Any | None = None) -> None:
        from upstash_ratelimit import FixedWindow
        from upstash_ratelimit.asyncio import Ratelimit
        from upstash_redis.asyncio import Redis

        redis = client or Redis(url=cfg.redis_url, token=cfg.redis_token)
        # The asyncio variant, not the sync one: the sync client issues
        # blocking HTTP on the event loop, stalling every concurrent request
        # behind it.
        self._limiters = [
            Ratelimit(
                redis=redis,
                limiter=FixedWindow(max_requests=cfg.per_minute, window=MINUTE),
                prefix="medrag:minute",
            ),
            Ratelimit(
                redis=redis,
                limiter=FixedWindow(max_requests=cfg.per_day, window=DAY),
                prefix="medrag:day",
            ),
        ]

    async def check(self, identifier: str) -> Decision:
        decisions: list[Decision] = []

        for limiter in self._limiters:
            try:
                response = await limiter.limit(identifier)
            except Exception as exc:  # noqa: BLE001 - fail open, see the module docstring
                # Loud, because a limiter failing open silently is
                # indistinguishable from having no limiter at all.
                logger.warning(
                    "rate limiter unavailable, allowing the request: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return Decision(allowed=True, limit=0, remaining=0, retry_after=0)

            decisions.append(
                Decision(
                    allowed=response.allowed,
                    limit=response.limit,
                    remaining=response.remaining,
                    retry_after=_retry_after(response.reset),
                )
            )

        return _worst(decisions)


def build_limiter(cfg: RateLimitConfig) -> RateLimiter:
    """Resolve the configured limiter, or none at all."""
    if not cfg.redis_url or not cfg.redis_token:
        logger.warning(
            "rate limiting is DISABLED: no Upstash credentials configured. "
            "This deployment is unprotected against quota exhaustion."
        )
        return NoLimiter()
    return UpstashLimiter(cfg)
