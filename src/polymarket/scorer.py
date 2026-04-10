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

_MIN_MARKETS = 15   # below this → insufficient_data flag


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _is_resolved(p) -> bool:
    """Position has a definitive outcome (redeemable or priced at 0 or 1)."""
    return p.redeemable or p.cur_price in (0.0, 1.0)


def _pos_won(p) -> bool:
    """True if the position ended in profit. Uses resolved signal when available."""
    if p.redeemable or p.cur_price == 1.0:
        return True
    if p.cur_price == 0.0:
        return False
    return p.cash_pnl > 0


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
        c3 = self._c3_liquidity(pos)

        skill       = s1 + s2 + s3
        reliability = r1 + r2 + r3
        copiability = c1 + c2 + c3
        total       = skill + reliability + copiability

        tier      = _tier(total, insufficient, unique_markets)
        size_pct  = _size_pct(tier, c1)
        categories = _strong_categories(trades, pos)

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
        Consistency of outcomes across 7d and 30d windows.

        For each window, BUY trades are matched to positions by condition_id.
        Resolved positions (definitive outcome) get 70% weight; open positions
        (unrealized mark) get 30% weight.  Pure open-only windows are penalised
        to 70% of face value to reflect weaker signal quality.
        """
        now = datetime.now(timezone.utc)
        pos_by_cid = {p.condition_id: p for p in positions if p.condition_id}

        windows = {
            "7d":  (now - timedelta(days=7)).timestamp(),
            "30d": (now - timedelta(days=30)).timestamp(),
        }

        window_wrs: list[float] = []
        for cutoff_ts in windows.values():
            window_cids = {
                t.condition_id for t in trades
                if t.side == "BUY" and t.condition_id and t.timestamp >= cutoff_ts
            }
            matched = [pos_by_cid[c] for c in window_cids
                       if c in pos_by_cid and pos_by_cid[c].initial_value > 0]
            if not matched:
                continue

            res = [p for p in matched if _is_resolved(p)]
            opn = [p for p in matched if not _is_resolved(p)]

            r_wr = (sum(1 for p in res if _pos_won(p)) / len(res)) if len(res) >= 2 else None
            o_wr = (sum(1 for p in opn if p.percent_pnl > 0) / len(opn)) if len(opn) >= 2 else None

            if r_wr is not None and o_wr is not None:
                window_wrs.append(0.70 * r_wr + 0.30 * o_wr)
            elif r_wr is not None:
                window_wrs.append(r_wr)
            elif o_wr is not None:
                window_wrs.append(o_wr * 0.70)  # penalise pure unrealized

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
        leader_rate = weighted_leader / total_weight
        High leader rate → high independence score.

        Unique trades (no peer overlap) count as 0.6 leader weight — uniqueness
        is not the same as directional independence.  Tightened to 15-minute
        window (was 1 hour) to reduce false-positive leaders in fast markets.
        """
        my_buys = {
            t.condition_id: t.timestamp
            for t in stats.recent_trades
            if t.side == "BUY" and t.condition_id
        }
        if not my_buys or not peers:
            return _S3 * 0.5  # neutral

        _LEADER_WINDOW = 900  # 15 minutes

        leader_w = 0.0
        total_w  = 0.0
        for cid, my_ts in my_buys.items():
            peer_ts_list = [
                t.timestamp
                for ps in peers
                for t in ps.recent_trades
                if t.condition_id == cid and t.side == "BUY" and t.timestamp > 0
            ]
            if not peer_ts_list:
                leader_w += 0.6  # unique trade — partial credit (unpopular ≠ independent)
                total_w  += 1.0
                continue
            earliest_peer = min(peer_ts_list)
            if my_ts <= earliest_peer + _LEADER_WINDOW:
                leader_w += 1.0
            total_w += 1.0

        if total_w == 0:
            return _S3 * 0.5
        return (leader_w / total_w) * _S3

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
        Realized-first Sharpe: uses cash_pnl of resolved positions when
        available (≥3 resolved).  Falls back to percent_pnl with a 30%
        penalty when only unrealized data exists.
        Resolved positions get ~70-100% weight; unrealized at most 30%.
        """
        def _sharpe_ratio(vals):
            avg = sum(vals) / len(vals)
            std = (sum((x - avg) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5
            if std == 0:
                return 1.0 if avg > 0 else 0.0
            return avg / std

        resolved = [p for p in positions if _is_resolved(p) and p.initial_value > 0]
        open_pos  = [p for p in positions if not _is_resolved(p) and p.initial_value > 0]

        if len(resolved) >= 3:
            rs = _sharpe_ratio([p.cash_pnl for p in resolved])
            realized_score = _clamp((rs + 1) / 2 * _R2, 0, _R2)

            # Open positions contribute at most 30%, scaled by their count
            open_weight = min(0.3, len(open_pos) / max(len(resolved), 1) * 0.3)
            if len(open_pos) >= 3:
                os_ = _sharpe_ratio([p.percent_pnl for p in open_pos])
                open_score = _clamp((os_ + 1) / 2 * _R2, 0, _R2)
            else:
                open_score = _R2 * 0.5

            return _clamp((1 - open_weight) * realized_score + open_weight * open_score, 0, _R2)

        # Insufficient resolved data: use percent_pnl with 30% penalty
        pcts = [p.percent_pnl for p in positions if p.initial_value > 0]
        if len(pcts) < 3:
            return _R2 * 0.5
        sharpe = _sharpe_ratio(pcts)
        return _clamp((sharpe + 1) / 2 * _R2 * 0.70, 0, _R2)

    # ── R3: Recency trend ────────────────────────────────────────────────────

    def _r3_recency_trend(self, trades, positions, stats: WalletStats) -> float:
        """
        Compare win rate of positions whose BUY appeared in last 7d
        vs. overall win rate.  Improving → full score; declining → low.

        Both win rates use resolved-aware logic: resolved positions use
        _pos_won (definitive outcome); open positions fall back to percent_pnl.
        This avoids contamination from stats.win_rate which is computed from
        percent_pnl alone.
        """
        def _resolved_aware_wr(pos_list) -> float:
            if not pos_list:
                return 0.0
            wins = sum(
                1 for p in pos_list
                if (_pos_won(p) if _is_resolved(p) else p.percent_pnl > 0)
            )
            return wins / len(pos_list)

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

        recent_wr = _resolved_aware_wr(recent_pos)

        all_valid = [p for p in positions if p.initial_value > 0]
        overall_wr = _resolved_aware_wr(all_valid) if all_valid else stats.win_rate

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
        if max(buy_sizes) >= 5_000:
            score *= 0.80  # single high-impact trade reduces copyability
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

    # ── C3: Liquidity proxy ──────────────────────────────────────────────────

    def _c3_liquidity(self, positions) -> float:
        """
        Proxy liquidity via market end_date on open positions.
        Markets resolving >14d from now offer a viable copy window → full score.
        Markets resolving in 7–14d → half score.
        Markets resolving in <7d → no score (too close to copy profitably).
        Unknown end_date → neutral (0.5 contribution).
        """
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()
        horizon_good = today + timedelta(days=14)
        horizon_poor = today + timedelta(days=7)

        open_pos = [p for p in positions if not _is_resolved(p)]
        if not open_pos:
            return _C3 * 0.5

        scored = []
        for p in open_pos:
            if not p.end_date:
                scored.append(0.5)
                continue
            try:
                ed = _date.fromisoformat(str(p.end_date)[:10])
            except (ValueError, TypeError):
                scored.append(0.5)
                continue
            if ed > horizon_good:
                scored.append(1.0)
            elif ed > horizon_poor:
                scored.append(0.5)
            else:
                scored.append(0.0)

        return _clamp(sum(scored) / len(scored) * _C3, 0, _C3)


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


def _strong_categories(trades, positions) -> list[str]:
    """
    Return categories where resolved win rate is ≥60% with ≥5 resolved BUY trades.
    Falls back to volume-only (≥5 BUY trades, suffix "~") when insufficient
    resolved data exists to compute a real win rate.
    """
    from collections import defaultdict
    pos_by_cid = {p.condition_id: p for p in positions if p.condition_id}
    category_buys: dict[str, list] = defaultdict(list)

    for t in trades:
        if t.side != "BUY":
            continue
        title = (t.title or "").lower()
        for cat, keywords in _CATEGORY_KEYWORDS.items():
            if any(kw in title for kw in keywords):
                category_buys[cat].append(t)
                break  # assign to first matching category only

    strong = []
    for cat, buys in category_buys.items():
        if len(buys) < _MIN_CATEGORY_TRADES:
            continue

        resolved_wins  = 0
        resolved_total = 0
        for t in buys:
            p = pos_by_cid.get(t.condition_id)
            if p and _is_resolved(p):
                resolved_total += 1
                if _pos_won(p):
                    resolved_wins += 1

        if resolved_total >= _MIN_CATEGORY_TRADES:
            if resolved_wins / resolved_total >= 0.60:
                strong.append(cat)
        else:
            # Not enough resolved outcomes — flag as volume-only signal
            strong.append(f"{cat}~")

    return strong


def _tier(total: float, insufficient: bool, unique_markets: int = 0) -> str:
    if insufficient:
        return "?"
    # Thin-sample cap: wallets with <25 unique markets cannot reach tier A
    if unique_markets < 25 and total >= 80:
        return "B"
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
