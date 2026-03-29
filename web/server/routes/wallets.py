from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


@router.get("")
async def get_wallets(request: Request):
    storage = request.app.state.storage
    wallets = await asyncio.to_thread(storage.get_wallets)
    return wallets


@router.get("/{address}")
async def get_wallet(address: str, request: Request):
    storage = request.app.state.storage
    wallets = await asyncio.to_thread(storage.get_wallets)
    match = next((w for w in wallets if w["address"] == address), None)
    if not match:
        raise HTTPException(404, "Wallet not found")
    return match
