"""
Real-time trade stream via Polygon WebSocket (eth_subscribe logs).

Connects to a Polygon WSS RPC endpoint (e.g. Alchemy free tier) and subscribes
to OrderFilled events on both Polymarket CTF Exchange contracts. When a top
wallet trades, a Signal is emitted *immediately* — no polling delay.

Setup
-----
1. Get a free Alchemy key: https://www.alchemy.com
   → Create an app → Polygon Mainnet → copy the WebSocket URL
2. Set it in config.yaml:  polygon_wss: "wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY"
   or via env var:          POLYMARKET_POLYGON_WSS=wss://...

CTF Exchange event decoded
--------------------------
event OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    uint256 makerAssetId,    ← 0 = USDC collateral
    uint256 takerAssetId,    ← 0 = USDC collateral
    uint256 makerAmountFilled,
    uint256 takerAmountFilled,
    uint256 fee
)
USDC has 6 decimals; outcome tokens have 18 decimals.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import websockets
from web3 import Web3

from .client import PolymarketClient
from .models import Signal, Wallet
from .scanner import LeaderboardScanner
from .storage import Storage

logger = logging.getLogger(__name__)

# ── Polymarket contracts on Polygon mainnet ───────────────────────────────────
CTF_EXCHANGE          = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# keccak256("OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
ORDER_FILLED_TOPIC = "0x" + Web3.keccak(
    text="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()

USDC_ASSET_ID  = 0     # asset ID 0 = USDC collateral inside the CTF Exchange
USDC_DECIMALS  = 1e6   # USDC.e on Polygon: 6 decimal places
TOKEN_DECIMALS = 1e18  # CTF outcome tokens: 18 decimal places


# ── Stream class ──────────────────────────────────────────────────────────────

class PolymarketStream:
    """
    Async WebSocket stream that detects top-wallet trades in real time.

    Usage::

        stream = PolymarketStream(wss_url, client, scanner, storage, wallets)
        await stream.run(on_signal)   # blocks; reconnects automatically
    """

    def __init__(
        self,
        wss_url: str,
        client: PolymarketClient,
        scanner: LeaderboardScanner,
        storage: Storage,
        top_wallets: list[Wallet],
        min_position_usdc: float = 50.0,
        wallet_refresh_interval: int = 600,  # seconds between leaderboard refreshes
    ) -> None:
        self._wss_url          = wss_url
        self._client           = client
        self._scanner          = scanner
        self._storage          = storage
        self._min_size         = min_position_usdc
        self._refresh_interval = wallet_refresh_interval

        # address (lower) → Wallet — refreshed in background
        self._wallets: dict[str, Wallet] = {
            w.address.lower(): w for w in top_wallets
        }
        # token_id → {condition_id, title, outcome} — populated on first trade
        self._market_cache: dict[str, dict] = {}
        # tx hashes seen this session — prevents duplicates on reconnect
        self._seen_tx: set[str] = set()

    # ── public ───────────────────────────────────────────────────────────────

    async def run(
        self, on_signal: Callable[[Signal], Awaitable[None]]
    ) -> None:
        """Connect, subscribe and stream forever.  Auto-reconnects on any error."""
        refresh_task = asyncio.create_task(self._wallet_refresh_loop())
        try:
            while True:
                try:
                    await self._connect_and_stream(on_signal)
                except websockets.exceptions.ConnectionClosed as exc:
                    logger.warning("WebSocket closed (%s) — reconnecting in 5 s…", exc)
                    await asyncio.sleep(5)
                except Exception as exc:
                    logger.error("Stream error: %s — reconnecting in 10 s…", exc)
                    await asyncio.sleep(10)
        finally:
            refresh_task.cancel()

    # ── internals ─────────────────────────────────────────────────────────────

    async def _connect_and_stream(
        self, on_signal: Callable[[Signal], Awaitable[None]]
    ) -> None:
        async with websockets.connect(
            self._wss_url, ping_interval=20, ping_timeout=30
        ) as ws:
            sub_id = await self._subscribe(ws)
            logger.info(
                "Streaming OrderFilled events (sub=%s) — tracking %d wallets",
                sub_id, len(self._wallets),
            )
            async for raw in ws:
                msg = json.loads(raw)
                result = msg.get("params", {}).get("result")
                if result and isinstance(result, dict):
                    await self._handle_log(result, on_signal)

    async def _subscribe(self, ws) -> str:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "logs",
                {
                    "address": [CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE],
                    "topics":  [ORDER_FILLED_TOPIC],
                },
            ],
        }))
        resp = json.loads(await ws.recv())
        if "error" in resp:
            raise RuntimeError(f"eth_subscribe failed: {resp['error']}")
        return resp.get("result", "?")

    async def _handle_log(
        self, log: dict, on_signal: Callable[[Signal], Awaitable[None]]
    ) -> None:
        # Skip chain-reorg removals
        if log.get("removed"):
            return

        tx_hash = log.get("transactionHash", "")
        if tx_hash in self._seen_tx:
            return
        self._seen_tx.add(tx_hash)
        # Keep seen-set from growing unbounded across long sessions
        if len(self._seen_tx) > 10_000:
            self._seen_tx = set(list(self._seen_tx)[-5_000:])

        try:
            ev = _decode_log(log)
        except Exception as exc:
            logger.debug("Failed to decode log %s: %s", tx_hash[:12], exc)
            return

        maker = ev["maker"]
        taker = ev["taker"]
        wallet = self._wallets.get(maker) or self._wallets.get(taker)
        if not wallet:
            return

        # ── Interpret from tracked wallet's perspective ───────────────────────
        is_maker  = (maker == wallet.address.lower())
        ma, ta    = ev["maker_asset_id"], ev["taker_asset_id"]
        mamt, tamt = ev["maker_amount"],  ev["taker_amount"]

        if is_maker:
            if ma == USDC_ASSET_ID:           # maker spending USDC → BUY
                side, token_id = "BUY",  str(ta)
                usdc   = mamt / USDC_DECIMALS
                shares = tamt / TOKEN_DECIMALS
            else:                             # maker selling tokens → SELL
                side, token_id = "SELL", str(ma)
                usdc   = tamt / USDC_DECIMALS
                shares = mamt / TOKEN_DECIMALS
        else:                                 # wallet is taker
            if ta == USDC_ASSET_ID:           # taker spending USDC → BUY
                side, token_id = "BUY",  str(ma)
                usdc   = tamt / USDC_DECIMALS
                shares = mamt / TOKEN_DECIMALS
            else:                             # taker selling tokens → SELL
                side, token_id = "SELL", str(ta)
                usdc   = mamt / USDC_DECIMALS
                shares = tamt / TOKEN_DECIMALS

        if side not in ("BUY", "SELL"):
            return
        # Apply min-size filter only to buys; sells always propagate
        if side == "BUY" and usdc < self._min_size:
            return

        price  = round(usdc / shares, 6) if shares > 0 else 0.0
        market = await self._market_info(token_id)

        # Skip resolved / closed markets
        if not market.get("active", True) or market.get("closed", False):
            logger.debug("Skipping signal for closed market: %s", market.get("title", token_id[:16]))
            return

        sig = Signal(
            wallet_address=wallet.address,
            username=wallet.username,
            wallet_rank=wallet.rank,
            condition_id=market.get("condition_id", ""),
            market_title=market.get("title") or f"(resolving… token:{token_id[:12]}…)",
            outcome=market.get("outcome", ""),
            side=side,
            size=round(shares, 4),
            usdc_size=round(usdc, 2),
            price=price,
            detected_at=datetime.now(timezone.utc).isoformat(),
            transaction_hash=tx_hash,
            token_id=token_id,
        )
        # Run the blocking DB insert in a thread so we never stall the event loop
        await asyncio.to_thread(self._storage.append_alert, sig)
        await on_signal(sig)

    async def _market_info(self, token_id: str) -> dict:
        """Fetch and cache {condition_id, title, outcome} for a token ID.

        Only caches entries that have a resolved title — so transient API
        failures or missing metadata trigger a fresh lookup next time rather
        than permanently storing a blank/token-id title.
        """
        cached = self._market_cache.get(token_id)
        if cached and cached.get("title"):   # only trust cache if title is populated
            return cached
        try:
            raw = await asyncio.to_thread(
                self._client.get,
                "https://gamma-api.polymarket.com",
                "/markets",
                {"token_id": token_id},
            )
            for m in (raw if isinstance(raw, list) else [raw]):
                title = (
                    m.get("question")
                    or m.get("title")
                    or m.get("name")
                    or ""
                ).strip()
                if not title:
                    continue

                condition_id = m.get("conditionId", "")

                # Index by every token in the tokens array (when present)
                for tok in (m.get("tokens") or []):
                    tid = str(tok.get("token_id") or tok.get("tokenId", ""))
                    if not tid:
                        continue
                    self._market_cache[tid] = {
                        "condition_id": condition_id,
                        "title":        title,
                        "outcome":      tok.get("outcome", ""),
                    }

                # ALWAYS also index by the queried token_id.
                # The Gamma API frequently returns tokens:null for older markets,
                # so the loop above produces nothing — but we still have the title.
                # Store active/closed status so handlers can skip resolved markets
                active = m.get("active", True)
                closed = m.get("closed", False)

                if token_id not in self._market_cache:
                    self._market_cache[token_id] = {
                        "condition_id": condition_id,
                        "title":        title,
                        "outcome":      "",   # outcome unknown without tokens array
                        "active":       active,
                        "closed":       closed,
                    }

                # Also update active/closed on any per-token entries written above
                for tid in list(self._market_cache):
                    if self._market_cache[tid].get("condition_id") == condition_id:
                        self._market_cache[tid].setdefault("active", active)
                        self._market_cache[tid].setdefault("closed", closed)
        except Exception as exc:
            logger.debug("Market lookup failed for token %s…: %s", token_id[:16], exc)
        return self._market_cache.get(token_id, {})

    async def _wallet_refresh_loop(self) -> None:
        """Re-fetch the leaderboard periodically so new top wallets are tracked."""
        while True:
            await asyncio.sleep(self._refresh_interval)
            try:
                wallets = await asyncio.to_thread(
                    self._scanner.fetch_top_wallets, True
                )
                self._wallets = {w.address.lower(): w for w in wallets}
                logger.info(
                    "Wallet list refreshed — %d wallets tracked", len(self._wallets)
                )
            except Exception as exc:
                logger.warning("Wallet refresh failed: %s", exc)


# ── Pure log decoder (no class state needed) ──────────────────────────────────

def _decode_log(log: dict) -> dict:
    """
    Decode a raw eth_subscribe log dict into an OrderFilled field map.

    topics[0] = event sig hash  (not used here)
    topics[1] = orderHash       (indexed bytes32)
    topics[2] = maker           (indexed address, zero-padded to 32 bytes)
    topics[3] = taker           (indexed address, zero-padded to 32 bytes)

    data = ABI-encoded uint256 × 5:
      [0] makerAssetId  [1] takerAssetId  [2] makerAmountFilled
      [3] takerAmountFilled  [4] fee
    """
    topics = log["topics"]
    raw    = log.get("data", "0x")
    data   = raw[2:] if raw.startswith("0x") else raw

    # Addresses are right-aligned in their 32-byte topic slots
    maker = "0x" + topics[2][-40:]
    taker = "0x" + topics[3][-40:]

    # Each non-indexed field is 32 bytes = 64 hex chars
    chunks = [data[i : i + 64] for i in range(0, len(data), 64)]
    def u(i: int) -> int:
        return int(chunks[i], 16) if i < len(chunks) and chunks[i] else 0

    return {
        "maker":          maker.lower(),
        "taker":          taker.lower(),
        "maker_asset_id": u(0),
        "taker_asset_id": u(1),
        "maker_amount":   u(2),
        "taker_amount":   u(3),
    }
