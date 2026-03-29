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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from polymarket import db
from polymarket.storage import Storage
from web.server.watcher import WatcherState
from web.server.routes import alerts, positions, settings, wallets, watcher

logger = logging.getLogger(__name__)

_CLIENT_DIST = Path(__file__).parent.parent / "client" / "dist"


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

    # Routes
    app.include_router(watcher.router)
    app.include_router(alerts.router)
    app.include_router(wallets.router)
    app.include_router(positions.router)
    app.include_router(settings.router)

    # Serve built React frontend (production)
    if _CLIENT_DIST.exists():
        app.mount("/", StaticFiles(directory=str(_CLIENT_DIST), html=True), name="static")
        logger.info("Serving React frontend from %s", _CLIENT_DIST)
    else:
        logger.warning(
            "React dist not found at %s — run `cd web/client && npm run build` first.",
            _CLIENT_DIST,
        )

    return app
