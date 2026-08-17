"""Two generation providers behind one port, with automatic failover.

ADR-004: free inference tiers are rate-limited, carry no SLA, and change terms
without notice, so "a single-provider design will break" on an artifact whose
only job is to work when someone clicks it months from now.

The whole design turns on one constraint: **failover happens only before the
first token.** Once a token is on the wire it cannot be retracted — the same
reason `sentinel.py` buffers rather than streaming a refusal it might have to
take back. A mid-stream failure therefore propagates, and the API shell reports
it as truncated, which is what the local build does.

That is not the limitation it sounds like. The failure that dominates in
practice is a rate limit rejecting the request outright, before a single token
has been generated, and that is exactly the case this covers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from rag_core.contracts import Token, TokenStream
from rag_core.errors import AllProvidersUnavailable, ProviderUnavailable
from rag_core.ports import GenerationProvider

# One retry on the primary, then the secondary. Three attempts, no delay
# between them: this runs on the request path inside a 2.5s time-to-first-token
# budget, where a backoff sleep would be most of the budget. The embedder backs
# off because it runs offline; this deliberately does not.
PRIMARY_ATTEMPTS = 2


class FailoverGenerator:
    """GenerationProvider decorating two others."""

    def __init__(self, primary: GenerationProvider, secondary: GenerationProvider) -> None:
        self._primary = primary
        self._secondary = secondary
        # The primary's id, because it serves the overwhelming majority of
        # requests. `TokenStream.served_by` is the authoritative per-request
        # answer and is what telemetry reports.
        self.model_id = primary.model_id

    def stream(self, messages: list[dict[str, str]]) -> TokenStream:
        # The generator needs to write `served_by` on the TokenStream that
        # wraps it, which is circular. Resolved with a per-call closure rather
        # than instance state: an attribute like `self._current` would be
        # overwritten by a second concurrent request before the first one
        # produced a token, and would then report the wrong provider — the
        # exact race TokenStream exists to avoid.
        #
        # Safe because an async generator's body does not run until the first
        # __anext__, by which point `stream` is bound.
        stream: TokenStream

        async def run() -> AsyncIterator[Token]:
            async for token in self._run(messages, stream):
                yield token

        stream = TokenStream(run())
        return stream

    async def _attempt(
        self,
        provider: GenerationProvider,
        messages: list[dict[str, str]],
        target: TokenStream,
    ) -> AsyncIterator[Token]:
        """Yield from one provider, committing to it at the first token."""
        inner = provider.stream(messages)
        first = True
        async for token in inner:
            if first:
                # The commitment point. Past here the provider owns this
                # response and a later failure cannot be papered over.
                target.served_by = provider.model_id
                first = False
            yield token

    async def _run(
        self, messages: list[dict[str, str]], target: TokenStream
    ) -> AsyncIterator[Token]:
        failures: list[Exception] = []

        for attempt in range(PRIMARY_ATTEMPTS):
            try:
                async for token in self._attempt(self._primary, messages, target):
                    yield token
                return
            except ProviderUnavailable as exc:
                if target.served_by is not None:
                    # Tokens already reached the reader. Failing over now would
                    # either duplicate text or require retracting what they have
                    # seen, so this propagates and the shell marks it truncated.
                    raise
                failures.append(exc)
                if attempt == PRIMARY_ATTEMPTS - 1:
                    break
            # ProviderProtocolError deliberately does NOT land here. A bad
            # request fails identically on the secondary, so retrying it burns
            # the fallback, doubles the latency of the failure, and turns one
            # clear error into two.

        try:
            async for token in self._attempt(self._secondary, messages, target):
                yield token
        except ProviderUnavailable as exc:
            if target.served_by is not None:
                raise
            failures.append(exc)
            raise AllProvidersUnavailable(
                f"both generation providers failed: {'; '.join(str(f) for f in failures)}"
            ) from exc
