import type { ScoreDetail } from "../lib/types";

interface Props {
  detail: ScoreDetail;
}

function ScoreBar({
  label,
  value,
  max,
  color,
}: {
  label: string;
  value: number;
  max: number;
  color: string;
}) {
  const pct = Math.min(100, (value / max) * 100);
  return (
    <div className="flex items-center gap-2">
      <span className="text-zinc-500 w-28 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-zinc-400 w-8 text-right">{value.toFixed(1)}</span>
      <span className="text-zinc-700 w-6 text-right text-xs">/{max}</span>
    </div>
  );
}

export function ScoreBreakdown({ detail }: Props) {
  return (
    <div className="p-3 space-y-4 text-xs">
      <div className="space-y-1.5">
        <p className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">
          Skill · {detail.skill.toFixed(1)}/45
        </p>
        <ScoreBar label="Calibrated Edge" value={detail.s1_calibrated_edge} max={20} color="bg-blue-500" />
        <ScoreBar label="Consistency" value={detail.s2_temporal_consistency} max={15} color="bg-blue-500" />
        <ScoreBar label="Independence" value={detail.s3_independence} max={10} color="bg-blue-500" />
      </div>
      <div className="space-y-1.5">
        <p className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">
          Reliability · {detail.reliability.toFixed(1)}/30
        </p>
        <ScoreBar label="Sample Breadth" value={detail.r1_sample_breadth} max={10} color="bg-violet-500" />
        <ScoreBar label="Sharpe" value={detail.r2_sharpe} max={10} color="bg-violet-500" />
        <ScoreBar label="Recency Trend" value={detail.r3_recency_trend} max={10} color="bg-violet-500" />
      </div>
      <div className="space-y-1.5">
        <p className="text-zinc-500 font-semibold uppercase tracking-wider text-[10px]">
          Copiability · {detail.copiability.toFixed(1)}/25
        </p>
        <ScoreBar label="Market Impact" value={detail.c1_market_impact} max={10} color="bg-amber-500" />
        <ScoreBar label="Signal Freshness" value={detail.c2_signal_freshness} max={10} color="bg-amber-500" />
        <ScoreBar label="Liquidity" value={detail.c3_liquidity} max={5} color="bg-amber-500" />
      </div>
      {detail.strong_categories?.length > 0 && (
        <div>
          <span className="text-zinc-500">Strong in: </span>
          <span className="text-zinc-300">{detail.strong_categories.join(", ")}</span>
        </div>
      )}
      <div className="text-zinc-600">
        {detail.trade_count} trades · {detail.unique_markets} markets
        {detail.insufficient_data && (
          <span className="text-amber-600 ml-2">(insufficient data)</span>
        )}
      </div>
    </div>
  );
}
