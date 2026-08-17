"""GET /api/health — is this deployment able to serve, and why not."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .chat import enforce_rate_limit
from .probe import probe_generators, probe_store
from .state import AppState

router = APIRouter()


@router.get("/api/health")
async def health(request: Request) -> Any:
    """Reports serviceability and what built the index.

    Deliberately says WHY when unserviceable. A health check that reports a
    capability present when it is not converts a clear failure into an
    unexplained one later — the lesson the local build recorded in its own
    health endpoint after a model-tag mismatch surfaced as a 404 from the chat
    endpoint instead.

    Shallow by default and free, so a monitor can poll it without burning the
    quota the rate limiter exists to protect. `?deep=1` additionally probes
    each generation provider INDIVIDUALLY — which is ADR-004's fourth action
    item, and the question whose absence let a retired secondary model go
    unnoticed until TICKET-4.
    """
    state: AppState = request.app.state.rag
    manifest = state.manifest
    deep = request.query_params.get("deep") in {"1", "true", "yes"}

    body: dict[str, Any] = {
        "serviceable": state.serviceable,
        "reason": state.reason,
        "profile": state.profile.name,
        "embedder": {
            "configured": state.profile.embedder.model_id,
            "dimension": state.profile.embedder.dimension,
        },
        "index": (
            {
                "built_by": manifest.embedding_model_id,
                "dimension": manifest.dimension,
                "ingested_at": manifest.ingested_at.isoformat(),
            }
            if manifest
            else None
        ),
    }

    healthy = state.serviceable

    if deep:
        # Deep costs a generation per provider, so it is rate limited like the
        # chat endpoint. Shallow is not — it must stay reachable for diagnosis.
        limited = await enforce_rate_limit(request)
        if limited is not None:
            return limited

        store = await probe_store(state.profile)
        generators = await probe_generators(state.profile)
        body["store"] = store.as_dict()
        body["generators"] = [g.as_dict() for g in generators]
        # ANY failed probe is unhealthy, including a healthy primary with a
        # dead secondary. A working demo with a dead fallback is not healthy;
        # it is one rate limit away from broken, and that is exactly the state
        # that went unnoticed.
        healthy = healthy and store.ok and all(g.ok for g in generators)

    return JSONResponse(status_code=200 if healthy else 503, content=body)
