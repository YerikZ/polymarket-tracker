from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["positions"])


@router.get("/positions")
async def get_positions(
    request: Request,
    mode: str = Query("all", pattern="^(dry|live|all)$"),
):
    storage = request.app.state.storage
    positions = await asyncio.to_thread(storage.get_paper_positions)
    if mode == "dry":
        positions = [p for p in positions if p.get("is_dry_run")]
    elif mode == "live":
        positions = [p for p in positions if not p.get("is_dry_run")]
    return positions


@router.post("/positions/refresh")
async def refresh_positions(request: Request):
    """Fetch live CLOB prices for all open positions, persist them, and return
    the updated positions list.  No auth needed — midpoint is a public endpoint.
    """
    storage = request.app.state.storage
    positions = await asyncio.to_thread(storage.get_paper_positions)

    # Separate open from already-resolved
    open_positions = [p for p in positions if p.get("position_status") == "open"]
    open_token_ids = list({
        p["token_id"] for p in open_positions if p.get("token_id")
    })

    # Fetch live midpoint prices for open token IDs (public CLOB endpoint)
    prices: dict[str, float] = {}
    if open_token_ids:
        def _fetch_prices() -> dict[str, float]:
            from polymarket.client import PolymarketClient
            client = PolymarketClient(request_delay=0.05, max_retries=2)
            return client.token_prices(open_token_ids)

        try:
            prices = await asyncio.to_thread(_fetch_prices)
        except Exception as exc:
            logger.warning("Price fetch failed during refresh: %s", exc)

    # Build update records for every position
    updates = []
    for pos in positions:
        tid    = pos.get("token_id", "")
        status = pos.get("position_status", "open")
        shares = float(pos.get("shares") or 0)

        if status == "won":
            cur_price = 1.0
        elif status == "lost":
            cur_price = 0.0
        elif tid in prices:
            cur_price = prices[tid]
            # Auto-resolve based on price threshold
            if cur_price >= 0.97:
                status = "won"
                cur_price = 1.0
            elif cur_price <= 0.03:
                status = "lost"
                cur_price = 0.0
        else:
            continue  # price unavailable — leave row untouched

        updates.append({
            "id":                 pos["id"],
            "current_price":      round(cur_price, 6),
            "current_value_usdc": round(cur_price * shares, 4),
            "position_status":    status,
            "resolution_outcome": pos.get("resolution_outcome", ""),
            "market_closed":      bool(pos.get("market_closed", False)),
        })

    if updates:
        await asyncio.to_thread(storage.update_position_prices, updates)
        logger.info("Refreshed prices for %d positions (%d open token IDs fetched)",
                    len(updates), len(open_token_ids))

    # Return the freshly-read positions after DB update
    return await asyncio.to_thread(storage.get_paper_positions)


@router.get("/pnl/summary")
async def get_pnl_summary(request: Request):
    storage = request.app.state.storage
    positions = await asyncio.to_thread(storage.get_paper_positions)
    today = date.today().isoformat()
    spent_today = await asyncio.to_thread(storage.get_daily_spend, today)

    cfg = storage.get_settings()
    daily_limit = float(
        cfg.get("copy_trading", {}).get("daily_limit_usdc", 1000.0)
    )

    open_positions = [p for p in positions if p.get("position_status") == "open"]
    closed_positions = [
        p for p in positions if p.get("position_status") in ("won", "lost", "closed")
    ]

    # PnL from open positions (current_value - spend)
    open_pnl = sum(
        (float(p.get("current_value_usdc") or 0) - float(p.get("spend_usdc") or 0))
        for p in open_positions
        if p.get("current_value_usdc") is not None
    )

    # PnL from closed positions
    won = sum(float(p.get("spend_usdc") or 0) for p in closed_positions if p.get("position_status") == "won")
    lost = sum(float(p.get("spend_usdc") or 0) for p in closed_positions if p.get("position_status") == "lost")
    closed_pnl = won - lost

    total_pnl = open_pnl + closed_pnl

    # Win rate: positions where we're in profit (or won)
    decided = [p for p in positions if p.get("position_status") in ("won", "lost")]
    win_rate = (
        len([p for p in decided if p["position_status"] == "won"]) / len(decided)
        if decided else None
    )

    return {
        "total_pnl": round(total_pnl, 2),
        "open_pnl": round(open_pnl, 2),
        "closed_pnl": round(closed_pnl, 2),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "open_count": len(open_positions),
        "total_positions": len(positions),
        "spent_today": round(spent_today, 2),
        "daily_limit": daily_limit,
        "remaining": round(max(0.0, daily_limit - spent_today), 2),
    }
