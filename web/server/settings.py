"""
Settings helpers — read/write the DB config store and build component configs.
"""
from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket.storage import Storage
    from polymarket.copier import CopierConfig

logger = logging.getLogger(__name__)

# Defaults shown in the UI when the settings table has never been written.
# These are merged under any values that come from config.yaml / env vars.
_DEFAULTS: dict = {
    "top_n": 20,
    "poll_interval": 300,
    "min_position_usdc": 50.0,
    "request_delay": 0.5,
    "max_retries": 3,
    "leaderboard_ttl": 3600,
    "wallet_refresh_interval": 600,
    "max_signal_age": 3600,
    "log_level": "INFO",
    "watcher_mode": "poll",
    "polygon_wss": "",
    "copy_trading": {
        "dry_run": True,
        "signature_type": 1,
        "sizing_mode": "fixed",
        "fixed_usdc": 1.0,
        "reference_trade_usdc": 50.0,
        "pct_balance": 0.02,
        "mirror_pct": 0.01,
        "max_trade_usdc": 10.0,
        "daily_limit_usdc": 30.0,
        "min_order_size_cap": 10.0,
        "slippage": 0.01,
        "min_score": 50.0,
        "score_scale_size": True,
        "single_wallet_mode": False,
        "enable_topup": False,
        "max_topups": 2,
        "topup_size_multiplier": 1.0,
        "blocked_keywords": [],
        "private_key": "",
        "funder": "",
    },
}

# Fields never returned in GET /api/settings (shown masked instead)
_SENSITIVE = {"private_key", "polygon_wss"}

# Nested sensitive fields under copy_trading
_SENSITIVE_NESTED = {"private_key"}


def get_settings(storage: "Storage", seed_cfg: dict | None = None) -> dict:
    """Return config with built-in defaults filled in for any missing keys.

    On the very first call (empty table) the defaults + seed_cfg are persisted.
    On subsequent calls the stored values take precedence but any keys absent
    from the DB (e.g. copy_trading section missing from an old record) are
    filled from _DEFAULTS so the frontend always receives a complete object.
    """
    stored = storage.get_settings()

    # Scrub any masked sentinels ("***") that may have been written to the DB
    # by earlier saves from the UI before the put_settings guard was added.
    # Sentinel values are not real credentials — drop them so seed_cfg wins.
    # If anything was removed, write the clean copy back so the DB heals itself.
    stored_clean = copy.deepcopy(stored)
    _scrub_sentinels(stored_clean)
    if stored_clean != stored:
        logger.warning("Scrubbed '***' sentinel(s) from stored settings — rewriting clean copy to DB.")
        storage.put_settings(stored_clean)
    stored = stored_clean

    # Build the canonical config: defaults ← seed_cfg ← stored (highest priority)
    merged = copy.deepcopy(_DEFAULTS)
    if seed_cfg:
        for k, v in _sanitise_for_seed(seed_cfg).items():
            if k == "copy_trading" and isinstance(v, dict):
                merged["copy_trading"].update(v)
            else:
                merged[k] = v
    for k, v in stored.items():
        if k == "copy_trading" and isinstance(v, dict):
            for ck, cv in v.items():
                # Don't let an empty stored sensitive field shadow a real seed value
                if ck in _SENSITIVE_NESTED and not cv:
                    continue
                merged["copy_trading"][ck] = cv
        else:
            if k in _SENSITIVE and not v:
                continue
            merged[k] = v

    if not stored:
        storage.put_settings(merged)
        logger.info("Settings table seeded with defaults.")

    return merged


def get_settings_masked(storage: "Storage", seed_cfg: dict | None = None) -> dict:
    """Return config with sensitive values replaced by '***' if set, '' if not."""
    cfg = get_settings(storage, seed_cfg)
    result = copy.deepcopy(cfg)

    for key in _SENSITIVE:
        if key in result:
            result[key] = "***" if result[key] else ""

    ct = result.get("copy_trading", {})
    for key in _SENSITIVE_NESTED:
        if key in ct:
            ct[key] = "***" if ct[key] else ""

    return result


def put_settings(storage: "Storage", updates: dict) -> dict:
    """Deep-merge updates into stored config, preserving sensitive fields when blank."""
    existing = storage.get_settings()

    # Start from existing, overlay updates (so unmentioned fields are preserved)
    merged = copy.deepcopy(existing) if existing else copy.deepcopy(_DEFAULTS)

    for k, v in updates.items():
        if k == "copy_trading" and isinstance(v, dict):
            merged.setdefault("copy_trading", {}).update(v)
        else:
            merged[k] = v

    # Top-level sensitive fields: blank string or masked sentinel = keep existing value
    for key in _SENSITIVE:
        if key in updates and updates[key] in ("", "***"):
            merged[key] = existing.get(key, "")

    # Nested copy_trading sensitive fields
    ct_updates = updates.get("copy_trading", {})
    ct_existing = existing.get("copy_trading", {})
    for key in _SENSITIVE_NESTED:
        if key in ct_updates and ct_updates[key] in ("", "***"):
            merged.setdefault("copy_trading", {})[key] = ct_existing.get(key, "")

    storage.put_settings(merged)
    return merged


def build_copier_config(cfg: dict) -> "CopierConfig":
    """Construct CopierConfig from stored config dict."""
    from polymarket.copier import CopierConfig
    ct = cfg.get("copy_trading", {})
    return CopierConfig(
        private_key=ct.get("private_key", ""),
        funder=ct.get("funder", ""),
        chain_id=int(ct.get("chain_id", 137)),
        signature_type=int(ct.get("signature_type", 1)),
        sizing_mode=ct.get("sizing_mode", "fixed"),
        fixed_usdc=float(ct.get("fixed_usdc", 1.0)),
        reference_trade_usdc=float(ct.get("reference_trade_usdc", 50.0)),
        pct_balance=float(ct.get("pct_balance", 0.02)),
        mirror_pct=float(ct.get("mirror_pct", 0.01)),
        max_trade_usdc=float(ct.get("max_trade_usdc", 10.0)),
        daily_limit_usdc=float(ct.get("daily_limit_usdc", 30.0)),
        min_order_size_cap=float(ct.get("min_order_size_cap", 10.0)),
        dry_run=bool(ct.get("dry_run", True)),
        slippage=float(ct.get("slippage", 0.01)),
        blocked_keywords=list(ct.get("blocked_keywords", [])),
        min_score=float(ct.get("min_score", 50.0)),
        score_scale_size=bool(ct.get("score_scale_size", True)),
        single_wallet_mode=bool(ct.get("single_wallet_mode", False)),
        enable_topup=bool(ct.get("enable_topup", False)),
        max_topups=int(ct.get("max_topups", 2)),
        topup_size_multiplier=float(ct.get("topup_size_multiplier", 1.0)),
    )


def _sanitise_for_seed(cfg: dict) -> dict:
    """Strip keys that don't belong in the DB (e.g. data_dir, database_url)."""
    skip = {"data_dir", "database_url"}
    return {k: v for k, v in cfg.items() if k not in skip}


def _scrub_sentinels(stored: dict) -> None:
    """Remove '***' mask values from stored config in-place.

    If the UI saved '***' as a sensitive field value (before the put_settings
    guard was in place), drop the key so the seed_cfg / real value wins during
    the merge in get_settings().
    """
    for key in _SENSITIVE:
        if stored.get(key) == "***":
            del stored[key]
    ct = stored.get("copy_trading", {})
    for key in _SENSITIVE_NESTED:
        if ct.get(key) == "***":
            del ct[key]
