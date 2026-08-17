"""The API shell.

Owns transport only: HTTP, NDJSON framing, telemetry assembly, error mapping.
ARCHITECTURE.md §3 gives the test to apply to anything added here — "if a
behaviour would be identical over gRPC, it belongs in rag_core, not here".

`app` is a module-level variable rather than a factory, because Vercel's Python
runtime resolves an entrypoint variable (`rag_api.main:app`, recorded in
pyproject.toml).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag_adapters.profile import build_profile
from rag_core.config import RagConfig, load_config

from .chat import router as chat_router
from .health import router as health_router
from .state import AppState, check_manifest

logger = logging.getLogger(__name__)


async def startup(app: FastAPI, cfg: RagConfig) -> AppState:
    """Resolve the profile once, connect it, and verify the index."""
    profile = build_profile(cfg)
    state = AppState(cfg=cfg, profile=profile)

    try:
        await profile.open()
        manifest = await profile.dense.read_manifest()
        check_manifest(state, manifest)
    except Exception as exc:  # noqa: BLE001 - startup must not crash-loop
        # A deployment that cannot reach its database should report that, not
        # crash-loop. /api/health explains; /api/chat returns 503.
        state.serviceable = False
        state.reason = f"could not reach the index at startup: {type(exc).__name__}"
        logger.exception("refusing to serve — startup failed")

    return state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # A pre-set state is an injected one: a test built the profile itself and
    # owns its lifecycle. Rebuilding here would silently replace the fakes with
    # a real profile, which is how the streaming test first "failed" — uvicorn
    # ran the lifespan and served a fresh, unserviceable app.
    injected = getattr(app.state, "rag", None)
    if injected is not None:
        yield
        return

    cfg = load_config()
    app.state.rag = await startup(app, cfg)
    try:
        yield
    finally:
        await app.state.rag.profile.close()


def create_app() -> FastAPI:
    """Factory, so a test can build an app over injected fakes.

    Set `app.state.rag` before serving and the lifespan leaves it alone.
    Production uses the module-level `app` below, whose lifespan does the real
    startup.
    """
    application = FastAPI(title="Medical RAG — hosted", lifespan=lifespan)
    # Permissive for local development. TICKET-7 knows the frontend's origin
    # and TICKET-10 deploys it; tighten there rather than guessing now.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(chat_router)
    return application


app = create_app()
