from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Wallet:
    address: str          # proxyWallet 0x...
    username: str
    rank: int
    pnl: float            # USD profit/loss from leaderboard
    trading_volume: float
    fetched_at: str       # ISO-8601
    score: Optional[float] = None
    tier: Optional[str] = None
    score_detail: Optional[dict] = None


@dataclass
class Position:
    condition_id: str
    title: str
    outcome: str
    size: float           # number of shares
    avg_price: float
    cur_price: float
    initial_value: float  # USDC spent
    current_value: float  # USDC value now
    cash_pnl: float       # unrealised P&L in USDC
    percent_pnl: float
    end_date: Optional[str]
    redeemable: bool = False


@dataclass
class Trade:
    condition_id: str
    title: str
    outcome: str
    side: str             # BUY | SELL
    size: float           # shares
    usdc_size: float      # USDC value
    price: float
    timestamp: int        # Unix seconds
    transaction_hash: str
    token_id: str = ""    # ERC-1155 asset ID (needed to place orders)


@dataclass
class WalletStats:
    wallet: Wallet
    total_pnl: float
    win_rate: float       # 0.0–1.0 (approximated from open positions)
    avg_position_size: float
    open_positions: list[Position]
    recent_trades: list[Trade]


@dataclass
class Signal:
    wallet_address: str
    username: str
    wallet_rank: int
    condition_id: str
    market_title: str
    outcome: str
    side: str
    size: float
    usdc_size: float
    price: float
    detected_at: str      # ISO-8601
    transaction_hash: str
    token_id: str = ""    # ERC-1155 asset ID — required to place a copy order


@dataclass
class WalletScore:
    address: str

    # ── Skill (45 pts max) ────────────────────────────────────────────────
    s1_calibrated_edge: float = 0.0      # 0–20  weighted mean(cur_price - avg_price)
    s2_temporal_consistency: float = 0.0  # 0–15  win rate consistency across 7d / 30d
    s3_independence: float = 0.0          # 0–10  leader vs. follower vs. peers

    # ── Reliability (30 pts max) ──────────────────────────────────────────
    r1_sample_breadth: float = 0.0        # 0–10  unique markets traded
    r2_sharpe: float = 0.0               # 0–10  Sharpe-like P&L consistency
    r3_recency_trend: float = 0.0         # 0–10  improving / stable / declining

    # ── Copiability (25 pts max) ──────────────────────────────────────────
    c1_market_impact: float = 0.0         # 0–10  avg trade size (smaller = less impact)
    c2_signal_freshness: float = 0.0      # 0–10  trade frequency (rarer = easier to catch)
    c3_liquidity: float = 2.5             # 0–5   stubbed neutral (needs order-book data)

    # ── Derived (computed by WalletScorer, stored for display) ────────────
    skill: float = 0.0
    reliability: float = 0.0
    copiability: float = 0.0
    total: float = 0.0

    # ── Meta ──────────────────────────────────────────────────────────────
    insufficient_data: bool = False
    trade_count: int = 0
    unique_markets: int = 0
    strong_categories: list = None       # e.g. ["Politics", "Sports"]
    copy_tier: str = "?"                 # A | B | C | WATCH | SKIP | ?
    copy_size_pct: float = 0.0           # multiplier for configured fixed_usdc

    def __post_init__(self):
        if self.strong_categories is None:
            self.strong_categories = []


@dataclass
class CopyResult:
    signal: Signal
    status: str           # "placed" | "dry_run" | "skipped" | "failed"
    reason: str           # human-readable explanation
    order_id: str = ""
    spend_usdc: float = 0.0
    price: float = 0.0
