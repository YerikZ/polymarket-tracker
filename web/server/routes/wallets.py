from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from polymarket.analyzer import WalletAnalyzer, compute_all_horizons
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


@router.post("/fetch-all-trades")
async def fetch_all_wallet_trades(request: Request):
    """Fetch and refresh trade history for every known wallet sequentially."""
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    cfg = await asyncio.to_thread(settings_helpers.get_settings, storage, seed_cfg)

    def _run():
        wallets = storage.get_wallets()
        total = len(wallets)
        fetched = 0
        errors = 0
        for w in wallets:
            try:
                _fetch_and_compute(w["address"], storage, cfg, force=True)
                fetched += 1
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "fetch-all-trades failed for %s: %s", w["address"][:10], exc
                )
                errors += 1
        return {"status": "ok", "total": total, "fetched": fetched, "errors": errors}

    return await asyncio.to_thread(_run)


@router.get("/{address}")
async def get_wallet(address: str, request: Request):
    storage = request.app.state.storage
    wallets = await asyncio.to_thread(storage.get_wallets)
    match = next((w for w in wallets if w["address"] == address), None)
    if not match:
        raise HTTPException(404, "Wallet not found")
    return match


def _build_client(cfg: dict) -> PolymarketClient:
    return PolymarketClient(
        request_delay=float(cfg.get("request_delay", 0.5)),
        max_retries=int(cfg.get("max_retries", 3)),
        proxy_url=cfg.get("proxy_url", "").strip(),
        proxy_username=cfg.get("proxy_username", "").strip(),
        proxy_password=cfg.get("proxy_password", "").strip(),
    )


_TRADE_TTL_SECONDS = 3600  # 1 hour cache before re-fetching


def _fetch_and_compute(address: str, storage, cfg: dict, force: bool = False) -> dict:
    """Fetch trade history and compute horizon metrics. Blocking — run in thread."""
    # Check freshness
    last = storage.get_trade_last_fetched_at(address)
    needs_fetch = (
        force
        or last is None
        or (datetime.now(timezone.utc) - last).total_seconds() > _TRADE_TTL_SECONDS
    )

    inserted = 0
    if needs_fetch:
        client = _build_client(cfg)
        raw_trades = client.activity_paginated(address, days=90)
        # Look up username from wallets table
        wallets = storage.get_wallets()
        username = next((w["username"] for w in wallets if w["address"] == address), "")
        inserted = storage.upsert_wallet_trades(address, raw_trades, username=username)

        # Resolve market outcomes for all traded condition_ids
        cids = list({t.get("conditionId") or t.get("condition_id") or "" for t in raw_trades})
        cids = [c for c in cids if c]
        unresolved = storage.get_unresolved_condition_ids(cids)
        if unresolved:
            try:
                all_statuses: dict = {}
                for batch, _, _ in client.market_statuses(unresolved):
                    all_statuses.update(batch)
                storage.upsert_market_outcomes(all_statuses)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("market_statuses fetch failed: %s", exc)

    trades = storage.get_wallet_trades(address, since_days=90)
    horizons = compute_all_horizons(trades)
    last_fetched = storage.get_trade_last_fetched_at(address)

    return {
        "address": address,
        "last_fetched_at": last_fetched.isoformat() if last_fetched else None,
        "horizons": horizons,
        "raw_trade_count": len(trades),
        "inserted": inserted,
    }


@router.get("/{address}/trades")
async def get_wallet_trades(address: str, request: Request, force: bool = False):
    """Return multi-horizon metrics for a wallet. Fetches trade history if stale."""
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    cfg = await asyncio.to_thread(settings_helpers.get_settings, storage, seed_cfg)
    return await asyncio.to_thread(_fetch_and_compute, address, storage, cfg, force)


@router.post("/{address}/fetch-trades")
async def fetch_wallet_trades(address: str, request: Request):
    """Force-refresh trade history and recompute metrics."""
    storage = request.app.state.storage
    seed_cfg = request.app.state.seed_cfg
    cfg = await asyncio.to_thread(settings_helpers.get_settings, storage, seed_cfg)
    result = await asyncio.to_thread(_fetch_and_compute, address, storage, cfg, force=True)
    return {"status": "ok", "inserted": result["inserted"], "raw_trade_count": result["raw_trade_count"]}
