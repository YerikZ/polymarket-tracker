export type CopierStatus = "placed" | "dry_run" | "shadow" | "skipped" | "failed";

export interface Alert {
  id: number;
  wallet_address: string;
  username: string;
  wallet_rank: number;
  condition_id: string;
  market_title: string;
  outcome: string;
  side: "BUY" | "SELL";
  size: number;
  usdc_size: number;
  price: number;
  detected_at: string;
  transaction_hash: string;
  token_id: string;
  copier_status: CopierStatus | null;
  copier_reason: string | null;
  copier_spend: number | null;
}

export interface Wallet {
  address: string;
  username: string;
  rank: number;
  pnl: number;
  trading_volume: number;
  fetched_at: string;
  score: number | null;
  tier: string | null;
  score_detail: ScoreDetail | null;
}

export interface ScoreDetail {
  address: string;
  s1_calibrated_edge: number;
  s2_temporal_consistency: number;
  s3_independence: number;
  r1_sample_breadth: number;
  r2_sharpe: number;
  r3_recency_trend: number;
  c1_market_impact: number;
  c2_signal_freshness: number;
  c3_liquidity: number;
  skill: number;
  reliability: number;
  copiability: number;
  total: number;
  insufficient_data: boolean;
  trade_count: number;
  unique_markets: number;
  strong_categories: string[];
  copy_tier: string;
  copy_size_pct: number;
}

export interface Position {
  id: number;
  condition_id: string;
  token_id: string;
  market_title: string;
  outcome: string;
  entry_price: number;
  shares: number;
  spend_usdc: number;
  opened_at: string;
  wallet_address: string;
  username: string;
  wallet_rank: number;
  is_dry_run: boolean;
  position_status: "open" | "won" | "lost" | "closed";
  resolution_outcome: string;
  market_closed: boolean;
  current_price: number | null;
  current_value_usdc: number | null;
  closed_at: string | null;
}

export interface PnlSummary {
  total_pnl: number;
  open_pnl: number;
  closed_pnl: number;
  win_rate: number | null;
  open_count: number;
  total_positions: number;
  spent_today: number;
  daily_limit: number;
  remaining: number;
}

export interface WatcherStatus {
  status: "stopped" | "starting" | "running" | "error";
  mode: "stream" | "poll" | "";
  wallets_tracked: number;
  last_signal_at: string | null;
  copy_enabled: boolean;
  target_wallet: string | null;
  target_wallet_username: string | null;
  error: string | null;
}

export interface Settings {
  top_n?: number;
  poll_interval?: number;
  min_position_usdc?: number;
  request_delay?: number;
  wallet_refresh_interval?: number;
  log_level?: string;
  max_signal_age?: number;
  watcher_mode?: "stream" | "poll";
  polygon_wss?: string;
  copy_trading?: {
    private_key?: string;
    funder?: string;
    chain_id?: number;
    signature_type?: number;
    dry_run?: boolean;
    sizing_mode?: string;
    fixed_usdc?: number;
    reference_trade_usdc?: number;
    pct_balance?: number;
    mirror_pct?: number;
    max_trade_usdc?: number;
    daily_limit_usdc?: number;
    min_order_size_cap?: number;
    slippage?: number;
    min_score?: number;
    score_scale_size?: boolean;
    single_wallet_mode?: boolean;
    enable_topup?: boolean;
    max_topups?: number;
    topup_size_multiplier?: number;
    blocked_keywords?: string[];
  };
}
