"""The answer stream: ordering, timing, and where each frame goes.

Frame order, and why each position is what it is:

    meta     after retrieval, carrying the gate decision and retrieval latency
    token*   the answer, or the decline copy
    sources  ONLY after both gates have cleared
    done     timings, token count, and which provider served

Two of those orderings were paid for once already in the local build and are
restated here because both fail invisibly.

RETRIEVAL RUNS INSIDE THE GENERATOR. Outside it, a provider failure happens
before headers are committed and the framework can still return a clean 503 —
which sounds better, and means the same failure behaves differently depending
on when it occurs. Inside, every failure after the response begins is an
`error` frame, and the client has exactly one shape to handle.

SOURCES COME LAST-BUT-ONE. Emitting them when the stage-1 gate passes would
show citations for an answer the model then declines to give — sources for a
refusal.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from rag_core.config import RagConfig
from rag_core.contracts import frame
from rag_core.pipeline import retrieve
from rag_core.prompts import ContextChunk, build_messages, decline_text
from rag_core.sentinel import filter_sentinel_async

from .errors import classify
from .state import AppState
from .telemetry import done_payload, meta_payload

SNIPPET_CHARS = 240


def _sources_payload(chunks: list[ContextChunk]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": c.chunk_id,
            "title": c.title,
            "page": c.page_number,
            "snippet": c.text[:SNIPPET_CHARS],
        }
        for c in chunks
    ]


async def answer_stream(question: str, state: AppState) -> AsyncIterator[str]:
    """NDJSON frames for one question."""
    cfg: RagConfig = state.cfg
    profile = state.profile
    started = time.perf_counter()

    # --- retrieval -------------------------------------------------------
    try:
        retrieval_started = time.perf_counter()
        result = await retrieve(question, profile.embedder, profile.dense, profile.lexical, cfg)
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
    except Exception as exc:  # noqa: BLE001 - classified, then reported as a frame
        failure = classify(exc)
        yield frame("error", code=failure.code, message=failure.message)
        yield frame("done", truncated=True)
        return

    # `meta` waits for retrieval because it carries the gate decision. That
    # costs the first byte a retrieval round-trip (budgeted <400ms p50) and
    # buys the refusal path a fully-populated telemetry strip before the
    # decline text renders, which is the point of the split.
    yield frame("meta", telemetry=meta_payload(result, cfg.gate, retrieval_ms))

    # --- stage 1: the gate -----------------------------------------------
    if not result.decision.proceed:
        # No prompt is built and no model is called (PRD F10). The copy is
        # server-authored, which is what makes it identical here and in the
        # eval harness.
        yield frame("token", text=decline_text(result.decision.reason))
        yield frame(
            "done",
            telemetry=done_payload(
                ttft_ms=(time.perf_counter() - started) * 1000,
                total_tokens=0,
                served_by=None,
                truncated=False,
            ),
            was_declined=True,
            decline_reason=result.decision.reason,
        )
        return

    # `build_messages` raises on empty chunks, deliberately — "answering with
    # no retrieved context is the failure this whole system exists to prevent".
    # Reaching it with none means the gate ordering above is wrong, so this is
    # not defended against: it should crash loudly in a test, not be caught.
    messages = build_messages(question, result.chunks, history=[], max_history=cfg.history_messages)

    # --- stage 2: generation and the sentinel -----------------------------
    stream = profile.generator.stream(messages)
    tokens = 0
    ttft_ms: float | None = None
    sources_sent = False

    try:
        async for kind, text in filter_sentinel_async(stream):
            if kind == "declined":
                # The model refused. Its refusal is replaced with server copy,
                # and no sources are emitted — there is no answer to cite.
                yield frame("token", text=decline_text("insufficient_context"))
                yield frame(
                    "done",
                    telemetry=done_payload(
                        ttft_ms=(time.perf_counter() - started) * 1000,
                        total_tokens=0,
                        served_by=stream.served_by,
                        truncated=False,
                    ),
                    was_declined=True,
                    decline_reason="insufficient_context",
                )
                return

            if not sources_sent:
                # Both gates have now cleared: stage 1 above, and stage 2 by
                # producing a token that is not the sentinel.
                yield frame("sources", items=_sources_payload(result.chunks))
                sources_sent = True
                ttft_ms = (time.perf_counter() - started) * 1000

            tokens += 1
            yield frame("token", text=text)
    except Exception as exc:  # noqa: BLE001 - classified, then reported as a frame
        # Headers are long since committed, so this can only be a frame. The
        # response is marked truncated: partial text stays on screen rather
        # than being silently replaced, and the client knows it is partial.
        failure = classify(exc)
        yield frame("error", code=failure.code, message=failure.message)
        yield frame(
            "done",
            telemetry=done_payload(
                ttft_ms=ttft_ms,
                total_tokens=tokens,
                served_by=stream.served_by,
                truncated=True,
            ),
            was_declined=False,
            decline_reason=None,
        )
        return

    yield frame(
        "done",
        telemetry=done_payload(
            ttft_ms=ttft_ms,
            total_tokens=tokens,
            served_by=stream.served_by,
            truncated=False,
        ),
        was_declined=False,
        decline_reason=None,
    )
