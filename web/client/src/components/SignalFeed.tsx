import { useState } from "react";
import { useSignals } from "../hooks/useSignals";
import { useWatcherStatus } from "../hooks/useWatcherStatus";
import { TierBadge } from "./TierBadge";
import { fmtPrice, fmtUsd, timeAgo } from "../lib/utils";
import { useQuery } from "@tanstack/react-query";
import type { CopierStatus, Wallet } from "../lib/types";

type SideFilter = "ALL" | "BUY" | "SELL";

const ACTION_STYLES: Record<
  CopierStatus,
  { label: string; className: string }
> = {
  placed: {
    label: "PLACED",
    className: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  },
  dry_run: {
    label: "SIM",
    className: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  },
  shadow: {
    label: "SHADOW",
    className: "bg-violet-500/15 text-violet-400 border-violet-500/30",
  },
  skipped: {
    label: "SKIP",
    className: "bg-zinc-700/50 text-zinc-500 border-zinc-600/40",
  },
  failed: {
    label: "ERR",
    className: "bg-red-500/15 text-red-400 border-red-500/30",
  },
};

function ActionBadge({
  status,
  reason,
  spend,
}: {
  status: CopierStatus | null;
  reason: string | null;
  spend: number | null;
}) {
  if (!status) {
    return <span className="text-zinc-700">—</span>;
  }

  const { label, className } = ACTION_STYLES[status];

  const tooltip = [
    reason,
    spend ? `$${spend.toFixed(2)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-semibold tracking-wider cursor-default ${className}`}
      title={tooltip || undefined}
    >
      {label}
      {spend != null && spend > 0 && (
        <span className="opacity-75">${spend.toFixed(2)}</span>
      )}
    </span>
  );
}

export function SignalFeed() {
  const signals = useSignals();
  const [sideFilter, setSideFilter] = useState<SideFilter>("ALL");
  const { data: status } = useWatcherStatus();

  const { data: wallets = [] } = useQuery<Wallet[]>({
    queryKey: ["wallets"],
    queryFn: () => fetch("/api/wallets").then((r) => r.json()),
    refetchInterval: 60_000,
  });

  const tierMap = Object.fromEntries(wallets.map((w) => [w.address, w.tier]));

  const targetWallet = status?.target_wallet ?? null;
  const filtered = signals.filter(
    (s) =>
      (sideFilter === "ALL" || s.side === sideFilter) &&
      (!targetWallet || s.wallet_address === targetWallet)
  );

  return (
    <div className="flex flex-col h-full">
      {/* Filter bar */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-zinc-800">
        <span className="text-xs text-zinc-500">Side:</span>
        {(["ALL", "BUY", "SELL"] as SideFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setSideFilter(f)}
            className={`px-2 py-0.5 rounded text-xs font-semibold transition-colors ${
              sideFilter === f
                ? f === "BUY"
                  ? "bg-emerald-500/20 text-emerald-400"
                  : f === "SELL"
                  ? "bg-red-500/20 text-red-400"
                  : "bg-zinc-700 text-zinc-200"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {f}
          </button>
        ))}
        <span className="ml-auto text-xs text-zinc-600">
          {filtered.length} signals
        </span>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-zinc-950 border-b border-zinc-800">
            <tr className="text-zinc-500 text-left">
              <th className="px-4 py-2 font-medium">Time</th>
              <th className="px-4 py-2 font-medium">Wallet</th>
              <th className="px-4 py-2 font-medium">Tier</th>
              <th className="px-4 py-2 font-medium">Side</th>
              <th className="px-4 py-2 font-medium">Market</th>
              <th className="px-4 py-2 font-medium">Outcome</th>
              <th className="px-4 py-2 font-medium text-right">Price</th>
              <th className="px-4 py-2 font-medium text-right">Size</th>
              <th className="px-4 py-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-zinc-600 py-16">
                  No signals yet — start the watcher to stream live trades.
                </td>
              </tr>
            )}
            {filtered.map((s) => (
              <tr
                key={s.id}
                className="border-b border-zinc-900 hover:bg-zinc-900/50 transition-colors"
              >
                <td className="px-4 py-2 text-zinc-500 whitespace-nowrap">
                  {timeAgo(s.detected_at)}
                </td>
                <td className="px-4 py-2 whitespace-nowrap">
                  <span className="text-zinc-200">
                    {s.username || s.wallet_address.slice(0, 8)}
                  </span>
                  <span className="text-zinc-600 ml-1">#{s.wallet_rank}</span>
                </td>
                <td className="px-4 py-2">
                  <TierBadge tier={tierMap[s.wallet_address] ?? null} />
                </td>
                <td className="px-4 py-2">
                  <span
                    className={`font-semibold ${
                      s.side === "BUY" ? "text-emerald-400" : "text-red-400"
                    }`}
                  >
                    {s.side}
                  </span>
                </td>
                <td
                  className="px-4 py-2 text-zinc-300 max-w-xs truncate"
                  title={s.market_title}
                >
                  {s.market_title}
                </td>
                <td className="px-4 py-2 text-zinc-400">{s.outcome}</td>
                <td className="px-4 py-2 text-right text-zinc-200">
                  {fmtPrice(s.price)}
                </td>
                <td className="px-4 py-2 text-right text-zinc-200">
                  {fmtUsd(s.usdc_size, 0).replace("+", "")}
                </td>
                <td className="px-4 py-2">
                  <ActionBadge
                    status={s.copier_status}
                    reason={s.copier_reason}
                    spend={s.copier_spend}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
