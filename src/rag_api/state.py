"""Application state, resolved once at startup.

Held on `app.state`, never in a module global. One process serves concurrent
requests, and a module-level mutable is the same race `TokenStream` was
designed around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rag_adapters.profile import Profile
from rag_core.config import RagConfig
from rag_core.contracts import IndexManifest

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """The profile, the config, and whether this deployment may serve.

    `serviceable` is False when the index cannot be trusted. The service stays
    up and refuses every request rather than failing to start: on serverless a
    startup crash surfaces as an opaque platform 500 per invocation, with the
    real reason buried in logs somebody has to go looking for. Refusing every
    request with the two disagreeing model ids in the body refuses just as
    hard, and can be diagnosed from a browser.
    """

    cfg: RagConfig
    profile: Profile
    serviceable: bool = True
    reason: str | None = None
    manifest: IndexManifest | None = None


def check_manifest(state: AppState, manifest: IndexManifest | None) -> None:
    """Compare the configured embedder against what actually built the index.

    ARCHITECTURE.md §5: the embedding model is part of the schema, not a
    runtime setting. Querying an index built by a different model returns
    plausible-looking garbage — the worst failure mode available, because
    nothing about the output looks wrong.
    """
    embedder = state.profile.embedder
    state.manifest = manifest

    if manifest is None:
        # The ingest job writes the manifest last, only after every document
        # succeeds — so its absence means an interrupted or never-run ingest,
        # and this is the check that was supposed to catch it.
        state.serviceable = False
        state.reason = (
            "no index manifest found: the corpus has not been ingested, or an "
            "ingest run was interrupted before it completed"
        )
        logger.error("refusing to serve — %s", state.reason)
        return

    if manifest.embedding_model_id != embedder.model_id:
        state.serviceable = False
        state.reason = (
            f"index was built by {manifest.embedding_model_id!r} but this service is "
            f"configured with {embedder.model_id!r}; querying it would return "
            "plausible-looking garbage"
        )
        logger.error("refusing to serve — %s", state.reason)
        return

    if manifest.dimension != embedder.dimension:
        state.serviceable = False
        state.reason = (
            f"index has {manifest.dimension}-dimension vectors but this service is "
            f"configured for {embedder.dimension}"
        )
        logger.error("refusing to serve — %s", state.reason)
        return

    state.serviceable = True
    state.reason = None
    logger.info(
        "serving index built by %s (%d dimensions)",
        manifest.embedding_model_id,
        manifest.dimension,
    )
