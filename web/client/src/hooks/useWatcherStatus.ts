import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { WatcherStatus } from "../lib/types";

async function fetchStatus(): Promise<WatcherStatus> {
  const r = await fetch("/api/watcher/status");
  return r.json();
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
    mutationFn: () => fetch("/api/watcher/start", { method: "POST" }).then((r) => r.json()),
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
