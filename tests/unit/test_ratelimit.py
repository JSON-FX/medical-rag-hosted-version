"""The rate-limit seam. No network, no Upstash."""

import time
from types import SimpleNamespace

import pytest

from rag_api.ratelimit import (
    Decision,
    InMemoryLimiter,
    NoLimiter,
    UpstashLimiter,
    _worst,
    build_limiter,
)
from rag_core.config import RateLimitConfig

# --- NoLimiter -----------------------------------------------------------


async def test_no_limiter_allows_everything():
    limiter = NoLimiter()
    for _ in range(50):
        assert (await limiter.check("1.2.3.4")).allowed is True


def test_build_limiter_returns_no_limiter_without_credentials():
    """The local-development path. Distinct from a limiter failing open —
    the shell logs that limiting is disabled, so an unprotected deployment is
    a stated fact rather than something to infer."""
    assert isinstance(build_limiter(RateLimitConfig()), NoLimiter)
    assert isinstance(build_limiter(RateLimitConfig(redis_url="https://x")), NoLimiter)
    assert isinstance(build_limiter(RateLimitConfig(redis_token="t")), NoLimiter)


# --- the shared selection rule -------------------------------------------


def test_any_denial_wins_over_an_allowance():
    allowed = Decision(allowed=True, limit=10, remaining=5, retry_after=30)
    denied = Decision(allowed=False, limit=100, remaining=0, retry_after=4000)
    assert _worst([allowed, denied]).allowed is False


def test_among_denials_the_longest_wait_is_reported():
    """Telling someone to retry in 30 seconds when the daily cap is what
    stopped them is worse than useless."""
    minute = Decision(allowed=False, limit=10, remaining=0, retry_after=30)
    day = Decision(allowed=False, limit=100, remaining=0, retry_after=40_000)
    assert _worst([minute, day]).retry_after == 40_000


def test_with_no_denials_the_tightest_budget_is_reported():
    minute = Decision(allowed=True, limit=10, remaining=2, retry_after=30)
    day = Decision(allowed=True, limit=100, remaining=88, retry_after=40_000)
    assert _worst([minute, day]).remaining == 2


# --- InMemoryLimiter -----------------------------------------------------


async def test_requests_under_the_limit_are_allowed():
    limiter = InMemoryLimiter(per_minute=3, per_day=100)
    for _ in range(3):
        assert (await limiter.check("1.2.3.4")).allowed is True


async def test_the_request_over_the_limit_is_denied():
    limiter = InMemoryLimiter(per_minute=3, per_day=100)
    for _ in range(3):
        await limiter.check("1.2.3.4")
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed is False
    assert decision.remaining == 0


async def test_identifiers_are_limited_independently():
    """Otherwise one busy visitor throttles everyone, which presents as an
    outage rather than as a limit."""
    limiter = InMemoryLimiter(per_minute=2, per_day=100)
    for _ in range(2):
        await limiter.check("1.1.1.1")
    assert (await limiter.check("1.1.1.1")).allowed is False
    assert (await limiter.check("2.2.2.2")).allowed is True


async def test_the_daily_cap_denies_even_when_the_minute_has_room():
    """The minute limit alone still permits 14,400 requests a day; the daily
    cap is what actually protects the quota."""
    limiter = InMemoryLimiter(per_minute=1000, per_day=3)
    for _ in range(3):
        assert (await limiter.check("1.2.3.4")).allowed is True
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed is False
    assert decision.limit == 3


async def test_retry_after_is_a_duration_not_a_timestamp():
    """Upstash returns `reset` as a unix timestamp. Handing that to a client as
    Retry-After would tell it to wait fifty-five years."""
    limiter = InMemoryLimiter(per_minute=1, per_day=100)
    await limiter.check("1.2.3.4")
    decision = await limiter.check("1.2.3.4")
    assert 0 < decision.retry_after <= 60, decision.retry_after
    assert decision.retry_after < time.time() / 2


async def test_retry_after_is_never_zero_or_negative():
    limiter = InMemoryLimiter(per_minute=1, per_day=100)
    for _ in range(5):
        decision = await limiter.check("1.2.3.4")
        assert decision.retry_after >= 1


# --- UpstashLimiter ------------------------------------------------------


class FakeRatelimitResponse(SimpleNamespace):
    pass


class FakeUpstashClient:
    """Stands in for upstash_redis; the Ratelimit objects are replaced wholesale
    in these tests, so this only needs to be constructible."""


def upstash_with(responses, raises=None):
    """A UpstashLimiter whose two Ratelimit instances are replaced by fakes."""

    class FakeLimiter:
        def __init__(self, response):
            self._response = response

        async def limit(self, identifier):
            if raises is not None:
                raise raises
            return self._response

    limiter = UpstashLimiter.__new__(UpstashLimiter)
    limiter._limiters = [FakeLimiter(r) for r in responses]
    return limiter


async def test_upstash_allows_when_both_windows_allow():
    limiter = upstash_with(
        [
            FakeRatelimitResponse(allowed=True, limit=10, remaining=9, reset=time.time() + 30),
            FakeRatelimitResponse(allowed=True, limit=100, remaining=99, reset=time.time() + 4000),
        ]
    )
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed is True
    assert decision.remaining == 9, "the tightest budget is what a client should see"


async def test_upstash_denies_when_either_window_denies():
    limiter = upstash_with(
        [
            FakeRatelimitResponse(allowed=True, limit=10, remaining=4, reset=time.time() + 30),
            FakeRatelimitResponse(allowed=False, limit=100, remaining=0, reset=time.time() + 4000),
        ]
    )
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed is False
    assert decision.retry_after > 3000, "the daily wait, not the minute wait"


async def test_upstash_converts_reset_timestamps_into_durations():
    limiter = upstash_with(
        [
            FakeRatelimitResponse(allowed=False, limit=10, remaining=0, reset=time.time() + 42),
            FakeRatelimitResponse(allowed=True, limit=100, remaining=50, reset=time.time() + 4000),
        ]
    )
    decision = await limiter.check("1.2.3.4")
    assert 40 <= decision.retry_after <= 43


async def test_a_limiter_failure_allows_the_request(caplog):
    """D1, pinned.

    Rate limiting is a third free tier with no SLA, and a fourth dependency
    whose failure takes the demo down trades a likely failure for an unlikely
    one. Without this test someone tightening the error handling later turns
    fail-open into fail-closed, and nothing notices until an Upstash blip takes
    the whole thing offline.
    """
    limiter = upstash_with(
        [FakeRatelimitResponse(allowed=True, limit=10, remaining=9, reset=time.time() + 30)],
        raises=ConnectionError("upstash unreachable"),
    )
    with caplog.at_level("WARNING"):
        decision = await limiter.check("1.2.3.4")

    assert decision.allowed is True
    assert any("allowing the request" in r.message for r in caplog.records), (
        "a limiter failing open silently is indistinguishable from no limiter at all"
    )


@pytest.mark.parametrize("failure", [ConnectionError("down"), TimeoutError(), ValueError("weird")])
async def test_every_kind_of_limiter_failure_fails_open(failure):
    limiter = upstash_with(
        [FakeRatelimitResponse(allowed=True, limit=1, remaining=0, reset=0)], raises=failure
    )
    assert (await limiter.check("1.2.3.4")).allowed is True


def test_the_upstash_limiter_holds_no_local_counter_state():
    """AC #2. The limit must survive across serverless invocations, so nothing
    about who has used what may live in this process.

    InMemoryLimiter is the counter-example and is documented as unfit for
    production; this asserts the real one keeps only its two Ratelimit handles.
    """
    limiter = upstash_with(
        [FakeRatelimitResponse(allowed=True, limit=10, remaining=9, reset=time.time() + 30)]
    )
    assert set(vars(limiter)) == {"_limiters"}
