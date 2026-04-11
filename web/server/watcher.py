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
            copy_trader = CopyTrader(
                config=build_copier_config(cfg),
                storage=storage,
            )
            async with state._lock:
                state.copy_enabled = True

        # Compute scores (writes back to wallets table) — skipped when skip_recalculation=True
        scores: dict = {}
        if skip_recalculation:
            logger.info("Skipping wallet score recalculation (using existing scores).")
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
            if scores:
                copy_trader.update_scores(scores)
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

        # Choose stream or poll (stream + wss_url already validated above)
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
