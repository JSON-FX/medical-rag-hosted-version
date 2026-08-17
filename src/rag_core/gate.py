"""Stage-1 confidence gate.

Runs before the LLM, so a clearly off-domain question costs nothing. Pure:
no I/O, no logging, no clock — which is what lets the eval harness sweep it
offline over thousands of parameter combinations (ARCHITECTURE.md §7, ADR-003).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import GateConfig

REASONS = ("ok", "off_domain", "weak_unsupported", "empty_corpus")


def _jsonable(value: float) -> float | None:
    """NaN and infinity are not valid JSON — json.dumps emits a bare NaN token
    that strict parsers reject — so gate_signals stores None instead."""
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class GateSignals:
    top_similarity: float
    mean_similarity: float
    lexical_support: bool
    corpus_empty: bool = False

    def as_dict(self) -> dict:
        return {
            "top_similarity": _jsonable(self.top_similarity),
            "mean_similarity": _jsonable(self.mean_similarity),
            "lexical_support": self.lexical_support,
            "corpus_empty": self.corpus_empty,
        }


@dataclass(frozen=True)
class GateDecision:
    proceed: bool
    reason: str
    signals: dict


def evaluate_gate(signals: GateSignals, cfg: GateConfig) -> GateDecision:
    payload = signals.as_dict()

    if signals.corpus_empty:
        return GateDecision(False, "empty_corpus", payload)

    # A non-finite similarity must fail CLOSED. NaN compares False against every
    # threshold, so without this guard it falls through every check and reaches
    # `ok` — the most permissive outcome from the most degenerate input, in the
    # one component whose job is to decline when uncertain.
    if not math.isfinite(signals.top_similarity):
        return GateDecision(False, "off_domain", payload)

    if signals.top_similarity < cfg.tau_abstain:
        return GateDecision(False, "off_domain", payload)

    # Middle band: semantically plausible but not confident. Require that the
    # question's actual terminology appears in the retrieved text. This is what
    # separates "metformin dosing" (documented) from "metformin pediatric
    # dosing" (absent) — both sit at nearly identical cosine distance.
    if signals.top_similarity < cfg.tau_strong and not signals.lexical_support:
        return GateDecision(False, "weak_unsupported", payload)

    return GateDecision(True, "ok", payload)
