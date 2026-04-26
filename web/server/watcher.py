"""
Watcher lifecycle — manages the stream/poll background asyncio task.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket.storage import Storage

from .settings import build_copier_config

logger = logging.getLogger(__name__)


def _load_scores_from_storage(storage) -> dict:
    """Reconstruct {address: WalletScore} from persisted score_detail JSONB."""
    from polymarket.models import WalletScore
    from dataclasses import fields as dc_fields
    valid_fields = {f.name for f in dc_fields(WalletScore)}
    result = {}
    for row in storage.get_wallets():
        detail = row.get("score_detail")
        if not detail:
            continue
        try:
            kwargs = {k: v for k, v in detail.items() if k in valid_fields}
            result[row["address"]] = WalletScore(**kwargs)
        except Exception:
            continue
    logger.info("Loaded %d existing wallet scores from DB.", len(result))
    return result


@dataclass
class WatcherState:
    task: asyncio.Task | None = None
    status: str = "stopped"          # stopped | starting | running | error
    mode: str = ""                   # stream | poll
    wallets_tracked: int = 0
    wallets_scored: int = 0
    last_signal_at: str | None = None
    error: str | None = None
    copy_enabled: bool = False
    target_wallets: list[str] = field(default_factory=list)
    target_wallet_usernames: list[str] = field(default_factory=list)
    target_mode: str = "auto"
    _monitor: object | None = field(default=None, repr=False)  # SignalMonitor, for clean stop
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


async def start_watcher(
    state: WatcherState,
    storage: "Storage",
    cfg: dict,
    skip_recalculation: bool = True,
) -> None:
    """Spawn the watcher background task from ``cfg``."""
    async with state._lock:
        if state.task is not None and not state.task.done():
            raise RuntimeError("Watcher is already running.")

        state.status = "starting"
        state.error = None
        state.copy_enabled = False
        state.target_wallets = []
        state.target_wallet_usernames = []
        state.target_mode = "auto"

    try:
        task = asyncio.create_task(
            _run_watcher(state, storage, cfg, skip_recalculation=skip_recalculation),
            name="polymarket-watcher",
        )
        async with state._lock:
            state.task = task
    except Exception as exc:
        async with state._lock:
            state.status = "error"
            state.error = str(exc)
        raise


async def stop_watcher(state: WatcherState) -> None:
    """Cancel the background task and wait for it to finish."""
    async with state._lock:
        task = state.task
        if task is None or task.done():
            state.status = "stopped"
            state.task = None
            state.copy_enabled = False
            return

    # Signal the monitor thread to exit its sleep/loop before cancelling the task.
    # Without this, the OS thread running monitor.run() keeps looping forever even
    # after the asyncio task is cancelled, leaking threads and eventually exhausting
    # the thread pool (causing asyncio.to_thread calls to hang indefinitely).
    if state._monitor is not None:
        try:
            state._monitor.stop()
        except Exception:
            pass

    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    async with state._lock:
        state.task = None
        state.status = "stopped"
        state.wallets_tracked = 0
        state.mode = ""
        state.target_wallets = []
        state.target_wallet_usernames = []
        state.target_mode = "auto"
        state.wallets_scored = 0
        state.copy_enabled = False
        state._monitor = None


async def _basket_trade_refresh_loop(
    storage: "Storage",
    client,
    basket_ids: list[int],
    interval: int,
) -> None:
    """Background coroutine: refresh wallet_trades for basket members every `interval` seconds.

    Runs concurrently with the main monitor/stream loop inside _run_watcher.
    Fetches the last 3 days of trades per wallet so the 48-hour consensus window
    always has fresh data even between signal detections.
    """
    logger.info(
        "Basket trade refresh loop started — %d basket(s), interval %ds.",
        len(basket_ids), interval,
    )
    while True:
        await asyncio.sleep(interval)

        # Collect all active basket wallet addresses
        basket_addrs: set[str] = set()
        for basket_id in basket_ids:
            try:
                basket = await asyncio.to_thread(storage.get_basket, basket_id)
                if basket and basket.get("active"):
                    basket_addrs.update(basket.get("wallet_addresses") or [])
            except Exception as exc:
                logger.warning("Basket refresh: failed to load basket %d: %s", basket_id, exc)

        if not basket_addrs:
            logger.debug("Basket refresh: no active basket wallets found, skipping.")
            continue

        logger.debug("Basket refresh: polling trades for %d wallet(s).", len(basket_addrs))
        for address in basket_addrs:
            try:
                # Fetch last 3 days — enough to cover the 48-hour consensus window
                trades = await asyncio.to_thread(client.activity_paginated, address, 3)
                n = await asyncio.to_thread(storage.upsert_wallet_trades, address, trades)
                if n:
                    logger.info(
                        "Basket refresh: +%d new trade(s) for %s…", n, address[:10]
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Basket refresh: failed to fetch trades for %s…: %s", address[:10], exc
                )


async def _position_monitor_loop(
    storage: "Storage",
    copy_trader,
    interval: int,
) -> None:
    """Background coroutine: check open positions for TP/SL conditions every `interval` seconds.

    For each open position with a current_price already set:
    - Stop-loss:   exit if current_price ≤ entry_price × (1 − stop_loss_pct)
    - Take-profit: exit if current_price ≥ take_profit_price (absolute ceiling, 0 = disabled)

    Delegates to CopyTrader.close_position() which reuses the same sell execution path
    (dry-run closes DB; live positions place a real SELL market order).
    """
    cfg = copy_trader._cfg
    stop_loss_pct = cfg.stop_loss_pct
    take_profit_price = cfg.take_profit_price

    if not stop_loss_pct and not take_profit_price:
        logger.info("Position monitor: TP/SL both disabled — loop exiting.")
        return

    logger.info(
        "Position monitor started — interval %ds, SL %.0f%%, TP price %.2f",
        interval,
        stop_loss_pct * 100 if stop_loss_pct else 0,
        take_profit_price,
    )

    while True:
        await asyncio.sleep(interval)

        try:
            positions = await asyncio.to_thread(storage.get_open_positions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Position monitor: failed to fetch open positions: %s", exc)
            continue

        for pos in positions:
            current_price = pos.get("current_price")
            if current_price is None:
                continue  # price hasn't been fetched yet for this position

            current_price = float(current_price)
            entry_price = float(pos.get("entry_price") or 0)
            market_title = pos.get("market_title", "")[:40]

            trigger_reason: str | None = None

            # Stop-loss: price dropped ≥ stop_loss_pct below entry
            if stop_loss_pct > 0 and entry_price > 0:
                sl_threshold = round(entry_price * (1.0 - stop_loss_pct), 4)
                if current_price <= sl_threshold:
                    trigger_reason = (
                        f"Stop-loss: price ${current_price:.4f} ≤ "
                        f"threshold ${sl_threshold:.4f} "
                        f"(entry ${entry_price:.4f} − {stop_loss_pct*100:.0f}%)"
                    )

            # Take-profit: price reached absolute ceiling
            if trigger_reason is None and take_profit_price > 0:
                if current_price >= take_profit_price:
                    trigger_reason = (
                        f"Take-profit: price ${current_price:.4f} ≥ "
                        f"ceiling ${take_profit_price:.4f}"
                    )

            if trigger_reason is None:
                continue

            logger.info(
                "Position monitor: triggering close for pos %d [%s] — %s",
                pos.get("id", 0), market_title, trigger_reason,
            )
            try:
                result = await asyncio.to_thread(
                    copy_trader.close_position, pos, trigger_reason
                )
                if result:
                    logger.info(
                        "Position monitor: pos %d closed — %s",
                        pos.get("id", 0), result.reason,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Position monitor: error closing pos %d: %s", pos.get("id", 0), exc
                )


async def _run_watcher(
    state: WatcherState,
    storage: "Storage",
    cfg: dict,
    skip_recalculation: bool = True,
) -> None:
    """Build all components from cfg and run the signal loop."""
    from polymarket.client import PolymarketClient
    from polymarket.scanner import LeaderboardScanner
    from polymarket.analyzer import WalletAnalyzer
    from polymarket.copier import CopyTrader
    from polymarket.stream import PolymarketStream
    from polymarket.monitor import SignalMonitor
    from polymarket.scorer import WalletScorer

    try:
        request_delay = float(cfg.get("request_delay", 0.5))
        max_retries = int(cfg.get("max_retries", 3))
        top_n = int(cfg.get("top_n", 20))
        min_position_usdc = float(cfg.get("min_position_usdc", 50.0))
        poll_interval = int(cfg.get("poll_interval", 300))
        max_signal_age = int(cfg.get("max_signal_age", 3600))
        wallet_refresh_interval = int(cfg.get("wallet_refresh_interval", 600))
        wss_url = cfg.get("polygon_wss", "").strip()
        watcher_mode = cfg.get("watcher_mode", "poll")
        ct_cfg_top = cfg.get("copy_trading", {})
        basket_ids: list[int] = [b for b in ct_cfg_top.get("basket_ids", []) if b]
        basket_trade_refresh_interval: int = int(
            ct_cfg_top.get("basket_trade_refresh_interval", 300)
        )
        position_check_interval: int = int(
            ct_cfg_top.get("position_check_interval", 60)
        )

        # Fail fast: stream mode requires polygon_wss before any network calls
        if watcher_mode == "stream" and not wss_url:
            err = "Stream mode requires a Polygon WSS URL. Add it in Settings → Credentials or switch to Poll mode."
            async with state._lock:
                state.status = "error"
                state.error = err
                state.task = None
            logger.error(err)
            return

        client = PolymarketClient(
            request_delay=request_delay,
            max_retries=max_retries,
            proxy_url=cfg.get("proxy_url", "").strip(),
            proxy_username=cfg.get("proxy_username", "").strip(),
            proxy_password=cfg.get("proxy_password", "").strip(),
        )
        scanner = LeaderboardScanner(
            client=client,
            storage=storage,
            top_n=top_n,
            leaderboard_ttl=int(cfg.get("leaderboard_ttl", 3600)),
        )
        analyzer = WalletAnalyzer(client=client)

        # Fetch wallets + compute scores
        wallets = await asyncio.to_thread(
            scanner.fetch_top_wallets, force_refresh=True
        )
        async with state._lock:
            state.wallets_tracked = len(wallets)
            state.wallets_scored = 0

        # Build copy trader if credentials present
        ct_cfg = cfg.get("copy_trading", {})
        has_creds = bool(ct_cfg.get("private_key") and ct_cfg.get("funder"))
        copy_trader: CopyTrader | None = None
        if has_creds:
            # Fail fast: copy trading needs at least one target source.
            # Mirrors the pre-flight check in CLI's _build_copy_trader().
            manual_wallets = [w for w in ct_cfg.get("manual_target_wallets", []) if str(w).strip()]
            basket_ids     = [b for b in ct_cfg.get("basket_ids", []) if b]
            if not manual_wallets and not basket_ids:
                err = (
                    "Copy trading is enabled but no targets are configured. "
                    "Add wallets to copy_trading.manual_target_wallets, "
                    "or create a basket with wallet addresses and set copy_trading.basket_ids."
                )
                async with state._lock:
                    state.status = "error"
                    state.error = err
                    state.task = None
                logger.error(err)
                return

            copy_trader = CopyTrader(
                config=build_copier_config(cfg),
                storage=storage,
            )
            async with state._lock:
                state.copy_enabled = True

        # Compute scores (writes back to wallets table) — or load from DB when skipping
        scores: dict = {}
        if skip_recalculation:
            logger.info("Skipping wallet score recalculation — loading existing scores from DB.")
            scores = await asyncio.to_thread(_load_scores_from_storage, storage)
        else:
            stats_list = []
            for w in wallets:
                try:
                    stats_list.append(await asyncio.to_thread(analyzer.analyze, w))
                except Exception as exc:
                    logger.warning("Score analysis failed for %s: %s", w.username, exc)

            scorer = WalletScorer()
            scores = await asyncio.to_thread(scorer.score_all, stats_list, storage)
            async with state._lock:
                state.wallets_scored = len(scores)

        if copy_trader:
            copy_trader.update_scores(scores)
            # If scores were empty but manual refs are configured, seed targets directly
            # so signals aren't rejected with "no scored wallets selected yet"
            if not copy_trader._target_wallets and copy_trader._manual_refs:
                copy_trader._target_wallets = set(copy_trader._manual_refs)
            wallet_name_map = {w.address: w.username for w in wallets}
            async with state._lock:
                state.target_wallets = sorted(copy_trader._target_wallets)
                state.target_wallet_usernames = [
                    wallet_name_map.get(address) or f"{address[:8]}…"
                    for address in state.target_wallets
                ]
                state.target_mode = "manual" if copy_trader._cfg.manual_target_wallets else "auto"
            target_mode_label = "manual" if copy_trader._cfg.manual_target_wallets else "auto"
            logger.info(
                "Copy targets (%s): %d wallet(s) — %s",
                target_mode_label,
                len(state.target_wallets),
                ", ".join(state.target_wallet_usernames) or "none",
            )

        # ── Sync callback (monitor / poll path) ─────────────────────────────
        # monitor.run() executes in a thread-pool thread (asyncio.to_thread),
        # so its callback must be synchronous — awaiting an async coroutine
        # inside a plain thread silently creates and discards the coroutine.
        def sync_on_signal(sig):
            state.last_signal_at = datetime.now(timezone.utc).isoformat()
            # Write every detected BUY into wallet_trades immediately so the
            # consensus query sees it without waiting for the next refresh cycle.
            if sig.side == "BUY" and basket_ids:
                try:
                    storage.upsert_signal_as_trade(sig)
                except Exception as exc:
                    logger.debug("Failed to write signal to wallet_trades: %s", exc)
            if copy_trader:
                try:
                    result = copy_trader.copy(sig)
                except Exception as exc:
                    logger.error("Copier exception for signal %s: %s", sig.alert_id, exc)
                    if sig.alert_id:
                        storage.update_alert_copier_result(sig.alert_id, "failed", str(exc), 0.0)
                    return
                if sig.alert_id:
                    storage.update_alert_copier_result(
                        sig.alert_id, result.status, result.reason, result.spend_usdc
                    )

        # ── Async callback (stream path) ─────────────────────────────────────
        async def async_on_signal(sig):
            async with state._lock:
                state.last_signal_at = datetime.now(timezone.utc).isoformat()
            # Write every detected BUY into wallet_trades immediately (same as poll path).
            if sig.side == "BUY" and basket_ids:
                try:
                    await asyncio.to_thread(storage.upsert_signal_as_trade, sig)
                except Exception as exc:
                    logger.debug("Failed to write signal to wallet_trades: %s", exc)
            if copy_trader:
                try:
                    result = await asyncio.to_thread(copy_trader.copy, sig)
                except Exception as exc:
                    logger.error("Copier exception for signal %s: %s", sig.alert_id, exc)
                    if sig.alert_id:
                        await asyncio.to_thread(
                            storage.update_alert_copier_result,
                            sig.alert_id, "failed", str(exc), 0.0,
                        )
                    return
                if sig.alert_id:
                    await asyncio.to_thread(
                        storage.update_alert_copier_result,
                        sig.alert_id,
                        result.status,
                        result.reason,
                        result.spend_usdc,
                    )

        # ── Basket trade refresh task ────────────────────────────────────────
        # Runs concurrently with the main loop; cancelled when the watcher stops.
        refresh_task: asyncio.Task | None = None
        if basket_ids:
            refresh_task = asyncio.create_task(
                _basket_trade_refresh_loop(
                    storage, client, basket_ids, basket_trade_refresh_interval
                ),
                name="basket-trade-refresh",
            )

        # ── Position monitor task (TP / SL) ──────────────────────────────────
        # Only spawned when copy trader is active and at least one TP/SL threshold is set.
        monitor_task: asyncio.Task | None = None
        if copy_trader and (
            copy_trader._cfg.stop_loss_pct > 0 or copy_trader._cfg.take_profit_price > 0
        ):
            monitor_task = asyncio.create_task(
                _position_monitor_loop(storage, copy_trader, position_check_interval),
                name="position-monitor",
            )

        # ── Choose stream or poll (stream + wss_url already validated above) ─
        try:
            if watcher_mode == "stream":
                async with state._lock:
                    state.mode = "stream"
                    state.status = "running"

                stream = PolymarketStream(
                    wss_url=wss_url,
                    client=client,
                    scanner=scanner,
                    storage=storage,
                    top_wallets=wallets,
                    min_position_usdc=min_position_usdc,
                    wallet_refresh_interval=wallet_refresh_interval,
                )
                await stream.run(async_on_signal)
            else:
                async with state._lock:
                    state.mode = "poll"
                    state.status = "running"

                monitor = SignalMonitor(
                    client=client,
                    scanner=scanner,
                    storage=storage,
                    poll_interval=poll_interval,
                    min_position_usdc=min_position_usdc,
                    max_signal_age=max_signal_age,
                )
                async with state._lock:
                    state._monitor = monitor
                await asyncio.to_thread(monitor.run, sync_on_signal)
        finally:
            for bg_task in (refresh_task, monitor_task):
                if bg_task and not bg_task.done():
                    bg_task.cancel()
                    try:
                        await bg_task
                    except asyncio.CancelledError:
                        pass

    except asyncio.CancelledError:
        logger.info("Watcher task cancelled.")
        async with state._lock:
            state._monitor = None
        raise
    except Exception as exc:
        logger.exception("Watcher task crashed: %s", exc)
        async with state._lock:
            state.status = "error"
            state.error = str(exc)
            state.task = None
            state._monitor = None
