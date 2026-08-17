"""Deferred SDK client construction.

Building a profile must never require a reachable service or a valid
credential. `PostgresPool` gets this by separating construction from `open()`;
generation and embedding adapters have no lifecycle to hang it on, so they defer
the client itself.

This is not premature caution. `genai.Client(api_key="")` raises immediately,
so an eagerly-constructed client makes `build_profile` fail on a machine with
no key — which breaks `uv run pytest` for anyone who has not set one, and makes
the composition root's "resolve once, connect later" split a fiction.

It also keeps the injection seam honest: a test passes a fake and the real
constructor is never reached, rather than being reached and then discarded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class LazyClient:
    """An SDK client built on first use, or an injected stand-in."""

    def __init__(self, build: Callable[[], Any], injected: Any | None = None) -> None:
        self._build = build
        self._client = injected

    def get(self) -> Any:
        if self._client is None:
            self._client = self._build()
        return self._client
