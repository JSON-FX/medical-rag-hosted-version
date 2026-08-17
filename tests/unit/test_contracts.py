import json
from dataclasses import FrozenInstanceError

import pytest

from rag_core.config import GateConfig
from rag_core.contracts import (
    FRAME_TYPES,
    Chunk,
    Scored,
    Telemetry,
    explain_gate,
    frame,
    make_chunk_id,
    split_chunk_id,
    to_context_chunk,
)
from rag_core.gate import GateSignals, evaluate_gate

CFG = GateConfig(tau_abstain=0.70, tau_strong=0.75)


def a_chunk(chunk_id="metformin_3", anchor="2") -> Chunk:
    document_id, ordinal = split_chunk_id(chunk_id)
    return Chunk(
        id=chunk_id,
        document_id=document_id,
        ordinal=ordinal,
        anchor=anchor,
        content="Adult starting dose is 500mg twice daily.",
        document_title="Metformin",
    )


# --- chunk ids -----------------------------------------------------------


def test_chunk_id_round_trips():
    assert split_chunk_id(make_chunk_id("metformin", 12)) == ("metformin", 12)


def test_chunk_id_splits_on_the_last_underscore_not_the_first():
    """A document id is a text slug here, not an integer as in the local build.
    Splitting on the first underscore yields ("some", "drug_name_3"), which does
    not raise — it silently resolves to nothing, drops every retrieved chunk,
    and makes the system refuse every question for no discoverable reason."""
    assert split_chunk_id("some_drug_name_3") == ("some_drug_name", 3)


def test_chunk_id_round_trips_for_a_slug_containing_underscores():
    assert split_chunk_id(make_chunk_id("some_drug_name", 7)) == ("some_drug_name", 7)


@pytest.mark.parametrize("bad", ["metformin", "metformin_", "_3", "metformin_x", ""])
def test_malformed_chunk_id_raises_rather_than_resolving_to_nothing(bad):
    with pytest.raises(ValueError, match="malformed chunk id"):
        split_chunk_id(bad)


# --- context chunk conversion -------------------------------------------


def test_to_context_chunk_maps_anchor_to_page_number():
    context = to_context_chunk(a_chunk(anchor="4"))
    assert context.page_number == 4
    assert context.chunk_id == "metformin_3"
    assert context.title == "Metformin"


def test_non_numeric_anchor_fails_loudly_rather_than_rendering_into_a_prompt():
    """format_context writes "p. {page_number}". A section anchor would render
    as "p. section-3", which is wrong in a citation and invisible in review."""
    with pytest.raises(ValueError, match="non-numeric anchor"):
        to_context_chunk(a_chunk(anchor="section-3"))


# --- scored --------------------------------------------------------------


def test_scored_carries_the_native_score_untransformed():
    scored = Scored(item=a_chunk(), score=0.23)
    assert scored.score == 0.23


def test_scored_is_frozen():
    scored = Scored(item=a_chunk(), score=0.23)
    with pytest.raises(FrozenInstanceError):
        scored.score = 0.9


# --- frames --------------------------------------------------------------


def test_frame_is_one_line_of_valid_json():
    line = frame("token", text="hello")
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == {"type": "token", "text": "hello"}


def test_embedded_newlines_cannot_split_a_frame():
    """Answer text containing line breaks is the ordinary case, not an edge one."""
    line = frame("token", text="line one\nline two")
    assert line.count("\n") == 1
    assert json.loads(line)["text"] == "line one\nline two"


def test_unknown_frame_type_raises():
    with pytest.raises(ValueError, match="unknown frame type"):
        frame("progress", pct=50)


def test_every_declared_frame_type_is_constructible():
    for kind in FRAME_TYPES:
        assert json.loads(frame(kind))["type"] == kind


# --- gate explanation ----------------------------------------------------


def test_off_domain_reports_the_similarity_condition_as_failed():
    decision = evaluate_gate(GateSignals(0.10, 0.10, lexical_support=True), CFG)
    similarity_ok, lexical_support = explain_gate(decision, CFG)
    assert similarity_ok is False
    assert lexical_support is True


def test_weak_unsupported_reports_similarity_ok_but_no_lexical_support():
    """This is the whole point of the two-condition gate: the telemetry strip
    can say which one failed (ADR-003)."""
    decision = evaluate_gate(GateSignals(0.72, 0.70, lexical_support=False), CFG)
    assert decision.reason == "weak_unsupported"
    assert explain_gate(decision, CFG) == (True, False)


def test_a_passing_gate_reports_both_conditions_met():
    decision = evaluate_gate(GateSignals(0.90, 0.85, lexical_support=True), CFG)
    assert decision.reason == "ok"
    assert explain_gate(decision, CFG) == (True, True)


def test_non_finite_similarity_does_not_report_the_condition_as_met():
    decision = evaluate_gate(GateSignals(float("nan"), 0.0, lexical_support=True), CFG)
    similarity_ok, _ = explain_gate(decision, CFG)
    assert similarity_ok is False


# --- telemetry -----------------------------------------------------------


def test_telemetry_survives_json_encoding_with_a_non_finite_similarity():
    """A bare NaN token is not valid JSON and strict parsers reject it, so the
    frontend would fail to parse the whole frame."""
    payload = Telemetry(
        gate_proceed=False,
        gate_reason="off_domain",
        similarity_ok=False,
        lexical_support=False,
        retrieval_ms=12.5,
        top_similarity=float("nan"),
    ).as_dict()
    encoded = json.dumps(payload)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert json.loads(encoded)["gate"]["top_similarity"] is None


def test_telemetry_reports_both_gate_conditions_separately():
    payload = Telemetry(
        gate_proceed=False,
        gate_reason="weak_unsupported",
        similarity_ok=True,
        lexical_support=False,
        retrieval_ms=8.0,
        top_similarity=0.72,
    ).as_dict()
    assert payload["gate"]["similarity_ok"] is True
    assert payload["gate"]["lexical_support"] is False


def test_telemetry_carries_the_serving_provider():
    payload = Telemetry(
        gate_proceed=True,
        gate_reason="ok",
        similarity_ok=True,
        lexical_support=True,
        retrieval_ms=40.0,
        ttft_ms=310.0,
        total_tokens=128,
        provider="secondary-model",
    ).as_dict()
    assert payload["provider"] == "secondary-model"
    assert payload["latency"]["ttft_ms"] == 310.0
