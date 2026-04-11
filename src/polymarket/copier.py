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
import threading
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

# Polymarket API rejects orders whose USDC value is below this threshold
_POLY_MIN_ORDER_USDC = 1.0


def _normalize_wallet_ref(value: str) -> str:
    return value.strip().lower()

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

    # Target selection
    wallets_to_copy: int = 5
    manual_target_wallets: list[str] = field(default_factory=list)

    # Repeated-order top-up
    enable_topup: bool = False
    max_topups: int = 2
    topup_size_multiplier: float = 1.0


class CopyTrader:
    def __init__(self, config: CopierConfig, storage: Storage):
        self._cfg = config
        self._storage = storage
        self._clob: object | None = None  # initialised lazily on first trade
        self._scores: dict[str, WalletScore] = {}  # address → latest score
        # Pre-expand category names → keyword lists once at startup
        self._blocked = _expand_keywords(config.blocked_keywords)
        # Serialises the check-then-record step so concurrent stream signals
        # for the same market don't both pass the dedup guard before either
        # has written to the DB.
        self._buy_lock = threading.Lock()
        self._pending_buys: set[str] = set()  # market keys currently being processed
        self._target_wallets: set[str] = set()
        self._manual_refs = {
            _normalize_wallet_ref(value)
            for value in config.manual_target_wallets
            if value.strip()
        }

    def update_scores(self, scores: dict[str, WalletScore]) -> None:
        """Called by cmd_watch after computing/refreshing wallet scores."""
        self._scores = scores
        self._target_wallets = self._select_target_wallets(scores)
        logger.info("Score cache updated for %d wallets.", len(scores))

    def _select_target_wallets(self, scores: dict[str, WalletScore]) -> set[str]:
        """Select target wallets from scored candidates."""
        refs = self._manual_refs
        selected: list[WalletScore]
        if refs:
            # Manual mode: match against ALL scored wallets — do not apply the
            # eligibility filter (copy_size_pct / insufficient_data).  The user
            # explicitly chose these addresses and we must honour all of them
            # regardless of scoring confidence.
            matched = [
                ws for ws in scores.values()
                if any(ref in {_normalize_wallet_ref(ws.address)} for ref in refs)
            ]
            selected = matched
            mode = "manual"
        else:
            # Auto mode: filter by eligibility, then pick top N by total score.
            eligible = [
                ws for ws in scores.values()
                if ws.copy_size_pct > 0 and not ws.insufficient_data
            ]
            if not eligible:
                logger.warning("No eligible target wallets found after scoring.")
                return set()
            ranked_by_total = sorted(eligible, key=lambda ws: ws.total, reverse=True)
            selected = ranked_by_total[: max(1, self._cfg.wallets_to_copy)]
            mode = "auto"

        if not selected:
            logger.warning("Target selection mode '%s' produced no scored wallets.", mode)
            return set()

        target_wallets = {ws.address for ws in selected}
        logger.info(
            "Selected %d target wallet(s) in %s mode: %s",
            len(selected),
            mode,
            ", ".join(ws.address for ws in selected),
        )
        return target_wallets

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

        # Target mode: only act on signals from the chosen target set
        if self._target_wallets or self._manual_refs:
            if not self._target_wallets:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason="Target selection: no scored wallets selected yet",
                )
            if signal.wallet_address not in self._target_wallets:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason="Target selection: wallet not in active target set",
                )

        if signal.side == "SELL":
            return self._copy_sell(signal)

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

        # Dedup: skip if already invested in this market.
        # Also check the in-memory pending set — concurrent stream signals for
        # the same market (multiple wallets buying simultaneously) can all pass
        # the DB check before any of them has written the position record.
        market_key = signal.condition_id or signal.token_id
        with self._buy_lock:
            if market_key in self._pending_buys:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Concurrent signal for same market already being processed ({signal.market_title[:40]})",
                )
            existing = self._storage.get_open_position(signal.condition_id, signal.token_id)
            if existing:
                if not self._cfg.enable_topup:
                    return CopyResult(
                        signal=signal, status="skipped",
                        reason=f"Already have an open position in this market ({signal.market_title[:40]})",
                    )
            else:
                existing = None
            # Reserve the market slot before releasing the lock
            if not existing:
                self._pending_buys.add(market_key)

        if existing:
            return self._topup_position(signal, existing)

        # Score-based filtering
        wallet_score = self._scores.get(signal.wallet_address)
        if wallet_score and self._cfg.min_score > 0:
            if wallet_score.copy_size_pct == 0.0:
                self._pending_buys.discard(market_key)
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
            self._pending_buys.discard(market_key)
            return CopyResult(signal=signal, status="skipped", reason="Computed spend is $0")

        # Check daily limit — evaluated live so sell credits can restore live mode
        spent_today = self._storage.get_daily_spend(date.today().isoformat())
        cap_hit = not self._cfg.dry_run and (spent_today + spend > self._cfg.daily_limit_usdc)
        if spent_today + spend > self._cfg.daily_limit_usdc:
            remaining = max(0.0, self._cfg.daily_limit_usdc - spent_today)
            if self._cfg.dry_run:
                self._pending_buys.discard(market_key)
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Daily limit reached (${self._cfg.daily_limit_usdc:.0f}). "
                           f"Spent today: ${spent_today:.2f}, remaining: ${remaining:.2f}",
                )
            logger.warning(
                "Daily cap of $%.0f reached (spent $%.2f). "
                "Switching to shadow dry-run — signals will be recorded but no real orders placed.",
                self._cfg.daily_limit_usdc, spent_today,
            )

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
            self._pending_buys.discard(market_key)
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

        # Polymarket API enforces a $1 USDC minimum per order
        if spend < _POLY_MIN_ORDER_USDC:
            self._pending_buys.discard(market_key)
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Computed spend ${spend:.2f} is below API minimum of ${_POLY_MIN_ORDER_USDC:.2f} USDC",
            )

        shares = round(spend / order_price, 2)

        if self._cfg.dry_run or cap_hit:
            label = "SHADOW DRY-RUN (cap hit)" if cap_hit else "DRY RUN"
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
            self._pending_buys.discard(market_key)
            if self._cfg.dry_run:
                # Only count dry-run spend toward daily limit (shadow orders don't count — cap already hit)
                self._storage.record_daily_spend(date.today().isoformat(), spend)
            status_label = "shadow" if cap_hit else "dry_run"
            return CopyResult(
                signal=signal, status=status_label,
                reason=f"{label}: would place BUY {shares:.2f} shares @ ${order_price:.4f} (≈${spend:.2f} USDC)",
                spend_usdc=spend,
                price=order_price,
            )

        # Live execution
        return self._place_order(signal, shares, order_price, spend)

    def _topup_position(self, signal: Signal, existing: dict) -> CopyResult:
        """Add to an existing open position when an active target buys again."""
        topup_count = int(existing.get("topup_count", 0) or 0)
        if self._cfg.max_topups > 0 and topup_count >= self._cfg.max_topups:
            return CopyResult(
                signal=signal,
                status="skipped",
                reason=f"Max top-ups reached ({self._cfg.max_topups}) for this market",
            )

        balance = self._get_balance()
        spend = self._compute_spend(signal, balance)

        wallet_score = self._scores.get(signal.wallet_address)
        if wallet_score and self._cfg.score_scale_size and wallet_score.copy_size_pct < 1.0:
            spend = round(spend * wallet_score.copy_size_pct, 2)

        multiplier = self._cfg.topup_size_multiplier ** topup_count
        spend = round(spend * multiplier, 2)
        if spend <= 0:
            return CopyResult(signal=signal, status="skipped", reason="Computed top-up spend is $0")

        spend = min(spend, self._cfg.max_trade_usdc)

        # Polymarket API enforces a $1 USDC minimum per order
        if spend < _POLY_MIN_ORDER_USDC:
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Top-up spend ${spend:.2f} is below API minimum of ${_POLY_MIN_ORDER_USDC:.2f} USDC",
            )

        spent_today = self._storage.get_daily_spend(date.today().isoformat())
        remaining_daily = self._cfg.daily_limit_usdc - spent_today
        if spend > remaining_daily:
            return CopyResult(
                signal=signal,
                status="skipped",
                reason=f"Top-up would exceed daily limit (remaining: ${remaining_daily:.2f})",
            )

        order_price = round(min(signal.price + self._cfg.slippage, 0.99), 4)
        shares = round(spend / order_price, 2)

        logger.info(
            "Top-up #%d for %s: +$%.2f (x%.2f) at %.4f -> %.2f shares",
            topup_count + 1, signal.market_title[:40], spend, multiplier, order_price, shares,
        )

        if self._cfg.dry_run:
            self._storage.add_to_position(existing["id"], shares, spend)
            self._storage.record_daily_spend(date.today().isoformat(), spend)
            return CopyResult(
                signal=signal,
                status="dry_run",
                spend_usdc=spend,
                price=order_price,
                reason=f"DRY RUN TOP-UP #{topup_count + 1}: +{shares:.2f} shares @ ${order_price:.4f}",
            )

        return self._place_topup_order(signal, existing, shares, order_price, spend)

    def _place_topup_order(
        self,
        signal: Signal,
        existing: dict,
        shares: float,
        price: float,
        spend: float,
    ) -> CopyResult:
        """Place a live top-up buy order and update the DB position on success."""
        try:
            _, OrderArgs, OrderType, BUY, _, _, _ = _clob_imports()
            client = self._get_client()
            order_args = OrderArgs(token_id=signal.token_id, price=price, size=shares, side=BUY)
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
        except Exception as exc:
            logger.error("Top-up order exception: %s", exc)
            return CopyResult(signal=signal, status="failed", reason=f"Top-up order exception: {exc}")

        if resp.get("success") or resp.get("orderID"):
            topup_num = int(existing.get("topup_count", 0) or 0) + 1
            self._storage.add_to_position(existing["id"], shares, spend)
            self._storage.record_daily_spend(date.today().isoformat(), spend)
            return CopyResult(
                signal=signal,
                status="placed",
                spend_usdc=spend,
                price=price,
                reason=f"Top-up #{topup_num}: +{shares:.2f} shares @ ${price:.4f} (order {resp.get('orderID','')})",
                order_id=resp.get("orderID", ""),
            )

        return CopyResult(signal=signal, status="failed", reason=f"Top-up order rejected: {resp}")

    def _copy_sell(self, signal: Signal) -> CopyResult:
        """Close our matching position when a tracked wallet sells."""
        pos = self._storage.get_open_position(signal.condition_id, signal.token_id)
        if pos is None:
            return CopyResult(
                signal=signal, status="skipped",
                reason="No open position to sell — we never bought this market",
            )

        shares         = float(pos["shares"])
        pos_is_dry_run = pos.get("is_dry_run", True)
        pos_is_shadow  = pos_is_dry_run and not self._cfg.dry_run

        # For live positions, verify we actually hold the tokens on-chain.
        # GTC buy orders are recorded immediately but may never be filled.
        if not pos_is_dry_run:
            on_chain = self._get_token_balance(signal.token_id)
            if on_chain == 0.0:
                logger.warning(
                    "Sell skipped for %s: on-chain token balance is 0 "
                    "(buy order was likely never filled). Cancelling DB position.",
                    signal.market_title[:40],
                )
                self._storage.cancel_paper_position(pos["id"])
                return CopyResult(
                    signal=signal, status="skipped",
                    reason="Buy order never filled — no tokens to sell",
                )
            if on_chain > 0.0 and on_chain < shares:
                logger.warning(
                    "On-chain balance %.4f < recorded shares %.4f for %s — selling actual balance",
                    on_chain, shares, signal.token_id[:16],
                )
                shares = on_chain

            # Fetch the actual market minimum from the CLOB order book.
            # If shares are below it (e.g. partial GTC fill) we can't sell —
            # leave the position open rather than erroring.
            market_min = 1.0
            try:
                book = self._get_client().get_order_book(signal.token_id)
                if book.min_order_size:
                    market_min = float(book.min_order_size)
            except Exception as exc:
                logger.debug("Could not fetch order book min size for sell: %s", exc)

            if shares < market_min:
                logger.warning(
                    "Sell skipped for %s: %.4f shares below market minimum %.2f — position left open",
                    signal.market_title[:40], shares, market_min,
                )
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Position too small to sell: {shares:.4f} shares (market min: {market_min:.2f})",
                )

        exit_price = round(max(signal.price - self._cfg.slippage, 0.01), 4)
        proceeds   = round(shares * exit_price, 2)
        cost       = float(pos["spend_usdc"])
        pnl        = round(proceeds - cost, 2)
        label      = "DRY RUN SELL" if pos_is_dry_run else "SELL"
        refund     = float(pos.get("spend_usdc") or 0)

        logger.info(
            "[%s] Closing %.4f shares of %s @ $%.4f → proceeds $%.2f  P&L %+.2f",
            label, shares, signal.market_title[:40], exit_price, proceeds, pnl,
        )

        # Dry-run / shadow: close DB immediately — no real order needed.
        if pos_is_dry_run:
            self._storage.close_paper_position(pos["id"], exit_price, proceeds)
            if refund > 0 and not pos_is_shadow:
                self._storage.record_daily_spend(date.today().isoformat(), -refund)
            return CopyResult(
                signal=signal,
                status="dry_run",
                reason=f"{label}: closed {shares:.4f} shares @ ${exit_price:.4f} → ${proceeds:.2f} (P&L {pnl:+.2f})",
                spend_usdc=-proceeds,
                price=exit_price,
            )

        # Shadow (cap hit in live mode): close DB, don't refund daily_spend (was never charged).
        spent_today = self._storage.get_daily_spend(date.today().isoformat())
        cap_hit = spent_today >= self._cfg.daily_limit_usdc
        if cap_hit:
            self._storage.close_paper_position(pos["id"], exit_price, proceeds)
            return CopyResult(
                signal=signal, status="shadow",
                reason=f"Shadow SELL: closed {shares:.4f} shares @ ${exit_price:.4f} → ${proceeds:.2f} (P&L {pnl:+.2f})",
                spend_usdc=-proceeds,
                price=exit_price,
            )

        # Live sell: place the order FIRST — close DB and refund only on success.
        return self._place_sell_order(signal, pos, shares, exit_price, proceeds, refund)

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

    def _get_token_balance(self, token_id: str) -> float:
        """Return actual on-chain conditional token balance (shares held in wallet).

        Returns -1.0 if the query fails so callers can decide to proceed anyway.
        """
        try:
            _, _, _, _, _, BalanceAllowanceParams, AssetType = _clob_imports()
            resp = self._get_client().get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
            raw = resp.get("balance", 0)
            return float(raw) / 1e6
        except Exception as exc:
            logger.warning("Token balance fetch failed for %s…: %s", token_id[:16], exc)
            return -1.0  # unknown — let sell proceed rather than silently drop it

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
                market_key = signal.condition_id or signal.token_id
                self._pending_buys.discard(market_key)
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
                self._pending_buys.discard(signal.condition_id or signal.token_id)
                return CopyResult(signal=signal, status="failed", reason=f"API rejected: {err}")

        except Exception as exc:
            logger.error("Order placement exception: %s", exc)
            self._pending_buys.discard(signal.condition_id or signal.token_id)
            return CopyResult(signal=signal, status="failed", reason=str(exc))

    def _place_sell_order(self, signal: Signal, pos: dict, shares: float, price: float, proceeds: float, refund: float) -> CopyResult:
        """Place a live SELL order on the CLOB.

        Closes the DB position and refunds daily_spend only on success.
        On failure the position is left open so it can be retried.
        """
        try:
            ClobClient, OrderArgs, OrderType, _, SELL, _, _ = _clob_imports()
            client = self._get_client()

            order_args = OrderArgs(
                token_id=signal.token_id,
                price=price,
                size=shares,
                side=SELL,
            )
            signed = client.create_order(order_args)
            resp   = client.post_order(signed, OrderType.GTC)

            if resp.get("success") or resp.get("orderID"):
                order_id = resp.get("orderID", "")
                logger.info(
                    "Sell order placed: %s — SELL %.4f shares @ $%.4f (≈$%.2f USDC)",
                    order_id, shares, price, proceeds,
                )
                # Confirmed — close DB position and restore daily headroom
                self._storage.close_paper_position(pos["id"], price, proceeds)
                if refund > 0:
                    self._storage.record_daily_spend(date.today().isoformat(), -refund)
                    logger.info(
                        "Daily spend refunded $%.2f (original cost) — new headroom: $%.2f",
                        refund,
                        max(0.0, self._cfg.daily_limit_usdc - self._storage.get_daily_spend(date.today().isoformat())),
                    )
                return CopyResult(
                    signal=signal, status="placed",
                    reason=f"SELL {shares:.4f} shares @ ${price:.4f} (≈${proceeds:.2f} USDC)",
                    order_id=order_id,
                    spend_usdc=-proceeds,
                    price=price,
                )
            else:
                err = resp.get("errorMsg") or str(resp)
                logger.error("Sell order rejected: %s — position left open for retry", err)
                return CopyResult(signal=signal, status="failed", reason=f"API rejected: {err}")

        except Exception as exc:
            logger.error("Sell order exception: %s — position left open for retry", exc)
            return CopyResult(signal=signal, status="failed", reason=str(exc))
