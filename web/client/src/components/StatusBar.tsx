import { useState } from "react";
import { Loader2, Play, Square, Wifi, WifiOff } from "lucide-react";
import { useWatcherStatus, useStartWatcher, useStopWatcher } from "../hooks/useWatcherStatus";
import { fmtUsd } from "../lib/utils";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { PnlSummary, Settings } from "../lib/types";

export function StatusBar() {
  const qc = useQueryClient();
  const { data: status } = useWatcherStatus();
  const start = useStartWatcher();
  const stop = useStopWatcher();
  const [skipRecalc, setSkipRecalc] = useState(true);

  const { data: pnl } = useQuery<PnlSummary>({
    queryKey: ["pnl-summary"],
    queryFn: () => fetch("/api/pnl/summary").then((r) => r.json()),
    refetchOnWindowFocus: false,
    refetchInterval: false,
  });

  const { data: settings } = useQuery<Settings>({
    queryKey: ["settings"],
    queryFn: () => fetch("/api/settings").then((r) => r.json()),
    staleTime: 30_000,
  });

  const modeMutation = useMutation({
    mutationFn: (mode: "poll" | "stream") =>
      fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ watcher_mode: mode }),
      }).then((r) => r.json()),
    onSuccess: (data) => qc.setQueryData(["settings"], data),
  });

  const isRunning = status?.status === "running";
  const isStarting = status?.status === "starting";
  const busy = start.isPending || stop.isPending || isStarting;

  const watcherMode = settings?.watcher_mode ?? "poll";
  const isDryRun = settings?.copy_trading?.dry_run !== false;
  const targetSummary = status?.target_wallet_usernames?.length
    ? status.target_wallet_usernames.join(", ")
    : null;
  const targetLabel = status?.target_mode === "manual" ? "Manual" : "Auto";
  const dailyLimit = pnl?.daily_limit && pnl.daily_limit > 0 ? pnl.daily_limit : 1;

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
                {status.mode === "stream" ? "Stream" : "Poll"} · {status.wallets_tracked} tracked · {status.wallets_scored} scored
              </span>
              {status.target_wallets.length > 0 && (
                <span
                  className="px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 text-[10px] font-semibold tracking-wide"
                  title={targetSummary ?? undefined}
                >
                  {targetLabel} · {status.target_wallets.length} target{status.target_wallets.length === 1 ? "" : "s"}
                </span>
              )}
            </>
          ) : isStarting ? (
            <>
              <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin" />
              <span className="text-amber-400">Starting…</span>
            </>
          ) : status?.status === "error" ? (
            <>
              <WifiOff className="w-3.5 h-3.5 text-red-400" />
              <span className="text-red-400" title={status.error ?? undefined}>
                Error
              </span>
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
                width: `${Math.min(100, (pnl.spent_today / dailyLimit) * 100)}%`,
              }}
            />
          </div>
          <span className="text-zinc-500">
            {fmtUsd(pnl.remaining, 2).replace("+", "")} left
          </span>
        </div>
      )}

      {/* Right: dry-run badge + mode toggle + start/stop */}
      <div className="flex items-center gap-2">
        {/* Dry-run badge */}
        {settings && (
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border ${
              isDryRun
                ? "bg-amber-500/10 text-amber-400 border-amber-500/30"
                : "bg-red-500/10 text-red-400 border-red-500/30"
            }`}
          >
            {isDryRun ? "Simulated" : "Live"}
          </span>
        )}

        {/* Mode toggle */}
        <div className="flex items-center rounded border border-zinc-700 overflow-hidden text-[10px] font-semibold uppercase tracking-wider">
          {(["poll", "stream"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => watcherMode !== m && modeMutation.mutate(m)}
              disabled={modeMutation.isPending}
              className={`px-2.5 py-1 transition-colors ${
                watcherMode === m
                  ? "bg-zinc-700 text-zinc-100"
                  : "bg-transparent text-zinc-500 hover:text-zinc-300"
              } disabled:cursor-not-allowed`}
            >
              {m}
            </button>
          ))}
        </div>

        {/* Skip recalculation toggle — only relevant before starting */}
        {!isRunning && (
          <label className="flex items-center gap-1.5 cursor-pointer select-none text-[10px]">
            <div
              onClick={() => setSkipRecalc((v) => !v)}
              className={`relative w-7 h-4 rounded-full transition-colors ${
                skipRecalc ? "bg-zinc-600" : "bg-emerald-600"
              }`}
            >
              <span
                className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-transform ${
                  skipRecalc ? "translate-x-0.5" : "translate-x-3.5"
                }`}
              />
            </div>
            <span className={skipRecalc ? "text-zinc-500" : "text-emerald-400"}>
              {skipRecalc ? "Skip rescore" : "Rescore on start"}
            </span>
          </label>
        )}

        {/* Start/Stop */}
        <button
          onClick={() => (isRunning ? stop.mutate() : start.mutate(skipRecalc))}
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
      </div>
    </header>
  );
}
