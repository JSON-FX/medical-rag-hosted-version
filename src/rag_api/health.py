"""GET /api/health — is this deployment able to serve, and why not."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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

    ADR-004's scheduled exercise of the secondary provider is TICKET-6; this
    does not call out to any provider.
    """
    state: AppState = request.app.state.rag
    manifest = state.manifest

    body = {
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
    return JSONResponse(status_code=200 if state.serviceable else 503, content=body)
