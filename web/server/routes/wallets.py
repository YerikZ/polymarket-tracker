from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from polymarket.analyzer import WalletAnalyzer
from polymarket.client import PolymarketClient
from polymarket.scanner import LeaderboardScanner
from polymarket.scorer import WalletScorer
from web.server import settings as settings_helpers

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


@router.get("")
async def get_wallets(request: Request):
    storage = request.app.state.storage
    wallets = await asyncio.to_thread(storage.get_wallets)
    return wallets


@router.post("/refresh")
async def refresh_wallets(request: Request):
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    cfg = await asyncio.to_thread(settings_helpers.get_settings, storage, seed_cfg)

    def _refresh():
        client = PolymarketClient(
            request_delay=float(cfg.get("request_delay", 0.5)),
            max_retries=int(cfg.get("max_retries", 3)),
        )
        scanner = LeaderboardScanner(
            client=client,
            storage=storage,
            top_n=int(cfg.get("top_n", 100)),
            leaderboard_ttl=int(cfg.get("leaderboard_ttl", 3600)),
        )
        analyzer = WalletAnalyzer(client=client)

        wallets = scanner.fetch_top_wallets(force_refresh=True)
        stats_list = []
        for wallet in wallets:
            try:
                stats_list.append(analyzer.analyze(wallet))
            except Exception:
                continue

        WalletScorer().score_all(stats_list, storage=storage)
        return storage.get_wallets()

    return await asyncio.to_thread(_refresh)


@router.get("/{address}")
async def get_wallet(address: str, request: Request):
    storage = request.app.state.storage
    wallets = await asyncio.to_thread(storage.get_wallets)
    match = next((w for w in wallets if w["address"] == address), None)
    if not match:
        raise HTTPException(404, "Wallet not found")
    return match
