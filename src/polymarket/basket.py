"""
Basket consensus logic.

A basket is a named group of wallets specialising in one topic category.
A copy signal passes the basket gate when ≥ threshold fraction of basket
wallets have recently bought the same outcome in the same market.

All functions are pure (no DB access) — callers fetch the data and pass it in.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_consensus(
    basket: dict,
    signal_condition_id: str,
    signal_outcome: str,
    recent_buys: list[dict],
) -> dict:
    """
    Evaluate whether a basket has reached consensus on a signal.

    Parameters
    ----------
    basket : dict
        A basket row from storage.get_basket() — must contain
        'wallet_addresses' (list[str]) and 'consensus_threshold' (float).
    signal_condition_id : str
        The market's condition_id (already filtered by the caller, passed
        for context/logging only).
    signal_outcome : str
        The outcome the signal wallet bought (e.g. "Yes").
    recent_buys : list[dict]
        Rows from storage.get_recent_buys_for_condition() — already
        filtered to the correct condition_id and time window.
        Each row must have: address, outcome, price (optional).

    Returns
    -------
    dict with keys:
        basket_id    : int
        basket_name  : str
        wallet_count : int    — total wallets in basket
        agree_count  : int    — wallets that bought the same outcome
        agree_pct    : float  — agree_count / wallet_count (0.0–1.0)
        price_spread : float  — max(price) – min(price) among agreeing buys
        threshold    : float  — the basket's configured threshold
        should_copy  : bool   — agree_pct >= threshold
        reason       : str    — human-readable explanation
    """
    addresses: list[str] = basket.get("wallet_addresses") or []
    threshold = float(basket.get("consensus_threshold") or 0.8)
    wallet_count = len(addresses)

    if wallet_count == 0:
        return _make_result(basket, 0, 0, 0.0, 0.0, threshold, False,
                            "Basket has no wallets")

    target_outcome = signal_outcome.strip().lower()

    # Count each basket wallet at most once
    agreeing_addrs = {
        b["address"]
        for b in recent_buys
        if (b.get("outcome") or "").strip().lower() == target_outcome
        and b.get("address") in addresses
    }
    agree_count = len(agreeing_addrs)
    agree_pct = agree_count / wallet_count

    # Price spread across agreeing buys
    prices = [
        float(b["price"])
        for b in recent_buys
        if b.get("address") in agreeing_addrs and b.get("price")
    ]
    price_spread = (max(prices) - min(prices)) if len(prices) >= 2 else 0.0

    should_copy = agree_pct >= threshold
    reason = (
        f"{agree_count}/{wallet_count} basket wallets agree on '{signal_outcome}' "
        f"({agree_pct:.0%} vs threshold {threshold:.0%})"
    )

    return _make_result(basket, wallet_count, agree_count, agree_pct, price_spread,
                        threshold, should_copy, reason)


def _make_result(
    basket: dict,
    wallet_count: int,
    agree_count: int,
    agree_pct: float,
    price_spread: float,
    threshold: float,
    should_copy: bool,
    reason: str,
) -> dict:
    return {
        "basket_id":    basket.get("id", 0),
        "basket_name":  basket.get("name", ""),
        "wallet_count": wallet_count,
        "agree_count":  agree_count,
        "agree_pct":    round(agree_pct, 4),
        "price_spread": round(price_spread, 4),
        "threshold":    threshold,
        "should_copy":  should_copy,
        "reason":       reason,
    }
