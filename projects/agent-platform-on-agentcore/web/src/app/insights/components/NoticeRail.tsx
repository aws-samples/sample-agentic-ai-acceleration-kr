"use client";

import { useState, type ComponentType, type ReactNode } from "react";
import { ChevronDown, ChevronUp, type LucideIcon } from "lucide-react";

import { Notice } from "@/app/components/PageHeader";
import { cn } from "@/lib/utils";
import { sortNotices } from "@/app/insights/insightsFormat.mjs";

export interface RailNotice {
  id: string;
  tone: "error" | "warning" | "info";
  icon?: LucideIcon | ComponentType<{ className?: string }>;
  children: ReactNode;
}

/**
 * The page's notices, folded into one line until they are asked for.
 *
 * This dashboard can raise five at once — no usage table, no CloudWatch, no Cost
 * Explorer, a backfilled window, a layout that only saved locally — and each is a
 * full-width strip carrying two lines of Korean. Stacked, they filled the first
 * screen and pushed every chart below the fold: the caveats became the dashboard
 * and the numbers became the footnote.
 *
 * So one notice still renders as a plain strip, since there is nothing to fold,
 * and two or more collapse to a single row naming the most severe. Nothing is
 * dropped — the counter says how many are hidden, and one click shows them all.
 */
export function NoticeRail({ notices }: { notices: RailNotice[] }) {
  const [open, setOpen] = useState(false);
  const ordered = sortNotices(notices) as RailNotice[];

  if (ordered.length === 0) return null;
  if (ordered.length === 1) {
    const only = ordered[0];
    return (
      <Notice tone={only.tone} icon={only.icon}>
        {only.children}
      </Notice>
    );
  }

  // Same tones as `Notice`, restated because this strip is a button: it needs the
  // hit target and the caret, which Notice does not have.
  const tones = {
    error: "border-destructive/30 bg-destructive/[0.07] text-destructive",
    warning: "border-warning/30 bg-warning/[0.07] text-warning",
    info: "border-info/30 bg-info/[0.07] text-info",
  } as const;
  const worst = ordered[0];
  const Icon = worst.icon;

  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-xs",
          tones[worst.tone],
        )}
      >
        {Icon && <Icon className="size-3.5 shrink-0" />}
        {/* Truncated to one line: the full text is one click away, and the strip
            must not grow taller than the row it replaced. */}
        <span className="min-w-0 flex-1 truncate">{worst.children}</span>
        <span className="shrink-0 tabular-nums opacity-80">+{ordered.length - 1}</span>
        {open ? (
          <ChevronUp className="size-3.5 shrink-0" />
        ) : (
          <ChevronDown className="size-3.5 shrink-0" />
        )}
      </button>
      {open && (
        <div className="mt-2">
          {ordered.map((notice) => (
            <Notice key={notice.id} tone={notice.tone} icon={notice.icon}>
              {notice.children}
            </Notice>
          ))}
        </div>
      )}
    </div>
  );
}
