"""
Copy-trade engine: places Polymarket orders that mirror signals from top wallets.

Sizing modes (pick one via config):
  fixed         — always spend a fixed USDC amount (e.g. $50)
  pct_balance   — spend X% of your current USDC balance (e.g. 2%)
  mirror_pct    — spend X% of what the original trader spent (e.g. 1%)

Safety guardrails (always active):
  max_trade_usdc   — hard cap per single order
  daily_limit_usdc — total USDC cap across all orders today

Set dry_run=True to simulate without submitting any orders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .models import CopyResult, Signal, WalletScore
from .scorer import CATEGORY_KEYWORDS
from .storage import Storage


def _expand_keywords(keywords: list[str]) -> list[str]:
    """Expand category names (e.g. 'sports') into their full keyword lists.

    Any entry that matches a known category name (case-insensitive) is replaced
    with all keywords for that category. Literal keywords pass through unchanged.

    Example:
        ["sports", "oscar"] → ["nba", "nfl", ..., "oscar"]
    """
    expanded = []
    known = {k.lower(): v for k, v in CATEGORY_KEYWORDS.items()}
    for entry in keywords:
        cat_keywords = known.get(entry.lower())
        if cat_keywords:
            expanded.extend(cat_keywords)
        else:
            expanded.append(entry)
    return expanded

logger = logging.getLogger(__name__)

# Lazy import so the tool works without py-clob-client for users who only watch
def _clob_imports():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    from py_clob_client.order_builder.constants import BUY, SELL
    return ClobClient, OrderArgs, OrderType, BUY, SELL, BalanceAllowanceParams, AssetType


@dataclass
class CopierConfig:
    # Auth
    private_key: str
    funder: str                          # proxy wallet address (same as your Polymarket address)
    chain_id: int = 137                  # Polygon mainnet
    signature_type: int = 2              # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE (default for Polymarket proxy wallets)

    # Sizing mode — exactly one should drive the spend amount
    sizing_mode: str = "fixed"           # "fixed" | "pct_balance" | "mirror_pct"
    fixed_usdc: float = 50.0             # baseline spend when signal.usdc_size == reference_trade_usdc
    reference_trade_usdc: float = 50.0   # reference signal size for fixed mode proportional scaling
    pct_balance: float = 0.02            # used when sizing_mode = "pct_balance" (2%)
    mirror_pct: float = 0.01             # used when sizing_mode = "mirror_pct" (1% of original)

    # Safety limits
    max_trade_usdc: float = 500.0        # hard cap per trade
    daily_limit_usdc: float = 1000.0     # total cap for today
    min_order_size_cap: float = 10.0     # skip if market's min_order_size exceeds this (avoids forced large buys)

    # Execution
    dry_run: bool = True                 # True = simulate only, never submit
    slippage: float = 0.01              # add this to price for better fill (e.g. 0.01 = +1¢)

    # Market filters
    blocked_keywords: list = field(default_factory=list)
    # Any market whose title contains one of these words (case-insensitive) is skipped.
    # Example: ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana"]

    # Scoring
    min_score: float = 50.0             # skip wallets with score below this (0 = disable)
    score_scale_size: bool = True        # scale position size by wallet's copy_size_pct


class CopyTrader:
    def __init__(self, config: CopierConfig, storage: Storage):
        self._cfg = config
        self._storage = storage
        self._clob: object | None = None  # initialised lazily on first trade
        self._scores: dict[str, WalletScore] = {}  # address → latest score
        self._cap_hit = False             # True once daily cap reached in live mode
        # Pre-expand category names → keyword lists once at startup
        self._blocked = _expand_keywords(config.blocked_keywords)

    def update_scores(self, scores: dict[str, WalletScore]) -> None:
        """Called by cmd_watch after computing/refreshing wallet scores."""
        self._scores = scores
        logger.info("Score cache updated for %d wallets.", len(scores))

    def is_daily_limit_reached(self) -> bool:
        """Return True if today's spend has hit or exceeded the daily cap."""
        spent = self._storage.get_daily_spend(date.today().isoformat())
        return spent >= self._cfg.daily_limit_usdc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def copy(self, signal: Signal) -> CopyResult:
        """Place (or simulate) a copy order for the given signal."""
        if not signal.token_id:
            return CopyResult(
                signal=signal, status="skipped",
                reason="No token_id in signal — cannot place order without ERC-1155 asset ID",
            )

        # Skip resolved markets — price is 0 (lost) or 1 (won), nothing left to trade
        if signal.price <= 0.01 or signal.price >= 0.97:
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Market appears resolved (price={signal.price:.4f}). Skipping.",
            )

        # Skip blocked market categories / keywords
        title_lower = signal.market_title.lower()
        if self._blocked:
            for kw in self._blocked:
                if kw.lower() in title_lower:
                    return CopyResult(
                        signal=signal, status="skipped",
                        reason=f"Market blocked by keyword '{kw}': {signal.market_title[:50]}",
                    )

        # Skip if already invested in this market
        if self._storage.has_paper_position(signal.condition_id, signal.token_id):
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Already have an open position in this market ({signal.market_title[:40]})",
            )

        # Score-based filtering
        wallet_score = self._scores.get(signal.wallet_address)
        if wallet_score and self._cfg.min_score > 0:
            if wallet_score.copy_size_pct == 0.0:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Wallet score too low (tier {wallet_score.copy_tier}, "
                           f"{wallet_score.total:.0f}/100 < min {self._cfg.min_score:.0f})",
                )

        # Compute USDC to spend
        balance = self._get_balance()
        spend = self._compute_spend(signal, balance)

        # Scale spend by wallet's copy_size_pct if scoring is enabled
        if wallet_score and self._cfg.score_scale_size and wallet_score.copy_size_pct < 1.0:
            original_spend = spend
            spend = round(spend * wallet_score.copy_size_pct, 2)
            logger.info(
                "Score scaling: tier %s (%.0f/100) → spend $%.2f → $%.2f (×%.0f%%)",
                wallet_score.copy_tier, wallet_score.total,
                original_spend, spend, wallet_score.copy_size_pct * 100,
            )

        if spend <= 0:
            return CopyResult(signal=signal, status="skipped", reason="Computed spend is $0")

        # Check daily limit
        spent_today = self._storage.get_daily_spend(date.today().isoformat())
        if spent_today + spend > self._cfg.daily_limit_usdc:
            remaining = max(0.0, self._cfg.daily_limit_usdc - spent_today)
            if self._cfg.dry_run:
                # Dry-run: just skip, nothing to fall back to
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Daily limit reached (${self._cfg.daily_limit_usdc:.0f}). "
                           f"Spent today: ${spent_today:.2f}, remaining: ${remaining:.2f}",
                )
            # Live mode: switch to shadow dry-run and keep watching
            if not self._cap_hit:
                self._cap_hit = True
                logger.warning(
                    "Daily cap of $%.0f reached (spent $%.2f). "
                    "Switching to shadow dry-run — signals will be recorded but no real orders placed.",
                    self._cfg.daily_limit_usdc, spent_today,
                )
            # Fall through with _cap_hit=True — handled in execution branch below

        order_price = round(signal.price + self._cfg.slippage, 4)
        order_price = min(order_price, 0.99)  # price can't exceed 0.99 on Polymarket

        # Fetch the per-market minimum order size from the CLOB order book.
        min_order_size = 1.0  # safe default if fetch fails
        if not self._cfg.dry_run:
            try:
                book = self._get_client().get_order_book(signal.token_id)
                if book.min_order_size:
                    min_order_size = float(book.min_order_size)
                    logger.debug("Market min_order_size: %.2f shares", min_order_size)
            except Exception as exc:
                logger.debug("Could not fetch order book min size: %s", exc)

        # Skip if the market's minimum is above our cap (prevents forced large buys)
        if min_order_size > self._cfg.min_order_size_cap:
            return CopyResult(
                signal=signal, status="skipped",
                reason=(
                    f"Market requires {min_order_size:.0f} shares minimum, "
                    f"exceeds cap of {self._cfg.min_order_size_cap:.0f}. "
                    f"Raise min_order_size_cap in config to allow."
                ),
            )

        # Bump spend up to meet the market minimum if needed, then cap at max_trade_usdc
        min_required = round(min_order_size * order_price, 2)
        if spend < min_required:
            logger.debug("Spend $%.2f below min floor $%.2f — bumping up", spend, min_required)
            spend = min_required

        # Cap per trade
        spend = min(spend, self._cfg.max_trade_usdc)
        shares = round(spend / order_price, 2)

        if self._cfg.dry_run or self._cap_hit:
            label = "SHADOW DRY-RUN (cap hit)" if self._cap_hit else "DRY RUN"
            logger.info(
                "[%s] Would BUY %.2f shares of %s @ $%.4f (≈$%.2f USDC)",
                label, shares, signal.market_title[:40], order_price, spend,
            )
            self._storage.append_paper_position({
                "condition_id": signal.condition_id,
                "token_id": signal.token_id,
                "market_title": signal.market_title,
                "outcome": signal.outcome,
                "entry_price": order_price,
                "shares": shares,
                "spend_usdc": spend,
                "opened_at": signal.detected_at,
                "wallet_address": signal.wallet_address,
                "username": signal.username,
                "wallet_rank": signal.wallet_rank,
                "is_dry_run": True,  # shadow orders are always marked dry-run
            })
            if self._cfg.dry_run:
                # Only count dry-run spend toward daily limit (shadow orders don't count — cap already hit)
                self._storage.record_daily_spend(date.today().isoformat(), spend)
            status_label = "shadow" if self._cap_hit else "dry_run"
            return CopyResult(
                signal=signal, status=status_label,
                reason=f"{label}: would place BUY {shares:.2f} shares @ ${order_price:.4f} (≈${spend:.2f} USDC)",
                spend_usdc=spend,
                price=order_price,
            )

        # Live execution
        return self._place_order(signal, shares, order_price, spend)

    def get_balance(self) -> float:
        """Return USDC balance (public helper for CLI)."""
        return self._get_balance()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._clob is None:
            ClobClient, _, _, _, _, _, _ = _clob_imports()
            self._clob = ClobClient(
                "https://clob.polymarket.com",
                key=self._cfg.private_key,
                chain_id=self._cfg.chain_id,
                signature_type=self._cfg.signature_type,
                funder=self._cfg.funder,
            )
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            signer = self._clob.get_address()
            logger.info(
                "CLOB client ready — signer (EOA): %s | funder (proxy): %s",
                signer, self._cfg.funder,
            )
            if signer.lower() == self._cfg.funder.lower():
                logger.info("EOA mode: signer == funder (signature_type should be 0)")
            else:
                logger.info(
                    "Proxy mode: signer is operator of funder proxy wallet "
                    "(ensure %s is approved on the CTF Exchange)", signer
                )
        return self._clob

    def _get_balance(self) -> float:
        if self._cfg.dry_run and not self._cfg.private_key:
            return 0.0
        try:
            _, _, _, _, _, BalanceAllowanceParams, AssetType = _clob_imports()
            resp = self._get_client().get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # resp is a dict: {"balance": "1000000", "allowance": "..."}
            raw = resp.get("balance", 0)
            return float(raw) / 1e6  # USDC has 6 decimals on-chain
        except Exception as exc:
            logger.warning("Balance fetch failed: %s", exc)
            return 0.0

    def _compute_spend(self, signal: Signal, balance: float) -> float:
        mode = self._cfg.sizing_mode
        if mode == "fixed":
            # Proportional scaling: fixed_usdc is the baseline for reference_trade_usdc.
            # A larger signal → proportionally more spend; smaller signal → less.
            ref = self._cfg.reference_trade_usdc
            if ref > 0 and signal.usdc_size > 0:
                spend = self._cfg.fixed_usdc * (signal.usdc_size / ref)
            else:
                spend = self._cfg.fixed_usdc
            logger.debug(
                "Fixed proportional: $%.2f × (%.2f / %.2f) = $%.2f",
                self._cfg.fixed_usdc, signal.usdc_size, ref, spend,
            )
            return round(spend, 2)
        if mode == "pct_balance":
            return balance * self._cfg.pct_balance
        if mode == "mirror_pct":
            return signal.usdc_size * self._cfg.mirror_pct
        logger.warning("Unknown sizing_mode '%s', defaulting to fixed", mode)
        return self._cfg.fixed_usdc

    def _place_order(self, signal: Signal, shares: float, price: float, spend: float) -> CopyResult:
        try:
            ClobClient, OrderArgs, OrderType, BUY, _, _, _ = _clob_imports()
            client = self._get_client()

            order_args = OrderArgs(
                token_id=signal.token_id,
                price=price,
                size=shares,
                side=BUY,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)

            if resp.get("success") or resp.get("orderID"):
                order_id = resp.get("orderID", "")
                logger.info(
                    "Order placed: %s — BUY %.2f shares @ $%.4f (≈$%.2f USDC)",
                    order_id, shares, price, spend,
                )
                self._storage.record_daily_spend(date.today().isoformat(), spend)
                # Record live trade so polymarket pnl can track it alongside dry-run positions
                self._storage.append_paper_position({
                    "condition_id":  signal.condition_id,
                    "token_id":      signal.token_id,
                    "market_title":  signal.market_title,
                    "outcome":       signal.outcome,
                    "entry_price":   price,
                    "shares":        shares,
                    "spend_usdc":    spend,
                    "opened_at":     signal.detected_at,
                    "wallet_address": signal.wallet_address,
                    "username":      signal.username,
                    "wallet_rank":   signal.wallet_rank,
                    "is_dry_run":    False,
                })
                return CopyResult(
                    signal=signal, status="placed",
                    reason=f"BUY {shares:.2f} shares @ ${price:.4f} (≈${spend:.2f} USDC)",
                    order_id=order_id,
                    spend_usdc=spend,
                    price=price,
                )
            else:
                err = resp.get("errorMsg") or str(resp)
                logger.error("Order rejected: %s", err)
                return CopyResult(signal=signal, status="failed", reason=f"API rejected: {err}")

        except Exception as exc:
            logger.error("Order placement exception: %s", exc)
            return CopyResult(signal=signal, status="failed", reason=str(exc))
