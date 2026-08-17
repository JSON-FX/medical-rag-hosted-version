"""Stage-2 refusal detection over a token stream.

The sentinel cannot be streamed to the browser and then retracted, so output is
buffered until there is enough text to decide (ARCHITECTURE.md §7). The buffer
costs a few tokens of latency and is imperceptible.

Pure string processing over a plain iterable: no I/O, no provider, and
deliberately not async. The shell adapts an async token stream to it; that is a
transport concern and does not belong here.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

from .prompts import SENTINEL

BUFFER_CHARS = 40
PREAMBLE_TOLERANCE = 24


def _is_sentinel(buffer: str, sentinel: str = SENTINEL) -> bool:
    """True when the buffered head is a refusal rather than an answer.

    Tolerates a short conversational preamble. The prompt forbids one, but an
    instruction-tuned model may still emit "Sure! INSUFFICIENT_CONTEXT", and a
    missed refusal is doubly bad: the model answers when it should have
    declined, and the raw sentinel token leaks into the user's visible stream.
    A real answer that happens to contain the sentinel this early is not a
    plausible output.
    """
    stripped = buffer.lstrip()
    if stripped.startswith(sentinel):
        return True
    position = stripped.find(sentinel)
    return 0 <= position <= PREAMBLE_TOLERANCE


def filter_sentinel(
    deltas: Iterable[str],
    sentinel: str = SENTINEL,
    buffer_chars: int = BUFFER_CHARS,
) -> Iterator[tuple[str, str | None]]:
    """Yield ('token', text) events, or exactly one ('declined', None).

    The sentinel commonly arrives split across deltas, so the decision waits
    until the buffer holds enough characters to be conclusive.
    """
    # The buffer must be able to hold a tolerated preamble AND the full sentinel,
    # or the decision fires before the sentinel has finished arriving, locks in a
    # false negative, and leaks the raw token to the user.
    threshold = max(buffer_chars, PREAMBLE_TOLERANCE + len(sentinel))
    buffer = ""
    decided = False

    for delta in deltas:
        if decided:
            yield ("token", delta)
            continue
        buffer += delta
        if len(buffer) >= threshold:
            decided = True
            if _is_sentinel(buffer, sentinel):
                yield ("declined", None)
                return
            yield ("token", buffer)
            buffer = ""

    if not decided and buffer:
        if _is_sentinel(buffer, sentinel):
            yield ("declined", None)
        else:
            yield ("token", buffer)
