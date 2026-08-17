"""tsquery construction.

Ported from the local build's FTS5 query builder. The tokeniser is unchanged;
only the join operator differs — `term | term` for `to_tsquery` rather than
`"term" OR "term"` for FTS5 MATCH.

The sanitising is still load-bearing, for a different reason than it was.
FTS5 needed it because raw questions contain characters it parses as query
syntax — quotes, `*`, `-`, `:`, and bare keywords like NEAR/AND/OR/NOT — so
"What's the max dose?" raised `fts5: syntax error`. `to_tsquery` has no
reserved words, but it raises on `&`, `|`, `!`, `(`, `)`, `:` and `*` just the
same. Reducing every term to alphanumerics makes the output safe by
construction in both dialects.

This module lives in `rag_adapters`, not `rag_core`: it encodes one backend's
query dialect, which is a provider decision. Note that `test_core_purity` would
NOT have caught it in the wrong place — the check is import-based and this
module imports only `re`. Placement here is a judgement the purity test cannot
make for you.
"""

from __future__ import annotations

import re

# A decimal number with an optional unit suffix is ONE token. Splitting
# "0.5mg" into ["0", "5mg"] and dropping the orphaned "0" made a pediatric
# 0.5mg question byte-identical to an adult 5mg one — a dosage confusion
# originating in the tokenizer.
TOKEN_RE = re.compile(r"\d+(?:\.\d+)+\w*|\w+", re.UNICODE)
MIN_TERM_LENGTH = 2
MAX_TERMS = 24

# Function words carry no retrieval signal but make every question match
# almost every chunk once OR-joined. That would leave the confidence gate's
# `lexical_support` signal permanently True and collapse its middle band.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have
    how i if in into is it its may might must of on or shall should that the their
    them then there these they this to was were what when where which who why will
    with would you your
    """.split()
)


def extract_terms(question: str) -> list[str]:
    """The searchable terms in a question, in order, deduplicated.

    Shared with the fake lexical store rather than living inside
    `build_tsquery`, because "what counts as a searchable term" is part of the
    lexical contract and not a Postgres detail. When only the real store
    stripped stopwords, a question of pure function words returned nothing on
    Postgres and matched everything on the fakes — which would have made the
    gate's `lexical_support` signal differ between profiles while both suites
    stayed green.
    """
    seen: list[str] = []
    for raw in TOKEN_RE.findall(question.lower()):
        if len(raw) < MIN_TERM_LENGTH or raw in STOPWORDS or raw in seen:
            continue
        seen.append(raw)
        if len(seen) >= MAX_TERMS:
            break
    return seen


def build_tsquery(question: str) -> str:
    """An OR-joined tsquery, or "" when nothing survives sanitising.

    OR rather than AND is deliberate and inherited: the stopword list above
    exists precisely because OR-joined function words would match everything.
    Switching to AND — which is what `websearch_to_tsquery` does by default —
    would change lexical recall and the gate's `lexical_support` signal at the
    same time, and TICKET-9 is trying to attribute a measured difference to
    `ts_rank_cd` alone.

    The empty return is not a corner case to tidy away: `to_tsquery('english',
    '')` raises a syntax error, so a question made entirely of stopwords
    ("what is it?") would 500 rather than simply finding nothing. Callers must
    check.
    """
    return " | ".join(extract_terms(question))
