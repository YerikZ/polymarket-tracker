"""
PostgreSQL-backed storage layer.

The public interface is identical to the previous JSON-file implementation,
so no callers (scanner, monitor, stream, copier, main) need to change.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import psycopg2.extras

from .models import Signal, Wallet
from . import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_str(val: Any) -> str:
    """Convert datetime objects (from TIMESTAMPTZ columns) to ISO strings."""
    if isinstance(val, datetime):
        return val.isoformat()
    return val or ""


def _row_to_dict(row: dict) -> dict:
    """Normalize a psycopg2 RealDictRow: convert datetimes to ISO strings."""
    return {k: (_to_str(v) if isinstance(v, datetime) else v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Storage:
    """All state lives in PostgreSQL.

    ``data_dir`` is accepted for backward-compatibility but ignored.
    The DB connection pool must be initialised via ``db.init_pool(dsn)``
    before constructing a Storage instance.
    """

    def __init__(self, data_dir=None) -> None:  # noqa: ARG002
        db.apply_schema()

    # ── Wallets ──────────────────────────────────────────────────────────────

    def get_wallets(self) -> list[dict]:
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM wallets ORDER BY rank")
                return [_row_to_dict(r) for r in cur.fetchall()]

    def save_wallets(self, wallets: list[Wallet]) -> None:
        """Replace the full wallet list atomically.

        Rows not present in ``wallets`` are deleted so stale entries never
        accumulate — identical semantics to the previous JSON overwrite.
        """
        rows = [asdict(w) for w in wallets]
        new_addresses = [r["address"] for r in rows]

        with db.get_conn() as conn:
            with conn.cursor() as cur:
                # Remove wallets that dropped off the leaderboard
                cur.execute(
                    "DELETE FROM wallets WHERE address <> ALL(%s)",
                    (new_addresses,),
                )
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO wallets
                        (address, username, rank, pnl, trading_volume, fetched_at)
                    VALUES %s
                    ON CONFLICT (address) DO UPDATE SET
                        username       = EXCLUDED.username,
                        rank           = EXCLUDED.rank,
                        pnl            = EXCLUDED.pnl,
                        trading_volume = EXCLUDED.trading_volume,
                        fetched_at     = EXCLUDED.fetched_at
                    """,
                    [
                        (r["address"], r["username"], r["rank"],
                         r["pnl"], r["trading_volume"], r["fetched_at"])
                        for r in rows
                    ],
                )

    # ── TX snapshots (deduplication) ─────────────────────────────────────────

    def get_snapshot(self, address: str) -> set[str]:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tx_hash FROM snapshots WHERE address = %s",
                    (address,),
                )
                return {row[0] for row in cur.fetchall()}

    def save_snapshot(self, address: str, tx_hashes: set[str]) -> None:
        """Insert new hashes; existing ones are silently skipped (ON CONFLICT DO NOTHING).

        Never deletes rows — the caller passes the full union (old ∪ new),
        but the DB handles deduplication safely without a race window.
        """
        rows = [(address, h) for h in tx_hashes if h]
        if not rows:
            return
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO snapshots (address, tx_hash) VALUES %s "
                    "ON CONFLICT DO NOTHING",
                    rows,
                )

    # ── Alerts (signals) ─────────────────────────────────────────────────────

    def append_alert(self, signal: Signal) -> int:
        """Insert a signal into the alerts table and return the new row id.

        Returns 0 if the row was skipped due to a duplicate transaction_hash.
        """
        d = asdict(signal)
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alerts
                        (wallet_address, username, wallet_rank, condition_id,
                         market_title, outcome, side, size, usdc_size, price,
                         detected_at, transaction_hash, token_id)
                    VALUES
                        (%(wallet_address)s, %(username)s, %(wallet_rank)s,
                         %(condition_id)s, %(market_title)s, %(outcome)s,
                         %(side)s, %(size)s, %(usdc_size)s, %(price)s,
                         %(detected_at)s, %(transaction_hash)s, %(token_id)s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    d,
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])
                # ON CONFLICT DO NOTHING — row already existed; return its id
                cur.execute(
                    "SELECT id FROM alerts WHERE transaction_hash = %s",
                    (d["transaction_hash"],),
                )
                existing = cur.fetchone()
                return int(existing[0]) if existing else 0

    def update_alert_copier_result(
        self,
        alert_id: int,
        status: str,
        reason: str,
        spend_usdc: float,
    ) -> None:
        """Write the copier decision back to the alerts row."""
        if not alert_id:
            return
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE alerts
                       SET copier_status = %s,
                           copier_reason = %s,
                           copier_spend  = %s
                     WHERE id = %s
                    """,
                    (status, reason, spend_usdc, alert_id),
                )

    def get_alerts(self, limit: int = 50) -> list[dict]:
        """Return the most recent ``limit`` alerts in chronological order
        (oldest-first within the window — same semantics as the former
        ``alerts[-limit:]`` JSON slice).
        """
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM alerts ORDER BY id DESC LIMIT %s
                    ) sub ORDER BY id ASC
                    """,
                    (limit,),
                )
                return [_row_to_dict(r) for r in cur.fetchall()]

    # ── Paper positions (dry-run) ─────────────────────────────────────────────

    def has_paper_position(self, condition_id: str, token_id: str) -> bool:
        """Return True if an open paper position already exists for this market."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM paper_positions
                    WHERE position_status = 'open'
                      AND ((condition_id <> '' AND condition_id = %s)
                        OR (token_id     <> '' AND token_id     = %s))
                    LIMIT 1
                    """,
                    (condition_id, token_id),
                )
                return cur.fetchone() is not None

    def get_open_position(self, condition_id: str, token_id: str) -> dict | None:
        """Return the open paper position for this market, or None if not found."""
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM paper_positions
                    WHERE position_status = 'open'
                      AND ((condition_id <> '' AND condition_id = %s)
                        OR (token_id     <> '' AND token_id     = %s))
                    LIMIT 1
                    """,
                    (condition_id, token_id),
                )
                row = cur.fetchone()
                return _row_to_dict(row) if row else None

    def add_to_position(self, position_id: int, additional_shares: float, additional_spend: float) -> None:
        """Atomically increment an existing open position's shares and cost basis."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_positions
                       SET shares      = shares + %s,
                           spend_usdc  = spend_usdc + %s,
                           topup_count = topup_count + 1
                     WHERE id = %s
                    """,
                    (additional_shares, additional_spend, position_id),
                )

    def close_paper_position(self, position_id: int, exit_price: float, exit_usdc: float) -> None:
        """Mark a position as manually closed (sell signal), recording exit details."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_positions
                       SET position_status     = 'closed',
                           resolution_outcome  = 'sold',
                           current_price       = %s,
                           current_value_usdc  = %s,
                           closed_at           = NOW()
                     WHERE id = %s
                    """,
                    (exit_price, exit_usdc, position_id),
                )

    def cancel_paper_position(self, position_id: int) -> None:
        """Mark a position as cancelled — buy order was submitted but never filled."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE paper_positions
                       SET position_status    = 'cancelled',
                           resolution_outcome = 'unfilled',
                           closed_at          = NOW()
                     WHERE id = %s
                    """,
                    (position_id,),
                )

    def append_paper_position(self, pos: dict) -> None:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_positions
                        (condition_id, token_id, market_title, outcome,
                         entry_price, shares, spend_usdc, opened_at,
                         wallet_address, username, wallet_rank, is_dry_run)
                    VALUES
                        (%(condition_id)s, %(token_id)s, %(market_title)s, %(outcome)s,
                         %(entry_price)s, %(shares)s, %(spend_usdc)s, %(opened_at)s,
                         %(wallet_address)s, %(username)s, %(wallet_rank)s, %(is_dry_run)s)
                    """,
                    pos,
                )

    def get_paper_positions(self) -> list[dict]:
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM paper_positions ORDER BY id")
                return [_row_to_dict(r) for r in cur.fetchall()]

    def update_position_prices(self, updates: list[dict]) -> None:
        """Persist live prices for a batch of positions.

        Each item must have:
            id, current_price, current_value_usdc,
            position_status, resolution_outcome, market_closed
        """
        if not updates:
            return
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    UPDATE paper_positions SET
                        current_price       = data.current_price,
                        current_value_usdc  = data.current_value_usdc,
                        position_status     = data.position_status,
                        resolution_outcome  = data.resolution_outcome,
                        market_closed       = data.market_closed
                    FROM (VALUES %s) AS data(
                        id, current_price, current_value_usdc,
                        position_status, resolution_outcome, market_closed
                    )
                    WHERE paper_positions.id = data.id::bigint
                    """,
                    [
                        (
                            u["id"],
                            u["current_price"],
                            u["current_value_usdc"],
                            u["position_status"],
                            u["resolution_outcome"],
                            u["market_closed"],
                        )
                        for u in updates
                    ],
                    template="(%s, %s::numeric, %s::numeric, %s, %s, %s::boolean)",
                )

    def update_position_statuses(self, updates: list[dict]) -> int:
        """Persist resolution status for a list of positions.

        Each item in ``updates`` must have:
            id, position_status, resolution_outcome, market_closed
        Returns the count of rows updated.
        """
        if not updates:
            return 0
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    UPDATE paper_positions SET
                        position_status    = data.status,
                        resolution_outcome = data.resolution_outcome,
                        market_closed      = data.market_closed
                    FROM (VALUES %s) AS data(id, status, resolution_outcome, market_closed)
                    WHERE paper_positions.id = data.id::bigint
                    """,
                    [
                        (u["id"], u["position_status"],
                         u["resolution_outcome"], u["market_closed"])
                        for u in updates
                    ],
                    template="(%s, %s, %s, %s::boolean)",
                )
        return len(updates)

    def update_paper_titles(self, title_map: dict[str, str]) -> int:
        """Overwrite unresolved market_titles using title_map (keyed by condition_id or token_id).

        Returns the number of rows updated.
        """
        if not title_map:
            return 0

        # Fetch all rows with unresolved titles
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, condition_id, token_id FROM paper_positions "
                    "WHERE market_title = '' OR market_title LIKE '(resolving%%'"
                )
                rows = cur.fetchall()

            updated = 0
            with conn.cursor() as cur:
                for row in rows:
                    resolved = (
                        title_map.get(row["condition_id"] or "")
                        or title_map.get(row["token_id"] or "")
                    )
                    if resolved:
                        cur.execute(
                            "UPDATE paper_positions SET market_title = %s WHERE id = %s",
                            (resolved, row["id"]),
                        )
                        updated += 1

        return updated

    # ── Daily spend ───────────────────────────────────────────────────────────

    def get_daily_spend(self, date_iso: str) -> float:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT amount FROM daily_spend WHERE date_iso = %s",
                    (date_iso,),
                )
                row = cur.fetchone()
                return float(row[0]) if row else 0.0

    def record_daily_spend(self, date_iso: str, amount: float) -> None:
        """Atomically increment the daily spend counter (no read-modify-write race)."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO daily_spend (date_iso, amount) VALUES (%s, %s)
                    ON CONFLICT (date_iso) DO UPDATE
                        SET amount = daily_spend.amount + EXCLUDED.amount
                    """,
                    (date_iso, amount),
                )

    # ── Settings (web UI config store) ───────────────────────────────────────

    def get_settings(self) -> dict:
        """Return the stored config dict. Empty dict if never seeded."""
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT config FROM settings WHERE id = 1")
                row = cur.fetchone()
                return dict(row["config"]) if row else {}

    def put_settings(self, updates: dict) -> dict:
        """Deep-merge ``updates`` into the stored config and return the full result.

        Uses PostgreSQL ``||`` operator for top-level merge; nested dicts
        (e.g. copy_trading) are replaced wholesale if provided.
        """
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                import json
                cur.execute(
                    """
                    INSERT INTO settings (id, config, updated_at)
                        VALUES (1, %s::jsonb, now())
                    ON CONFLICT (id) DO UPDATE
                        SET config     = settings.config || EXCLUDED.config,
                            updated_at = now()
                    RETURNING config
                    """,
                    (json.dumps(updates),),
                )
                row = cur.fetchone()
                return dict(row["config"])

    # ── Wallet scores ─────────────────────────────────────────────────────────

    def update_wallet_score(
        self,
        address: str,
        score: float,
        tier: str,
        detail: dict,
    ) -> None:
        """Persist wallet score columns after a score_all() run."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                import json
                cur.execute(
                    """
                    UPDATE wallets
                       SET score        = %s,
                           tier         = %s,
                           score_detail = %s::jsonb
                     WHERE address = %s
                    """,
                    (score, tier, json.dumps(detail), address),
                )

    # ── Wallet trade history ──────────────────────────────────────────────────

    @staticmethod
    def _normalise_ts(val) -> datetime:
        """Convert API timestamp (int seconds or ms, or datetime) to UTC datetime."""
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        ts = int(val or 0)
        if ts > 1_000_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def upsert_wallet_trades(self, address: str, trades: list[dict], username: str = "") -> int:
        """Insert new trades for a wallet; skip duplicates by (address, tx_hash).
        Returns number of rows actually inserted."""
        if not trades:
            return 0
        rows = []
        for t in trades:
            tx = t.get("transactionHash") or t.get("transaction_hash") or ""
            try:
                traded_at = self._normalise_ts(t.get("timestamp"))
            except Exception:
                continue
            rows.append((
                address,
                username,
                t.get("conditionId") or t.get("condition_id") or "",
                t.get("tokenId") or t.get("token_id") or "",
                t.get("title") or "",
                t.get("outcome") or "",
                (t.get("side") or "").upper(),
                float(t.get("size") or 0),
                float(t.get("usdcSize") or t.get("usdc_size") or 0),
                float(t.get("price") or 0),
                traded_at,
                tx,
            ))
        if not rows:
            return 0
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO wallet_trades
                        (address, username, condition_id, token_id, title, outcome, side,
                         size, usdc_size, price, traded_at, transaction_hash)
                    VALUES %s
                    ON CONFLICT (address, transaction_hash)
                        WHERE transaction_hash <> ''
                    DO NOTHING
                    """,
                    rows,
                )
                return cur.rowcount

    def get_wallet_trades(self, address: str, since_days: int = 90) -> list[dict]:
        """Return trades joined with market_outcomes for horizon metric computation."""
        with db.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        wt.id, wt.address, wt.condition_id, wt.token_id,
                        wt.title, wt.outcome, wt.side, wt.size, wt.usdc_size,
                        wt.price, wt.traded_at, wt.transaction_hash,
                        mo.resolved,
                        mo.winner_outcome,
                        mo.winner_token_id,
                        mo.closed AS market_closed
                    FROM wallet_trades wt
                    LEFT JOIN market_outcomes mo USING (condition_id)
                    WHERE wt.address = %s
                      AND wt.traded_at >= now() - (%s || ' days')::interval
                    ORDER BY wt.traded_at DESC
                    """,
                    (address, str(since_days)),
                )
                return [_row_to_dict(r) for r in cur.fetchall()]

    def get_trade_last_fetched_at(self, address: str) -> datetime | None:
        """Return the most recent fetched_at for this wallet's trades, or None."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(fetched_at) FROM wallet_trades WHERE address = %s",
                    (address,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None

    def upsert_market_outcomes(self, outcomes: dict) -> None:
        """Upsert market resolution status. outcomes: {condition_id: {resolved, winner_outcome, ...}}"""
        if not outcomes:
            return
        rows = [
            (
                cid,
                bool(d.get("closed", False)),
                bool(d.get("resolved", False)),
                d.get("winner_outcome") or d.get("winner") or "",
                d.get("winner_token_id") or "",
            )
            for cid, d in outcomes.items()
            if cid
        ]
        if not rows:
            return
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO market_outcomes
                        (condition_id, closed, resolved, winner_outcome, winner_token_id, checked_at)
                    VALUES %s
                    ON CONFLICT (condition_id) DO UPDATE SET
                        closed          = EXCLUDED.closed,
                        resolved        = EXCLUDED.resolved,
                        winner_outcome  = EXCLUDED.winner_outcome,
                        winner_token_id = EXCLUDED.winner_token_id,
                        checked_at      = now()
                    """,
                    rows,
                )

    def get_unresolved_condition_ids(self, cids: list[str]) -> list[str]:
        """Return condition_ids that are either missing from market_outcomes or not yet resolved."""
        if not cids:
            return []
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT condition_id FROM market_outcomes
                    WHERE condition_id = ANY(%s) AND resolved = TRUE
                    """,
                    (cids,),
                )
                already_resolved = {row[0] for row in cur.fetchall()}
        return [c for c in cids if c and c not in already_resolved]
