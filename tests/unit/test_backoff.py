import pytest

from rag_adapters._backoff import retry_with_backoff


class Boom(Exception):
    pass


class NotRetryable(Exception):
    pass


def recording_sleeper():
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    return delays, sleep


async def test_a_successful_call_is_not_retried():
    calls = []

    async def op():
        calls.append(1)
        return "ok"

    assert await retry_with_backoff(op, retry_on=(Boom,)) == "ok"
    assert len(calls) == 1


async def test_a_retryable_failure_is_retried_and_can_succeed():
    calls = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise Boom("rate limited")
        return "ok"

    _, sleep = recording_sleeper()
    assert await retry_with_backoff(op, retry_on=(Boom,), sleep=sleep) == "ok"
    assert len(calls) == 3


async def test_delays_double():
    async def op():
        raise Boom("always")

    delays, sleep = recording_sleeper()
    with pytest.raises(Boom):
        await retry_with_backoff(op, retry_on=(Boom,), attempts=4, base_delay=1.0, sleep=sleep)
    assert delays == [1.0, 2.0, 4.0]


async def test_exhausting_attempts_reraises_the_last_failure():
    async def op():
        raise Boom("still failing")

    _, sleep = recording_sleeper()
    with pytest.raises(Boom, match="still failing"):
        await retry_with_backoff(op, retry_on=(Boom,), attempts=2, sleep=sleep)


async def test_an_excluded_exception_is_not_retried():
    """retry_on is explicit precisely so the embedder's count and dimension
    guards are not retried — they cannot succeed on a second attempt, and
    retrying them would triple the time to a clear error."""
    calls = []

    async def op():
        calls.append(1)
        raise NotRetryable("bad payload")

    _, sleep = recording_sleeper()
    with pytest.raises(NotRetryable):
        await retry_with_backoff(op, retry_on=(Boom,), sleep=sleep)
    assert len(calls) == 1


async def test_no_sleep_happens_after_the_final_attempt():
    """Sleeping then giving up wastes the delay for nothing."""

    async def op():
        raise Boom("always")

    delays, sleep = recording_sleeper()
    with pytest.raises(Boom):
        await retry_with_backoff(op, retry_on=(Boom,), attempts=2, sleep=sleep)
    assert len(delays) == 1


async def test_zero_attempts_is_a_programming_error():
    async def op():
        return "unreachable"

    with pytest.raises(ValueError, match="at least 1"):
        await retry_with_backoff(op, retry_on=(Boom,), attempts=0)
