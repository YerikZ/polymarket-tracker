import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Settings, Wallet } from "../lib/types";
import { TierBadge } from "./TierBadge";
import { ScoreBreakdown } from "./ScoreBreakdown";
import { fmtUsd } from "../lib/utils";

export function WalletTable() {
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data: wallets = [], isLoading } = useQuery<Wallet[]>({
    queryKey: ["wallets"],
    queryFn: () => fetch("/api/wallets").then((r) => r.json()),
    refetchInterval: 60_000,
  });
  const qc = useQueryClient();
  const { data: settings } = useQuery<Settings>({
    queryKey: ["settings"],
    queryFn: () => fetch("/api/settings").then((r) => r.json()),
    staleTime: 30_000,
  });
  const manualTargets = settings?.copy_trading?.manual_target_wallets ?? [];
  const manualTargetSet = new Set(manualTargets.map((value) => value.toLowerCase()));
  const saveTargets = useMutation({
    mutationFn: async (targets: string[]) => {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ copy_trading: { ...(settings?.copy_trading ?? {}), manual_target_wallets: targets } }),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: (data) => qc.setQueryData(["settings"], data),
  });
  const refreshWallets = useMutation({
    mutationFn: async () => {
      const r = await fetch("/api/wallets/refresh", { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: (data) => qc.setQueryData(["wallets"], data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["wallets"] });
      qc.invalidateQueries({ queryKey: ["watcher-status"] });
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-zinc-600 text-sm">
        Loading wallets…
      </div>
    );
  }

  return (
    <div className="overflow-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-900">
        <div className="text-xs text-zinc-500">
          Refresh leaderboard wallets and recompute scores.
        </div>
        <button
          type="button"
          onClick={() => refreshWallets.mutate()}
          disabled={refreshWallets.isPending}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-zinc-700 bg-zinc-900 text-zinc-200 text-xs font-semibold hover:border-zinc-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${refreshWallets.isPending ? "animate-spin" : ""}`} />
          {refreshWallets.isPending ? "Refreshing…" : "Refresh Wallets"}
        </button>
      </div>
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-zinc-950 border-b border-zinc-800">
          <tr className="text-zinc-500 text-left">
            <th className="px-4 py-2 font-medium w-8"></th>
            <th className="px-4 py-2 font-medium">Rank</th>
            <th className="px-4 py-2 font-medium">Username</th>
            <th className="px-4 py-2 font-medium">Tier</th>
            <th className="px-4 py-2 font-medium text-right">Leaderboard PnL</th>
            <th className="px-4 py-2 font-medium">Score</th>
            <th className="px-4 py-2 font-medium">Manual Target</th>
          </tr>
        </thead>
        <tbody>
          {wallets.length === 0 && (
            <tr>
              <td colSpan={7} className="text-center text-zinc-600 py-16">
                No wallets — run <code className="text-zinc-400">polymarket top</code> or start the watcher.
              </td>
            </tr>
          )}
          {wallets.map((w) => (
            <Fragment key={w.address}>
              <tr
                className="border-b border-zinc-900 hover:bg-zinc-900/50 cursor-pointer transition-colors"
                onClick={() => setExpanded(expanded === w.address ? null : w.address)}
              >
                <td className="px-4 py-2 text-zinc-600">
                  {expanded === w.address ? (
                    <ChevronDown className="w-3.5 h-3.5" />
                  ) : (
                    <ChevronRight className="w-3.5 h-3.5" />
                  )}
                </td>
                <td className="px-4 py-2 text-zinc-400">#{w.rank}</td>
                <td className="px-4 py-2 text-zinc-200">
                  {w.username || w.address.slice(0, 10) + "…"}
                </td>
                <td className="px-4 py-2">
                  <TierBadge tier={w.tier} />
                </td>
                <td className="px-4 py-2 text-right">
                  <span className={w.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                    {fmtUsd(w.pnl, 0)}
                  </span>
                </td>
                <td className="px-4 py-2">
                  {w.score != null ? (
                    <div className="flex items-center gap-2">
                      <div className="w-20 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-emerald-500 rounded-full"
                          style={{ width: `${w.score}%` }}
                        />
                      </div>
                      <span className="text-zinc-300">{w.score.toFixed(0)}</span>
                    </div>
                  ) : (
                    <span className="text-zinc-600">—</span>
                  )}
                </td>
                <td className="px-4 py-2">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      const next = manualTargetSet.has(w.address.toLowerCase())
                        ? manualTargets.filter((value) => value.toLowerCase() !== w.address.toLowerCase())
                        : [...manualTargets, w.address];
                      saveTargets.mutate(next);
                    }}
                    className={`px-2 py-1 rounded border text-[10px] font-semibold transition-colors ${
                      manualTargetSet.has(w.address.toLowerCase())
                        ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                        : "bg-zinc-900 text-zinc-500 border-zinc-800 hover:text-zinc-300"
                    }`}
                  >
                    {manualTargetSet.has(w.address.toLowerCase()) ? "Picked" : "Pick"}
                  </button>
                </td>
              </tr>
              {expanded === w.address && w.score_detail && (
                <tr className="border-b border-zinc-900 bg-zinc-900/30">
                  <td colSpan={7}>
                    <ScoreBreakdown detail={w.score_detail} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
