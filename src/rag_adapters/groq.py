"""Groq adapter — the primary generator.

Primary because time-to-first-token is the NFR with a hard number attached
(<2.5s p50, <5s p95) and Groq's inference is substantially faster. Gemini is
the secondary, and being a different company with a different quota is the
point: ADR-004 is buying two independent failure domains, not two models.

Naming this module `groq.py` next to `import groq` looks like a shadowing bug
and is not. Python 3 resolves absolute imports, so `import groq` here finds the
installed SDK, never this file. Named for consistency with `postgres.py`, and
pinned by a test because the next reader will otherwise "fix" it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from groq import APIConnectionError, APIStatusError, AsyncGroq, GroqError, RateLimitError

from rag_core.config import RagConfig
from rag_core.contracts import Token, TokenStream
from rag_core.errors import ProviderProtocolError, ProviderUnavailable

from ._client import LazyClient

# Auth and entitlement failures are PROVIDER-SPECIFIC, so they fail over.
#
# The first version of this classified every non-429 4xx as a bad request, on
# the reasoning that "the secondary would reject it identically". That is true
# of a malformed request and false of a credential: the secondary is a
# different vendor holding a different key. A revoked, rotated or
# quota-suspended key is exactly what the fallback chain exists for, and PRD
# success criterion 4 states it outright — "killing the primary generation
# provider's key produces a working answer from the fallback".
#
# Found by revoking a real key, not by a fake: the fake tests validated the
# classification against itself.
_AUTH_STATUSES = (401, 403)


def _as_provider_error(exc: GroqError) -> Exception:
    """Map an SDK error into this codebase's error family.

    `RateLimitError` is the one that will actually fire in production — it is
    what a free tier does when the demo gets attention — and it is the failure
    the whole fallback chain exists for. A 4xx that is not a rate limit is a
    bad request, which the secondary would reject identically, so it must not
    consume the fallback.
    """
    if isinstance(exc, RateLimitError | APIConnectionError):
        return ProviderUnavailable(f"groq unavailable: {exc}")
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and (status >= 500 or status in _AUTH_STATUSES):
            return ProviderUnavailable(f"groq unavailable ({status}): {exc}")
        return ProviderProtocolError(f"groq rejected the request ({status}): {exc}")
    # Every other GroqError is transport-shaped (timeouts, response validation).
    return ProviderUnavailable(f"groq unavailable: {exc}")


class GroqGenerator:
    """GenerationProvider over Groq. The failover primary (ADR-004)."""

    def __init__(self, cfg: RagConfig, model_id: str | None = None, client: Any | None = None):
        self.model_id = model_id or cfg.generation.primary_model_id
        # Deferred for symmetry with the Gemini adapter, which must be lazy
        # because its constructor rejects an empty key. Same seam, same shape.
        self._lazy = LazyClient(lambda: AsyncGroq(api_key=cfg.providers.groq_api_key), client)

    @property
    def _client(self) -> Any:
        return self._lazy.get()

    def stream(self, messages: list[dict[str, str]]) -> TokenStream:
        return TokenStream(self._stream(messages), model_id=self.model_id)

    async def _stream(self, messages: list[dict[str, str]]) -> AsyncIterator[Token]:
        try:
            # Groq is OpenAI-compatible, so `prompts.build_messages` output
            # passes through UNCHANGED — no system hoisting, no role renaming.
            # Gemini needs both (see gemini.to_gemini_contents); the asymmetry
            # is easy to miss when reading the two adapters side by side.
            completion = await self._client.chat.completions.create(
                messages=messages,
                model=self.model_id,
                stream=True,
            )
            # The create() call is awaited before iteration begins, so a rate
            # limit raises here rather than mid-stream. That is exactly what
            # the failover chain needs: still before any token has been
            # committed to the wire.
            async for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                # None on the final chunk and on role-only chunks. Yielding it
                # would put a non-str into a str stream; yielding "" would
                # confuse the sentinel filter's buffer accounting.
                if delta:
                    yield delta
        except GroqError as exc:
            # Narrow to the SDK's own base: catching Exception here would
            # report a programming error in this adapter as a provider outage,
            # and quietly fail over to hide it.
            raise _as_provider_error(exc) from exc
