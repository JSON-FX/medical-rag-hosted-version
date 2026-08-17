"""Assembling the per-request telemetry (PRD F13).

Split across two frames, deliberately. `meta` carries what is known before
generation — the gate decision and how long retrieval took. `done` carries what
is only knowable after — time to first token, token count, and which provider
actually served.

The split is for the refusal path. PRD §7 budgets a refusal at under 800ms
"fast and visibly so", and PRD G5 wants that path discoverable in under a
minute by someone who was not told about it. A refusal is the outcome readers
assume is a bug, so the strip has to explain itself at exactly the moment it
happens — not after. With the split, a decline emits the gate reason and both
conditions BEFORE the decline text renders: the reader sees why before what.

This module is transport, not domain: it only reshapes what `rag_core` already
decided. `explain_gate` is what recovers the two conditions; nothing here
re-derives a gate outcome.
"""

from __future__ import annotations

from typing import Any

from rag_core.config import GateConfig
from rag_core.contracts import Telemetry, explain_gate
from rag_core.pipeline import RetrievalResult


def meta_payload(
    result: RetrievalResult, gate_cfg: GateConfig, retrieval_ms: float
) -> dict[str, Any]:
    """Everything known once retrieval has returned."""
    similarity_ok, lexical_support = explain_gate(result.decision, gate_cfg)
    telemetry = Telemetry(
        gate_proceed=result.decision.proceed,
        gate_reason=result.decision.reason,
        # Reported separately and never collapsed into one confidence number.
        # ADR-003 chose a two-condition gate specifically so telemetry could
        # say WHICH condition failed — "worth the second parameter on its own".
        similarity_ok=similarity_ok,
        lexical_support=lexical_support,
        retrieval_ms=retrieval_ms,
        top_similarity=result.decision.signals.get("top_similarity"),
        fused_scores=list(result.fused_scores),
    )
    payload = telemetry.as_dict()
    # TTFT, tokens and provider are unknowable here; omit rather than send
    # nulls the frontend would have to distinguish from real zeroes.
    payload.pop("total_tokens", None)
    payload.pop("provider", None)
    payload["latency"].pop("ttft_ms", None)
    return payload


def done_payload(
    *,
    ttft_ms: float | None,
    total_tokens: int,
    served_by: str | None,
    truncated: bool,
) -> dict[str, Any]:
    """Everything only knowable once the stream has ended.

    `served_by` comes from `TokenStream`, which cannot know it until the first
    token — the whole point of the failover chain is that who serves depends on
    whether the primary accepted the request (ADR-004, PRD F14).
    """
    telemetry = Telemetry(
        # The gate half already went out on `meta`; these are placeholders that
        # as_dict() needs and the caller drops below.
        gate_proceed=True,
        gate_reason="",
        similarity_ok=True,
        lexical_support=True,
        retrieval_ms=0.0,
        ttft_ms=ttft_ms,
        total_tokens=total_tokens,
        provider=served_by,
    )
    payload = telemetry.as_dict()
    payload.pop("gate", None)
    payload["latency"].pop("retrieval_ms", None)
    payload.pop("fused_scores", None)
    payload["truncated"] = truncated
    return payload
