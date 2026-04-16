import { Fragment, useState, useMemo } from "react";
import {
  ChevronDown,
  ChevronRight,
  RefreshCw,
  X,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  BarChart2,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Settings, Wallet } from "../lib/types";
import { TierBadge } from "./TierBadge";
import { ScoreBreakdown } from "./ScoreBreakdown";
import { WalletDetail } from "./WalletDetail";
import { fmtUsd } from "../lib/utils";

// ── Sort ─────────────────────────────────────────────────────────────────────

type SortField =
  | "rank"
  | "pnl"
  | "score"
  | "tier"
  | "skill"
  | "reliability"
  | "copiability";
type SortDir = "asc" | "desc";

const TIER_ORDER: Record<string, number> = {
  A: 0,
  B: 1,
  C: 2,
  WATCH: 3,
  SKIP: 4,
  "?": 5,
};

const DEFAULT_DIR: Record<SortField, SortDir> = {
  rank: "asc",
  pnl: "desc",
  score: "desc",
  tier: "asc",
  skill: "desc",
  reliability: "desc",
  copiability: "desc",
};

function sortWallets(
  wallets: Wallet[],
  field: SortField,
  dir: SortDir
): Wallet[] {
  return [...wallets].sort((a, b) => {
    let delta = 0;
    switch (field) {
      case "rank":
        delta = (a.rank ?? Infinity) - (b.rank ?? Infinity);
        break;
      case "pnl":
        delta = (a.pnl ?? 0) - (b.pnl ?? 0);
        break;
      case "score":
        delta = (a.score ?? -1) - (b.score ?? -1);
        break;
      case "tier":
        delta =
          (TIER_ORDER[a.tier ?? "?"] ?? 99) -
          (TIER_ORDER[b.tier ?? "?"] ?? 99);
        break;
      case "skill":
        delta =
          (a.score_detail?.skill ?? -1) - (b.score_detail?.skill ?? -1);
        break;
      case "reliability":
        delta =
          (a.score_detail?.reliability ?? -1) -
          (b.score_detail?.reliability ?? -1);
        break;
      case "copiability":
        delta =
          (a.score_detail?.copiability ?? -1) -
          (b.score_detail?.copiability ?? -1);
        break;
    }
    return dir === "asc" ? delta : -delta;
  });
}

// ── Filter ────────────────────────────────────────────────────────────────────

const ALL_TIERS = ["A", "B", "C", "WATCH", "SKIP", "?"] as const;
const ALL_CATEGORIES = [
  "Politics",
  "Sports",
  "Crypto",
  "Economics",
  "Science",
  "Culture",
  "Geopolitics",
] as const;

function filterWallets(
  wallets: Wallet[],
  tiers: string[],
  categories: string[],
  minScore: number | null
): Wallet[] {
  return wallets.filter((w) => {
    if (tiers.length > 0 && !tiers.includes(w.tier ?? "")) return false;
    if (minScore != null && (w.score ?? -1) < minScore) return false;
    if (categories.length > 0) {
      // Strip trailing ~ (volume-only flag) before matching
      const walletCats = (w.score_detail?.strong_categories ?? []).map((c) =>
        c.replace(/~$/, "")
      );
      if (!categories.some((cat) => walletCats.includes(cat))) return false;
    }
    return true;
  });
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SortIcon({
  active,
  dir,
}: {
  active: boolean;
  dir: SortDir;
}) {
  if (!active)
    return <ArrowUpDown className="w-3 h-3 ml-1 opacity-30 inline" />;
  return dir === "asc" ? (
    <ArrowUp className="w-3 h-3 ml-1 inline text-emerald-400" />
  ) : (
    <ArrowDown className="w-3 h-3 ml-1 inline text-emerald-400" />
  );
}

function PillButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2 py-0.5 rounded text-[10px] font-medium border transition-colors ${
        active
          ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/40"
          : "bg-zinc-900 text-zinc-500 border-zinc-800 hover:text-zinc-300 hover:border-zinc-600"
      }`}
    >
      {children}
    </button>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function WalletTable() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detailWallet, setDetailWallet] = useState<Wallet | null>(null);

  // Sort state
  const [sortField, setSortField] = useState<SortField>("rank");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Filter state
  const [selectedTiers, setSelectedTiers] = useState<string[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [minScoreInput, setMinScoreInput] = useState("");

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
  const manualTargetSet = new Set(manualTargets.map((v) => v.toLowerCase()));

  const saveTargets = useMutation({
    mutationFn: async (targets: string[]) => {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          copy_trading: {
            ...(settings?.copy_trading ?? {}),
            manual_target_wallets: targets,
          },
        }),
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

  const [fetchAllResult, setFetchAllResult] = useState<{ status: string; total: number } | null>(null);
  const fetchAllTrades = useMutation({
    mutationFn: async () => {
      setFetchAllResult(null);
      const r = await fetch("/api/wallets/fetch-all-trades", { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      return r.json() as Promise<{ status: string; total: number }>;
    },
    onSuccess: (data) => {
      setFetchAllResult(data);
      // Invalidate all wallet-detail queries so they refetch fresh data when opened
      qc.invalidateQueries({ queryKey: ["wallet-detail"] });
    },
  });

  // Sort helpers
  function handleSortColumn(field: SortField) {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(DEFAULT_DIR[field]);
    }
  }

  // Filter helpers
  function toggleTier(tier: string) {
    setSelectedTiers((prev) =>
      prev.includes(tier) ? prev.filter((t) => t !== tier) : [...prev, tier]
    );
  }
  function toggleCategory(cat: string) {
    setSelectedCategories((prev) =>
      prev.includes(cat) ? prev.filter((c) => c !== cat) : [...prev, cat]
    );
  }

  const hasFilters =
    selectedTiers.length > 0 ||
    selectedCategories.length > 0 ||
    minScoreInput !== "";

  function clearFilters() {
    setSelectedTiers([]);
    setSelectedCategories([]);
    setMinScoreInput("");
  }

  // Derived display list
  const displayedWallets = useMemo(() => {
    const minScore =
      minScoreInput !== "" ? parseFloat(minScoreInput) : null;
    const filtered = filterWallets(
      wallets,
      selectedTiers,
      selectedCategories,
      minScore != null && !isNaN(minScore) ? minScore : null
    );
    return sortWallets(filtered, sortField, sortDir);
  }, [wallets, sortField, sortDir, selectedTiers, selectedCategories, minScoreInput]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-zinc-600 text-sm">
        Loading wallets…
      </div>
    );
  }

  const thBtn =
    "cursor-pointer select-none hover:text-zinc-300 transition-colors";

  return (
    <div className="overflow-auto">
      {/* ── Toolbar ── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-900">
        <div className="text-xs text-zinc-500">
          {fetchAllResult
            ? `Background fetch started for ${fetchAllResult.total} wallets — check drawer to see updated data.`
            : fetchAllTrades.isPending
            ? "Starting background fetch…"
            : "Refresh leaderboard wallets and recompute scores."}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => fetchAllTrades.mutate()}
            disabled={fetchAllTrades.isPending || refreshWallets.isPending}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-zinc-700 bg-zinc-900 text-zinc-200 text-xs font-semibold hover:border-zinc-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <BarChart2 className="w-3.5 h-3.5" />
            {fetchAllTrades.isPending ? "Starting…" : "Fetch All Trades"}
          </button>
          <button
            type="button"
            onClick={() => refreshWallets.mutate()}
            disabled={refreshWallets.isPending || fetchAllTrades.isPending}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-zinc-700 bg-zinc-900 text-zinc-200 text-xs font-semibold hover:border-zinc-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 ${refreshWallets.isPending ? "animate-spin" : ""}`}
            />
            {refreshWallets.isPending ? "Refreshing…" : "Refresh Wallets"}
          </button>
        </div>
      </div>

      {/* ── Filter bar ── */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 border-b border-zinc-900 bg-zinc-950/60 text-xs">
        {/* Tier filter */}
        <div className="flex items-center gap-1.5">
          <span className="text-zinc-600 shrink-0">Tier</span>
          {ALL_TIERS.map((t) => (
            <PillButton
              key={t}
              active={selectedTiers.includes(t)}
              onClick={() => toggleTier(t)}
            >
              {t}
            </PillButton>
          ))}
        </div>

        {/* Category filter */}
        <div className="flex items-center gap-1.5">
          <span className="text-zinc-600 shrink-0">Category</span>
          {ALL_CATEGORIES.map((cat) => (
            <PillButton
              key={cat}
              active={selectedCategories.includes(cat)}
              onClick={() => toggleCategory(cat)}
            >
              {cat}
            </PillButton>
          ))}
        </div>

        {/* Min score */}
        <div className="flex items-center gap-1.5">
          <span className="text-zinc-600 shrink-0">Score ≥</span>
          <input
            type="number"
            min={0}
            max={100}
            value={minScoreInput}
            onChange={(e) => setMinScoreInput(e.target.value)}
            placeholder="0"
            className="w-14 px-2 py-0.5 rounded border border-zinc-800 bg-zinc-900 text-zinc-300 text-xs placeholder-zinc-700 focus:border-zinc-600 focus:outline-none"
          />
        </div>

        {/* Sub-score sort pills */}
        <div className="flex items-center gap-1.5 ml-auto">
          <span className="text-zinc-600 shrink-0">Sort</span>
          {(
            [
              ["skill", "Skill"],
              ["reliability", "Reliability"],
              ["copiability", "Copiability"],
            ] as [SortField, string][]
          ).map(([field, label]) => (
            <PillButton
              key={field}
              active={sortField === field}
              onClick={() => handleSortColumn(field)}
            >
              {label}
              {sortField === field && (
                <span className="ml-0.5">
                  {sortDir === "asc" ? "↑" : "↓"}
                </span>
              )}
            </PillButton>
          ))}
        </div>

        {/* Clear */}
        {hasFilters && (
          <button
            type="button"
            onClick={clearFilters}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded border border-zinc-700 text-zinc-500 hover:text-zinc-300 hover:border-zinc-500 text-[10px] transition-colors"
          >
            <X className="w-2.5 h-2.5" />
            Clear
          </button>
        )}
      </div>

      {/* ── Result count ── */}
      {(hasFilters || sortField !== "rank") && (
        <div className="px-4 py-1.5 text-[10px] text-zinc-600 border-b border-zinc-900">
          {displayedWallets.length} of {wallets.length} wallets
        </div>
      )}

      {/* ── Table ── */}
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-zinc-950 border-b border-zinc-800">
          <tr className="text-zinc-500 text-left">
            <th className="px-4 py-2 font-medium w-8" />
            <th
              className={`px-4 py-2 font-medium ${thBtn}`}
              onClick={() => handleSortColumn("rank")}
            >
              Rank
              <SortIcon active={sortField === "rank"} dir={sortDir} />
            </th>
            <th className="px-4 py-2 font-medium">Username</th>
            <th
              className={`px-4 py-2 font-medium ${thBtn}`}
              onClick={() => handleSortColumn("tier")}
            >
              Tier
              <SortIcon active={sortField === "tier"} dir={sortDir} />
            </th>
            <th
              className={`px-4 py-2 font-medium text-right ${thBtn}`}
              onClick={() => handleSortColumn("pnl")}
            >
              Leaderboard PnL
              <SortIcon active={sortField === "pnl"} dir={sortDir} />
            </th>
            <th
              className={`px-4 py-2 font-medium ${thBtn}`}
              onClick={() => handleSortColumn("score")}
            >
              Score
              <SortIcon active={sortField === "score"} dir={sortDir} />
            </th>
            <th className="px-4 py-2 font-medium">Manual Target</th>
            <th className="px-4 py-2 font-medium">Analytics</th>
          </tr>
        </thead>
        <tbody>
          {displayedWallets.length === 0 && (
            <tr>
              <td
                colSpan={8}
                className="text-center text-zinc-600 py-16"
              >
                {wallets.length === 0
                  ? <>No wallets — run <code className="text-zinc-400">polymarket top</code> or start the watcher.</>
                  : "No wallets match the current filters."}
              </td>
            </tr>
          )}
          {displayedWallets.map((w) => (
            <Fragment key={w.address}>
              <tr
                className="border-b border-zinc-900 hover:bg-zinc-900/50 cursor-pointer transition-colors"
                onClick={() =>
                  setExpanded(expanded === w.address ? null : w.address)
                }
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
                  <span
                    className={w.pnl >= 0 ? "text-emerald-400" : "text-red-400"}
                  >
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
                      const next = manualTargetSet.has(
                        w.address.toLowerCase()
                      )
                        ? manualTargets.filter(
                            (v) => v.toLowerCase() !== w.address.toLowerCase()
                          )
                        : [...manualTargets, w.address];
                      saveTargets.mutate(next);
                    }}
                    className={`px-2 py-1 rounded border text-[10px] font-semibold transition-colors ${
                      manualTargetSet.has(w.address.toLowerCase())
                        ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                        : "bg-zinc-900 text-zinc-500 border-zinc-800 hover:text-zinc-300"
                    }`}
                  >
                    {manualTargetSet.has(w.address.toLowerCase())
                      ? "Picked"
                      : "Pick"}
                  </button>
                </td>
                <td className="px-4 py-2">
                  <button
                    type="button"
                    title="View trade analytics"
                    onClick={(e) => {
                      e.stopPropagation();
                      setDetailWallet(w);
                    }}
                    className="p-1.5 rounded hover:bg-zinc-700 text-zinc-500 hover:text-emerald-400 transition-colors"
                  >
                    <BarChart2 className="w-3.5 h-3.5" />
                  </button>
                </td>
              </tr>
              {expanded === w.address && w.score_detail && (
                <tr className="border-b border-zinc-900 bg-zinc-900/30">
                  <td colSpan={8}>
                    <ScoreBreakdown detail={w.score_detail} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>

      <WalletDetail
        wallet={detailWallet}
        onClose={() => setDetailWallet(null)}
      />
    </div>
  );
}
