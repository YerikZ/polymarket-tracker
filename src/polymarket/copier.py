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
from .storage import Storage

logger = logging.getLogger(__name__)

# Lazy import so the tool works without py-clob-client for users who only watch
def _clob_imports():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    return ClobClient, OrderArgs, OrderType, BUY, SELL


@dataclass
class CopierConfig:
    # Auth
    private_key: str
    funder: str                          # proxy wallet address (same as your Polymarket address)
    chain_id: int = 137                  # Polygon mainnet

    # Sizing mode — exactly one should drive the spend amount
    sizing_mode: str = "fixed"           # "fixed" | "pct_balance" | "mirror_pct"
    fixed_usdc: float = 50.0             # baseline spend when signal.usdc_size == reference_trade_usdc
    reference_trade_usdc: float = 50.0   # reference signal size for fixed mode proportional scaling
    pct_balance: float = 0.02            # used when sizing_mode = "pct_balance" (2%)
    mirror_pct: float = 0.01             # used when sizing_mode = "mirror_pct" (1% of original)

    # Safety limits
    max_trade_usdc: float = 500.0        # hard cap per trade
    daily_limit_usdc: float = 1000.0     # total cap for today

    # Execution
    dry_run: bool = True                 # True = simulate only, never submit
    slippage: float = 0.01              # add this to price for better fill (e.g. 0.01 = +1¢)

    # Scoring
    min_score: float = 50.0             # skip wallets with score below this (0 = disable)
    score_scale_size: bool = True        # scale position size by wallet's copy_size_pct


class CopyTrader:
    def __init__(self, config: CopierConfig, storage: Storage):
        self._cfg = config
        self._storage = storage
        self._clob: object | None = None  # initialised lazily on first trade
        self._scores: dict[str, WalletScore] = {}  # address → latest score

    def update_scores(self, scores: dict[str, WalletScore]) -> None:
        """Called by cmd_watch after computing/refreshing wallet scores."""
        self._scores = scores
        logger.info("Score cache updated for %d wallets.", len(scores))

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
            remaining = self._cfg.daily_limit_usdc - spent_today
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Daily limit reached (${self._cfg.daily_limit_usdc:.0f}). "
                       f"Spent today: ${spent_today:.2f}, remaining: ${remaining:.2f}",
            )

        # Cap per trade
        spend = min(spend, self._cfg.max_trade_usdc)

        order_price = round(signal.price + self._cfg.slippage, 4)
        order_price = min(order_price, 0.99)  # price can't exceed 0.99 on Polymarket
        shares = round(spend / order_price, 2)

        if self._cfg.dry_run:
            logger.info(
                "[DRY RUN] Would BUY %.2f shares of %s @ $%.4f (≈$%.2f USDC)",
                shares, signal.market_title[:40], order_price, spend,
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
            })
            # Bug fix: track dry-run spend so the daily limit is enforced for simulated trades too
            self._storage.record_daily_spend(date.today().isoformat(), spend)
            return CopyResult(
                signal=signal, status="dry_run",
                reason=f"Dry run: would place BUY {shares:.2f} shares @ ${order_price:.4f} (≈${spend:.2f} USDC)",
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
            ClobClient, _, _, _, _ = _clob_imports()
            self._clob = ClobClient(
                "https://clob.polymarket.com",
                key=self._cfg.private_key,
                chain_id=self._cfg.chain_id,
                signature_type=1,
                funder=self._cfg.funder,
            )
            self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
            logger.info("CLOB client initialised for funder %s", self._cfg.funder[:10] + "…")
        return self._clob

    def _get_balance(self) -> float:
        if self._cfg.dry_run and not self._cfg.private_key:
            return 0.0
        try:
            raw = self._get_client().get_balance()
            return float(raw) / 1e6  # USDC has 6 decimals
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
            ClobClient, OrderArgs, OrderType, BUY, _ = _clob_imports()
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
