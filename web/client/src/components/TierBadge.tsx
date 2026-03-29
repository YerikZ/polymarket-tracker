import { cn } from "../lib/utils";

const TIER_STYLES: Record<string, string> = {
  A: "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30",
  B: "bg-blue-500/20 text-blue-400 border border-blue-500/30",
  C: "bg-amber-500/20 text-amber-400 border border-amber-500/30",
  WATCH: "bg-zinc-500/20 text-zinc-400 border border-zinc-500/30",
  SKIP: "bg-red-500/20 text-red-400 border border-red-500/30",
  "?": "bg-zinc-800 text-zinc-500 border border-zinc-700",
};

export function TierBadge({ tier }: { tier: string | null }) {
  const t = tier ?? "?";
  return (
    <span
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 rounded text-xs font-semibold tracking-wider",
        TIER_STYLES[t] ?? TIER_STYLES["?"]
      )}
    >
      {t}
    </span>
  );
}
