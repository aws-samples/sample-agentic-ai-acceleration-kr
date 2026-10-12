"use client";

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { formatDelta } from "@/app/insights/insightsFormat.mjs";
import { Sparkline } from "./charts";

/**
 * One figure, stated exactly, with the two things that make a number readable
 * at a glance: how it moved against the earlier half of the window, and the
 * shape of its days. Shared by the Insights KPI row and a record's Usage tab so
 * a count reads the same wherever it appears.
 */

export type PeriodDelta = { pct: number; half: number; earlier: number; later: number } | null;

export function KpiDelta({ delta }: { delta: PeriodDelta }) {
  const pct = delta ? Math.round(delta.pct) : 0;
  const Icon = !delta || pct === 0 ? Minus : pct > 0 ? ArrowUpRight : ArrowDownRight;

  return (
    <span className="inline-flex items-center gap-1 text-xxs text-muted-foreground">
      <Icon className="size-3 shrink-0" />
      <span className="tabular-nums">{formatDelta(delta)}</span>
      {delta && <span className="opacity-70">· 앞 {delta.half}일 대비</span>}
    </span>
  );
}

export function KpiTile({
  label,
  value,
  hint,
  spark,
  delta,
}: {
  label: string;
  value: string;
  hint?: string;
  spark?: {
    data: Array<Record<string, unknown>>;
    dataKey: string;
    colorIndex: number;
  };
  delta?: PeriodDelta;
}) {
  return (
    <div className="flex h-full flex-col justify-between rounded-md border border-border p-3">
      <div>
        <div className="mb-1 text-xs text-muted-foreground">{label}</div>
        <div className="text-lg font-semibold">{value}</div>
        {delta !== undefined && (
          <div className="mt-0.5">
            <KpiDelta delta={delta} />
          </div>
        )}
        {hint && <div className="mt-1 text-xxs text-muted-foreground">{hint}</div>}
      </div>
      {spark && (
        <div className="mt-2">
          <Sparkline data={spark.data} dataKey={spark.dataKey} name={label} colorIndex={spark.colorIndex} />
        </div>
      )}
    </div>
  );
}

