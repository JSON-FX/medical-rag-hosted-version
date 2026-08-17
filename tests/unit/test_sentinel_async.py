"""The async sentinel twin, held to the sync original's behaviour.

The shell reads from a TokenStream and cannot use the sync filter; the eval
harness holds an already-collected list and cannot use the async one. So there
are two implementations of one state machine, and the duplication is the risk.

These drive BOTH over the same inputs and assert identical output. The
preamble-boundary cases are exactly where a divergence would hide — the sync
version has a regression test for a bug at 40 vs 44 characters, and the twin
inherits that bug's shape if the threshold is ever computed differently.

Kept separate from test_sentinel.py so that file stays what it is: the vendored
regression suite, diffable against the local build.
"""

import pytest

from rag_core.prompts import SENTINEL
from rag_core.sentinel import filter_sentinel, filter_sentinel_async


def sync_events(deltas, **kw):
    return list(filter_sentinel(iter(deltas), **kw))


async def async_events(deltas, **kw):
    async def source():
        for d in deltas:
            yield d

    return [e async for e in filter_sentinel_async(source(), **kw)]


EQUIVALENCE_CASES = [
    pytest.param(["Metformin ", "is ", "a biguanide."], id="normal"),
    pytest.param([SENTINEL], id="clean-sentinel"),
    pytest.param(["INSUFF", "ICIENT_CONTEXT"], id="split-in-two"),
    pytest.param(list(SENTINEL), id="one-char-at-a-time"),
    pytest.param(["\n\n", SENTINEL], id="leading-whitespace"),
    pytest.param(["INSUFFICIENT data ", "was available."], id="sentinel-like-prefix"),
    pytest.param([f"Sure! {SENTINEL}"], id="short-preamble"),
    pytest.param(["Of course. ", SENTINEL], id="preamble-across-deltas"),
    pytest.param(["ok"], id="shorter-than-buffer"),
    pytest.param([], id="empty"),
    pytest.param(["a" * 10, "b" * 40, "c" * 10], id="buffer-boundary"),
    pytest.param(list("A" * 23 + SENTINEL), id="preamble-23"),
    pytest.param(list("A" * 24 + SENTINEL), id="preamble-24-at-tolerance"),
    pytest.param(list("B" * 60 + SENTINEL), id="preamble-beyond-tolerance"),
    pytest.param(
        ["The adult dose is 500mg twice daily, per the monograph. " + SENTINEL],
        id="sentinel-late-in-a-real-answer",
    ),
]


@pytest.mark.parametrize("deltas", EQUIVALENCE_CASES)
async def test_the_async_twin_agrees_with_the_sync_original(deltas):
    assert await async_events(deltas) == sync_events(deltas)


@pytest.mark.parametrize("preamble_length", [20, 21, 22, 23, 24])
async def test_the_async_twin_never_leaks_the_sentinel(preamble_length):
    """Asserted independently of the sync version, in case both were wrong
    together. A missed refusal is doubly bad: the model answers when it should
    have declined, AND the raw token leaks into the reader's stream."""
    deltas = list("A" * preamble_length + SENTINEL)
    assert await async_events(deltas) == [("declined", None)]


async def test_the_async_twin_streams_rather_than_collecting():
    """It must yield as tokens arrive. Collecting the source first would make
    the shell's streaming vacuous while every other assertion still passed —
    which is precisely the failure mode the local build hit under ASGI."""
    emitted = []

    async def source():
        for d in ["x" * 50, "y", "z"]:
            emitted.append(d)
            yield d

    seen_after_first = None
    async for kind, _text in filter_sentinel_async(source()):
        if kind == "token" and seen_after_first is None:
            seen_after_first = len(emitted)
    assert seen_after_first == 1, "the first token waited for the whole source"
