import { useState, useEffect, useCallback } from "react";
import { RefreshCw } from "lucide-react";
import type { Position, PnlSummary } from "../lib/types";
import { PnLChart } from "./PnLChart";
import { fmtPct, fmtPrice, fmtUsd, fmtDate, timeAgo } from "../lib/utils";
import { apiUrl } from "../lib/api";

type ModeFilter = "all" | "dry" | "live";

function StatusChip({ status, isDryRun }: { status: string; isDryRun: boolean }) {
  const label = isDryRun ? (status === "open" ? "Dry" : `Dry·${status}`) : status;
  const color =
    status === "won"
      ? "text-emerald-400 bg-emerald-500/10"
      : status === "lost"
      ? "text-red-400 bg-red-500/10"
      : isDryRun
      ? "text-zinc-400 bg-zinc-800"
      : "text-blue-400 bg-blue-500/10";
  return (
    <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${color}`}>
      {label}
    </span>
  );
}

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-[10px] uppercase tracking-wider mb-1">{label}</p>
      <p className="text-zinc-100 text-lg font-semibold leading-none">{value}</p>
      {sub && <p className="text-zinc-600 text-xs mt-1">{sub}</p>}
    </div>
  );
}

export function PositionsTable() {
  const [mode, setMode] = useState<ModeFilter>("all");
  const [positions, setPositions] = useState<Position[]>([]);
  const [summary, setSummary] = useState<PnlSummary | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const fetchAll = useCallback(async (withPriceRefresh = false) => {
    if (withPriceRefresh) {
      // POST /api/positions/refresh fetches live CLOB prices, writes them to DB,
      // and returns the updated positions — use that directly
      const refreshed: Position[] = await fetch(apiUrl("/api/positions/refresh"), {
        method: "POST",
      }).then((r) => r.json());

      // Filter client-side by mode
      const filtered =
        mode === "dry"
          ? refreshed.filter((p) => p.is_dry_run)
          : mode === "live"
          ? refreshed.filter((p) => !p.is_dry_run)
          : refreshed;

      setPositions(filtered);
    } else {
      const pos: Position[] = await fetch(
        `/api/positions?mode=${mode}`
      ).then((r) => r.json());
      setPositions(pos);
    }

    const sum = await fetch(apiUrl("/api/pnl/summary")).then((r) => r.json());
    setSummary(sum);
    setLastRefreshed(new Date());
  }, [mode]);

  // Initial load + reload when mode changes
  useEffect(() => {
    setIsLoading(true);
    fetchAll().finally(() => setIsLoading(false));
  }, [fetchAll]);

  async function handleRefresh() {
    setIsRefreshing(true);
    try {
      await fetchAll(true);
    } finally {
      setIsRefreshing(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      {/* Metric cards */}
      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <MetricCard
            label="Total PnL"
            value={fmtUsd(summary.total_pnl)}
          />
          <MetricCard
            label="Win Rate"
            value={summary.win_rate != null ? fmtPct(summary.win_rate) : "—"}
            sub={`${summary.total_positions} positions`}
          />
          <MetricCard
            label="Open"
            value={String(summary.open_count)}
            sub="positions"
          />
          <MetricCard
            label="Spent Today"
            value={`$${summary.spent_today.toFixed(2)}`}
            sub={`$${summary.remaining.toFixed(2)} remaining`}
          />
        </div>
      )}

      {/* PnL chart */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-zinc-500 text-[10px] uppercase tracking-wider mb-2">
          Cumulative PnL
        </p>
        <PnLChart positions={positions} />
      </div>

      {/* Filter + refresh */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-zinc-500">Mode:</span>
        {(["all", "dry", "live"] as ModeFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setMode(f)}
            className={`px-2 py-0.5 rounded text-xs font-semibold capitalize transition-colors ${
              mode === f ? "bg-zinc-700 text-zinc-200" : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {f}
          </button>
        ))}

        <div className="ml-auto flex items-center gap-2">
          {lastRefreshed && (
            <span className="text-[11px] text-zinc-600">
              Updated {timeAgo(lastRefreshed.toISOString())}
            </span>
          )}
          <button
            onClick={handleRefresh}
            disabled={isRefreshing || isLoading}
            className="flex items-center gap-1.5 px-2.5 py-1 rounded border border-zinc-700 text-xs text-zinc-400 hover:text-zinc-200 hover:border-zinc-500 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <RefreshCw className={`w-3 h-3 ${isRefreshing ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Positions table */}
      {isLoading ? (
        <div className="text-center text-zinc-600 text-sm py-8">Loading…</div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <thead className="border-b border-zinc-800">
              <tr className="text-zinc-500 text-left">
                <th className="px-3 py-2 font-medium">Market</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
                <th className="px-3 py-2 font-medium">Mode</th>
                <th className="px-3 py-2 font-medium text-right">Entry</th>
                <th className="px-3 py-2 font-medium text-right">Current</th>
                <th className="px-3 py-2 font-medium text-right">Cost</th>
                <th className="px-3 py-2 font-medium text-right">Value</th>
                <th className="px-3 py-2 font-medium text-right">PnL%</th>
                <th className="px-3 py-2 font-medium">Opened</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 && (
                <tr>
                  <td colSpan={10} className="text-center text-zinc-600 py-12">
                    No positions found.
                  </td>
                </tr>
              )}
              {positions.map((p) => {
                const pnl =
                  p.current_value_usdc != null
                    ? p.current_value_usdc - p.spend_usdc
                    : null;
                const pnlPct =
                  pnl != null && p.spend_usdc > 0 ? pnl / p.spend_usdc : null;
                return (
                  <tr
                    key={p.id}
                    className="border-b border-zinc-900 hover:bg-zinc-900/40 transition-colors"
                  >
                    <td className="px-3 py-2 text-zinc-300 max-w-[200px] truncate">
                      {p.market_title}
                    </td>
                    <td className="px-3 py-2 text-zinc-400">{p.outcome}</td>
                    <td className="px-3 py-2">
                      <StatusChip status={p.position_status} isDryRun={p.is_dry_run} />
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300">
                      {fmtPrice(p.entry_price)}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300">
                      {p.current_price != null ? fmtPrice(p.current_price) : "—"}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-400">
                      {fmtUsd(p.spend_usdc, 2).replace("+", "")}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300">
                      {p.current_value_usdc != null
                        ? fmtUsd(p.current_value_usdc, 2).replace("+", "")
                        : "—"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {pnlPct != null ? (
                        <span
                          className={pnlPct >= 0 ? "text-emerald-400" : "text-red-400"}
                        >
                          {fmtPct(pnlPct)}
                        </span>
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-zinc-500">{fmtDate(p.opened_at)}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`${
                          p.position_status === "won"
                            ? "text-emerald-400"
                            : p.position_status === "lost"
                            ? "text-red-400"
                            : "text-zinc-400"
                        }`}
                      >
                        {p.position_status}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
