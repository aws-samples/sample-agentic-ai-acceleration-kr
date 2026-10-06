"use client";

import React from "react";
import { CheckCircle2, AlertTriangle, XCircle } from "lucide-react";

import type { Verification } from "@/app/types/types";
import { cn } from "@/lib/utils";

/**
 * How the agent checked the numbers it just gave.
 *
 * When a question turns on an exact figure the agent writes several candidate
 * queries, executes each, and adopts the result most of them return. Agreement
 * below 1 means the candidates disagreed — the answer is the majority's, but the
 * figure deserves a second look. Surfacing that is the difference between a number
 * a reader has to trust and one they can weigh.
 *
 * Advisory, never a gate: the answer has already streamed and this does not
 * retract it. So the note is deliberately quiet — one line, muted, below the
 * answer — rather than a warning banner that would cry wolf on a PASS.
 */
const VERDICTS = {
  PASS: {
    Icon: CheckCircle2,
    tone: "text-success",
    label: "검증됨",
  },
  WARN: {
    Icon: AlertTriangle,
    tone: "text-warning",
    label: "검증 주의",
  },
  FAIL: {
    Icon: XCircle,
    tone: "text-destructive",
    label: "검증 실패",
  },
} as const;

interface Props {
  verification: Verification;
  className?: string;
}

export const VerificationNote = React.memo<Props>(function VerificationNote({
  verification,
  className,
}) {
  const verdict = (verification.verdict || "").toUpperCase();
  // An unrecognised verdict is shown as-is rather than dropped: the runtime may
  // add one, and a silent omission would hide a real signal.
  const style = VERDICTS[verdict as keyof typeof VERDICTS];
  const Icon = style?.Icon ?? CheckCircle2;

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md border border-border bg-background px-2.5 py-2",
        className
      )}
    >
      <Icon
        className={cn("mt-px size-3.5 shrink-0", style?.tone ?? "text-muted-foreground")}
        aria-hidden
      />
      <p className="text-xxs leading-relaxed text-muted-foreground">
        {/* The label, not the colour, carries the verdict — the icon is a
            reinforcement, so this reads correctly in monochrome too. */}
        <span className="font-medium text-foreground">
          {style?.label ?? verdict ?? "검증"}
        </span>
        {describe(verification)}
      </p>
    </div>
  );
});

/**
 * The evidence, in words.
 *
 * Each clause is omitted when its field is missing, so a runtime that sends only
 * a verdict still renders a clean sentence.
 */
function describe(v: Verification): string {
  const parts: string[] = [];

  if (typeof v.k === "number" && v.k > 1) {
    const valid =
      typeof v.n_valid === "number" && v.n_valid !== v.k
        ? `${v.k}개 중 ${v.n_valid}개 실행 성공`
        : `후보 ${v.k}개 실행`;
    parts.push(valid);
  }
  if (typeof v.agreement === "number") {
    // Agreement is a ratio; shown as a percentage because that is how a reader
    // judges "most of them agreed".
    parts.push(`합의도 ${Math.round(v.agreement * 100)}%`);
  }
  if (v.tie) {
    parts.push("결과가 갈렸습니다");
  }
  if (v.order_sensitive) {
    parts.push("정렬 순서에 민감한 질의");
  }

  return parts.length > 0 ? ` · ${parts.join(", ")}` : "";
}
