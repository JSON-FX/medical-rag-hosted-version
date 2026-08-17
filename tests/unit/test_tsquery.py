"""Ported from the local build's test_lexical.py.

The assertions are the same behaviours, restated for the tsquery output shape:
`metformin | dose` where FTS5 produced `"metformin" OR "dose"`.
"""

import pytest

from rag_adapters.tsquery import MAX_TERMS, build_tsquery


def test_simple_question_becomes_or_of_terms():
    assert build_tsquery("metformin dose") == "metformin | dose"


@pytest.mark.parametrize(
    "question",
    [
        "What's the max dose?",
        'He said "take two"',
        "dose*",
        "metformin - adult",
        "dose NEAR adult",
        "metformin AND atenolol",
        "a OR b NOT c",
        "50% w/v (10:1)",
        "^caret $dollar",
        "metformin & atenolol",
        "dose | rate",
        "!negated",
        "colon:separated",
    ],
)
def test_tsquery_syntax_characters_never_survive_sanitising(question):
    """FTS5 raised `fts5: syntax error` on these. to_tsquery raises on its own
    set — `&`, `|`, `!`, `(`, `)`, `:`, `*` — so the guard is the same guard
    for a different dialect."""
    result = build_tsquery(question)
    terms = result.split(" | ") if result else []
    for term in terms:
        assert all(ch.isalnum() or ch == "." for ch in term), f"{term!r} in {result!r}"


def test_reserved_words_of_the_old_dialect_are_now_ordinary_terms():
    """FTS5 needed NEAR quoted to stop it parsing as an operator. to_tsquery
    has no such word, so it becomes a plain term — which is what it always
    meant to the user."""
    result = build_tsquery("dose NEAR adult")
    assert "near" in result.split(" | ")
    assert " NEAR " not in result


def test_single_character_terms_are_dropped_as_noise():
    assert build_tsquery("a b metformin") == "metformin"


def test_stopwords_are_removed():
    """Function words OR-joined would match almost every chunk, leaving the
    gate's lexical_support permanently True and collapsing its middle band."""
    assert build_tsquery("what is the dose of metformin") == "dose | metformin"


def test_a_question_of_only_stopwords_yields_an_empty_string():
    """to_tsquery('english', '') raises. The caller must check this, so it must
    be an empty string and not a query that happens to match nothing."""
    assert build_tsquery("what is it?") == ""
    assert build_tsquery("???") == ""
    assert build_tsquery("") == ""


def test_a_decimal_dose_stays_one_token():
    """Splitting "0.5mg" into "0" and "5mg" made a paediatric question
    byte-identical to an adult one — a dosage confusion originating in the
    tokenizer."""
    assert build_tsquery("0.5mg dose") == "0.5mg | dose"


def test_duplicate_terms_appear_once():
    assert build_tsquery("dose dose dose metformin") == "dose | metformin"


def test_term_count_is_capped():
    question = " ".join(f"term{i}" for i in range(MAX_TERMS + 10))
    assert len(build_tsquery(question).split(" | ")) == MAX_TERMS


def test_case_is_normalised():
    assert build_tsquery("Metformin DOSE") == "metformin | dose"


def test_the_output_is_a_valid_tsquery_shape():
    """Terms joined by ` | `, no leading or trailing operator — a stray
    separator is a syntax error, not a no-op."""
    result = build_tsquery("metformin adult dose")
    assert not result.startswith("|")
    assert not result.endswith("|")
    assert "||" not in result
