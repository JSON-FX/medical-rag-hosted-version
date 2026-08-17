import pytest

from rag_core.config import GateConfig
from rag_core.gate import GateDecision, GateSignals, evaluate_gate

CFG = GateConfig(tau_abstain=0.30, tau_strong=0.45)


def signals(top=0.9, mean=0.8, lexical=True, empty=False) -> GateSignals:
    return GateSignals(
        top_similarity=top, mean_similarity=mean, lexical_support=lexical, corpus_empty=empty
    )


def test_empty_corpus_declines_before_any_other_check():
    decision = evaluate_gate(signals(top=0.99, empty=True), CFG)
    assert decision.proceed is False
    assert decision.reason == "empty_corpus"


def test_similarity_below_tau_abstain_is_off_domain():
    decision = evaluate_gate(signals(top=0.10), CFG)
    assert decision.proceed is False
    assert decision.reason == "off_domain"


def test_high_similarity_proceeds():
    decision = evaluate_gate(signals(top=0.80), CFG)
    assert decision.proceed is True
    assert decision.reason == "ok"


def test_middle_band_without_lexical_support_declines():
    decision = evaluate_gate(signals(top=0.40, lexical=False), CFG)
    assert decision.proceed is False
    assert decision.reason == "weak_unsupported"


def test_middle_band_with_lexical_support_proceeds():
    assert evaluate_gate(signals(top=0.40, lexical=True), CFG).proceed is True


def test_lexical_support_cannot_rescue_below_tau_abstain():
    """Off-domain is off-domain even if a stray word matched."""
    decision = evaluate_gate(signals(top=0.05, lexical=True), CFG)
    assert decision.reason == "off_domain"


@pytest.mark.parametrize("top,expected", [(0.2999, "off_domain"), (0.30, "weak_unsupported")])
def test_tau_abstain_boundary_is_inclusive_upward(top, expected):
    assert evaluate_gate(signals(top=top, lexical=False), CFG).reason == expected


@pytest.mark.parametrize("top,expected", [(0.4499, "weak_unsupported"), (0.45, "ok")])
def test_tau_strong_boundary_is_inclusive_upward(top, expected):
    assert evaluate_gate(signals(top=top, lexical=False), CFG).reason == expected


def test_mean_similarity_does_not_affect_the_decision():
    """Pins the documented v1 behaviour (spec 6.5). If mean_similarity is
    ever promoted to a rule, this test must fail rather than the change
    passing unnoticed."""
    low = evaluate_gate(signals(top=0.80, mean=0.01), CFG)
    high = evaluate_gate(signals(top=0.80, mean=0.79), CFG)
    assert low.proceed is True and high.proceed is True
    assert low.reason == high.reason


def test_signals_are_carried_on_the_decision_for_observability():
    decision = evaluate_gate(signals(top=0.8, mean=0.7, lexical=True), CFG)
    assert decision.signals["top_similarity"] == 0.8
    assert decision.signals["mean_similarity"] == 0.7
    assert decision.signals["lexical_support"] is True


def test_decision_is_frozen():
    decision = evaluate_gate(signals(), CFG)
    assert isinstance(decision, GateDecision)
    with pytest.raises(Exception):
        decision.proceed = False


def test_negative_similarity_is_off_domain_not_an_error():
    """similarity = 1 - cosine_distance, and Chroma's distance ranges [0, 2],
    so similarity legitimately goes negative for opposed vectors."""
    for top in (-0.01, -0.5, -1.0):
        decision = evaluate_gate(signals(top=top), CFG)
        assert decision.proceed is False
        assert decision.reason == "off_domain"


def test_nan_similarity_fails_closed_rather_than_open():
    """NaN compares False against every threshold, so an unguarded gate would
    fall through to `ok` — the most permissive result from the worst input."""
    decision = evaluate_gate(signals(top=float("nan")), CFG)
    assert decision.proceed is False, "gate failed OPEN on NaN"
    assert decision.reason == "off_domain"


def test_infinite_similarity_fails_closed():
    for top in (float("inf"), float("-inf")):
        assert evaluate_gate(signals(top=top), CFG).proceed is False


def test_gate_signals_are_json_serialisable_even_when_non_finite():
    """gate_signals is a JSONField; a bare NaN token is not valid JSON."""
    import json

    payload = evaluate_gate(signals(top=float("nan"), mean=float("inf")), CFG).signals
    encoded = json.dumps(payload)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert json.loads(encoded)["top_similarity"] is None
