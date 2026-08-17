"""POST /api/chat — the streaming answer endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import index_unavailable, rate_limited
from .ratelimit import NoLimiter, RateLimiter
from .state import AppState
from .streaming import answer_stream

router = APIRouter()

NDJSON = "application/x-ndjson"


def client_identifier(request: Request) -> str:
    """Who is being limited.

    `x-forwarded-for` is a COMMA-SEPARATED LIST and the leftmost entry is the
    client; everything after it is a proxy. Taking the last entry would bucket
    every visitor behind the edge network into one identifier — rate-limiting
    the entire world together, which presents as an outage rather than as a
    limit.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    # Degenerate: no forwarding headers and no peer. Shared bucket rather than
    # unlimited — the point is that nothing bypasses the limit by being
    # unidentifiable.
    return "unknown"


def get_limiter(request: Request) -> RateLimiter:
    return getattr(request.app.state, "limiter", None) or NoLimiter()


async def enforce_rate_limit(request: Request) -> JSONResponse | None:
    """Guards the paths that COST something.

    Deliberately not global middleware over every route. PRD F15 says "public
    endpoints", but cost is what matters: shallow /api/health is free and must
    stay reachable for diagnosis, and a monitor polling it once a minute would
    exhaust a 100/day cap on its own. Limiting by cost rather than by URL is
    the decision.
    """
    decision = await get_limiter(request).check(client_identifier(request))
    if decision.allowed:
        return None
    failure = rate_limited(decision.retry_after)
    return JSONResponse(
        status_code=failure.status,
        content={"code": failure.code, "message": failure.message},
        headers={"Retry-After": str(decision.retry_after)},
    )


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"code": "invalid_request", "message": message})


@router.post("/api/chat")
async def chat(request: Request) -> Any:
    limited = await enforce_rate_limit(request)
    if limited is not None:
        return limited

    state: AppState = request.app.state.rag

    if not state.serviceable:
        # Refusing every request IS "refuse to serve" (ARCHITECTURE.md §5).
        # Doing it here rather than by failing to start keeps the reason
        # readable from a browser instead of only from platform logs.
        failure = index_unavailable(state.reason or "this deployment cannot serve queries")
        return JSONResponse(
            status_code=failure.status,
            content={"code": failure.code, "message": failure.message},
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure is the same 400
        return _bad_request("body must be valid JSON")

    if not isinstance(payload, dict):
        return _bad_request("body must be a JSON object")

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return _bad_request("question must be a non-empty string")

    return StreamingResponse(
        answer_stream(question.strip(), state),
        media_type=NDJSON,
        headers={
            "Cache-Control": "no-cache",
            # Tells an intervening proxy not to buffer. Without it the whole
            # response can be held back and delivered at once, which passes
            # every functional test and destroys the streaming demo.
            "X-Accel-Buffering": "no",
        },
    )
