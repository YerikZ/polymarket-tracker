import logging
from collections import Counter
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


# ── Shared helpers ────────────────────────────────────────────────────────────

def _dt(t: dict) -> datetime:
    """Parse traded_at from a trade dict to a UTC-aware datetime."""
    v = t.get("traded_at")
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _is_winner(t: dict) -> bool:
    """Return True when a resolved buy trade ended up winning."""
    wt = (t.get("winner_token_id") or "").strip()
    wo = (t.get("winner_outcome") or "").strip().lower()
    tt = (t.get("token_id") or "").strip()
    to_ = (t.get("outcome") or "").strip().lower()
    return bool((wt and wt == tt) or (wo and wo == to_))


def _is_resolved(t: dict) -> bool:
    """Return True when a trade has resolution data (resolved flag or winner set)."""
    return bool(t.get("resolved") or t.get("winner_outcome") or t.get("winner_token_id"))


# ── Multi-horizon analytics ───────────────────────────────────────────────────

HORIZONS = [7, 14, 30, 60, 90, 120]

_MIN_CATEGORY_TRADES = 5  # minimum buys to count a category as "active"


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

    window = [t for t in trades if _dt(t) >= cutoff]
    if not window:
        return _empty_metrics()

    buys = [t for t in window if (t.get("side") or "").upper() == "BUY"]

    usdc_sizes = [float(t.get("usdc_size") or 0) for t in buys if float(t.get("usdc_size") or 0) > 0]
    unique_cids = {t.get("condition_id") for t in buys if t.get("condition_id")}
    all_dates = {_dt(t).date() for t in window}

    resolved_buys = [t for t in buys if _is_resolved(t)]
    wins = sum(1 for t in resolved_buys if _is_winner(t))
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


# ── Qualification scorecard ───────────────────────────────────────────────────

def _win_rate_for_window(buys: list[dict]) -> tuple[float | None, int]:
    """Return (win_rate, resolved_count) for a list of BUY trades."""
    resolved = [t for t in buys if _is_resolved(t)]
    if not resolved:
        return None, 0
    wins = sum(1 for t in resolved if _is_winner(t))
    return wins / len(resolved), len(resolved)


def compute_qualification_check(trades: list[dict]) -> dict[str, Any]:
    """Evaluate a wallet against 6 qualification criteria using its full trade history.

    Returns:
        status:  "qualified" | "not_qualified" | "insufficient_data"
        passes:  dict[criterion → bool | None]   (None = insufficient data)
        metrics: dict of raw values behind each criterion
    """
    # Import here to avoid a module-level circular import risk (scorer → analyzer cycle check)
    try:
        from .scorer import CATEGORY_KEYWORDS
    except ImportError:
        CATEGORY_KEYWORDS = {}

    now = datetime.now(timezone.utc)
    cutoff_30d  = now - timedelta(days=30)
    cutoff_90d  = now - timedelta(days=90)

    passes: dict[str, bool | None] = {
        "win_rate":    None,
        "track_record": None,
        "niche_focus": None,
        "frequency":   None,
        "accumulation": None,
        "no_decline":  None,
    }

    # ── Slice windows ────────────────────────────────────────────────────────
    all_buys = [t for t in trades if (t.get("side") or "").upper() == "BUY"]
    buys_90d  = [t for t in all_buys if _dt(t) >= cutoff_90d]
    buys_30d  = [t for t in all_buys if _dt(t) >= cutoff_30d]

    # ── Criterion 1: Win rate ≥60% across ≥50 resolved trades (90d) ─────────
    win_rate_90d, resolved_count_90d = _win_rate_for_window(buys_90d)
    if resolved_count_90d < 50:
        passes["win_rate"] = None   # insufficient sample
    else:
        passes["win_rate"] = (win_rate_90d or 0) >= 0.60

    # ── Criterion 2: Track record — oldest trade ≥120 days ago ──────────────
    if not trades:
        earliest_trade_days: float | None = None
        passes["track_record"] = None
    else:
        oldest_dt = min(_dt(t) for t in trades)
        earliest_trade_days = (now - oldest_dt).total_seconds() / 86400
        passes["track_record"] = earliest_trade_days >= 120

    # ── Criterion 3: Niche focus — 2–3 active categories ────────────────────
    category_counts: dict[str, int] = {}
    for t in all_buys:
        title = (t.get("title") or "").lower()
        for cat, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in title for kw in keywords):
                category_counts[cat] = category_counts.get(cat, 0) + 1
                break  # first match wins — no double-counting

    active_cats = [cat for cat, cnt in category_counts.items() if cnt >= _MIN_CATEGORY_TRADES]
    niche_count = len(active_cats)
    passes["niche_focus"] = None if niche_count == 0 else (1 <= niche_count <= 3)

    # ── Criterion 4: Frequency — <100 buys in rolling 30d window ────────────
    passes["frequency"] = len(buys_30d) < 100

    # ── Criterion 5: Position accumulation — avg >1 entry per market (90d) ──
    if buys_90d:
        entries_per_mkt = Counter(
            t["condition_id"] for t in buys_90d if t.get("condition_id")
        )
        avg_entries: float | None = (
            sum(entries_per_mkt.values()) / len(entries_per_mkt)
            if entries_per_mkt else None
        )
    else:
        avg_entries = None
    passes["accumulation"] = None if avg_entries is None else avg_entries > 1.0

    # ── Criterion 6: No decline — 30d win rate not trailing 90d by >10pp ────
    win_rate_30d, resolved_count_30d = _win_rate_for_window(buys_30d)
    if win_rate_30d is None or win_rate_90d is None or resolved_count_30d < 5:
        passes["no_decline"] = None
    else:
        passes["no_decline"] = (win_rate_90d - win_rate_30d) <= 0.10

    # ── Overall status ───────────────────────────────────────────────────────
    non_null = [v for v in passes.values() if v is not None]
    if len(non_null) < 4:
        status = "insufficient_data"
    elif all(non_null):
        status = "qualified"
    else:
        status = "not_qualified"

    return {
        "status": status,
        "passes": passes,
        "metrics": {
            "win_rate_90d":           win_rate_90d,
            "resolved_count_90d":     resolved_count_90d,
            "earliest_trade_days":    round(earliest_trade_days, 1) if earliest_trade_days is not None else None,
            "categories_detected":    active_cats,
            "niche_category_count":   niche_count,
            "trades_per_month":       float(len(buys_30d)),
            "avg_entries_per_market": round(avg_entries, 2) if avg_entries is not None else None,
            "win_rate_30d":           win_rate_30d,
        },
    }
