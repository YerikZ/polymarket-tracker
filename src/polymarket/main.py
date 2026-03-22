import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from . import config as cfg_module
from . import db
from .client import PolymarketClient
from .copier import CopierConfig, CopyTrader
from .models import CopyResult, Signal, Wallet, WalletScore, WalletStats
from .storage import Storage
from .scanner import LeaderboardScanner
from .analyzer import WalletAnalyzer
from .monitor import SignalMonitor
from .scorer import WalletScorer
from .stream import PolymarketStream

console = Console()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fmt_pnl(value: float) -> str:
    sign = "+" if value >= 0 else ""
    color = "green" if value >= 0 else "red"
    return f"[{color}]{sign}${value:,.2f}[/{color}]"


def _fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    color = "green" if value >= 0 else "red"
    return f"[{color}]{sign}{value:.1f}%[/{color}]"


def _short_addr(addr: str) -> str:
    if len(addr) > 12:
        return addr[:6] + "…" + addr[-4:]
    return addr


_TIER_STYLE = {
    "A":    "bold green",
    "B":    "green",
    "C":    "yellow",
    "WATCH":"dim yellow",
    "SKIP": "dim red",
    "?":    "dim",
}

def _fmt_score(score: WalletScore | None) -> str:
    if score is None:
        return "[dim]—[/dim]"
    if score.insufficient_data:
        return f"[dim]{score.total:.0f} ?data[/dim]"
    style = _TIER_STYLE.get(score.copy_tier, "white")
    return f"[{style}]{score.total:.0f}[/{style}] [dim]{score.copy_tier}[/dim]"


def render_top_table(
    wallets: list[Wallet],
    stats: list[WalletStats],
    scores: dict[str, WalletScore] | None = None,
) -> Table:
    table = Table(
        title=f"Polymarket Top Wallets  (updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        box=box.HEAVY_HEAD,
        show_lines=False,
    )
    table.add_column("Rank",            style="bold cyan",  justify="right")
    table.add_column("Username",        style="bold white")
    table.add_column("Address",         style="dim")
    table.add_column("Leaderboard P&L", justify="right")
    table.add_column("Open P&L",        justify="right")
    table.add_column("Win Rate*",       justify="right")
    table.add_column("Avg Size",        justify="right")
    table.add_column("Positions",       justify="right")
    table.add_column("Score",           justify="right")

    stats_by_addr  = {s.wallet.address: s for s in stats}

    for w in wallets:
        s  = stats_by_addr.get(w.address)
        sc = (scores or {}).get(w.address)
        table.add_row(
            str(w.rank),
            w.username or "—",
            _short_addr(w.address),
            _fmt_pnl(w.pnl),
            _fmt_pnl(s.total_pnl) if s else "—",
            _fmt_pct(s.win_rate * 100) if s else "—",
            f"${s.avg_position_size:,.0f}" if s else "—",
            str(len(s.open_positions)) if s else "—",
            _fmt_score(sc),
        )

    return table


def render_score_breakdown(
    wallets: list[Wallet],
    scores: dict[str, WalletScore],
) -> None:
    """Print a detailed per-signal score table for all wallets."""

    def _bar(value: float, max_val: float, width: int = 8) -> str:
        filled = round(value / max_val * width) if max_val else 0
        bar = "█" * filled + "░" * (width - filled)
        color = "green" if value / max_val >= 0.6 else ("yellow" if value / max_val >= 0.35 else "red")
        return f"[{color}]{bar}[/{color}]"

    table = Table(
        title="Wallet Score Breakdown",
        box=box.SIMPLE_HEAVY,
        show_lines=True,
    )
    table.add_column("Wallet",    style="bold", width=14)
    table.add_column("Tier",      justify="center", width=5)
    # Skill
    table.add_column("S1 Edge\n/20",  justify="right", width=10)
    table.add_column("S2 Consist\n/15", justify="right", width=11)
    table.add_column("S3 Indep\n/10", justify="right", width=10)
    table.add_column("Skill\n/45",    justify="right", width=9, style="bold")
    # Reliability
    table.add_column("R1 Breadth\n/10", justify="right", width=11)
    table.add_column("R2 Sharpe\n/10",  justify="right", width=10)
    table.add_column("R3 Trend\n/10",   justify="right", width=10)
    table.add_column("Rely\n/30",       justify="right", width=8, style="bold")
    # Copiability
    table.add_column("C1 Impact\n/10",  justify="right", width=10)
    table.add_column("C2 Fresh\n/10",   justify="right", width=10)
    table.add_column("C3 Liq\n/5",      justify="right", width=8)
    table.add_column("Copy\n/25",       justify="right", width=8, style="bold")
    # Total
    table.add_column("TOTAL\n/100",     justify="right", width=9, style="bold")
    table.add_column("Copy size",        justify="right", width=9)

    for w in wallets:
        sc = scores.get(w.address)
        if sc is None:
            continue
        tier_style = _TIER_STYLE.get(sc.copy_tier, "white")
        name = (w.username or _short_addr(w.address))[:12]
        insuff = " [dim]?[/dim]" if sc.insufficient_data else ""

        def _cell(val, mx):
            return f"{val:.1f} {_bar(val, mx, 5)}"

        table.add_row(
            name + insuff,
            f"[{tier_style}]{sc.copy_tier}[/{tier_style}]",
            _cell(sc.s1_calibrated_edge, 20),
            _cell(sc.s2_temporal_consistency, 15),
            _cell(sc.s3_independence, 10),
            f"[bold]{sc.skill:.1f}[/bold]",
            _cell(sc.r1_sample_breadth, 10),
            _cell(sc.r2_sharpe, 10),
            _cell(sc.r3_recency_trend, 10),
            f"[bold]{sc.reliability:.1f}[/bold]",
            _cell(sc.c1_market_impact, 10),
            _cell(sc.c2_signal_freshness, 10),
            _cell(sc.c3_liquidity, 5),
            f"[bold]{sc.copiability:.1f}[/bold]",
            f"[bold {tier_style}]{sc.total:.0f}[/bold {tier_style}]",
            f"{sc.copy_size_pct*100:.0f}%",
        )

    console.print(table)

    # Copy-usage legend
    legend = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    legend.add_column(style="bold", width=7)
    legend.add_column(width=52)
    legend.add_column(style="dim", width=24)
    for tier, style, desc, note in [
        ("A",    "bold green",    "Copy at full configured size",              "80–100 pts"),
        ("B",    "green",         "Copy at 70% size",                          "65–79 pts"),
        ("C",    "yellow",        "Copy at 40%, strong-category signals only", "50–64 pts"),
        ("WATCH","dim yellow",    "Paper trade only — do not go live",         "35–49 pts"),
        ("SKIP", "dim red",       "Ignore entirely",                           "<35 pts"),
        ("?",    "dim",           "Insufficient data — paper trade only",      f"<10 unique markets"),
    ]:
        legend.add_row(f"[{style}]{tier}[/{style}]", desc, note)
    console.print(Panel(legend, title="[bold]Score Tiers — Copy Strategy[/bold]", border_style="dim"))

    # Category annotation
    categorised = [
        (w.username or _short_addr(w.address), scores[w.address].strong_categories)
        for w in wallets
        if w.address in scores and scores[w.address].strong_categories
    ]
    if categorised:
        console.print("\n[bold]Domain Edge[/bold] [dim](categories with ≥5 trades)[/dim]")
        for name, cats in categorised:
            console.print(f"  [cyan]{name}[/cyan]  {', '.join(cats)}")


def render_wallet_detail(stats: WalletStats) -> None:
    w = stats.wallet
    console.print(Panel(
        f"[bold]{w.username}[/bold]  [dim]{w.address}[/dim]\n"
        f"Rank #{w.rank}  |  Leaderboard P&L: {_fmt_pnl(w.pnl)}  |  Volume: ${w.trading_volume:,.0f}",
        title="Wallet Profile",
        border_style="cyan",
    ))

    # Summary
    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column(style="dim", width=22)
    summary.add_column()
    summary.add_row("Open P&L", _fmt_pnl(stats.total_pnl))
    summary.add_row("Win Rate*", _fmt_pct(stats.win_rate * 100))
    summary.add_row("Avg Position Size", f"${stats.avg_position_size:,.2f}")
    summary.add_row("Open Positions", str(len(stats.open_positions)))
    console.print(summary)
    console.print("[dim italic]* Win rate approximated from % of open positions currently in profit.[/dim italic]\n")

    # Positions table
    if stats.open_positions:
        pos_table = Table(title="Open Positions", box=box.SIMPLE_HEAVY, show_lines=False)
        pos_table.add_column("Market", max_width=50)
        pos_table.add_column("Outcome", style="cyan")
        pos_table.add_column("Entry", justify="right")
        pos_table.add_column("Current", justify="right")
        pos_table.add_column("Size ($)", justify="right")
        pos_table.add_column("P&L", justify="right")
        for p in sorted(stats.open_positions, key=lambda x: x.cash_pnl, reverse=True):
            pos_table.add_row(
                p.title[:50],
                p.outcome,
                f"${p.avg_price:.3f}",
                f"${p.cur_price:.3f}",
                f"${p.initial_value:,.0f}",
                _fmt_pnl(p.cash_pnl),
            )
        console.print(pos_table)
    else:
        console.print("[dim]No open positions found.[/dim]\n")

    # Recent trades
    if stats.recent_trades:
        trade_table = Table(title="Recent Trades (last 30 days)", box=box.SIMPLE_HEAVY, show_lines=False)
        trade_table.add_column("Date", style="dim")
        trade_table.add_column("Market", max_width=45)
        trade_table.add_column("Outcome", style="cyan")
        trade_table.add_column("Side")
        trade_table.add_column("Price", justify="right")
        trade_table.add_column("USDC", justify="right")
        for t in stats.recent_trades[:25]:
            dt = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m-%d") if t.timestamp else "—"
            side_fmt = "[green]BUY[/green]" if t.side == "BUY" else "[red]SELL[/red]"
            trade_table.add_row(dt, t.title[:45], t.outcome, side_fmt, f"${t.price:.3f}", f"${t.usdc_size:,.0f}")
        console.print(trade_table)
    else:
        console.print("[dim]No recent trades found.[/dim]")


def render_signal(sig: Signal, copy_result: CopyResult | None = None) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    content = (
        f"  [bold]Trader[/bold]   [cyan]{sig.username}[/cyan] (#{sig.wallet_rank})  [dim]{_short_addr(sig.wallet_address)}[/dim]\n"
        f"  [bold]Market[/bold]   {sig.market_title}\n"
        f"  [bold]Side[/bold]     [green]{sig.side}[/green]  [cyan]{sig.outcome}[/cyan]  @ [yellow]${sig.price:.4f}[/yellow]\n"
        f"  [bold]Size[/bold]     [bold green]${sig.usdc_size:,.2f} USDC[/bold green]\n"
        f"  [bold]Tx[/bold]       [dim]{sig.transaction_hash[:18]}…[/dim]"
    )
    if copy_result:
        status_colors = {"placed": "green", "dry_run": "yellow", "skipped": "dim", "failed": "red"}
        color = status_colors.get(copy_result.status, "white")
        icon = {"placed": "✓", "dry_run": "~", "skipped": "–", "failed": "✗"}.get(copy_result.status, "?")
        content += f"\n\n  [bold]Copy[/bold]     [{color}]{icon} {copy_result.status.upper()}[/{color}]  {copy_result.reason}"

    border = "green" if not copy_result or copy_result.status in ("placed", "dry_run") else "dim"
    title = "[bold green]COPY-TRADE ALERT[/bold green]" if not copy_result else {
        "placed": "[bold green]ORDER PLACED[/bold green]",
        "dry_run": "[bold yellow]DRY RUN — WOULD PLACE[/bold yellow]",
        "skipped": "[bold dim]SIGNAL (copy skipped)[/bold dim]",
        "failed": "[bold red]ORDER FAILED[/bold red]",
    }.get(copy_result.status, "[bold]SIGNAL[/bold]")

    console.print(f"\n[dim]{ts}[/dim]  [bold yellow]NEW SIGNAL DETECTED[/bold yellow]")
    console.print(Panel(content, title=title, border_style=border))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_top(args: argparse.Namespace, client: PolymarketClient, scanner: LeaderboardScanner, analyzer: WalletAnalyzer) -> None:
    with console.status("Fetching leaderboard…"):
        wallets = scanner.fetch_top_wallets(force_refresh=args.refresh)

    if not wallets:
        console.print("[red]No wallets found. Try --refresh or check your internet connection.[/red]")
        return

    n = min(args.limit, len(wallets))
    console.print(f"[dim]Analyzing {n} wallets…[/dim]")
    stats_list: list[WalletStats] = []
    for i, w in enumerate(wallets[:n], 1):
        with console.status(f"  [{i}/{n}] Analyzing {w.username or _short_addr(w.address)}…"):
            stats_list.append(analyzer.analyze(w))

    with console.status("Scoring wallets…"):
        scorer = WalletScorer()
        scores = scorer.score_all(stats_list)

    console.print(render_top_table(wallets[:n], stats_list, scores))
    console.print()
    render_score_breakdown(wallets[:n], scores)
    console.print("\n[dim]Use [bold]polymarket wallet <address>[/bold] for detailed breakdown.[/dim]")


def cmd_wallet(args: argparse.Namespace, client: PolymarketClient, analyzer: WalletAnalyzer) -> None:
    address = args.address
    # Build a minimal Wallet stub so analyzer.analyze() works
    stub = Wallet(address=address, username=address[:8] + "…", rank=0, pnl=0.0, trading_volume=0.0, fetched_at="")

    with console.status("Fetching wallet data…"):
        stats = analyzer.analyze(stub)

    render_wallet_detail(stats)


def _build_copy_trader(
    args: argparse.Namespace, cfg: dict, storage: Storage
) -> CopyTrader | None:
    """Construct a CopyTrader from config + CLI flags, or return None."""
    if not args.copy:
        return None

    ct_cfg      = cfg.get("copy_trading", {})
    private_key = ct_cfg.get("private_key", "")
    funder      = ct_cfg.get("funder", "")
    dry_run     = args.dry_run or ct_cfg.get("dry_run", True)

    if not dry_run and (not private_key or not funder):
        console.print(
            "[red bold]Error:[/red bold] Live copy trading requires [bold]private_key[/bold] and "
            "[bold]funder[/bold] in config.yaml (or env vars).\n"
            "Use [bold]--dry-run[/bold] to simulate without credentials."
        )
        return None  # caller should bail if copy was requested but can't build trader

    return CopyTrader(
        config=CopierConfig(
            private_key=private_key,
            funder=funder,
            sizing_mode=ct_cfg.get("sizing_mode", "fixed"),
            fixed_usdc=float(ct_cfg.get("fixed_usdc", 50.0)),
            pct_balance=float(ct_cfg.get("pct_balance", 0.02)),
            mirror_pct=float(ct_cfg.get("mirror_pct", 0.01)),
            max_trade_usdc=float(ct_cfg.get("max_trade_usdc", 500.0)),
            daily_limit_usdc=float(ct_cfg.get("daily_limit_usdc", 1000.0)),
            dry_run=dry_run,
            slippage=float(ct_cfg.get("slippage", 0.01)),
            min_score=float(ct_cfg.get("min_score", 50.0)),
            score_scale_size=bool(ct_cfg.get("score_scale_size", True)),
        ),
        storage=storage,
    )


def _copy_info_line(copy_trader: CopyTrader | None) -> str:
    """One-line status string for the watch banner."""
    if copy_trader is None:
        return "\n  Copy mode: [dim]alert-only (use --copy to enable)[/dim]"
    c = copy_trader._cfg
    mode  = "[yellow]DRY RUN[/yellow]" if c.dry_run else "[bold green]LIVE TRADING[/bold green]"
    sizing = {
        "fixed":       f"${c.fixed_usdc:.0f} fixed",
        "pct_balance": f"{c.pct_balance*100:.1f}% of balance",
        "mirror_pct":  f"{c.mirror_pct*100:.1f}% of original",
    }.get(c.sizing_mode, c.sizing_mode)
    return (
        f"\n  Copy mode: {mode}  |  Sizing: [cyan]{sizing}[/cyan]  |  "
        f"Max/trade: [cyan]${c.max_trade_usdc:.0f}[/cyan]  |  "
        f"Daily limit: [cyan]${c.daily_limit_usdc:.0f}[/cyan]"
    )


def _compute_and_push_scores(
    wallets,
    analyzer: WalletAnalyzer,
    copy_trader: CopyTrader | None,
) -> dict[str, WalletScore]:
    """Fetch stats for all wallets, compute scores, and push to copy_trader."""
    stats_list = []
    for w in wallets:
        try:
            stats_list.append(analyzer.analyze(w))
        except Exception as exc:
            logger.warning("Score analysis failed for %s: %s", w.username, exc)
    scorer = WalletScorer()
    scores = scorer.score_all(stats_list)
    if copy_trader:
        copy_trader.update_scores(scores)
    return scores


def cmd_watch(
    args: argparse.Namespace,
    client: PolymarketClient,
    scanner: LeaderboardScanner,
    storage: Storage,
    cfg: dict,
    analyzer: WalletAnalyzer,
) -> None:
    copy_trader = _build_copy_trader(args, cfg, storage)
    # If --copy was requested but credentials are missing and not dry-run → bail
    if args.copy and copy_trader is None and not (args.dry_run or cfg.get("copy_trading", {}).get("dry_run", True)):
        return

    # Compute initial scores so copy_trader knows tiers before first signal
    if copy_trader:
        with console.status("Computing initial wallet scores…"):
            wallets = scanner.fetch_top_wallets()
            _compute_and_push_scores(wallets, analyzer, copy_trader)

    if args.poll:
        _cmd_watch_poll(args, client, scanner, storage, copy_trader)
    else:
        wss_url = cfg.get("polygon_wss", "").strip()
        if not wss_url:
            console.print(
                "[yellow]No polygon_wss configured.[/yellow]  "
                "Set [bold]polygon_wss[/bold] in config.yaml or [bold]POLYMARKET_POLYGON_WSS[/bold] env var.\n"
                "Get a free key at [link=https://www.alchemy.com]alchemy.com[/link] "
                "(Polygon Mainnet → WebSocket URL).\n\n"
                "Falling back to polling mode ([dim]--poll[/dim])."
            )
            _cmd_watch_poll(args, client, scanner, storage, cfg, copy_trader)
        else:
            asyncio.run(_cmd_watch_stream(args, client, scanner, storage, cfg, copy_trader, wss_url, analyzer))


# ── Polling mode (fallback) ────────────────────────────────────────────────────

def _cmd_watch_poll(
    args: argparse.Namespace,
    client: PolymarketClient,
    scanner: LeaderboardScanner,
    storage: Storage,
    cfg: dict,
    copy_trader: CopyTrader | None,
) -> None:
    monitor = SignalMonitor(
        client=client,
        scanner=scanner,
        storage=storage,
        poll_interval=args.interval,
        min_position_usdc=args.min_size,
        max_signal_age=cfg.get("max_signal_age", 3600),
    )
    console.print(Panel(
        f"Tracking [bold cyan]{scanner._top_n}[/bold cyan] wallets  |  "
        f"poll every [bold]{args.interval}s[/bold]  |  "
        f"min size [bold]${args.min_size:.0f}[/bold]  |  "
        "[dim]Ctrl-C to stop[/dim]"
        + _copy_info_line(copy_trader),
        title="[bold]Polymarket Watch[/bold] [dim](polling)[/dim]",
        border_style="cyan",
    ))

    def on_signal(sig: Signal) -> None:
        result = copy_trader.copy(sig) if copy_trader else None
        render_signal(sig, result)

    try:
        monitor.run(on_signal=on_signal)
    except KeyboardInterrupt:
        alerts = storage.get_alerts(limit=1000)
        console.print(f"\n[bold]Stopped.[/bold] {len(alerts)} total alerts saved.")


# ── Streaming mode (default) ───────────────────────────────────────────────────

async def _cmd_watch_stream(
    args: argparse.Namespace,
    client: PolymarketClient,
    scanner: LeaderboardScanner,
    storage: Storage,
    cfg: dict,
    copy_trader: CopyTrader | None,
    wss_url: str,
    analyzer: WalletAnalyzer | None = None,
) -> None:
    with console.status("Fetching top wallets…"):
        wallets = scanner.fetch_top_wallets()

    stream = PolymarketStream(
        wss_url=wss_url,
        client=client,
        scanner=scanner,
        storage=storage,
        top_wallets=wallets,
        min_position_usdc=args.min_size,
        wallet_refresh_interval=cfg.get("wallet_refresh_interval", 600),
    )

    console.print(Panel(
        f"Tracking [bold cyan]{len(wallets)}[/bold cyan] wallets  |  "
        f"min size [bold]${args.min_size:.0f}[/bold]  |  "
        f"wallet refresh every [bold]{cfg.get('wallet_refresh_interval', 600)}s[/bold]  |  "
        "[dim]Ctrl-C to stop[/dim]"
        + _copy_info_line(copy_trader),
        title="[bold]Polymarket Watch[/bold] [bold green]⚡ WebSocket[/bold green]",
        border_style="green",
    ))

    async def on_signal(sig: Signal) -> None:
        result = await asyncio.to_thread(copy_trader.copy, sig) if copy_trader else None
        render_signal(sig, result)

    try:
        await stream.run(on_signal=on_signal)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        alerts = storage.get_alerts(limit=1000)
        console.print(f"\n[bold]Stopped.[/bold] {len(alerts)} total alerts saved.")


def cmd_balance(args: argparse.Namespace, cfg: dict, storage: Storage) -> None:
    """Show USDC balance and daily spend."""
    from datetime import date
    ct_cfg = cfg.get("copy_trading", {})
    private_key = ct_cfg.get("private_key", "")
    funder = ct_cfg.get("funder", "")

    if not private_key or not funder:
        console.print(
            "[yellow]No credentials configured.[/yellow] Set [bold]private_key[/bold] and [bold]funder[/bold] "
            "in config.yaml or via POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER env vars."
        )
        return

    copier_config = CopierConfig(private_key=private_key, funder=funder, dry_run=False)
    trader = CopyTrader(config=copier_config, storage=storage)

    with console.status("Fetching balance…"):
        balance = trader.get_balance()

    today = date.today().isoformat()
    spent_today = storage.get_daily_spend(today)
    daily_limit = float(ct_cfg.get("daily_limit_usdc", 1000.0))

    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column(style="dim", width=22)
    summary.add_column()
    summary.add_row("USDC Balance", f"[bold green]${balance:,.2f}[/bold green]")
    summary.add_row("Spent today", f"${spent_today:.2f}")
    summary.add_row("Daily limit", f"${daily_limit:.2f}")
    summary.add_row("Remaining today", f"[cyan]${max(0, daily_limit - spent_today):.2f}[/cyan]")
    summary.add_row("Wallet", f"[dim]{funder}[/dim]")
    console.print(Panel(summary, title="[bold]Wallet Balance[/bold]", border_style="cyan"))


def cmd_positions(args: argparse.Namespace, client: PolymarketClient, analyzer: WalletAnalyzer, cfg: dict) -> None:
    """Show real open positions and P&L for your own wallet."""
    ct_cfg = cfg.get("copy_trading", {})
    address = ct_cfg.get("funder", "").strip()

    if not address:
        console.print(
            "[yellow]No wallet configured.[/yellow] Set [bold]funder[/bold] in config.yaml "
            "or [bold]POLYMARKET_FUNDER[/bold] env var."
        )
        return

    stub = Wallet(address=address, username="My Wallet", rank=0, pnl=0.0, trading_volume=0.0, fetched_at="")
    with console.status(f"Fetching positions for {address[:10]}…"):
        stats = analyzer.analyze(stub)

    positions = stats.open_positions
    trades    = stats.recent_trades

    if not positions:
        console.print("[dim]No open positions found for this wallet.[/dim]")
    else:
        # Sort worst → best so losses are obvious at a glance
        sort_key = args.sort if hasattr(args, "sort") else "pnl"
        if sort_key == "size":
            positions = sorted(positions, key=lambda p: p.initial_value, reverse=True)
        elif sort_key == "pct":
            positions = sorted(positions, key=lambda p: p.percent_pnl)
        else:  # default: absolute P&L worst first
            positions = sorted(positions, key=lambda p: p.cash_pnl)

        pos_table = Table(
            title=f"Open Positions  [dim]{address[:10]}…{address[-6:]}[/dim]",
            box=box.HEAVY_HEAD, show_lines=False,
        )
        pos_table.add_column("Market",      max_width=44)
        pos_table.add_column("Outcome",     style="cyan",   width=7)
        pos_table.add_column("Entry $",     justify="right", width=8)
        pos_table.add_column("Now $",       justify="right", width=8)
        pos_table.add_column("Shares",      justify="right", width=8)
        pos_table.add_column("Invested",    justify="right", width=10)
        pos_table.add_column("Value",       justify="right", width=10)
        pos_table.add_column("P&L",         justify="right", width=12)
        pos_table.add_column("%",           justify="right", width=8)
        pos_table.add_column("Ends",        style="dim",    width=11)
        pos_table.add_column("",            width=3)  # redeemable flag

        total_invested = 0.0
        total_value    = 0.0
        total_pnl      = 0.0

        for p in positions:
            pnl_color = "green" if p.cash_pnl >= 0 else "red"
            pnl_sign  = "+" if p.cash_pnl >= 0 else ""
            pct_color = "green" if p.percent_pnl >= 0 else "red"
            pct_sign  = "+" if p.percent_pnl >= 0 else ""
            redeem    = "[bold green]✓[/bold green]" if p.redeemable else ""
            end       = (p.end_date or "")[:10]

            pos_table.add_row(
                p.title[:44],
                p.outcome,
                f"${p.avg_price:.4f}",
                f"${p.cur_price:.4f}",
                f"{p.size:.1f}",
                f"${p.initial_value:,.2f}",
                f"${p.current_value:,.2f}",
                f"[{pnl_color}]{pnl_sign}${p.cash_pnl:,.2f}[/{pnl_color}]",
                f"[{pct_color}]{pct_sign}{p.percent_pnl:.1f}%[/{pct_color}]",
                end,
                redeem,
            )
            total_invested += p.initial_value
            total_value    += p.current_value
            total_pnl      += p.cash_pnl

        console.print(pos_table)

        # Summary
        pnl_color = "green" if total_pnl >= 0 else "red"
        pnl_sign  = "+" if total_pnl >= 0 else ""
        roi       = (total_pnl / total_invested * 100) if total_invested else 0.0
        redeemable_count = sum(1 for p in positions if p.redeemable)

        summary = Table(box=box.SIMPLE, show_header=False)
        summary.add_column(style="dim", width=22)
        summary.add_column()
        summary.add_row("Open positions",    str(len(positions)))
        summary.add_row("Win rate",          _fmt_pct(stats.win_rate * 100))
        summary.add_row("Total invested",    f"${total_invested:,.2f} USDC")
        summary.add_row("Current value",     f"${total_value:,.2f} USDC")
        summary.add_row("Unrealised P&L",    f"[bold {pnl_color}]{pnl_sign}${total_pnl:,.2f} USDC  ({pnl_sign}{roi:.1f}%)[/bold {pnl_color}]")
        if redeemable_count:
            summary.add_row("Redeemable ✓",  f"[bold green]{redeemable_count} position(s) — claim your winnings![/bold green]")
        console.print(Panel(summary, title="[bold]Portfolio Summary[/bold]", border_style=pnl_color))

    # Recent trades
    if trades:
        trade_table = Table(title="Recent Trades (last 30 days)", box=box.SIMPLE_HEAVY, show_lines=False)
        trade_table.add_column("Date",    style="dim",  width=11)
        trade_table.add_column("Market",  max_width=44)
        trade_table.add_column("Outcome", style="cyan", width=7)
        trade_table.add_column("Side",    width=5)
        trade_table.add_column("Price",   justify="right", width=8)
        trade_table.add_column("USDC",    justify="right", width=10)
        for t in trades[:30]:
            dt       = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m-%d") if t.timestamp else "—"
            side_fmt = "[green]BUY[/green]" if t.side == "BUY" else "[red]SELL[/red]"
            trade_table.add_row(dt, t.title[:44], t.outcome, side_fmt, f"${t.price:.4f}", f"${t.usdc_size:,.2f}")
        console.print(trade_table)


def cmd_pnl(args: argparse.Namespace, client: PolymarketClient, storage: Storage) -> None:
    """Show paper-trading P&L for all dry-run positions."""
    positions = storage.get_paper_positions()

    if not positions:
        console.print(
            "[yellow]No paper positions found.[/yellow]\n"
            "Run [bold]polymarket watch --copy --dry-run[/bold] to start tracking simulated trades."
        )
        return

    condition_ids = list({p["condition_id"] for p in positions if p.get("condition_id")})

    # Collect token_ids for positions whose title is still unresolved
    unresolved_token_ids = list({
        p["token_id"]
        for p in positions
        if p.get("token_id")
        and (not p.get("market_title") or p.get("market_title", "").startswith("(resolving"))
        and not p.get("condition_id")   # condition_id lookup will cover the rest
    })

    with console.status(f"Fetching live prices + titles for {len(condition_ids)} markets…"):
        prices     = client.token_prices(condition_ids)
        title_map  = client.market_questions(
            condition_ids=condition_ids,
            token_ids=unresolved_token_ids or None,
        )

    # Persist resolved titles back to disk so future runs don't need to re-fetch
    fixed = storage.update_paper_titles(title_map)
    if fixed:
        # Reload with the freshly resolved titles
        positions = storage.get_paper_positions()

    # Build rows
    total_invested = 0.0
    total_current  = 0.0
    total_pnl      = 0.0
    wins           = 0
    losses         = 0
    unpriced       = 0
    best_pnl       = float("-inf")
    worst_pnl      = float("inf")
    best_title     = ""
    worst_title    = ""

    table = Table(
        title="Dry-Run Paper P&L",
        box=box.HEAVY_HEAD,
        show_lines=False,
    )
    table.add_column("#",          style="dim",        justify="right", width=3)
    table.add_column("Opened",     style="dim",        width=11)
    table.add_column("Market",     max_width=42)
    table.add_column("Outcome",    style="cyan",       width=7)
    table.add_column("Copied from",style="dim",        width=12)
    table.add_column("Entry $",    justify="right",    width=8)
    table.add_column("Now $",      justify="right",    width=8)
    table.add_column("Shares",     justify="right",    width=8)
    table.add_column("Invested",   justify="right",    width=10)
    table.add_column("Value",      justify="right",    width=10)
    table.add_column("P&L",        justify="right",    width=11)

    for i, pos in enumerate(positions, 1):
        token_id    = pos.get("token_id", "")
        entry_price = float(pos.get("entry_price", 0))
        shares      = float(pos.get("shares", 0))
        invested    = float(pos.get("spend_usdc", 0))
        cur_price   = prices.get(token_id)

        if cur_price is None:
            # Price unavailable — show dashes
            cur_str = "[dim]?[/dim]"
            pnl_str = "[dim]—[/dim]"
            val_str = "[dim]—[/dim]"
            unpriced += 1
        else:
            current_value  = cur_price * shares
            pnl            = current_value - invested
            total_invested += invested
            total_current  += current_value
            total_pnl      += pnl

            if pnl >= 0:
                wins += 1
            else:
                losses += 1

            label = stored_title[:32] if (stored_title := pos.get("market_title", "")) else "?"
            if pnl > best_pnl:
                best_pnl, best_title = pnl, label
            if pnl < worst_pnl:
                worst_pnl, worst_title = pnl, label

            sign  = "+" if pnl >= 0 else ""
            color = "green" if pnl >= 0 else "red"
            cur_str = f"${cur_price:.4f}"
            pnl_str = f"[{color}]{sign}${pnl:,.2f}[/{color}]"
            val_str = f"${current_value:,.2f}"

        opened = pos.get("opened_at", "")[:10]
        trader = f"{pos.get('username', '?')} (#{pos.get('wallet_rank', '?')})"

        # Use stored title; fall back to title_map if still unresolved
        stored_title = pos.get("market_title", "")
        if not stored_title or stored_title.startswith("(resolving"):
            stored_title = (
                title_map.get(pos.get("condition_id", ""))
                or title_map.get(pos.get("token_id", ""))
                or stored_title
                or "Unknown market"
            )

        table.add_row(
            str(i),
            opened,
            stored_title[:42],
            pos.get("outcome", ""),
            trader,
            f"${entry_price:.4f}",
            cur_str,
            f"{shares:.1f}",
            f"${invested:,.2f}",
            val_str,
            pnl_str,
        )

    console.print(table)

    # Summary footer
    priced = wins + losses
    pnl_color  = "green" if total_pnl >= 0 else "red"
    pnl_sign   = "+" if total_pnl >= 0 else ""
    roi        = (total_pnl / total_invested * 100) if total_invested else 0.0
    win_rate   = (wins / priced * 100) if priced else 0.0
    avg_pnl    = (total_pnl / priced) if priced else 0.0
    avg_sign   = "+" if avg_pnl >= 0 else ""
    avg_color  = "green" if avg_pnl >= 0 else "red"

    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column(style="dim", width=24)
    summary.add_column()

    # ── Capital
    summary.add_row("Total positions",   str(len(positions)))
    summary.add_row("Total invested",    f"${total_invested:,.2f} USDC")
    summary.add_row("Current value",     f"${total_current:,.2f} USDC")
    summary.add_row(
        "Total P&L",
        f"[bold {pnl_color}]{pnl_sign}${total_pnl:,.2f}[/bold {pnl_color}]"
        f"  [dim]({pnl_sign}{roi:.1f}% ROI)[/dim]",
    )

    # ── Win/loss breakdown (only if any priced positions)
    if priced:
        summary.add_row("", "")  # spacer
        wl_color = "green" if win_rate >= 50 else "red"
        summary.add_row(
            "Win / Loss",
            f"[green]{wins}W[/green] / [red]{losses}L[/red]"
            f"  [{wl_color}]{win_rate:.0f}% win rate[/{wl_color}]",
        )
        summary.add_row(
            "Avg P&L per position",
            f"[{avg_color}]{avg_sign}${avg_pnl:,.2f}[/{avg_color}]",
        )

    # ── Best / worst trade
    if best_pnl != float("-inf"):
        b_sign = "+" if best_pnl >= 0 else ""
        summary.add_row(
            "Best trade",
            f"[green]{b_sign}${best_pnl:,.2f}[/green]  [dim]{best_title}[/dim]",
        )
    if worst_pnl != float("inf"):
        w_sign = "+" if worst_pnl >= 0 else ""
        w_color = "green" if worst_pnl >= 0 else "red"
        summary.add_row(
            "Worst trade",
            f"[{w_color}]{w_sign}${worst_pnl:,.2f}[/{w_color}]  [dim]{worst_title}[/dim]",
        )

    # ── Unpriced warning
    if unpriced:
        summary.add_row("", "")
        summary.add_row(
            "Unpriced positions",
            f"[dim]{unpriced} (market resolved or token ID missing)[/dim]",
        )

    console.print(Panel(summary, title="[bold]Summary[/bold]", border_style=pnl_color))
    if unpriced:
        console.print("[dim]Prices marked ? = token not found in live market data.[/dim]")


# ---------------------------------------------------------------------------
# DB management commands
# ---------------------------------------------------------------------------

def cmd_db_init() -> None:
    """Create (or verify) the PostgreSQL schema — safe to run repeatedly."""
    with console.status("Applying schema…"):
        db.apply_schema()
    console.print("[bold green]✓[/bold green] Schema applied successfully.")
    console.print(
        "[dim]Tables: wallets, snapshots, alerts, paper_positions, daily_spend[/dim]"
    )


def cmd_db_migrate(data_dir: str) -> None:
    """Import legacy JSON flat files from ``data_dir`` into PostgreSQL.

    Skips records that already exist (idempotent — safe to run more than once).
    """
    import json
    from pathlib import Path

    root = Path(data_dir)
    if not root.exists():
        console.print(f"[yellow]data_dir {root} not found — nothing to migrate.[/yellow]")
        return

    stats: dict[str, int] = {}

    # ── wallets ──────────────────────────────────────────────────────────────
    wallets_path = root / "wallets.json"
    if wallets_path.exists():
        raw: list[dict] = json.loads(wallets_path.read_text())
        wallets = [Wallet(**w) for w in raw]
        storage = Storage()
        storage.save_wallets(wallets)
        stats["wallets"] = len(wallets)
        console.print(f"  wallets       {len(wallets):>6}")

    # ── snapshots ────────────────────────────────────────────────────────────
    snap_path = root / "snapshots.json"
    if snap_path.exists():
        snap_data: dict[str, list[str]] = json.loads(snap_path.read_text())
        _storage = Storage()
        total_hashes = 0
        for address, hashes in snap_data.items():
            _storage.save_snapshot(address, set(hashes))
            total_hashes += len(hashes)
        stats["snapshots"] = total_hashes
        console.print(f"  snapshots     {total_hashes:>6}  ({len(snap_data)} wallets)")

    # ── alerts ───────────────────────────────────────────────────────────────
    alerts_path = root / "alerts.json"
    if alerts_path.exists():
        alerts_raw: list[dict] = json.loads(alerts_path.read_text())
        _storage = Storage()
        imported = 0
        for rec in alerts_raw:
            try:
                sig = Signal(
                    wallet_address   = rec.get("wallet_address", ""),
                    username         = rec.get("username", ""),
                    wallet_rank      = int(rec.get("wallet_rank", 0)),
                    condition_id     = rec.get("condition_id", ""),
                    market_title     = rec.get("market_title", ""),
                    outcome          = rec.get("outcome", ""),
                    side             = rec.get("side", "BUY"),
                    size             = float(rec.get("size", 0)),
                    usdc_size        = float(rec.get("usdc_size", 0)),
                    price            = float(rec.get("price", 0)),
                    detected_at      = rec.get("detected_at", ""),
                    transaction_hash = rec.get("transaction_hash", ""),
                    token_id         = rec.get("token_id", ""),
                )
                _storage.append_alert(sig)
                imported += 1
            except Exception as exc:
                logger.debug("Skipping alert record: %s", exc)
        stats["alerts"] = imported
        console.print(f"  alerts        {imported:>6}  (of {len(alerts_raw)} records)")

    # ── paper_positions ───────────────────────────────────────────────────────
    pp_path = root / "paper_positions.json"
    if pp_path.exists():
        pp_raw: list[dict] = json.loads(pp_path.read_text())
        _storage = Storage()
        imported = 0
        for pos in pp_raw:
            try:
                _storage.append_paper_position({
                    "condition_id":  pos.get("condition_id", ""),
                    "token_id":      pos.get("token_id", ""),
                    "market_title":  pos.get("market_title", ""),
                    "outcome":       pos.get("outcome", ""),
                    "entry_price":   float(pos.get("entry_price", 0)),
                    "shares":        float(pos.get("shares", 0)),
                    "spend_usdc":    float(pos.get("spend_usdc", 0)),
                    "opened_at":     pos.get("opened_at", ""),
                    "wallet_address": pos.get("wallet_address", ""),
                    "username":      pos.get("username", ""),
                    "wallet_rank":   int(pos.get("wallet_rank", 0)),
                })
                imported += 1
            except Exception as exc:
                logger.debug("Skipping paper_position: %s", exc)
        stats["paper_positions"] = imported
        console.print(f"  paper_positions {imported:>4}  (of {len(pp_raw)} records)")

    # ── daily_spend ───────────────────────────────────────────────────────────
    ds_path = root / "daily_spend.json"
    if ds_path.exists():
        ds_data: dict[str, float] = json.loads(ds_path.read_text())
        _storage = Storage()
        for date_iso, amount in ds_data.items():
            _storage.record_daily_spend(date_iso, float(amount))
        stats["daily_spend"] = len(ds_data)
        console.print(f"  daily_spend   {len(ds_data):>6}  day(s)")

    if not stats:
        console.print("[yellow]No JSON data files found in[/yellow] " + str(root))
    else:
        console.print(
            Panel(
                "[bold green]Migration complete.[/bold green]  "
                "All existing JSON data has been imported into PostgreSQL.\n"
                f"[dim]Source: {root}[/dim]",
                border_style="green",
            )
        )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket",
        description="Track profitable Polymarket wallets and copy-trade signals.",
    )
    sub = parser.add_subparsers(dest="command")

    p_top = sub.add_parser("top", help="Show top profitable wallets")
    p_top.add_argument("--limit", type=int, default=20, help="Number of wallets to show")
    p_top.add_argument("--refresh", action="store_true", help="Force refresh leaderboard cache")

    p_wallet = sub.add_parser("wallet", help="Analyze a specific wallet")
    p_wallet.add_argument("address", help="Wallet address (0x…)")

    p_watch = sub.add_parser(
        "watch",
        help="Stream real-time trade signals via WebSocket (default) or polling (--poll)",
    )
    p_watch.add_argument(
        "--poll", action="store_true",
        help="Use polling instead of WebSocket (no polygon_wss key required)",
    )
    p_watch.add_argument(
        "--interval", type=int, default=300,
        help="Poll interval in seconds — only used with --poll (default: 300)",
    )
    p_watch.add_argument(
        "--min-size", type=float, default=50.0, dest="min_size",
        help="Minimum USDC trade size to alert on (default: 50)",
    )
    p_watch.add_argument(
        "--copy", action="store_true",
        help="Enable copy trading (places orders mirroring top wallets)",
    )
    p_watch.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Simulate orders without submitting (overrides config dry_run)",
    )

    sub.add_parser("balance",   help="Show USDC wallet balance and daily spend")
    sub.add_parser("pnl",       help="Show paper P&L for all dry-run simulated positions")

    p_pos = sub.add_parser("positions", help="Show real open positions and P&L for your wallet")
    p_pos.add_argument(
        "--sort", choices=["pnl", "pct", "size"], default="pnl",
        help="Sort by: pnl (worst first, default), pct (worst %% first), size (largest first)",
    )

    sub.add_parser(
        "db-init",
        help="Create the PostgreSQL schema (run once after setting up the database)",
    )
    p_migrate = sub.add_parser(
        "db-migrate",
        help="Import legacy JSON data files from data_dir into PostgreSQL",
    )
    p_migrate.add_argument(
        "--data-dir", default=None, dest="data_dir",
        help="Override data directory (default: value from config.yaml)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cfg = cfg_module.load()
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"], logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    client = PolymarketClient(
        request_delay=cfg["request_delay"],
        max_retries=cfg["max_retries"],
    )
    # Initialise PostgreSQL pool — must happen before Storage() is constructed
    try:
        db.init_pool(cfg["database_url"])
    except Exception as exc:
        console.print(
            f"[red bold]Cannot connect to PostgreSQL:[/red bold] {exc}\n"
            "Set [bold]database_url[/bold] in config.yaml or "
            "[bold]POLYMARKET_DATABASE_URL[/bold] env var.\n"
            "Run [bold]polymarket db-init[/bold] to create the database schema."
        )
        sys.exit(1)

    storage = Storage()
    scanner = LeaderboardScanner(
        client=client,
        storage=storage,
        top_n=cfg["top_n"],
        leaderboard_ttl=cfg["leaderboard_ttl"],
    )
    analyzer = WalletAnalyzer(client=client)

    if args.command == "top":
        cmd_top(args, client, scanner, analyzer)
    elif args.command == "wallet":
        cmd_wallet(args, client, analyzer)
    elif args.command == "watch":
        cmd_watch(args, client, scanner, storage, cfg, analyzer)
    elif args.command == "balance":
        cmd_balance(args, cfg, storage)
    elif args.command == "pnl":
        cmd_pnl(args, client, storage)
    elif args.command == "positions":
        cmd_positions(args, client, analyzer, cfg)
    elif args.command == "db-init":
        cmd_db_init()
    elif args.command == "db-migrate":
        data_dir = getattr(args, "data_dir", None) or cfg["data_dir"]
        cmd_db_migrate(data_dir)
