import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import type { Position } from "../lib/types";
import { fmtDate, fmtUsd } from "../lib/utils";

interface Props {
  positions: Position[];
}

export function PnLChart({ positions }: Props) {
  // Build cumulative PnL series from closed positions sorted by open date
  const closed = positions
    .filter((p) => ["won", "lost", "closed"].includes(p.position_status))
    .sort((a, b) => new Date(a.opened_at).getTime() - new Date(b.opened_at).getTime());

  let cumulative = 0;
  const data = closed.map((p) => {
    const pnl =
      p.position_status === "won"
        ? p.spend_usdc  // rough: won back spend
        : p.position_status === "lost"
        ? -p.spend_usdc
        : (p.current_value_usdc ?? p.spend_usdc) - p.spend_usdc;
    cumulative += pnl;
    return {
      date: fmtDate(p.opened_at),
      pnl: Math.round(cumulative * 100) / 100,
    };
  });

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 text-zinc-600 text-xs">
        No closed positions yet
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
        <XAxis
          dataKey="date"
          tick={{ fill: "#52525b", fontSize: 10 }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fill: "#52525b", fontSize: 10 }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(v) => `$${v}`}
          width={48}
        />
        <ReferenceLine y={0} stroke="#3f3f46" strokeDasharray="3 3" />
        <Tooltip
          contentStyle={{
            background: "#18181b",
            border: "1px solid #27272a",
            borderRadius: 6,
            fontSize: 11,
            fontFamily: "monospace",
          }}
          formatter={(v: number) => [fmtUsd(v), "Cumulative PnL"]}
        />
        <Line
          type="monotone"
          dataKey="pnl"
          stroke="#10b981"
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 3, fill: "#10b981" }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
