"""Retry with exponential backoff, for the offline path only.

Used by the embedder, whose caller is the ingestion job. A full corpus pass on
a free quota may need to span more than one day (ARCHITECTURE.md §6), so
sleeping through a rate limit costs nothing there.

Deliberately NOT used by the failover chain. ADR-004 specifies "one retry
before falling through", and that runs on the request path inside a 2.5s
time-to-first-token budget — where a two-second sleep is most of the budget.
The chain retries immediately and moves on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

Sleeper = Callable[[float], Awaitable[None]]


async def retry_with_backoff[T](
    operation: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Sleeper | None = None,
) -> T:
    """Run `operation`, retrying only `retry_on` with doubling delays.

    `retry_on` is an explicit tuple rather than a catch-all because retrying
    everything would retry the embedder's count and dimension guards — which
    cannot succeed on a second attempt, and would triple the time it takes to
    surface a clear error.

    `sleep` is injectable so tests assert the delays without waiting them.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    sleeper = sleep or asyncio.sleep

    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except retry_on as exc:
            last = exc
            if attempt == attempts - 1:
                break
            await sleeper(base_delay * (2**attempt))

    assert last is not None  # only reachable after a caught exception
    raise last
