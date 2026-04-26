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


class CopyConfigError(RuntimeError):
    """Raised when copy trading is enabled but no target wallets can be resolved.

    Indicates misconfiguration: either manual_target_wallets and basket_ids are
    both empty, or basket_ids are set but the baskets contain no wallet addresses.
    """


def _normalize_wallet_ref(value: str) -> str:
    return value.strip().lower()

# Lazy import so the tool works without py-clob-client for users who only watch
def _clob_imports():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    return ClobClient, MarketOrderArgs, OrderType, BUY, SELL, BalanceAllowanceParams, AssetType


@dataclass
class CopierConfig:
    # Auth
    private_key: str
    funder: str                          # proxy wallet address (same as your Polymarket address)
    chain_id: int = 137                  # Polygon mainnet
    signature_type: int = 2              # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE (default for Polymarket proxy wallets)

    # Sizing mode — exactly one should drive the spend amount
    sizing_mode: str = "fixed"           # "fixed" | "pct_balance" | "mirror_pct"
    fixed_usdc: float = 50.0             # flat USDC spend per order (sizing_mode="fixed")
    reference_trade_usdc: float = 50.0   # unused in fixed mode; kept for mirror_pct reference
    pct_balance: float = 0.02            # used when sizing_mode = "pct_balance" (2%)
    mirror_pct: float = 0.01             # used when sizing_mode = "mirror_pct" (1% of original)

    # Safety limits
    max_trade_usdc: float = 500.0        # hard cap per trade
    daily_limit_usdc: float = 1000.0     # total cap for today

    # Execution
    dry_run: bool = True                 # True = simulate only, never submit
    slippage: float = 0.01              # add this to price for better fill (e.g. 0.01 = +1¢)

    # Market filters
    blocked_keywords: list = field(default_factory=list)
    # Any market whose title contains one of these words (case-insensitive) is skipped.
    # Example: ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana"]
    max_price: float = 0.85
    # Skip BUY signals where the market price exceeds this value (0 = disabled).
    # E.g. 0.85 skips markets already trading at ≥85¢ — limited upside, high risk.

    # Scoring
    min_score: float = 50.0             # skip wallets with score below this (0 = disable)
    score_scale_size: bool = True        # scale position size by wallet's copy_size_pct

    # Target selection — manual list only; no auto-copy fallback
    manual_target_wallets: list[str] = field(default_factory=list)

    # Basket / consensus copy
    # List of basket IDs (from the baskets table) to gate copy signals through.
    # If a signal's wallet is in one of these baskets, the copy only executes
    # when ≥ consensus_threshold of basket wallets agree on the same outcome.
    basket_ids: list[int] = field(default_factory=list)

    # Repeated-order top-up
    enable_topup: bool = False
    max_topups: int = 2
    topup_size_multiplier: float = 1.0

    # Take-profit / stop-loss (position monitor)
    # stop_loss_pct:          0 = disabled; 0.40 = exit if price drops ≥ 40% below entry_price
    # trailing_stop_pct:      0 = disabled; 0.30 = exit if price retreats 30% below its peak
    # trailing_stop_min_gain: only arm trailing stop once price ≥ this multiple of entry_price
    #                         2.0 = don't trail until price at least doubles (avoids noise near entry)
    stop_loss_pct: float = 0.0
    trailing_stop_pct: float = 0.0
    trailing_stop_min_gain: float = 2.0


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
        self._config_error: str | None = None  # set by update_scores() on CopyConfigError
        self._manual_refs = {
            _normalize_wallet_ref(value)
            for value in config.manual_target_wallets
            if value.strip()
        }

    def update_scores(self, scores: dict[str, WalletScore]) -> None:
        """Called by cmd_watch after computing/refreshing wallet scores."""
        self._scores = scores
        try:
            self._target_wallets = self._select_target_wallets(scores)
            self._config_error = None  # cleared once a valid target set is resolved
        except CopyConfigError as exc:
            logger.error(
                "Copy trading misconfigured — no orders will be placed until fixed: %s", exc
            )
            self._target_wallets = set()
            self._config_error = str(exc)
        logger.info("Score cache updated for %d wallets.", len(scores))

    def _select_target_wallets(self, scores: dict[str, WalletScore]) -> set[str]:
        """Resolve the effective copy target set — exactly one of three branches applies.

        Branch 1 — manual_target_wallets is non-empty:
            Copy only from those wallets (filtered against current scored set).
            Returns set() transiently if none have been scored yet (will resolve next cycle).

        Branch 2 — manual list is empty AND basket_ids resolves to ≥1 address:
            Copy only from basket-member wallets (consensus gate will also apply in copy()).

        Branch 3 — both are empty/unresolvable:
            Raise CopyConfigError — misconfiguration must be fixed; no orders are placed.
        """
        # Branch 1 — manual wallet list
        if self._manual_refs:
            matched = [
                ws for ws in scores.values()
                if _normalize_wallet_ref(ws.address) in self._manual_refs
            ]
            if not matched:
                logger.warning(
                    "Manual target wallets configured but none have been scored yet — "
                    "will retry next scoring cycle."
                )
                return set()  # transient: gate stays active, all signals skipped until matched
            logger.info(
                "Branch 1 — manual targets: %d wallet(s) selected: %s",
                len(matched),
                ", ".join(ws.address for ws in matched),
            )
            return {ws.address for ws in matched}

        # Branch 2 — basket wallets (manual list is empty)
        if self._cfg.basket_ids:
            basket_addrs: set[str] = set()
            for basket_id in self._cfg.basket_ids:
                try:
                    basket = self._storage.get_basket(basket_id)
                    if basket and basket.get("active"):
                        basket_addrs.update(basket.get("wallet_addresses") or [])
                except Exception as exc:
                    logger.warning(
                        "Failed to load basket %d for target selection: %s", basket_id, exc
                    )
            if basket_addrs:
                logger.info(
                    "Branch 2 — basket targets: using %d wallet(s) from basket(s) %s.",
                    len(basket_addrs),
                    self._cfg.basket_ids,
                )
                return basket_addrs
            raise CopyConfigError(
                f"Basket IDs {self._cfg.basket_ids} are configured but resolve to zero active "
                "wallet addresses. Add wallets to the basket or disable copy trading."
            )

        # Branch 3 — nothing configured → hard error
        raise CopyConfigError(
            "Copy trading is enabled but no target wallets are configured. "
            "Set copy_trading.manual_target_wallets to specific wallet addresses, "
            "OR create a basket with wallet addresses and set copy_trading.basket_ids."
        )

    def _check_basket_consensus(self, signal) -> dict | None:
        """
        Check whether any configured basket that contains signal.wallet_address
        has reached consensus on this market+outcome.

        Returns None if the wallet is not in any configured basket (fail-open).
        Returns the consensus result dict if the wallet IS in a basket.
        Exceptions are caught and logged; returns None on error (fail-open).
        """
        from .basket import check_consensus as _consensus

        for basket_id in self._cfg.basket_ids:
            try:
                basket = self._storage.get_basket(basket_id)
                if not basket or not basket.get("active"):
                    continue
                addresses = basket.get("wallet_addresses") or []
                if signal.wallet_address not in addresses:
                    continue  # this wallet is not in this basket — skip

                recent_buys = self._storage.get_recent_buys_for_condition(
                    addresses, signal.condition_id, within_hours=48,
                )
                result = _consensus(basket, signal.condition_id, signal.outcome, recent_buys)
                logger.info(
                    "Basket '%s' consensus for market %s: %s",
                    basket.get("name"), signal.condition_id[:16], result["reason"],
                )
                return result

            except Exception as exc:
                logger.warning(
                    "Basket consensus check failed for basket %d: %s", basket_id, exc
                )
                # Fail open — a DB error should not silently block a valid copy signal
                return None

        return None  # wallet not in any configured basket

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

        # Target gate — always active when copy trading has any configuration.
        # The condition is intentionally broad so a startup race (before the first
        # score cycle) never lets every leaderboard wallet through.
        if self._target_wallets or self._manual_refs or self._cfg.basket_ids:
            # Misconfiguration detected during last update_scores() call.
            if self._config_error:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Config error: {self._config_error}",
                )
            # Transient: manual refs present but no wallets scored yet (first cycle).
            if not self._target_wallets:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason="Target selection: no wallets matched yet — retry after scoring cycle",
                )
            if signal.wallet_address not in self._target_wallets:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason="Target selection: wallet not in active target set",
                )

        if signal.side == "SELL":
            return self._copy_sell(signal)

        # Basket gate: require consensus before copying.
        # When basket_ids are configured without manual targets, _select_target_wallets()
        # already restricts signals to basket-member wallets, so this check will always
        # find the wallet in its basket and return a real consensus result (not None).
        if self._cfg.basket_ids:
            consensus = self._check_basket_consensus(signal)
            if consensus is not None and not consensus["should_copy"]:
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=f"Basket consensus not reached: {consensus['reason']}",
                )

        # Skip high-price markets — limited upside, user-configurable ceiling
        if self._cfg.max_price > 0 and signal.price >= self._cfg.max_price:
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Price {signal.price:.4f} ≥ max_price {self._cfg.max_price:.2f}. Skipping.",
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

        # Note: min_order_size (share-based) is a limit-order concept and does not apply
        # to market orders where we pass a USDC amount directly (BUY side). No share minimum
        # check needed at buy time — but log a warning if the estimated share count is very
        # small relative to typical market minimums, since such positions may be dust at sell time.
        est_shares_pre = round(spend / order_price, 2) if order_price else 0
        if 0 < est_shares_pre < 2.0:
            logger.warning(
                "Small position warning: $%.2f USDC @ $%.4f ≈ %.2f estimated shares — "
                "some markets enforce a 5+ share sell minimum; this position may be dust at close.",
                spend, order_price, est_shares_pre,
            )

        # Cap per trade
        spend = min(spend, self._cfg.max_trade_usdc)

        # Polymarket API enforces a $1 USDC minimum per order
        if spend < _POLY_MIN_ORDER_USDC:
            self._pending_buys.discard(market_key)
            return CopyResult(
                signal=signal, status="skipped",
                reason=f"Computed spend ${spend:.2f} is below API minimum of ${_POLY_MIN_ORDER_USDC:.2f} USDC",
            )

        # Estimate shares for logging and DB records (actual fill may differ slightly at market).
        est_shares = round(spend / order_price, 2)

        if self._cfg.dry_run or cap_hit:
            label = "SHADOW DRY-RUN (cap hit)" if cap_hit else "DRY RUN"
            logger.info(
                "[%s] Would BUY $%.2f USDC of %s @ worst-price $%.4f (≈%.2f shares)",
                label, spend, signal.market_title[:40], order_price, est_shares,
            )
            self._storage.append_paper_position({
                "condition_id": signal.condition_id,
                "token_id": signal.token_id,
                "market_title": signal.market_title,
                "outcome": signal.outcome,
                "entry_price": order_price,
                "shares": est_shares,
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
                reason=f"{label}: would BUY $%.2f USDC @ worst-price ${order_price:.4f} (≈{est_shares:.2f} shares)" % spend,
                spend_usdc=spend,
                price=order_price,
            )

        # Live execution — pass USDC spend directly; market order handles sizing
        return self._place_order(signal, order_price, spend)

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
                reason=f"Computed top-up spend ${spend:.2f} is below API minimum of ${_POLY_MIN_ORDER_USDC:.2f} USDC",
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
        est_shares = round(spend / order_price, 2)

        logger.info(
            "Top-up #%d for %s: +$%.2f USDC (x%.2f) @ worst-price %.4f (≈%.2f shares)",
            topup_count + 1, signal.market_title[:40], spend, multiplier, order_price, est_shares,
        )

        if self._cfg.dry_run:
            self._storage.add_to_position(existing["id"], est_shares, spend)
            self._storage.record_daily_spend(date.today().isoformat(), spend)
            return CopyResult(
                signal=signal,
                status="dry_run",
                spend_usdc=spend,
                price=order_price,
                reason=f"DRY RUN TOP-UP #{topup_count + 1}: +${spend:.2f} USDC @ worst-price ${order_price:.4f} (≈{est_shares:.2f} shares)",
            )

        return self._place_topup_order(signal, existing, order_price, spend)

    def _place_topup_order(
        self,
        signal: Signal,
        existing: dict,
        price: float,
        spend: float,
    ) -> CopyResult:
        """Place a live market top-up BUY order spending `spend` USDC (FOK)."""
        try:
            _, MarketOrderArgs, OrderType, BUY, _, _, _ = _clob_imports()
            client = self._get_client()
            order_args = MarketOrderArgs(
                token_id=signal.token_id,
                amount=spend,   # USDC to spend
                side=BUY,
                price=price,    # worst-price limit
            )
            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, OrderType.FOK)
        except Exception as exc:
            logger.error("Top-up market order exception: %s", exc)
            return CopyResult(signal=signal, status="failed", reason=f"Top-up order exception: {exc}")

        if resp.get("success") or resp.get("orderID"):
            topup_num = int(existing.get("topup_count", 0) or 0) + 1
            est_shares = round(spend / price, 2)
            self._storage.add_to_position(existing["id"], est_shares, spend)
            self._storage.record_daily_spend(date.today().isoformat(), spend)
            return CopyResult(
                signal=signal,
                status="placed",
                spend_usdc=spend,
                price=price,
                reason=f"Top-up #{topup_num}: +$%.2f USDC @ worst-price ${price:.4f} (order {resp.get('orderID','')})" % spend,
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
        # FOK buy orders either fill completely or cancel; a zero balance means
        # the original buy was rejected (price moved past our worst-price limit).
        if not pos_is_dry_run:
            on_chain = self._get_token_balance(signal.token_id)
            if on_chain == 0.0:
                logger.warning(
                    "Sell skipped for %s: on-chain token balance is 0 "
                    "(FOK buy was likely cancelled — price exceeded worst-price limit). "
                    "Cancelling DB position.",
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
            # For SELL market orders, amount = shares (unlike BUY where amount = USDC);
            # the exchange enforces min_order_size shares and will reject anything below it.
            # Note: FOK buys shouldn't partially fill, so if on-chain shares < market_min
            # the position is permanently dust — cancel it rather than leaving it open forever.
            market_min = 1.0
            try:
                book = self._get_client().get_order_book(signal.token_id)
                if book.min_order_size:
                    market_min = float(book.min_order_size)
            except Exception as exc:
                logger.debug("Could not fetch order book min size for sell: %s", exc)

            if shares < market_min:
                # Cannot exit early — the exchange requires ≥ market_min shares per SELL
                # order and we hold fewer than that.  The tokens ARE on-chain though; if
                # the market resolves YES Polymarket will redeem them automatically.
                # Leave the DB position open so the normal resolution-tracking path
                # (price update / market_closed) can close it correctly at expiry.
                logger.warning(
                    "Early exit skipped for %s: %.4f on-chain shares < market min %.2f. "
                    "Position left open — tokens held on-chain, will resolve at market close.",
                    signal.market_title[:40], shares, market_min,
                )
                return CopyResult(
                    signal=signal, status="skipped",
                    reason=(
                        f"Below sell minimum: {shares:.4f} shares < {market_min:.2f} required. "
                        f"Tokens held on-chain — position will close at market resolution."
                    ),
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

    def close_position(self, pos: dict, reason: str) -> CopyResult | None:
        """Close an open position due to a take-profit or stop-loss trigger.

        Builds a synthetic SELL signal from the position record and delegates
        to the same execution path as a normal tracked-wallet sell:
        - Dry-run / shadow positions → DB closed immediately, no real order.
        - Live positions → market SELL order placed (FOK).

        Returns None if the position cannot be closed (missing price / shares).
        """
        from datetime import datetime, timezone as tz
        token_id = pos.get("token_id", "")
        condition_id = pos.get("condition_id", "")
        shares = float(pos.get("shares") or 0)
        current_price = pos.get("current_price")

        if not shares or not current_price or not token_id:
            logger.warning(
                "close_position: cannot close pos %s — missing shares/price/token_id",
                pos.get("id"),
            )
            return None

        # Build a synthetic SELL signal so we can reuse _copy_sell() unchanged.
        from .models import Signal
        synthetic = Signal(
            wallet_address=pos.get("wallet_address", ""),
            username=pos.get("username", ""),
            wallet_rank=int(pos.get("wallet_rank") or 0),
            condition_id=condition_id,
            market_title=pos.get("market_title", ""),
            outcome=pos.get("outcome", ""),
            side="SELL",
            size=shares,
            usdc_size=round(shares * float(current_price), 2),
            price=float(current_price),
            detected_at=datetime.now(tz.utc).isoformat(),
            transaction_hash="",
            token_id=token_id,
        )

        logger.info(
            "[TP/SL] Closing position %d (%s) @ $%.4f — %s",
            pos.get("id", 0),
            pos.get("market_title", "")[:40],
            float(current_price),
            reason,
        )
        result = self._copy_sell(synthetic)
        # Augment reason so the UI/logs clearly show this was automated
        result.reason = f"[TP/SL] {reason} | {result.reason}"
        return result

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
            # Flat fixed amount — always spend exactly fixed_usdc regardless of signal size.
            logger.debug("Fixed: $%.2f USDC", self._cfg.fixed_usdc)
            return self._cfg.fixed_usdc
        if mode == "pct_balance":
            return balance * self._cfg.pct_balance
        if mode == "mirror_pct":
            return signal.usdc_size * self._cfg.mirror_pct
        logger.warning("Unknown sizing_mode '%s', defaulting to fixed", mode)
        return self._cfg.fixed_usdc

    def _place_order(self, signal: Signal, price: float, spend: float) -> CopyResult:
        """Place a live market BUY order spending exactly `spend` USDC.

        Uses MarketOrderArgs with FOK (Fill-Or-Kill): the order either fills
        completely at or below `price` (worst-price slippage limit) or is
        cancelled immediately. No partial fills, no resting on the order book.
        """
        try:
            _, MarketOrderArgs, OrderType, BUY, _, _, _ = _clob_imports()
            client = self._get_client()

            order_args = MarketOrderArgs(
                token_id=signal.token_id,
                amount=spend,      # USDC to spend (BUY semantics)
                side=BUY,
                price=price,       # worst-price limit (slippage protection)
            )
            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, OrderType.FOK)

            if resp.get("success") or resp.get("orderID"):
                order_id = resp.get("orderID", "")
                # Estimate shares from spend/price for the position record.
                # takingAmount in the response may be empty for FOK orders.
                est_shares = round(spend / price, 2)
                logger.info(
                    "Market BUY placed: %s — $%.2f USDC @ worst-price $%.4f (≈%.2f shares)",
                    order_id, spend, price, est_shares,
                )
                self._storage.record_daily_spend(date.today().isoformat(), spend)
                self._storage.append_paper_position({
                    "condition_id":  signal.condition_id,
                    "token_id":      signal.token_id,
                    "market_title":  signal.market_title,
                    "outcome":       signal.outcome,
                    "entry_price":   price,
                    "shares":        est_shares,
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
                    reason=f"Market BUY $%.2f USDC @ worst-price ${price:.4f} (≈{est_shares:.2f} shares)" % spend,
                    order_id=order_id,
                    spend_usdc=spend,
                    price=price,
                )
            else:
                err = resp.get("errorMsg") or str(resp)
                logger.error("Market BUY rejected: %s", err)
                self._pending_buys.discard(signal.condition_id or signal.token_id)
                return CopyResult(signal=signal, status="failed", reason=f"API rejected: {err}")

        except Exception as exc:
            logger.error("Market BUY exception: %s", exc)
            self._pending_buys.discard(signal.condition_id or signal.token_id)
            return CopyResult(signal=signal, status="failed", reason=str(exc))

    def _place_sell_order(self, signal: Signal, pos: dict, shares: float, price: float, proceeds: float, refund: float) -> CopyResult:
        """Place a live market SELL order on the CLOB (FOK).

        For SELL, MarketOrderArgs.amount is shares (not USDC).
        `price` is the worst-price floor — order is cancelled if market is below it.
        Closes the DB position and refunds daily_spend only on success.
        On failure the position is left open so it can be retried.
        """
        try:
            _, MarketOrderArgs, OrderType, _, SELL, _, _ = _clob_imports()
            client = self._get_client()

            order_args = MarketOrderArgs(
                token_id=signal.token_id,
                amount=shares,   # SELL semantics: amount = shares to sell
                side=SELL,
                price=price,     # worst-price floor (slippage protection)
            )
            signed = client.create_market_order(order_args)
            resp   = client.post_order(signed, OrderType.FOK)

            if resp.get("success") or resp.get("orderID"):
                order_id = resp.get("orderID", "")
                logger.info(
                    "Market SELL placed: %s — %.4f shares @ worst-price $%.4f (≈$%.2f USDC)",
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
                    reason=f"Market SELL {shares:.4f} shares @ worst-price ${price:.4f} (≈${proceeds:.2f} USDC)",
                    order_id=order_id,
                    spend_usdc=-proceeds,
                    price=price,
                )
            else:
                err = resp.get("errorMsg") or str(resp)
                logger.error("Market SELL rejected: %s — position left open for retry", err)
                return CopyResult(signal=signal, status="failed", reason=f"API rejected: {err}")

        except Exception as exc:
            logger.error("Market SELL exception: %s — position left open for retry", exc)
            return CopyResult(signal=signal, status="failed", reason=str(exc))
