"""Mapping failures to responses (ARCHITECTURE.md §8).

Two columns, because the response changes shape halfway through a request.
Before streaming starts the status code is still available; once the first
frame is on the wire the response is committed to 200 and every later failure
can only be an `error` frame followed by `done`.

Nothing here returns a provider's message verbatim. Groq's 401 body carries
`'message': 'Invalid API Key'` — harmless in itself, but the habit of passing
vendor strings through is what eventually leaks a key or a DSN into a public
response. Log the detail; return the category.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rag_core.errors import (
    AllProvidersUnavailable,
    ProviderProtocolError,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Failure:
    """How one failure should be reported, in either position."""

    status: int
    code: str
    message: str


# All generators down is the end of the chain, and PRD F16 is explicit: an
# explicit service message, "never to an ungrounded answer".
ALL_PROVIDERS = Failure(
    status=503,
    code="all_providers_unavailable",
    message=(
        "Both answer providers are unavailable right now. This demo runs on free "
        "tiers with no availability guarantee — please try again shortly."
    ),
)

# ARCHITECTURE.md §8: there is no meaningful degraded retrieval without a query
# vector, so this fails the request rather than answering from the lexical leg
# alone.
PROVIDER_DOWN = Failure(
    status=503,
    code="provider_unavailable",
    message="A required service is temporarily unavailable. Please try again shortly.",
)

PROVIDER_REJECTED = Failure(
    status=502,
    code="provider_error",
    message="A required service rejected the request. This is a bug on our side, not yours.",
)

UNEXPECTED = Failure(
    status=500,
    code="internal_error",
    message="Something went wrong handling this question.",
)


def index_unavailable(reason: str) -> Failure:
    """The startup manifest check refused this deployment.

    The reason is safe to return: it names two model ids, which are
    configuration rather than credentials, and a deployment that cannot serve
    should say why in a browser rather than only in logs.
    """
    return Failure(status=503, code="index_unavailable", message=reason)


def classify(exc: BaseException) -> Failure:
    """Map an exception to how it should be reported.

    Ordered most specific first: AllProvidersUnavailable is a ProviderError, so
    checking the base type first would swallow it.
    """
    if isinstance(exc, AllProvidersUnavailable):
        logger.warning("all providers unavailable: %s", exc)
        return ALL_PROVIDERS
    if isinstance(exc, ProviderProtocolError):
        logger.error("provider rejected the request: %s", exc)
        return PROVIDER_REJECTED
    if isinstance(exc, ProviderUnavailable):
        logger.warning("provider unavailable: %s", exc)
        return PROVIDER_DOWN
    logger.exception("unhandled failure while answering")
    return UNEXPECTED
