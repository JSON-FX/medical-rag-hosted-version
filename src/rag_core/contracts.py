"""Data model and wire contract.

The storage shape (`Chunk`, `EmbeddedChunk`, `Scored`) follows ARCHITECTURE.md
§5. The wire shape (`frame`, `Telemetry`) is what the API shell serialises and
the frontend parses — defined here rather than in the shell so both sides can
be built against one definition without waiting for each other.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import GateConfig
from .gate import GateDecision
from .prompts import ContextChunk

Vector = list[float]
Token = str


# --------------------------------------------------------------------------
# Storage model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit. Dense and lexical retrieval return the same row.

    `anchor` is the citation target — a page number for this corpus (see the
    chunking decision in docs/tickets: chunks never span a page boundary, so
    every chunk has an exact page). ARCHITECTURE.md §5 types it as text to
    leave room for a section anchor later.
    """

    id: str
    document_id: str
    ordinal: int
    anchor: str
    content: str
    document_title: str


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk: Chunk
    embedding: Vector


@dataclass(frozen=True)
class IndexManifest:
    """What built the index, recorded alongside it.

    The embedding model is part of the schema, not a runtime setting
    (ARCHITECTURE.md §5). At startup the service compares the configured
    embedder against this and refuses to serve on disagreement — querying an
    index built by a different model returns plausible-looking garbage, which
    is the worst failure mode available because nothing about the output looks
    wrong.
    """

    embedding_model_id: str
    dimension: int
    ingested_at: datetime


@dataclass(frozen=True)
class Scored[T]:
    """An item with the provider's NATIVE score, untransformed.

    Normalising here would be the bug ADR-003 exists to prevent: the gate needs
    raw magnitude, and RRF needs only rank. Each store's docstring in ports.py
    states the direction its score runs in, and the contract suite tests it.
    """

    item: T
    score: float


def make_chunk_id(document_id: str, ordinal: int) -> str:
    return f"{document_id}_{ordinal}"


def split_chunk_id(chunk_id: str) -> tuple[str, int]:
    """Inverse of `make_chunk_id`. Splits on the LAST underscore.

    The local build could split on the first, because its document ids were
    integers. Here they are text slugs, and `some_drug_name_3` is an entirely
    plausible id. Splitting on the first underscore does not raise — it yields
    a document id of "some" and a non-numeric ordinal — so the failure would
    surface as every retrieved chunk silently vanishing and the system refusing
    every question, which is close to the hardest bug shape to trace.
    """
    document_id, separator, ordinal = chunk_id.rpartition("_")
    if not separator or not document_id or not ordinal.isdigit():
        raise ValueError(f"malformed chunk id: {chunk_id!r}")
    return document_id, int(ordinal)


def to_context_chunk(chunk: Chunk) -> ContextChunk:
    """Bridge the storage model to the prompt model.

    `ContextChunk.page_number` is an int because `format_context` renders
    "p. {page_number}". Converting here rather than storing an int anchor keeps
    the schema open to section anchors, and makes a non-numeric anchor fail
    loudly at the boundary instead of rendering "p. section-3" into a prompt.
    """
    if not chunk.anchor.isdigit():
        raise ValueError(
            f"chunk {chunk.id} has a non-numeric anchor {chunk.anchor!r}; "
            "prompts.format_context renders anchors as page numbers"
        )
    return ContextChunk(
        chunk_id=chunk.id,
        title=chunk.document_title,
        page_number=int(chunk.anchor),
        text=chunk.content,
    )


# --------------------------------------------------------------------------
# Wire contract
# --------------------------------------------------------------------------

FRAME_TYPES = ("meta", "token", "sources", "error", "done")


def frame(kind: str, **fields: Any) -> str:
    """One NDJSON frame: one JSON object per line.

    `json.dumps` escapes embedded newlines, so answer text containing line
    breaks cannot split a frame.
    """
    if kind not in FRAME_TYPES:
        raise ValueError(f"unknown frame type {kind!r}; expected one of {FRAME_TYPES}")
    return json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n"


def _jsonable(value: float | None) -> float | None:
    """NaN and infinity are not valid JSON — json.dumps emits a bare NaN token
    that strict parsers reject — so the payload carries None instead."""
    if value is None:
        return None
    return value if math.isfinite(value) else None


def explain_gate(decision: GateDecision, cfg: GateConfig) -> tuple[bool, bool]:
    """The gate's two conditions, reported independently.

    ADR-003 chose a two-condition gate over a single threshold specifically so
    telemetry could say WHICH condition failed: "being able to say in the
    telemetry strip which condition failed is worth the second parameter on its
    own." `GateDecision` carries a single reason string, so the conditions are
    recovered here rather than by widening the ported gate.

    Returns (similarity_ok, lexical_support).
    """
    top = decision.signals.get("top_similarity")
    similarity_ok = top is not None and top >= cfg.tau_abstain
    lexical_support = bool(decision.signals.get("lexical_support"))
    return similarity_ok, lexical_support


@dataclass(frozen=True)
class Telemetry:
    """Per-request measurements surfaced in the response (PRD F13).

    A product feature, not debug output — it is the part of the interface that
    shows the engineering rather than describing it (ARCHITECTURE.md §3).
    """

    gate_proceed: bool
    gate_reason: str
    similarity_ok: bool
    lexical_support: bool
    retrieval_ms: float
    top_similarity: float | None = None
    ttft_ms: float | None = None
    total_tokens: int = 0
    provider: str | None = None
    fused_scores: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": {
                "proceed": self.gate_proceed,
                "reason": self.gate_reason,
                # Reported separately and never collapsed into one "confidence"
                # number — that collapse is precisely what ADR-003 rejected.
                "similarity_ok": self.similarity_ok,
                "lexical_support": self.lexical_support,
                "top_similarity": _jsonable(self.top_similarity),
            },
            "latency": {
                "retrieval_ms": _jsonable(self.retrieval_ms),
                "ttft_ms": _jsonable(self.ttft_ms),
            },
            "total_tokens": self.total_tokens,
            "provider": self.provider,
            "fused_scores": [_jsonable(s) for s in self.fused_scores],
        }
