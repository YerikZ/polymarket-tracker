import logging
from datetime import datetime, timezone, timedelta

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
