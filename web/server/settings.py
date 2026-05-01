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
    "top_n": 100,
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
    "proxy_url": "",
    "proxy_username": "",
    "proxy_password": "",
    "copy_trading": {
        "dry_run": True,
        "signature_type": 2,
        "sizing_mode": "fixed",
        "fixed_usdc": 1.0,
        "reference_trade_usdc": 50.0,
        "pct_balance": 0.02,
        "mirror_pct": 0.01,
        "max_trade_usdc": 10.0,
        "daily_limit_usdc": 30.0,
        "slippage": 0.01,
        "max_price": 0.85,
        "min_score": 50.0,
        "score_scale_size": True,
        "manual_target_wallets": [],
        "basket_ids": [],
        "basket_trade_refresh_interval": 300,
        "enable_topup": False,
        "max_topups": 2,
        "topup_size_multiplier": 1.0,
        "blocked_keywords": [],
        "stop_loss_pct": 0.0,
        "trailing_stop_pct": 0.0,
        "trailing_stop_min_gain": 2.0,
        "position_check_interval": 60,
        "private_key": "",
        "funder": "",
        "builder_code": "",
    },
}

# Fields never returned in GET /api/settings (shown masked instead)
_SENSITIVE = {"private_key", "polygon_wss", "proxy_password"}

# Nested sensitive fields under copy_trading
_SENSITIVE_NESTED = {"private_key"}
_LEGACY_COPY_TRADING_KEYS = {
    "single_wallet_mode",
}


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
    _scrub_legacy_copy_trading_keys(stored_clean)
    if stored_clean != stored:
        logger.warning("Scrubbed stale masked or legacy settings values — rewriting clean copy to DB.")
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
            v = {ck: cv for ck, cv in v.items() if ck not in _LEGACY_COPY_TRADING_KEYS}
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
        signature_type=int(ct.get("signature_type", 2)),
        builder_code=ct.get("builder_code", ""),
        sizing_mode=ct.get("sizing_mode", "fixed"),
        fixed_usdc=float(ct.get("fixed_usdc", 1.0)),
        reference_trade_usdc=float(ct.get("reference_trade_usdc", 50.0)),
        pct_balance=float(ct.get("pct_balance", 0.02)),
        mirror_pct=float(ct.get("mirror_pct", 0.01)),
        max_trade_usdc=float(ct.get("max_trade_usdc", 10.0)),
        daily_limit_usdc=float(ct.get("daily_limit_usdc", 30.0)),
        dry_run=bool(ct.get("dry_run", True)),
        slippage=float(ct.get("slippage", 0.01)),
        max_price=float(ct.get("max_price", 0.85)),
        blocked_keywords=list(ct.get("blocked_keywords", [])),
        min_score=float(ct.get("min_score", 50.0)),
        score_scale_size=bool(ct.get("score_scale_size", True)),
        manual_target_wallets=list(ct.get("manual_target_wallets", [])),
        basket_ids=list(ct.get("basket_ids", [])),
        enable_topup=bool(ct.get("enable_topup", False)),
        max_topups=int(ct.get("max_topups", 2)),
        topup_size_multiplier=float(ct.get("topup_size_multiplier", 1.0)),
        stop_loss_pct=float(ct.get("stop_loss_pct", 0.0)),
        trailing_stop_pct=float(ct.get("trailing_stop_pct", 0.0)),
        trailing_stop_min_gain=float(ct.get("trailing_stop_min_gain", 2.0)),
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


def _scrub_legacy_copy_trading_keys(stored: dict) -> None:
    """Remove deprecated single-wallet settings keys from stored config."""
    ct = stored.get("copy_trading", {})
    for key in _LEGACY_COPY_TRADING_KEYS:
        ct.pop(key, None)
