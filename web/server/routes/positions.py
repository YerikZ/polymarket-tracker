from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Query, Request

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
