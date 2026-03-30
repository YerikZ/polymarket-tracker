from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from web.server import settings as settings_helpers
from web.server.watcher import start_watcher, stop_watcher

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    model_config = {"extra": "allow"}


@router.get("")
async def get_settings(request: Request):
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    return await asyncio.to_thread(
        settings_helpers.get_settings_masked, storage, seed_cfg
    )


@router.put("")
async def put_settings(body: SettingsUpdate, request: Request):
    storage = request.app.state.storage
    updates = body.model_dump()

    seed_cfg = request.app.state.seed_cfg
    await asyncio.to_thread(settings_helpers.put_settings, storage, updates)

    # Auto-restart watcher if it's running
    state = request.app.state.watcher_state
    if state.status == "running":
        await stop_watcher(state)
        new_cfg = await asyncio.to_thread(
            settings_helpers.get_settings, storage, seed_cfg
        )
        try:
            await start_watcher(state, storage, new_cfg)
        except Exception:
            pass  # Error is stored in state.error

    return await asyncio.to_thread(
        settings_helpers.get_settings_masked, storage, seed_cfg
    )
