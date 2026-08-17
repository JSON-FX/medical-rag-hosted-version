"""Provider-agnostic failure types.

`rag_core` names the failures; the adapters raise them. This is what lets the
API shell map an outcome to a response without knowing which provider it was
talking to (ARCHITECTURE.md §8).
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base for provider transport failures."""


class ProviderUnavailable(ProviderError):
    """The provider is not reachable, timed out, or refused the request."""


class ProviderProtocolError(ProviderError, ValueError):
    """The provider responded, but the payload was not usable.

    Subclasses both `ProviderError` (so callers that correctly catch the base
    transport-failure type also catch this) and `ValueError` (so assertions on
    count and dimension checks read naturally as what they are — this genuinely
    is a bad value, just one that also belongs to the provider error family).
    """


class AllProvidersUnavailable(ProviderError):
    """Every generation provider in the fallback chain failed.

    Distinct from `ProviderUnavailable` because the response differs: a single
    provider failing falls through to the secondary, whereas this is the end of
    the chain and must produce an explicit service message. Never an ungrounded
    answer (ARCHITECTURE.md §8, PRD F16).
    """


class ManifestMismatch(RuntimeError):
    """The configured embedder disagrees with the index manifest.

    Deliberately NOT a `ProviderError`. This is a deployment fault, not a
    transport fault, and the two map to different behaviours: a transport
    failure fails one request, whereas this refuses to serve at all. Querying
    an index built by a different embedding model returns plausible-looking
    garbage, which is the worst failure mode available (ARCHITECTURE.md §5).
    """
