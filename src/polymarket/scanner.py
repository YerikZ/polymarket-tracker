import logging
from datetime import datetime, timezone

from .client import PolymarketClient
from .models import Wallet
from .storage import Storage

logger = logging.getLogger(__name__)


class LeaderboardScanner:
    def __init__(
        self,
        client: PolymarketClient,
        storage: Storage,
        top_n: int = 20,
        leaderboard_ttl: int = 3600,
    ):
        self._client = client
        self._storage = storage
        self._top_n = top_n
        self._ttl = leaderboard_ttl

    _PAGE_SIZE = 50  # API hard cap — returns at most 50 per call regardless of limit

    def fetch_top_wallets(self, force_refresh: bool = False) -> list[Wallet]:
        if not force_refresh and self._is_cache_fresh():
            raw = self._storage.get_wallets()
            wallets = [Wallet(**w) for w in raw]
            logger.debug("Loaded %d wallets from cache.", len(wallets))
            return wallets[: self._top_n]

        logger.info("Fetching leaderboard (top %d by all-time P&L)…", self._top_n)

        wallets: list[Wallet] = []
        now = datetime.now(timezone.utc).isoformat()
        offset = 0

        while len(wallets) < self._top_n:
            entries = self._client.leaderboard(
                limit=self._PAGE_SIZE,
                offset=offset,
            )
            if not entries:
                break  # API returned nothing — exhausted

            for entry in entries:
                if len(wallets) >= self._top_n:
                    break

                username = entry.get("userName") or entry.get("name") or entry.get("username") or ""
                pnl = float(entry.get("pnl") or entry.get("profitLoss") or 0)
                volume = float(entry.get("vol") or entry.get("tradingVolume") or entry.get("volume") or 0)
                rank = int(entry.get("rank") or 0)

                address = self._resolve_address(entry)
                if not address:
                    logger.debug("Skipping %s — could not resolve wallet address.", username)
                    continue

                wallets.append(
                    Wallet(
                        address=address,
                        username=username,
                        rank=rank,
                        pnl=pnl,
                        trading_volume=volume,
                        fetched_at=now,
                    )
                )

            if len(entries) < self._PAGE_SIZE:
                break  # partial page — no more data available

            offset += self._PAGE_SIZE

        if wallets:
            self._storage.save_wallets(wallets)
            logger.info("Saved %d wallets.", len(wallets))
        else:
            logger.warning("No wallets resolved from leaderboard.")

        return wallets

    def _resolve_address(self, entry: dict) -> str | None:
        # 1. Address may already be in the leaderboard entry
        for key in ("proxyWallet", "address", "walletAddress"):
            if entry.get(key):
                return entry[key]

        # 2. Try profiles API by username
        username = entry.get("name") or entry.get("username") or ""
        if username:
            profile = self._client.profile(username)
            if profile:
                for key in ("proxyWallet", "address", "walletAddress"):
                    if profile.get(key):
                        return profile[key]

        return None

    def _is_cache_fresh(self) -> bool:
        wallets = self._storage.get_wallets()
        if not wallets:
            return False
        # If the cache was built with fewer wallets than top_n, it's stale
        if len(wallets) < self._top_n:
            logger.debug(
                "Cache has %d wallets but top_n=%d — forcing refresh.",
                len(wallets), self._top_n,
            )
            return False
        try:
            fetched_at = wallets[0].get("fetched_at", "")
            dt = datetime.fromisoformat(fetched_at)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return age < self._ttl
        except Exception:
            return False
