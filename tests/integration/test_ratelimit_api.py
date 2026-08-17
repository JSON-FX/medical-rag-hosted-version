"""The limiter attached to the endpoints. Over InMemoryLimiter, no Upstash."""

import httpx
import pytest

from rag_api.chat import client_identifier
from rag_api.main import create_app
from rag_api.ratelimit import InMemoryLimiter, NoLimiter

GROUNDED = "What is the metformin starting dose?"


def app_with(limiter, state):
    app = create_app()
    app.state.rag = state
    app.state.limiter = limiter
    return app


async def post(app, ip="203.0.113.9", question=GROUNDED):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/chat", json={"question": question}, headers={"x-forwarded-for": ip}
        )


async def get_health(app, ip="203.0.113.9", deep=False):
    transport = httpx.ASGITransport(app=app)
    url = "/api/health?deep=1" if deep else "/api/health"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url, headers={"x-forwarded-for": ip})


# --- identifying the client ----------------------------------------------


def identifier_for(headers=None, peer="127.0.0.1"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 1234) if peer else None,
    }
    return client_identifier(Request(scope))


def test_the_leftmost_forwarded_address_is_the_client():
    """x-forwarded-for is a comma-separated list and the leftmost entry is the
    client; the rest are proxies. Taking the last one buckets every visitor
    behind the edge network into a single identifier — rate-limiting the whole
    world together, which presents as an outage."""
    assert identifier_for({"x-forwarded-for": "203.0.113.9, 10.0.0.1, 10.0.0.2"}) == "203.0.113.9"


def test_forwarded_addresses_are_stripped():
    assert identifier_for({"x-forwarded-for": "  203.0.113.9 , 10.0.0.1"}) == "203.0.113.9"


def test_x_real_ip_is_the_fallback():
    assert identifier_for({"x-real-ip": "198.51.100.7"}) == "198.51.100.7"


def test_the_peer_address_is_used_when_no_headers_are_present():
    assert identifier_for({}, peer="192.0.2.5") == "192.0.2.5"


def test_an_unidentifiable_caller_shares_one_bucket_rather_than_bypassing():
    """Nothing gets unlimited access by being unidentifiable."""
    assert identifier_for({}, peer=None) == "unknown"


# --- the limit on /api/chat ----------------------------------------------


async def test_requests_under_the_limit_stream_normally(state):
    app = app_with(InMemoryLimiter(per_minute=5, per_day=100), state)
    for _ in range(5):
        response = await post(app)
        assert response.status_code == 200
        assert "meta" in response.text


async def test_the_request_over_the_limit_returns_429(state):
    app = app_with(InMemoryLimiter(per_minute=2, per_day=100), state)
    for _ in range(2):
        assert (await post(app)).status_code == 200
    response = await post(app)
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


async def test_the_429_carries_a_retry_after_header(state):
    app = app_with(InMemoryLimiter(per_minute=1, per_day=100), state)
    await post(app)
    response = await post(app)
    assert "retry-after" in response.headers
    assert 1 <= int(response.headers["retry-after"]) <= 60


async def test_the_429_body_is_plain_language_naming_the_wait(state):
    """PRD F15: "returning a clear message rather than a raw 429"."""
    app = app_with(InMemoryLimiter(per_minute=1, per_day=100), state)
    await post(app)
    message = (await post(app)).json()["message"]
    assert "too many requests" in message.lower()
    assert "second" in message or "minute" in message or "hour" in message
    assert "429" not in message


async def test_a_daily_cap_is_phrased_in_hours_not_seconds(state):
    """ "Retry in 47,000 seconds" is not a message a human acts on."""
    app = app_with(InMemoryLimiter(per_minute=1000, per_day=1), state)
    await post(app)
    message = (await post(app)).json()["message"]
    assert "hour" in message


async def test_different_addresses_are_limited_independently(state):
    app = app_with(InMemoryLimiter(per_minute=1, per_day=100), state)
    assert (await post(app, ip="203.0.113.9")).status_code == 200
    assert (await post(app, ip="203.0.113.9")).status_code == 429
    assert (await post(app, ip="198.51.100.4")).status_code == 200


async def test_an_app_without_a_limiter_is_permissive(state):
    """Every prior ticket's tests build apps with no limiter; the default must
    not start rejecting them."""
    app = create_app()
    app.state.rag = state
    for _ in range(20):
        assert (await post(app)).status_code == 200


async def test_no_limiter_is_the_explicit_permissive_case(state):
    app = app_with(NoLimiter(), state)
    for _ in range(20):
        assert (await post(app)).status_code == 200


# --- health ---------------------------------------------------------------


async def test_shallow_health_is_never_rate_limited(state):
    """It costs nothing and must stay reachable for diagnosis. A monitor
    polling it once a minute would exhaust a 100/day cap on its own."""
    app = app_with(InMemoryLimiter(per_minute=1, per_day=2), state)
    for _ in range(10):
        assert (await get_health(app)).status_code in (200, 503)


async def test_deep_health_is_rate_limited(state):
    """It costs a generation per provider, so it is limited like /api/chat."""
    app = app_with(InMemoryLimiter(per_minute=1, per_day=100), state)
    first = await get_health(app, deep=True)
    assert first.status_code in (200, 503)
    assert (await get_health(app, deep=True)).status_code == 429


@pytest.mark.parametrize("flag", ["1", "true", "yes"])
async def test_deep_is_recognised_in_its_usual_spellings(state, flag):
    app = app_with(NoLimiter(), state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/health?deep={flag}")
    assert "generators" in response.json()
