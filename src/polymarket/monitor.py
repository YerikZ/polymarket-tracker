import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .client import PolymarketClient
from .models import Signal, Wallet
from .scanner import LeaderboardScanner
from .storage import Storage

logger = logging.getLogger(__name__)


def _is_raw_token_title(title: str) -> bool:
    """Return True when the activity API returned a raw token ID instead of a market title."""
    if not title:
        return True
    t = title.strip().lower()
    # Patterns the activity API emits when it hasn't resolved the market name yet:
    # "token: 16678291…"   "token:1667…"   very long hex-like strings with no spaces
    if t.startswith("token:") or t.startswith("token "):
        return True
    # Bare token ID: all hex digits, length > 30, no spaces
    if len(t) > 30 and " " not in t and all(c in "0123456789abcdef" for c in t):
        return True
    return False


class SignalMonitor:
    def __init__(
        self,
        client: PolymarketClient,
        scanner: LeaderboardScanner,
        storage: Storage,
        poll_interval: int = 300,
        min_position_usdc: float = 50.0,
        max_signal_age: int = 3600,
    ):
        self._client = client
        self._scanner = scanner
        self._storage = storage
        self._interval = poll_interval
        self._min_size = min_position_usdc
        self._max_signal_age = max_signal_age
        # condition_id → resolved title (avoid repeated GAMMA lookups within a session)
        self._title_cache: dict[str, str] = {}

    def run(self, on_signal: Callable[[Signal], None], force_refresh: bool = False) -> None:
        """Blocking loop — Ctrl-C to stop.

        Args:
            force_refresh: If True, bypass the leaderboard TTL cache on the
                           first fetch (useful after changing top_n in config).
        """
        logger.info(
            "Starting monitor: %d wallets, poll every %ds, min size $%.0f",
            self._scanner._top_n,
            self._interval,
            self._min_size,
        )
        poll_count = 0
        while True:
            poll_count += 1
            logger.info("Poll #%d …", poll_count)
            try:
                # Force refresh only on the first poll — subsequent polls use TTL normally
                wallets = self._scanner.fetch_top_wallets(force_refresh=force_refresh and poll_count == 1)
                for wallet in wallets:
                    signals = self._poll_wallet(wallet)
                    for sig in signals:
                        sig.alert_id = self._storage.append_alert(sig)
                        on_signal(sig)
            except Exception as exc:
                logger.error("Error during poll: %s", exc)

            logger.info("Sleeping %ds until next poll…", self._interval)
            time.sleep(self._interval)

    def _poll_wallet(self, wallet: Wallet) -> list[Signal]:
        try:
            raw_activity = self._client.activity(wallet.address, limit=50)
        except Exception as exc:
            logger.warning("Activity fetch failed for %s: %s", wallet.username, exc)
            return []

        snapshot_hashes = self._storage.get_snapshot(wallet.address)
        signals = self._diff_activity(wallet, raw_activity or [], snapshot_hashes)

        # Update snapshot with all seen hashes
        new_hashes = {t.get("transactionHash", "") for t in (raw_activity or []) if t.get("transactionHash")}
        self._storage.save_snapshot(wallet.address, snapshot_hashes | new_hashes)

        return signals

    def _diff_activity(
        self,
        wallet: Wallet,
        current: list[dict],
        snapshot_hashes: set[str],
    ) -> list[Signal]:
        signals: list[Signal] = []
        now = datetime.now(timezone.utc).isoformat()

        cutoff_ts = time.time() - self._max_signal_age

        for trade in current:
            tx_hash = trade.get("transactionHash", "")
            if not tx_hash or tx_hash in snapshot_hashes:
                continue

            # Skip stale trades — prevents historical activity from flooding as signals
            ts = int(trade.get("timestamp") or 0)
            if ts > 1e12:
                ts = ts // 1000  # milliseconds → seconds
            if ts and ts < cutoff_ts:
                continue

            side = trade.get("side", "").upper()
            usdc_size = float(trade.get("usdcSize") or 0)

            if side not in ("BUY", "SELL"):
                continue
            # Apply min-size filter only to buys; sells always propagate
            if side == "BUY" and usdc_size < self._min_size:
                continue

            condition_id = trade.get("conditionId", "")
            raw_title    = trade.get("title", "")
            market_title = self._resolve_title(condition_id, raw_title)

            signals.append(
                Signal(
                    wallet_address=wallet.address,
                    username=wallet.username,
                    wallet_rank=wallet.rank,
                    condition_id=condition_id,
                    market_title=market_title,
                    outcome=trade.get("outcome", ""),
                    side=side,
                    size=float(trade.get("size") or 0),
                    usdc_size=usdc_size,
                    price=float(trade.get("price") or 0),
                    detected_at=now,
                    transaction_hash=tx_hash,
                    token_id=trade.get("asset", ""),  # ERC-1155 token ID for order placement
                )
            )

        return signals

    def _resolve_title(self, condition_id: str, raw_title: str) -> str:
        """Return a human-readable market title, falling back to the GAMMA API when needed."""
        if not _is_raw_token_title(raw_title):
            # Activity API gave us a real title — cache it and move on
            if condition_id and raw_title:
                self._title_cache[condition_id] = raw_title
            return raw_title or "Unknown market"

        # Check session cache first (avoids an extra HTTP call for repeat trades)
        if condition_id and condition_id in self._title_cache:
            return self._title_cache[condition_id]

        # Fall back to GAMMA API
        if condition_id:
            try:
                markets = self._client.markets([condition_id])
                for m in (markets if isinstance(markets, list) else [markets]):
                    title = (
                        m.get("question") or m.get("title") or m.get("name") or ""
                    ).strip()
                    if title:
                        self._title_cache[condition_id] = title
                        return title
            except Exception as exc:
                logger.debug("Title resolution failed for %s: %s", condition_id[:16], exc)

        # Last resort: show a shortened token reference (never the raw 60-char token ID)
        return f"Market {condition_id[:10]}…" if condition_id else "Unknown market"
