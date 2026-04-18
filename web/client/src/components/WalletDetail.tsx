import { X, RefreshCw, AlertCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { HorizonMetrics, QualificationCheck, QualificationPasses, Wallet, WalletTradeDetail } from "../lib/types";
import { TierBadge } from "./TierBadge";
import { ScoreBreakdown } from "./ScoreBreakdown";

// ── Formatting helpers ────────────────────────────────────────────────────────

function fmtUsd(v: number | null | undefined, decimals = 2): string {
  if (v == null) return "—";
  return "$" + v.toFixed(decimals);
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return (v * 100).toFixed(1) + "%";
}

function fmtNum(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toFixed(0);
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toFixed(3);
}

function fmtTs(iso: string | null): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleString();
}

// ── Horizon grid ──────────────────────────────────────────────────────────────

const WINDOWS = ["7", "14", "30", "60", "90", "120"] as const;

interface MetricRow {
  key: keyof HorizonMetrics;
  label: string;
  fmt: (v: number | null) => string;
  highlight?: (v: number | null) => boolean;
}

const METRIC_ROWS: MetricRow[] = [
  { key: "trade_count",        label: "Trades",               fmt: fmtNum },
  { key: "buy_count",          label: "Buys",                 fmt: fmtNum },
  { key: "avg_order_usdc",     label: "Avg order",            fmt: fmtUsd },
  { key: "median_order_usdc",  label: "Median order",         fmt: fmtUsd },
  { key: "total_invested",     label: "Total invested",       fmt: (v) => fmtUsd(v, 0) },
  { key: "unique_markets",     label: "Unique markets",       fmt: fmtNum },
  { key: "active_days",        label: "Active days",          fmt: fmtNum },
  {
    key: "win_rate",
    label: "Win rate (resolved)",
    fmt: fmtPct,
    highlight: (v) => v != null && v >= 0.55,
  },
  { key: "resolved_count",     label: "Resolved trades",      fmt: fmtNum },
  { key: "avg_entry_price",    label: "Avg entry price",      fmt: fmtPrice },
];

// ── Qualification scorecard ───────────────────────────────────────────────────

interface CriterionDef {
  key: keyof QualificationPasses;
  label: string;
  metricFn: (m: QualificationCheck["metrics"]) => string;
}

const CRITERIA: CriterionDef[] = [
  {
    key: "win_rate",
    label: "Win rate ≥60%",
    metricFn: (m) =>
      m.win_rate_90d != null
        ? `${(m.win_rate_90d * 100).toFixed(1)}% (${m.resolved_count_90d} resolved)`
        : `${m.resolved_count_90d} resolved trades`,
  },
  {
    key: "track_record",
    label: "4+ month history",
    metricFn: (m) =>
      m.earliest_trade_days != null
        ? `${Math.floor(m.earliest_trade_days)}d of data`
        : "No data",
  },
  {
    key: "niche_focus",
    label: "2–3 topic areas",
    metricFn: (m) =>
      m.categories_detected.length > 0
        ? `${m.niche_category_count}: ${m.categories_detected.join(", ")}`
        : "No categories found",
  },
  {
    key: "frequency",
    label: "<100 trades/month",
    metricFn: (m) =>
      m.trades_per_month != null ? `${m.trades_per_month.toFixed(0)}/mo` : "—",
  },
  {
    key: "accumulation",
    label: "Position building",
    metricFn: (m) =>
      m.avg_entries_per_market != null
        ? `${m.avg_entries_per_market.toFixed(2)}× per market`
        : "—",
  },
  {
    key: "no_decline",
    label: "No recent decline",
    metricFn: (m) => {
      if (m.win_rate_30d == null || m.win_rate_90d == null) return "Insufficient data";
      const delta = (m.win_rate_30d - m.win_rate_90d) * 100;
      return `30d ${(m.win_rate_30d * 100).toFixed(1)}% vs 90d ${(m.win_rate_90d * 100).toFixed(1)}% (${delta >= 0 ? "+" : ""}${delta.toFixed(1)}pp)`;
    },
  },
];

function QualificationScorecard({ q }: { q: QualificationCheck }) {
  const statusConfig = {
    qualified:         { label: "Qualified",         cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30" },
    not_qualified:     { label: "Not Qualified",     cls: "bg-red-500/15 text-red-400 border-red-500/30" },
    insufficient_data: { label: "Insufficient Data", cls: "bg-zinc-700/30 text-zinc-400 border-zinc-600" },
  }[q.status];

  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/50 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider">
          Qualification Check
        </span>
        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${statusConfig.cls}`}>
          {statusConfig.label}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        {CRITERIA.map(({ key, label, metricFn }) => {
          const pass = q.passes[key];
          const isNull = pass === null || pass === undefined;
          const cardCls = isNull
            ? "border-zinc-800 bg-zinc-800/20"
            : pass
            ? "border-emerald-800/50 bg-emerald-900/20"
            : "border-red-800/50 bg-red-900/20";
          const iconCls = isNull
            ? "text-zinc-600"
            : pass
            ? "text-emerald-400"
            : "text-red-400";
          return (
            <div key={key} className={`rounded p-2 border text-[10px] ${cardCls}`}>
              <div className="flex items-center gap-1 mb-0.5">
                <span className={`font-bold ${iconCls}`}>
                  {isNull ? "·" : pass ? "✓" : "✗"}
                </span>
                <span className="font-medium text-zinc-300 truncate">{label}</span>
              </div>
              <div className="text-zinc-500 truncate leading-tight">{metricFn(q.metrics)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Horizon grid ──────────────────────────────────────────────────────────────

function HorizonMetricsGrid({ horizons }: { horizons: WalletTradeDetail["horizons"] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-zinc-800">
            <th className="text-left py-2 pr-4 text-zinc-500 font-medium w-40">Metric</th>
            {WINDOWS.map((w) => (
              <th key={w} className="text-right py-2 px-3 text-zinc-400 font-medium">
                {w}d
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {METRIC_ROWS.map((row) => (
            <tr key={row.key} className="border-b border-zinc-800/50 hover:bg-zinc-800/20">
              <td className="py-2 pr-4 text-zinc-500">{row.label}</td>
              {WINDOWS.map((w) => {
                const m = horizons[w];
                const raw = m ? (m[row.key] as number | null) : null;
                const isWinRate = row.key === "win_rate";
                const noData = isWinRate && m?.resolved_count === 0;
                const highlighted = !noData && row.highlight?.(raw);
                return (
                  <td
                    key={w}
                    className={[
                      "py-2 px-3 text-right tabular-nums",
                      highlighted ? "text-emerald-400 font-medium" : "text-zinc-300",
                      noData ? "text-zinc-600" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    {noData ? "—" : row.fmt(raw)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Main drawer ───────────────────────────────────────────────────────────────

interface Props {
  wallet: Wallet | null;
  onClose: () => void;
}

export function WalletDetail({ wallet, onClose }: Props) {
  const qc = useQueryClient();

  const {
    data: detail,
    isLoading,
    isError,
  } = useQuery<WalletTradeDetail>({
    queryKey: ["wallet-detail", wallet?.address],
    queryFn: () =>
      fetch(`/api/wallets/${wallet!.address}/trades`).then((r) => r.json()),
    enabled: !!wallet,
    staleTime: 60 * 60 * 1000,
  });

  const fetchMutation = useMutation({
    mutationFn: () =>
      fetch(`/api/wallets/${wallet!.address}/fetch-trades`, { method: "POST" }).then((r) =>
        r.json()
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wallet-detail", wallet?.address] });
    },
  });

  if (!wallet) return null;

  return (
    <>
      {/* Overlay */}
      <div
        className="fixed inset-0 z-40 bg-black/50"
        onClick={onClose}
      />

      {/* Drawer */}
      <div className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-3xl bg-zinc-900 border-l border-zinc-800 flex flex-col shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800 shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <TierBadge tier={wallet.tier ?? "?"} />
            <div className="min-w-0">
              <div className="text-sm font-semibold text-zinc-100 truncate">
                {wallet.username || wallet.address}
              </div>
              <div className="text-xs text-zinc-500 font-mono truncate">
                {wallet.address}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0 ml-4">
            {detail && (
              <span className="text-[11px] text-zinc-500">
                Fetched {fmtTs(detail.last_fetched_at)}
              </span>
            )}
            <button
              onClick={() => fetchMutation.mutate()}
              disabled={fetchMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 disabled:opacity-50 transition-colors"
            >
              <RefreshCw
                size={12}
                className={fetchMutation.isPending ? "animate-spin" : ""}
              />
              {fetchMutation.isPending ? "Fetching…" : "Fetch trades"}
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-6">
          {/* Horizon metrics */}
          <section>
            <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
              Performance by horizon
            </h3>
            {isLoading && (
              <div className="text-zinc-500 text-sm py-6 text-center">
                Loading trade history…
              </div>
            )}
            {isError && (
              <div className="flex items-center gap-2 text-red-400 text-sm py-4">
                <AlertCircle size={14} />
                Failed to load trade history. Click "Fetch trades" to retry.
              </div>
            )}
            {detail && detail.raw_trade_count === 0 && !isLoading && (
              <div className="text-zinc-500 text-sm py-4 text-center">
                No trade history found. Click "Fetch trades" to load data.
              </div>
            )}
            {detail && detail.raw_trade_count > 0 && (
              <>
                {detail.qualification && (
                  <QualificationScorecard q={detail.qualification} />
                )}
                <HorizonMetricsGrid horizons={detail.horizons} />
                <p className="text-[11px] text-zinc-600 mt-2">
                  Based on {detail.raw_trade_count} trades in the last 120 days.
                  Win rate only counts resolved markets.
                </p>
              </>
            )}
          </section>

          {/* Score breakdown */}
          {wallet.score_detail && (
            <section>
              <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-3">
                Score breakdown
              </h3>
              <ScoreBreakdown detail={wallet.score_detail} />
            </section>
          )}
        </div>
      </div>
    </>
  );
}
