"""
PostgreSQL connection-pool and schema management.

Usage
-----
    from . import db
    db.init_pool(dsn)        # once, at startup
    db.apply_schema()        # once, at startup (idempotent DDL)

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(...)
        # conn.commit() is called automatically on clean exit;
        # conn.rollback() on any exception.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.sql
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None

# ---------------------------------------------------------------------------
# DDL — all statements are idempotent (IF NOT EXISTS / ON CONFLICT)
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    username        TEXT             NOT NULL DEFAULT '',
    rank            INTEGER          NOT NULL DEFAULT 0,
    pnl             DOUBLE PRECISION NOT NULL DEFAULT 0,
    trading_volume  DOUBLE PRECISION NOT NULL DEFAULT 0,
    fetched_at      TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS snapshots (
    address  TEXT NOT NULL,
    tx_hash  TEXT NOT NULL,
    PRIMARY KEY (address, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_address ON snapshots (address);

CREATE TABLE IF NOT EXISTS alerts (
    id                BIGSERIAL        PRIMARY KEY,
    wallet_address    TEXT             NOT NULL DEFAULT '',
    username          TEXT             NOT NULL DEFAULT '',
    wallet_rank       INTEGER          NOT NULL DEFAULT 0,
    condition_id      TEXT             NOT NULL DEFAULT '',
    market_title      TEXT             NOT NULL DEFAULT '',
    outcome           TEXT             NOT NULL DEFAULT '',
    side              TEXT             NOT NULL DEFAULT 'BUY',
    size              DOUBLE PRECISION NOT NULL DEFAULT 0,
    usdc_size         DOUBLE PRECISION NOT NULL DEFAULT 0,
    price             DOUBLE PRECISION NOT NULL DEFAULT 0,
    detected_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    transaction_hash  TEXT             NOT NULL DEFAULT '',
    token_id          TEXT             NOT NULL DEFAULT ''
);
-- Partial unique index: only enforce uniqueness on non-empty tx hashes
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_tx_hash
    ON alerts (transaction_hash)
    WHERE transaction_hash <> '';
CREATE INDEX IF NOT EXISTS idx_alerts_detected_at ON alerts (detected_at DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
    id                  BIGSERIAL        PRIMARY KEY,
    condition_id        TEXT             NOT NULL DEFAULT '',
    token_id            TEXT             NOT NULL DEFAULT '',
    market_title        TEXT             NOT NULL DEFAULT '',
    outcome             TEXT             NOT NULL DEFAULT '',
    entry_price         DOUBLE PRECISION NOT NULL DEFAULT 0,
    shares              DOUBLE PRECISION NOT NULL DEFAULT 0,
    spend_usdc          DOUBLE PRECISION NOT NULL DEFAULT 0,
    opened_at           TIMESTAMPTZ      NOT NULL DEFAULT now(),
    wallet_address      TEXT             NOT NULL DEFAULT '',
    username            TEXT             NOT NULL DEFAULT '',
    wallet_rank         INTEGER          NOT NULL DEFAULT 0,
    is_dry_run          BOOLEAN          NOT NULL DEFAULT TRUE,
    -- TRUE  = simulated trade (--dry-run), FALSE = real money placed on-chain
    -- Market resolution status (kept up-to-date by cmd_pnl)
    position_status     TEXT             NOT NULL DEFAULT 'open',
    -- 'open'   = market still active
    -- 'won'    = market resolved, our outcome won  (final price 1.0)
    -- 'lost'   = market resolved, our outcome lost (final price 0.0)
    -- 'closed' = market closed/settled but resolution unclear
    resolution_outcome  TEXT             NOT NULL DEFAULT '',
    -- label of the winning outcome, e.g. 'Yes', 'No', 'Yokohama F·Marinos'
    market_closed       BOOLEAN          NOT NULL DEFAULT FALSE,
    topup_count         INTEGER          NOT NULL DEFAULT 0
);
-- Migrate existing tables: add new columns if they don't exist yet
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS is_dry_run         BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS position_status    TEXT    NOT NULL DEFAULT 'open';
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS resolution_outcome TEXT    NOT NULL DEFAULT '';
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS market_closed      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS current_price      NUMERIC(18,6);
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS current_value_usdc NUMERIC(18,6);
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS closed_at          TIMESTAMPTZ;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS topup_count        INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_paper_condition ON paper_positions (condition_id)
    WHERE condition_id <> '';
CREATE INDEX IF NOT EXISTS idx_paper_token ON paper_positions (token_id)
    WHERE token_id <> '';
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions (position_status);

CREATE TABLE IF NOT EXISTS daily_spend (
    date_iso TEXT             PRIMARY KEY,   -- 'YYYY-MM-DD'
    amount   DOUBLE PRECISION NOT NULL DEFAULT 0
);

-- Web UI config store — single row (id always 1), seeded from config.yaml on first run
CREATE TABLE IF NOT EXISTS settings (
    id         INT     PRIMARY KEY DEFAULT 1,
    config     JSONB   NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Score columns on wallets — populated by WalletScorer after each score_all() run
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS score        DOUBLE PRECISION;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS tier         TEXT;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS score_detail JSONB;

-- Copier outcome columns on alerts — written back after CopyTrader.copy() returns
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS copier_status TEXT;
  -- placed | dry_run | shadow | skipped | failed  (NULL = no copier configured)
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS copier_reason TEXT;
  -- human-readable explanation for the status
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS copier_spend  DOUBLE PRECISION;
  -- USDC actually spent (non-zero for placed / dry_run / shadow)

-- ── Wallet trade history ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wallet_trades (
    id               BIGSERIAL        PRIMARY KEY,
    address          TEXT             NOT NULL,
    condition_id     TEXT             NOT NULL DEFAULT '',
    token_id         TEXT             NOT NULL DEFAULT '',
    title            TEXT             NOT NULL DEFAULT '',
    outcome          TEXT             NOT NULL DEFAULT '',
    side             TEXT             NOT NULL DEFAULT '',   -- BUY | SELL
    size             DOUBLE PRECISION NOT NULL DEFAULT 0,
    usdc_size        DOUBLE PRECISION NOT NULL DEFAULT 0,
    price            DOUBLE PRECISION NOT NULL DEFAULT 0,
    traded_at        TIMESTAMPTZ      NOT NULL,
    transaction_hash TEXT             NOT NULL DEFAULT '',
    fetched_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);
ALTER TABLE wallet_trades ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_trades_tx
    ON wallet_trades (address, transaction_hash) WHERE transaction_hash <> '';
CREATE INDEX IF NOT EXISTS idx_wallet_trades_addr_at
    ON wallet_trades (address, traded_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_condition
    ON wallet_trades (condition_id) WHERE condition_id <> '';

-- ── Market resolution cache ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS market_outcomes (
    condition_id    TEXT        PRIMARY KEY,
    title           TEXT        NOT NULL DEFAULT '',
    resolved        BOOLEAN     NOT NULL DEFAULT FALSE,
    winner_outcome  TEXT        NOT NULL DEFAULT '',
    winner_token_id TEXT        NOT NULL DEFAULT '',
    closed          BOOLEAN     NOT NULL DEFAULT FALSE,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Back-fill: rows with a clear winner (outcome price >=95%) should be resolved.
UPDATE market_outcomes
   SET resolved = TRUE
 WHERE winner_outcome <> ''
   AND resolved = FALSE;

-- ── Baskets (consensus copy groups) ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS baskets (
    id                   SERIAL           PRIMARY KEY,
    name                 TEXT             NOT NULL,
    category             TEXT             NOT NULL DEFAULT '',
    wallet_addresses     TEXT[]           NOT NULL DEFAULT '{}',
    consensus_threshold  DOUBLE PRECISION NOT NULL DEFAULT 0.8,
    active               BOOLEAN          NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_baskets_active ON baskets (active) WHERE active = TRUE;
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_pool(dsn: str, minconn: int = 1, maxconn: int = 10) -> None:
    """Initialise the global connection pool.  Call once at process start."""
    global _pool
    _pool = ThreadedConnectionPool(minconn, maxconn, dsn)
    logger.info("PostgreSQL pool ready (dsn: %s…)", dsn[:40])


def apply_schema() -> None:
    """Create all tables / indexes if they do not exist yet (idempotent)."""
    # Suppress collation-version warnings that appear on every new connection
    # when running on Alpine/musl Docker images.  Clearing datcollversion tells
    # PostgreSQL to skip the version check entirely — safe when using libc locale.
    # Must run outside a transaction block (autocommit).
    conn = _pool.getconn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pg_database SET datcollversion = NULL "
                "WHERE datname = current_database() "
                "AND datcollversion IS NOT NULL"
            )
        conn.autocommit = False
    except Exception as exc:
        logger.debug("Collation version clear skipped: %s", exc)
        conn.autocommit = False
    finally:
        _pool.putconn(conn)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
    logger.debug("Schema applied.")


@contextmanager
def get_conn() -> Generator:
    """Yield a connection from the pool; commit on clean exit, rollback on error."""
    global _pool
    if _pool is None:
        raise RuntimeError(
            "DB pool not initialised — call db.init_pool(dsn) before using Storage."
        )
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            _pool.putconn(conn)
        except Exception:
            pass
