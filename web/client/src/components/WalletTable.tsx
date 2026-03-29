import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { Wallet } from "../lib/types";
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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-zinc-600 text-sm">
        Loading wallets…
      </div>
    );
  }

  return (
    <div className="overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-zinc-950 border-b border-zinc-800">
          <tr className="text-zinc-500 text-left">
            <th className="px-4 py-2 font-medium w-8"></th>
            <th className="px-4 py-2 font-medium">Rank</th>
            <th className="px-4 py-2 font-medium">Username</th>
            <th className="px-4 py-2 font-medium">Tier</th>
            <th className="px-4 py-2 font-medium text-right">Leaderboard PnL</th>
            <th className="px-4 py-2 font-medium">Score</th>
          </tr>
        </thead>
        <tbody>
          {wallets.length === 0 && (
            <tr>
              <td colSpan={6} className="text-center text-zinc-600 py-16">
                No wallets — run <code className="text-zinc-400">polymarket top</code> or start the watcher.
              </td>
            </tr>
          )}
          {wallets.map((w) => (
            <>
              <tr
                key={w.address}
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
              </tr>
              {expanded === w.address && w.score_detail && (
                <tr key={`${w.address}-detail`} className="border-b border-zinc-900 bg-zinc-900/30">
                  <td colSpan={6}>
                    <ScoreBreakdown detail={w.score_detail} />
                  </td>
                </tr>
              )}
            </>
          ))}
        </tbody>
      </table>
    </div>
  );
}
