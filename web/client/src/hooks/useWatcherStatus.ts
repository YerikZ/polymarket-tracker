import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { WatcherStatus } from "../lib/types";

async function fetchStatus(): Promise<WatcherStatus> {
  const r = await fetch("/api/watcher/status");
  const data = await r.json();
  return {
    status: data.status ?? "stopped",
    mode: data.mode ?? "",
    wallets_tracked: data.wallets_tracked ?? 0,
    wallets_scored: data.wallets_scored ?? 0,
    last_signal_at: data.last_signal_at ?? null,
    copy_enabled: data.copy_enabled ?? false,
    target_wallets: Array.isArray(data.target_wallets) ? data.target_wallets : [],
    target_wallet_usernames: Array.isArray(data.target_wallet_usernames)
      ? data.target_wallet_usernames
      : [],
    target_mode: data.target_mode === "manual" ? "manual" : "auto",
    error: data.error ?? null,
  };
}

export function useWatcherStatus() {
  return useQuery<WatcherStatus>({
    queryKey: ["watcher-status"],
    queryFn: fetchStatus,
    refetchInterval: 2_000,
  });
}

export function useStartWatcher() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (skipRecalculation: boolean = true) =>
      fetch("/api/watcher/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skip_recalculation: skipRecalculation }),
      }).then((r) => r.json()),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watcher-status"] }),
  });
}

export function useStopWatcher() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => fetch("/api/watcher/stop", { method: "POST" }).then((r) => r.json()),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watcher-status"] }),
  });
}
