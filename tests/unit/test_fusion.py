import pytest

from rag_core.fusion import FusedHit, reciprocal_rank_fusion


def test_identical_rankings_preserve_order():
    ranking = ["a", "b", "c"]
    assert [h.chunk_id for h in reciprocal_rank_fusion([ranking, ranking])] == ["a", "b", "c"]


def test_scores_match_the_rrf_formula():
    hits = reciprocal_rank_fusion([["a", "b"]], k=60)
    assert hits[0].score == pytest.approx(1 / 61)
    assert hits[1].score == pytest.approx(1 / 62)


def test_appearing_in_both_legs_beats_a_better_rank_in_one():
    """The whole point of hybrid retrieval: agreement outranks a single strong hit."""
    vector = ["x", "agreed"]
    lexical = ["y", "agreed"]
    order = [h.chunk_id for h in reciprocal_rank_fusion([vector, lexical], k=60)]
    assert order[0] == "agreed"


def test_disjoint_rankings_interleave_by_rank():
    order = [h.chunk_id for h in reciprocal_rank_fusion([["a", "b"], ["c", "d"]], k=60)]
    assert set(order[:2]) == {"a", "c"}
    assert set(order[2:]) == {"b", "d"}


def test_one_empty_leg_returns_the_other_unchanged():
    assert [h.chunk_id for h in reciprocal_rank_fusion([["a", "b"], []])] == ["a", "b"]


def test_all_empty_returns_empty():
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([]) == []


def test_ties_break_deterministically_by_chunk_id():
    first = [h.chunk_id for h in reciprocal_rank_fusion([["b", "a"], ["a", "b"]])]
    second = [h.chunk_id for h in reciprocal_rank_fusion([["b", "a"], ["a", "b"]])]
    assert first == second


def test_returns_fused_hit_instances():
    assert isinstance(reciprocal_rank_fusion([["a"]])[0], FusedHit)
