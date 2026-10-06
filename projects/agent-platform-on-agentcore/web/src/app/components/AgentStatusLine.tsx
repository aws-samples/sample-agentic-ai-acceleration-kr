"use client";

import React, { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface AgentStatusLineProps {
  label: string;
  /** Coarse stage the runtime reports (think / sql / viz / …), if any. */
  phase?: string;
  /** Client clock reading from when the wait began. */
  startedAt: number;
  className?: string;
}

const formatElapsed = (ms: number) => {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
};

/**
 * What the agent is doing while it streams no text.
 *
 * An agent that delegates to sub-agents goes quiet for tens of seconds before
 * its first token, which is indistinguishable from a hang. The label says which
 * stage it is in, and the timer keeps moving between updates so the wait reads
 * as progress rather than a freeze.
 *
 * The interval lives here rather than in the stream hook on purpose: a timer in
 * shared stream state would re-render the whole message list every second.
 */
export const AgentStatusLine = React.memo<AgentStatusLineProps>(
  ({ label, phase, startedAt, className }) => {
    // Seeded from startedAt, not 0: on remount mid-wait the timer must resume
    // where the wait actually is.
    const [elapsed, setElapsed] = useState(() => Date.now() - startedAt);

    useEffect(() => {
      // Re-sync immediately: without this a remount shows a stale value for up
      // to a second before the first tick lands.
      setElapsed(Date.now() - startedAt);
      const id = setInterval(() => setElapsed(Date.now() - startedAt), 1000);
      return () => clearInterval(id);
    }, [startedAt]);

    return (
      <div
        className={cn(
          "flex w-fit items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground",
          className
        )}
        // A polite live region: screen readers announce the stage change without
        // interrupting, and the timer is left out of the announcement so it does
        // not re-read every second.
        role="status"
        aria-live="polite"
      >
        <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden="true" />
        <span className="min-w-0 break-words">{label}</span>
        <span
          className="shrink-0 font-mono text-xs tabular-nums opacity-60"
          aria-hidden="true"
        >
          {formatElapsed(elapsed)}
        </span>
        {phase ? <span className="sr-only">{`단계: ${phase}`}</span> : null}
      </div>
    );
  }
);

AgentStatusLine.displayName = "AgentStatusLine";
