"use client";

import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ListFilter } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ThreadItem } from "@/app/hooks/useThreads";

export type StatusFilter = "all" | "idle" | "busy" | "interrupted" | "error";

const STATUS_COLORS: Record<ThreadItem["status"], string> = {
  idle: "bg-success",
  busy: "bg-info",
  interrupted: "bg-warning",
  error: "bg-destructive",
};

export function getThreadColor(status: ThreadItem["status"]): string {
  return STATUS_COLORS[status] ?? "bg-muted-foreground";
}

interface ThreadFilterContextValue {
  status: StatusFilter;
  setStatus: (status: StatusFilter) => void;
  /**
   * Threads with `interrupted` status among the pages loaded so far — a hint
   * that the filter has something to show, not a total.
   *
   * Published by the list because only the list fetches; the trigger lives in
   * the sidebar header, where there is no data.
   */
  interruptedCount: number;
  setInterruptedCount: (count: number) => void;
}

const ThreadFilterContext = createContext<ThreadFilterContextValue | null>(null);

/**
 * Holds the thread list's status filter.
 *
 * The state sits above the sidebar rather than inside the list because the two
 * halves of this control are in different places: the trigger is in the sidebar
 * header (the list gave up its own header row to it), the filtering happens in
 * the list.
 */
export function ThreadFilterProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [interruptedCount, setInterruptedCount] = useState(0);

  const value = useMemo(
    () => ({ status, setStatus, interruptedCount, setInterruptedCount }),
    [status, interruptedCount]
  );

  return (
    <ThreadFilterContext.Provider value={value}>
      {children}
    </ThreadFilterContext.Provider>
  );
}

export function useThreadFilter(): ThreadFilterContextValue {
  const ctx = useContext(ThreadFilterContext);
  if (!ctx) {
    throw new Error("useThreadFilter must be used within a ThreadFilterProvider");
  }
  return ctx;
}

function StatusFilterItem({
  status,
  label,
  badge,
}: {
  status: ThreadItem["status"];
  label: string;
  badge?: number;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={cn("inline-block size-2 rounded-full", getThreadColor(status))}
      />
      {label}
      {badge !== undefined && badge > 0 && (
        <span className="ml-1 inline-flex items-center justify-center rounded-full bg-destructive px-1.5 py-0.5 text-xxs font-bold leading-none text-destructive-foreground">
          {badge}
        </span>
      )}
    </span>
  );
}

/**
 * The status filter, as an icon button meant to sit beside the sidebar's
 * collapse toggle.
 *
 * It has to stay outside the list body: filtering to `error` with no matches
 * leaves only the empty state, and a trigger rendered among the rows would go
 * with them — with no way left to clear the filter.
 */
export function ThreadFilterButton() {
  const { status, setStatus, interruptedCount } = useThreadFilter();

  return (
    <Select
      value={status}
      onValueChange={(v) => setStatus(v as StatusFilter)}
    >
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <SelectTrigger
              aria-label="Filter threads by status"
              // Ghost, to match the collapse toggle next to it: the default
              // bordered field read as a stray input dropped in the header.
              className={cn(
                "h-8 w-8 justify-center border-transparent bg-transparent p-0",
                "text-muted-foreground hover:border-transparent hover:bg-accent hover:text-foreground",
                "[&>svg:last-child]:hidden"
              )}
            >
              {/* A div, not a span: SelectTrigger's base `[&>span]:line-clamp-1`
                  puts `overflow: hidden` on any span child, which clipped the
                  dot's top-right corner off and left a quarter-circle wedge. */}
              <div className="relative flex items-center">
                <ListFilter className="h-4 w-4" />
                {(status !== "all" || interruptedCount > 0) && (
                  <span
                    className={cn(
                      "absolute -right-1 -top-1 size-1.5 rounded-full",
                      status !== "all" ? "bg-primary" : "bg-warning"
                    )}
                  />
                )}
              </div>
            </SelectTrigger>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {status === "all" ? "All statuses" : `Filter: ${status}`}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <SelectContent align="end">
        <SelectItem value="all">All statuses</SelectItem>
        <SelectSeparator />
        <SelectGroup>
          <SelectLabel>Active</SelectLabel>
          <SelectItem value="idle">
            <StatusFilterItem
              status="idle"
              label="Idle"
            />
          </SelectItem>
          <SelectItem value="busy">
            <StatusFilterItem
              status="busy"
              label="Busy"
            />
          </SelectItem>
        </SelectGroup>
        <SelectSeparator />
        <SelectGroup>
          <SelectLabel>Attention</SelectLabel>
          <SelectItem value="interrupted">
            <StatusFilterItem
              status="interrupted"
              label="Interrupted"
              badge={interruptedCount}
            />
          </SelectItem>
          <SelectItem value="error">
            <StatusFilterItem
              status="error"
              label="Error"
            />
          </SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
