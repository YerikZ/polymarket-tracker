import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from .client import PolymarketClient
from .models import Position, Trade, Wallet, WalletStats

logger = logging.getLogger(__name__)


class WalletAnalyzer:
    def __init__(self, client: PolymarketClient):
        self._client = client

    def analyze(self, wallet: Wallet) -> WalletStats:
        positions = self._fetch_positions(wallet.address)
        trades = self._fetch_recent_trades(wallet.address, days=30)

        return WalletStats(
            wallet=wallet,
            total_pnl=self._compute_total_pnl(positions),
            win_rate=self._compute_win_rate(positions),
            avg_position_size=self._compute_avg_size(positions),
            open_positions=positions,
            recent_trades=trades,
        )

    def _fetch_positions(self, address: str) -> list[Position]:
        try:
            raw = self._client.positions(address)
        except Exception as exc:
            logger.warning("Failed to fetch positions for %s: %s", address, exc)
            return []

        positions = []
        for p in raw or []:
            try:
                positions.append(
                    Position(
                        condition_id=p.get("conditionId", ""),
                        title=p.get("title", "Unknown market"),
                        outcome=p.get("outcome", ""),
                        size=float(p.get("size") or 0),
                        avg_price=float(p.get("avgPrice") or 0),
                        cur_price=float(p.get("curPrice") or 0),
                        initial_value=float(p.get("initialValue") or 0),
                        current_value=float(p.get("currentValue") or 0),
                        cash_pnl=float(p.get("cashPnl") or 0),
                        percent_pnl=float(p.get("percentPnl") or 0),
                        end_date=p.get("endDate"),
                        redeemable=bool(p.get("redeemable", False)),
                    )
                )
            except Exception as exc:
                logger.debug("Skipping malformed position: %s", exc)

        return positions

    def _fetch_recent_trades(self, address: str, days: int = 30) -> list[Trade]:
        try:
            raw = self._client.activity(address, limit=200)
        except Exception as exc:
            logger.warning("Failed to fetch activity for %s: %s", address, exc)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        trades = []

        for t in raw or []:
            try:
                ts = int(t.get("timestamp") or 0)
                # API returns milliseconds in some cases
                if ts > 1e12:
                    ts = ts // 1000
                if ts and datetime.fromtimestamp(ts, tz=timezone.utc) < cutoff:
                    continue
                trades.append(
                    Trade(
                        condition_id=t.get("conditionId", ""),
                        title=t.get("title", "Unknown market"),
                        outcome=t.get("outcome", ""),
                        side=t.get("side", ""),
                        size=float(t.get("size") or 0),
                        usdc_size=float(t.get("usdcSize") or 0),
                        price=float(t.get("price") or 0),
                        timestamp=ts,
                        transaction_hash=t.get("transactionHash", ""),
                    )
                )
            except Exception as exc:
                logger.debug("Skipping malformed trade: %s", exc)

        return trades

    def _compute_total_pnl(self, positions: list[Position]) -> float:
        return sum(p.cash_pnl for p in positions)

    def _compute_win_rate(self, positions: list[Position]) -> float:
        """Approximate: fraction of open positions currently in profit."""
        if not positions:
            return 0.0
        winning = sum(1 for p in positions if p.percent_pnl > 0)
        return winning / len(positions)

    def _compute_avg_size(self, positions: list[Position]) -> float:
        if not positions:
            return 0.0
        return sum(p.initial_value for p in positions) / len(positions)


# ── Multi-horizon analytics ───────────────────────────────────────────────────

HORIZONS = [7, 14, 30, 60, 90]


def _empty_metrics() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "buy_count": 0,
        "avg_order_usdc": 0.0,
        "median_order_usdc": 0.0,
        "total_invested": 0.0,
        "unique_markets": 0,
        "active_days": 0,
        "win_rate": None,
        "resolved_count": 0,
        "avg_entry_price": None,
    }


def compute_horizon_metrics(trades: list[dict], days: int) -> dict[str, Any]:
    """Compute performance metrics for all trades within the last `days` days.

    Each trade dict must have at minimum: side, usdc_size, price, traded_at,
    condition_id, token_id, outcome, resolved (bool|None), winner_outcome, winner_token_id.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # traded_at may be a datetime or an ISO string (from _row_to_dict)
    def _dt(t: dict) -> datetime:
        v = t.get("traded_at")
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    window = [t for t in trades if _dt(t) >= cutoff]
    if not window:
        return _empty_metrics()

    buys = [t for t in window if (t.get("side") or "").upper() == "BUY"]

    usdc_sizes = [float(t.get("usdc_size") or 0) for t in buys if float(t.get("usdc_size") or 0) > 0]
    unique_cids = {t.get("condition_id") for t in buys if t.get("condition_id")}
    all_dates = {_dt(t).date() for t in window}

    # Win rate — only among resolved markets
    resolved_buys = [t for t in buys if t.get("resolved")]
    wins = 0
    for t in resolved_buys:
        wt = (t.get("winner_token_id") or "").strip()
        wo = (t.get("winner_outcome") or "").strip().lower()
        tt = (t.get("token_id") or "").strip()
        to_ = (t.get("outcome") or "").strip().lower()
        if (wt and wt == tt) or (wo and wo == to_):
            wins += 1
    win_rate = (wins / len(resolved_buys)) if resolved_buys else None

    sorted_sizes = sorted(usdc_sizes)
    n = len(sorted_sizes)
    median = sorted_sizes[n // 2] if n else 0.0

    prices = [float(t.get("price") or 0) for t in buys if 0 < float(t.get("price") or 0) < 1]

    return {
        "trade_count": len(window),
        "buy_count": len(buys),
        "avg_order_usdc": sum(usdc_sizes) / len(usdc_sizes) if usdc_sizes else 0.0,
        "median_order_usdc": median,
        "total_invested": sum(usdc_sizes),
        "unique_markets": len(unique_cids),
        "active_days": len(all_dates),
        "win_rate": win_rate,
        "resolved_count": len(resolved_buys),
        "avg_entry_price": sum(prices) / len(prices) if prices else None,
    }


def compute_all_horizons(trades: list[dict]) -> dict[str, dict]:
    """Return metrics for each horizon in HORIZONS."""
    return {str(d): compute_horizon_metrics(trades, d) for d in HORIZONS}
