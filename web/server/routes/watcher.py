from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from web.server import settings as settings_helpers

router = APIRouter(prefix="/api/watcher", tags=["watcher"])


@router.post("/start")
async def start(request: Request):
    state = request.app.state.watcher_state
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    cfg = await asyncio.to_thread(settings_helpers.get_settings, storage, seed_cfg)
    if not cfg:
        raise HTTPException(400, "No settings configured. Visit /settings first.")
    from web.server.watcher import start_watcher
    try:
        await start_watcher(state, storage, cfg)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"status": state.status}


@router.post("/stop")
async def stop(request: Request):
    state = request.app.state.watcher_state
    from web.server.watcher import stop_watcher
    await stop_watcher(state)
    return {"status": "stopped"}


@router.get("/status")
async def status(request: Request):
    state = request.app.state.watcher_state
    return {
        "status": state.status,
        "mode": state.mode,
        "wallets_tracked": state.wallets_tracked,
        "last_signal_at": state.last_signal_at,
        "copy_enabled": state.copy_enabled,
        "error": state.error,
    }
