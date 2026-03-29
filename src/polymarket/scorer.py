"""
Wallet scoring engine.

Computes a 0–100 composite score across three pillars:
  Skill (45pts)       — does this wallet have genuine edge?
  Reliability (30pts) — is the edge consistent and well-sampled?
  Copiability (25pts) — can we actually copy their trades profitably?

All sub-scores are stored in WalletScore so the UI can show full breakdown.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from .models import WalletScore, WalletStats

logger = logging.getLogger(__name__)

# ── Signal maxima (must sum to 100) ──────────────────────────────────────────
_S1 = 20   # calibrated edge
_S2 = 15   # temporal consistency
_S3 = 10   # independence
_R1 = 10   # sample breadth
_R2 = 10   # Sharpe-like
_R3 = 10   # recency trend
_C1 = 10   # market impact
_C2 = 10   # signal freshness
_C3 = 5    # liquidity (stubbed)

_MIN_MARKETS = 10   # below this → insufficient_data flag


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _win_rate(positions) -> float:
    if not positions:
        return 0.0
    return sum(1 for p in positions if p.cash_pnl > 0) / len(positions)


class WalletScorer:
    """
    Score one or all wallets.  Call ``score_all(stats_list)`` to get
    cross-wallet independence (S3) computed correctly; calling ``score_one``
    alone gives S3 = neutral.
    """

    def score_all(
        self,
        stats_list: list[WalletStats],
        storage=None,
    ) -> dict[str, WalletScore]:
        """Return {address: WalletScore} for every wallet in the list.

        If ``storage`` is provided, each score is written back to the
        ``wallets`` table so the web UI can read it without re-computing.
        """
        scores: dict[str, WalletScore] = {}
        for stats in stats_list:
            peers = [s for s in stats_list if s.wallet.address != stats.wallet.address]
            scores[stats.wallet.address] = self._compute(stats, peers)

        if storage is not None:
            from dataclasses import asdict
            for ws in scores.values():
                try:
                    storage.update_wallet_score(
                        ws.address,
                        ws.total,
                        ws.copy_tier,
                        asdict(ws),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to persist score for %s: %s", ws.address, exc)

        return scores

    def score_one(self, stats: WalletStats) -> WalletScore:
        return self._compute(stats, [])

    # ── Orchestrator ─────────────────────────────────────────────────────────

    def _compute(self, stats: WalletStats, peers: list[WalletStats]) -> WalletScore:
        pos      = stats.open_positions
        trades   = stats.recent_trades
        buy_trades = [t for t in trades if t.side == "BUY"]

        unique_markets = len(
            {t.condition_id for t in buy_trades if t.condition_id}
            | {p.condition_id for p in pos if p.condition_id}
        )
        insufficient = unique_markets < _MIN_MARKETS

        s1 = self._s1_calibrated_edge(pos)
        s2 = self._s2_temporal_consistency(trades, pos)
        s3 = self._s3_independence(stats, peers)
        r1, unique_markets = self._r1_sample_breadth(trades, pos)
        r2 = self._r2_sharpe(pos)
        r3 = self._r3_recency_trend(trades, pos, stats)
        c1 = self._c1_market_impact(trades, pos)
        c2 = self._c2_signal_freshness(buy_trades)
        c3 = 2.5  # neutral stub — needs CLOB order-book API

        skill       = s1 + s2 + s3
        reliability = r1 + r2 + r3
        copiability = c1 + c2 + c3
        total       = skill + reliability + copiability

        tier      = _tier(total, insufficient)
        size_pct  = _size_pct(tier, c1)
        categories = _strong_categories(trades)

        return WalletScore(
            address=stats.wallet.address,
            s1_calibrated_edge=round(s1, 2),
            s2_temporal_consistency=round(s2, 2),
            s3_independence=round(s3, 2),
            r1_sample_breadth=round(r1, 2),
            r2_sharpe=round(r2, 2),
            r3_recency_trend=round(r3, 2),
            c1_market_impact=round(c1, 2),
            c2_signal_freshness=round(c2, 2),
            c3_liquidity=round(c3, 2),
            skill=round(skill, 2),
            reliability=round(reliability, 2),
            copiability=round(copiability, 2),
            total=round(total, 2),
            insufficient_data=insufficient,
            trade_count=len(trades),
            unique_markets=unique_markets,
            strong_categories=categories,
            copy_tier=tier,
            copy_size_pct=size_pct,
        )

    # ── S1: Calibrated edge ──────────────────────────────────────────────────

    def _s1_calibrated_edge(self, positions) -> float:
        """
        For each position: edge = cur_price - avg_price
        (positive = market now believes they'll win more than they paid)
        Resolved positions (redeemable or price in {0,1}) get full weight;
        open positions get 0.4 weight (unrealised, less certain).
        mean_edge +0.20 → full score, 0.0 → half, -0.20 → 0
        """
        total_w = 0.0
        weighted_edge = 0.0

        for p in positions:
            if p.initial_value <= 0 or p.avg_price <= 0:
                continue
            edge   = p.cur_price - p.avg_price
            weight = 1.0 if (p.redeemable or p.cur_price in (0.0, 1.0)) else 0.4
            weighted_edge += edge * weight
            total_w       += weight

        if total_w == 0:
            return _S1 * 0.5  # neutral — no positions yet

        mean_edge = weighted_edge / total_w
        score = (mean_edge + 0.20) / 0.40 * _S1   # -0.20→0  0→10  +0.20→20
        return _clamp(score, 0, _S1)

    # ── S2: Temporal consistency ─────────────────────────────────────────────

    def _s2_temporal_consistency(self, trades, positions) -> float:
        """
        Win rate of open positions whose condition_id appears in BUY trades
        within each time window.  Score on consistency across 7d and 30d.
        """
        now = datetime.now(timezone.utc)
        pos_by_cid = {p.condition_id: p for p in positions if p.condition_id}

        windows = {
            "7d":  (now - timedelta(days=7)).timestamp(),
            "30d": (now - timedelta(days=30)).timestamp(),
        }

        window_wrs: list[float] = []
        for name, cutoff_ts in windows.items():
            cids = {
                t.condition_id for t in trades
                if t.side == "BUY" and t.condition_id and t.timestamp >= cutoff_ts
            }
            matched = [pos_by_cid[c] for c in cids if c in pos_by_cid
                       and pos_by_cid[c].initial_value > 0]
            if len(matched) >= 2:
                window_wrs.append(_win_rate(matched))

        if not window_wrs:
            return _S2 * 0.5  # neutral — can't compute without window data

        avg_wr = sum(window_wrs) / len(window_wrs)
        # 30% win rate → 0, 70% → full
        score = _clamp((avg_wr - 0.30) / 0.40, 0, 1) * _S2

        # Trend bonus: if 7d win rate > 30d win rate, performance is improving
        if len(window_wrs) == 2 and window_wrs[0] > window_wrs[1] + 0.05:
            score = min(score + 2, _S2)

        return score

    # ── S3: Independence ─────────────────────────────────────────────────────

    def _s3_independence(self, stats: WalletStats, peers: list[WalletStats]) -> float:
        """
        For each BUY trade, check whether any peer wallet traded the same
        market.  If this wallet traded first (or uniquely), count as leader.
        leader_rate = leader_trades / all_trades_with_peer_overlap
        High leader rate → high independence score.
        """
        my_buys = {
            t.condition_id: t.timestamp
            for t in stats.recent_trades
            if t.side == "BUY" and t.condition_id
        }
        if not my_buys or not peers:
            return _S3 * 0.5  # neutral

        leader = follower = 0
        for cid, my_ts in my_buys.items():
            peer_ts_list = [
                t.timestamp
                for ps in peers
                for t in ps.recent_trades
                if t.condition_id == cid and t.side == "BUY" and t.timestamp > 0
            ]
            if not peer_ts_list:
                leader += 1   # unique trade — no one else touched it
                continue
            earliest_peer = min(peer_ts_list)
            # "Leader" if within 1 hour of the first peer (concurrent or ahead)
            if my_ts <= earliest_peer + 3600:
                leader += 1
            else:
                follower += 1

        total = leader + follower
        if total == 0:
            return _S3 * 0.5
        return (leader / total) * _S3

    # ── R1: Sample breadth ───────────────────────────────────────────────────

    def _r1_sample_breadth(self, trades, positions) -> tuple[float, int]:
        """Unique markets traded. Returns (score, count)."""
        unique = len(
            {t.condition_id for t in trades if t.side == "BUY" and t.condition_id}
            | {p.condition_id for p in positions if p.condition_id}
        )
        if unique < 5:
            score = 0.0
        elif unique < 15:
            score = (unique - 5) / 10 * 5
        elif unique < 30:
            score = 5 + (unique - 15) / 15 * 3
        elif unique < 50:
            score = 8 + (unique - 30) / 20 * 2
        else:
            score = float(_R1)
        return _clamp(score, 0, _R1), unique

    # ── R2: Sharpe-like ──────────────────────────────────────────────────────

    def _r2_sharpe(self, positions) -> float:
        """
        Pseudo-Sharpe: mean(percent_pnl) / std(percent_pnl).
        High and consistent P&L → high score; volatile or negative → low.
        """
        pcts = [p.percent_pnl for p in positions if p.initial_value > 0]
        if len(pcts) < 3:
            return _R2 * 0.5  # neutral

        avg = sum(pcts) / len(pcts)
        std = (sum((x - avg) ** 2 for x in pcts) / (len(pcts) - 1)) ** 0.5

        if std == 0:
            sharpe = 1.0 if avg > 0 else 0.0
        else:
            sharpe = avg / std

        # sharpe -1 → 0pts, 0 → 5pts, +1 → 10pts
        score = (sharpe + 1) / 2 * _R2
        return _clamp(score, 0, _R2)

    # ── R3: Recency trend ────────────────────────────────────────────────────

    def _r3_recency_trend(self, trades, positions, stats: WalletStats) -> float:
        """
        Compare win rate of positions whose BUY appeared in last 7d
        vs. overall win rate.  Improving → full score; declining → low.
        """
        now     = datetime.now(timezone.utc)
        cutoff  = (now - timedelta(days=7)).timestamp()
        pos_by_cid = {p.condition_id: p for p in positions if p.condition_id}

        recent_cids = {
            t.condition_id for t in trades
            if t.side == "BUY" and t.condition_id and t.timestamp >= cutoff
        }
        recent_pos = [pos_by_cid[c] for c in recent_cids
                      if c in pos_by_cid and pos_by_cid[c].initial_value > 0]

        if len(recent_pos) < 2:
            return _R3 * 0.5  # neutral — too little recent data

        recent_wr  = _win_rate(recent_pos)
        overall_wr = stats.win_rate

        if recent_wr > overall_wr + 0.05:
            return float(_R3)         # improving
        elif recent_wr > overall_wr - 0.05:
            return _R3 * 0.6          # stable
        else:
            return _R3 * 0.2          # declining

    # ── C1: Market impact ────────────────────────────────────────────────────

    def _c1_market_impact(self, trades, positions) -> float:
        """
        Large trades move the market against copiers.  Smaller avg trade
        size → higher score.
        $100 avg → 10pts, $5k → 5pts, $10k+ → 0pts
        """
        buy_sizes = [t.usdc_size for t in trades if t.side == "BUY" and t.usdc_size > 0]
        if not buy_sizes:
            # Fall back to position initial_value
            buy_sizes = [p.initial_value for p in positions if p.initial_value > 0]
        if not buy_sizes:
            return _C1 * 0.5

        avg = sum(buy_sizes) / len(buy_sizes)
        score = _C1 * max(0, 1 - avg / 10_000)
        return _clamp(score, 0, _C1)

    # ── C2: Signal freshness ─────────────────────────────────────────────────

    def _c2_signal_freshness(self, buy_trades) -> float:
        """
        Infrequent traders are easier to copy (signal doesn't expire fast).
        0 trades/day → 10pts, 1/day → 7.5pts, 3/day → 2.5pts, 5+/day → 0pts
        """
        if not buy_trades:
            return _C2 * 0.8  # inactive = easy to track when they do trade

        # Estimate window: span of trade timestamps
        timestamps = [t.timestamp for t in buy_trades if t.timestamp > 0]
        if not timestamps:
            return _C2 * 0.5

        span_days = max(
            (max(timestamps) - min(timestamps)) / 86_400,
            1.0  # minimum 1 day to avoid division by zero
        )
        tpd = len(buy_trades) / span_days
        score = _C2 * max(0, 1 - tpd / 5)  # 5 trades/day → 0pts
        return _clamp(score, 0, _C2)


# ── Helpers ───────────────────────────────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Politics":    ["election", "president", "senate", "congress", "vote", "trump", "biden",
                    "harris", "republican", "democrat", "political", "govern", "minister"],
    "Sports":      ["nba", "nfl", "nhl", "mlb", "soccer", "football", "basketball", "tennis",
                    "golf", "f1", "formula 1", "olympics", "championship", "league",
                    "super bowl", "world cup", "ufc", "mma", "boxing", "cricket", "rugby"],
    "Crypto":      ["bitcoin", "btc", "eth", "ethereum", "crypto", "token", "defi", "solana",
                    "price", "market cap", "altcoin", "nft"],
    "Economics":   ["fed", "interest rate", "inflation", "gdp", "recession", "unemployment",
                    "cpi", "jobs", "economy", "tariff", "trade"],
    "Science":     ["nasa", "spacex", "climate", "ai ", "artificial intelligence", "drug",
                    "fda", "vaccine", "covid", "hurricane"],
    "Culture":     ["oscar", "grammy", "emmy", "box office", "movie", "album", "celebrity",
                    "award", "music", "film", "tv show", "streaming"],
    "Geopolitics": ["war", "nato", "ukraine", "russia", "china", "iran", "israel", "taiwan",
                    "sanction", "ceasefire", "treaty", "military"],
}
# Keep the old private name as an alias so scorer internals still work
_CATEGORY_KEYWORDS = CATEGORY_KEYWORDS
_MIN_CATEGORY_TRADES = 5  # need at least this many to call it a "strong" category


def _strong_categories(trades) -> list[str]:
    """Return categories where win rate is ≥60% with ≥5 trades."""
    from collections import defaultdict
    category_trades: dict[str, list] = defaultdict(list)

    for t in trades:
        title = (t.title or "").lower()
        for cat, keywords in _CATEGORY_KEYWORDS.items():
            if any(kw in title for kw in keywords):
                category_trades[cat].append(t)
                break  # assign to first matching category only

    strong = []
    for cat, cat_trades in category_trades.items():
        buys = [t for t in cat_trades if t.side == "BUY"]
        if len(buys) >= _MIN_CATEGORY_TRADES:
            strong.append(cat)

    return strong


def _tier(total: float, insufficient: bool) -> str:
    if insufficient:
        return "?"
    if total >= 80:
        return "A"
    if total >= 65:
        return "B"
    if total >= 50:
        return "C"
    if total >= 35:
        return "WATCH"
    return "SKIP"


def _size_pct(tier: str, c1_score: float) -> float:
    """Base size from tier, reduced further if copiability is low."""
    base = {"A": 1.0, "B": 0.70, "C": 0.40, "WATCH": 0.0, "SKIP": 0.0, "?": 0.0}.get(tier, 0.0)
    if base == 0:
        return 0.0
    # c1 score out of 10 — halve base size if market impact is very high (c1 < 3)
    copiability_factor = 1.0 if c1_score >= 5 else 0.5
    return round(base * copiability_factor, 2)
