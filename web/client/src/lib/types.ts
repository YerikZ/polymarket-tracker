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
  wallets_scored: number;
  last_signal_at: string | null;
  copy_enabled: boolean;
  target_wallets: string[];
  target_wallet_usernames: string[];
  target_mode: "auto" | "manual";
  error: string | null;
}

export interface HorizonMetrics {
  trade_count: number;
  buy_count: number;
  avg_order_usdc: number;
  median_order_usdc: number;
  total_invested: number;
  unique_markets: number;
  active_days: number;
  win_rate: number | null;
  resolved_count: number;
  avg_entry_price: number | null;
}

export interface QualificationPasses {
  win_rate:     boolean | null;
  track_record: boolean | null;
  niche_focus:  boolean | null;
  frequency:    boolean | null;
  accumulation: boolean | null;
  no_decline:   boolean | null;
}

export interface QualificationMetrics {
  win_rate_90d:           number | null;
  resolved_count_90d:     number;
  earliest_trade_days:    number | null;
  categories_detected:    string[];
  niche_category_count:   number;
  trades_per_month:       number | null;
  avg_entries_per_market: number | null;
  win_rate_30d:           number | null;
}

export interface QualificationCheck {
  status:  "qualified" | "not_qualified" | "insufficient_data";
  passes:  QualificationPasses;
  metrics: QualificationMetrics;
}

export interface WalletTradeDetail {
  address: string;
  last_fetched_at: string | null;
  horizons: Record<"7" | "14" | "30" | "60" | "90" | "120", HorizonMetrics>;
  raw_trade_count: number;
  qualification: QualificationCheck;
}

export interface Basket {
  id: number;
  name: string;
  category: string;
  wallet_addresses: string[];
  consensus_threshold: number;
  active: boolean;
  created_at: string;
}

export interface BasketConsensus {
  basket_id:    number;
  basket_name:  string;
  wallet_count: number;
  agree_count:  number;
  agree_pct:    number;
  price_spread: number;
  threshold:    number;
  should_copy:  boolean;
  reason:       string;
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
  proxy_url?: string;
  proxy_username?: string;
  proxy_password?: string;
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
    max_price?: number;
    min_score?: number;
    score_scale_size?: boolean;
    manual_target_wallets?: string[];
    basket_ids?: number[];
    basket_trade_refresh_interval?: number;
    enable_topup?: boolean;
    max_topups?: number;
    topup_size_multiplier?: number;
    blocked_keywords?: string[];
  };
}
