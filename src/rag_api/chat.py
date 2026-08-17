"""POST /api/chat — the streaming answer endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import index_unavailable
from .state import AppState
from .streaming import answer_stream

router = APIRouter()

NDJSON = "application/x-ndjson"


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"code": "invalid_request", "message": message})


@router.post("/api/chat")
async def chat(request: Request) -> Any:
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
