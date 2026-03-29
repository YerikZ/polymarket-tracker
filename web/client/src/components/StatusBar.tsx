import { Loader2, Play, Square, Wifi, WifiOff } from "lucide-react";
import { useWatcherStatus, useStartWatcher, useStopWatcher } from "../hooks/useWatcherStatus";
import { fmtUsd } from "../lib/utils";
import { useQuery } from "@tanstack/react-query";
import type { PnlSummary } from "../lib/types";

export function StatusBar() {
  const { data: status } = useWatcherStatus();
  const start = useStartWatcher();
  const stop = useStopWatcher();
  const { data: pnl } = useQuery<PnlSummary>({
    queryKey: ["pnl-summary"],
    queryFn: () => fetch("/api/pnl/summary").then((r) => r.json()),
    refetchInterval: 15_000,
  });

  const isRunning = status?.status === "running";
  const isStarting = status?.status === "starting";
  const busy = start.isPending || stop.isPending || isStarting;

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-zinc-900 border-b border-zinc-800 sticky top-0 z-10">
      {/* Left: logo + stream status */}
      <div className="flex items-center gap-3">
        <span className="text-sm font-semibold text-zinc-100 tracking-tight">
          POLYMARKET<span className="text-emerald-400">.</span>TRACKER
        </span>
        <div className="flex items-center gap-1.5 text-xs">
          {isRunning ? (
            <>
              <Wifi className="w-3.5 h-3.5 text-emerald-400" />
              <span className="text-emerald-400">
                {status.mode === "stream" ? "Stream" : "Poll"} · {status.wallets_tracked} wallets
              </span>
            </>
          ) : isStarting ? (
            <>
              <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin" />
              <span className="text-amber-400">Starting…</span>
            </>
          ) : status?.status === "error" ? (
            <>
              <WifiOff className="w-3.5 h-3.5 text-red-400" />
              <span className="text-red-400">Error</span>
            </>
          ) : (
            <>
              <WifiOff className="w-3.5 h-3.5 text-zinc-500" />
              <span className="text-zinc-500">Stopped</span>
            </>
          )}
        </div>
      </div>

      {/* Center: daily spend */}
      {pnl && (
        <div className="flex items-center gap-3 text-xs text-zinc-400">
          <span>
            Daily:{" "}
            <span className="text-zinc-200">
              {fmtUsd(pnl.spent_today, 2).replace("+", "")}
            </span>{" "}
            / {fmtUsd(pnl.daily_limit, 0).replace("+", "")}
          </span>
          <div className="w-24 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div
              className="h-full bg-emerald-500 rounded-full transition-all"
              style={{
                width: `${Math.min(100, (pnl.spent_today / pnl.daily_limit) * 100)}%`,
              }}
            />
          </div>
          <span className="text-zinc-500">
            {fmtUsd(pnl.remaining, 2).replace("+", "")} left
          </span>
        </div>
      )}

      {/* Right: start/stop */}
      <button
        onClick={() => (isRunning ? stop.mutate() : start.mutate())}
        disabled={busy}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold transition-colors ${
          isRunning
            ? "bg-red-500/15 text-red-400 hover:bg-red-500/25 border border-red-500/30"
            : "bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25 border border-emerald-500/30"
        } disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        {busy ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
        ) : isRunning ? (
          <Square className="w-3.5 h-3.5" />
        ) : (
          <Play className="w-3.5 h-3.5" />
        )}
        {isRunning ? "Stop" : "Start"}
      </button>
    </header>
  );
}
