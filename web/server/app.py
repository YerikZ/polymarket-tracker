"""
FastAPI application factory.

Usage:
    from web.server.app import create_app
    app = create_app(seed_cfg)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from polymarket import db
from polymarket.storage import Storage
from web.server.watcher import WatcherState
from web.server.routes import alerts, baskets, positions, settings, wallets, watcher
from web.server.routes.alerts import ws_router as alerts_ws_router

logger = logging.getLogger(__name__)

_CLIENT_DIST = Path(__file__).parent.parent / "client" / "dist"

# Runtime base path -- set BASE_PATH env var to serve at a sub-path, e.g. "/tracker/"
# Defaults to "/" for local / direct Docker usage.
_BASE_PATH = os.environ.get("BASE_PATH", "/")
if not _BASE_PATH.endswith("/"):
    _BASE_PATH += "/"


def _inject_base_path(html: str) -> str:
    """Inject <base href> and window.__BASE_PATH__ into the index.html <head>."""
    injection = (
        f'<base href="{_BASE_PATH}">'
        f'<script>window.__BASE_PATH__="{_BASE_PATH}";</script>'
    )
    # Insert right after <head> (Vite always emits one)
    return html.replace("<head>", f"<head>{injection}", 1)


def create_app(seed_cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="Polymarket Tracker", version="1.0.0")

    # Allow Vite dev server to call the API during development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Shared state
    dsn = seed_cfg.get("database_url") if seed_cfg else None
    dsn = dsn or os.environ.get(
        "POLYMARKET_DATABASE_URL",
        "postgresql://polymarket:polymarket@localhost:5433/polymarket",
    )
    db.init_pool(dsn)
    storage = Storage()

    app.state.storage = storage
    app.state.seed_cfg = seed_cfg or {}
    app.state.watcher_state = WatcherState()

    # Routes (registered before static so API paths are never caught by SPA fallback)
    app.include_router(watcher.router)
    app.include_router(alerts.router)
    app.include_router(alerts_ws_router)  # WebSocket -- no prefix, must be before StaticFiles
    app.include_router(wallets.router)
    app.include_router(baskets.router)
    app.include_router(positions.router)
    app.include_router(settings.router)

    # Serve built React frontend (production)
    if _CLIENT_DIST.exists():
        _index_html = (_CLIENT_DIST / "index.html").read_text(encoding="utf-8")
        _patched_html = _inject_base_path(_index_html)

        async def _serve_spa(request: Request, full_path: str = "") -> HTMLResponse:
            return HTMLResponse(_patched_html)

        # Mount /assets BEFORE the SPA catch-all so that JS/CSS chunk requests are
        # served by StaticFiles and never reach the catch-all route.
        assets_dir = _CLIENT_DIST / "assets"
        if assets_dir.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="assets",
            )

        # SPA catch-all: any path not matched above returns the patched index.html
        # so that client-side routing works for deep links.
        app.add_api_route("/", _serve_spa, include_in_schema=False)
        app.add_api_route("/{full_path:path}", _serve_spa, include_in_schema=False)

        logger.info(
            "Serving React frontend from %s (BASE_PATH=%s)", _CLIENT_DIST, _BASE_PATH
        )
    else:
        logger.warning(
            "React dist not found at %s -- run `cd web/client && npm run build` first.",
            _CLIENT_DIST,
        )

    return app
