"use client";

import { useEffect, useMemo, useState, useRef, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";
import { format } from "date-fns";
import { Bot, Loader2, MessageSquare, RefreshCw, Trash2 } from "lucide-react";
import { useQueryState } from "nuqs";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Popover,
  PopoverContent,
} from "@/components/ui/popover";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";
import type { ThreadItem } from "@/app/hooks/useThreads";
import { useThreads } from "@/app/hooks/useThreads";
import { getThreadColor, useThreadFilter } from "@/app/components/ThreadFilter";

// Time only. Status used to pull `interrupted` threads into a "Requiring
// Attention" group of their own, but almost every interruption is the user's own
// doing — Stop, closing the tab, navigating away — so the group overstated what
// it held while yanking the thread out of the axis people actually search by.
// The row's warning dot and the status filter already surface it.
const GROUP_LABELS = {
  today: "Today",
  yesterday: "Yesterday",
  week: "This Week",
  older: "Older",
} as const;

function formatTime(date: Date, now = new Date()): string {
  const diff = now.getTime() - date.getTime();
  const days = Math.floor(diff / (1000 * 60 * 60 * 24));

  if (days === 0) return format(date, "HH:mm");
  if (days === 1) return "Yesterday";
  if (days < 7) return format(date, "EEEE");
  return format(date, "MM/dd");
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center p-8 text-center">
      <p className="text-xs font-medium text-destructive">스레드를 불러오지 못했습니다</p>
      <p className="mt-1 text-xs text-muted-foreground">{message}</p>
      {/* SWR retries on its own, but backs off exponentially (5s, 10s, 20s…),
          so without this the list can sit stale for a long time after the
          backend comes back. */}
      <Button
        variant="outline"
        size="sm"
        className="mt-3"
        onClick={onRetry}
      >
        <RefreshCw className="size-3.5" />
        다시 시도
      </Button>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-1.5 p-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-7 w-full"
        />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center p-6 text-center">
      <MessageSquare className="mb-2 size-6 text-muted-foreground/40" />
      <p className="text-xs text-muted-foreground">아직 대화가 없습니다</p>
    </div>
  );
}

interface ThreadListProps {
  /** Registers this list's refetch with the shell. */
  onMutateReady?: (mutate: () => void) => void;
}

export function ThreadList({ onMutateReady }: ThreadListProps) {
  const router = useRouter();
  const pathname = usePathname();
  const [threadIdParam, setThreadIdParam] = useQueryState("threadId");
  const { status: statusFilter, setInterruptedCount } = useThreadFilter();
  const [deletingThreadId, setDeletingThreadId] = useState<string | null>(null);
  const [openPopoverId, setOpenPopoverId] = useState<string | null>(null);
  const [popoverPosition, setPopoverPosition] = useState<{ x: number; y: number } | null>(null);
  const apiClient = useClient();

  // The list is in the sidebar on every route, but a thread is only *open* on
  // the chat route — elsewhere the param does not exist, so nothing is marked
  // current rather than marking a thread the page isn't showing.
  const isChatRoute = pathname === "/";
  const currentThreadId = isChatRoute ? threadIdParam : null;

  // From another route, selecting a thread has to navigate to the chat as well
  // as set the param; setting the param alone would hang `?threadId=` off e.g.
  // /registry, where nothing reads it.
  const openThread = useCallback(
    (threadId: string) => {
      if (isChatRoute) {
        void setThreadIdParam(threadId);
      } else {
        router.push(`/?threadId=${encodeURIComponent(threadId)}`);
      }
    },
    [isChatRoute, setThreadIdParam, router]
  );

  const threads = useThreads({
    status: statusFilter === "all" ? undefined : statusFilter,
    limit: 20,
  });

  const flattened = useMemo(() => {
    return threads.data?.flat() ?? [];
  }, [threads.data]);

  const isLoadingMore =
    threads.size > 0 && threads.data?.[threads.size - 1] == null;
  const isEmpty = threads.data?.at(0)?.length === 0;
  const isReachingEnd = isEmpty || (threads.data?.at(-1)?.length ?? 0) < 20;

  // Group threads by time
  const grouped = useMemo(() => {
    const now = new Date();
    const groups: Record<keyof typeof GROUP_LABELS, ThreadItem[]> = {
      today: [],
      yesterday: [],
      week: [],
      older: [],
    };

    flattened.forEach((thread) => {
      const diff = now.getTime() - thread.updatedAt.getTime();
      const days = Math.floor(diff / (1000 * 60 * 60 * 24));

      if (days === 0) {
        groups.today.push(thread);
      } else if (days === 1) {
        groups.yesterday.push(thread);
      } else if (days < 7) {
        groups.week.push(thread);
      } else {
        groups.older.push(thread);
      }
    });

    return groups;
  }, [flattened]);

  // Counts the pages loaded so far, not every thread on the server — it is a
  // hint that the interrupted filter has something to show, not a total. The
  // filter trigger it marks lives in the sidebar header, so the count is
  // published upward rather than read here.
  const interruptedCount = useMemo(() => {
    return flattened.filter((t) => t.status === "interrupted").length;
  }, [flattened]);

  useEffect(() => {
    setInterruptedCount(interruptedCount);
  }, [interruptedCount, setInterruptedCount]);

  // The list ends flush against the sidebar navigation, and a row sliced through
  // its middle by that edge read as the list scrolling *behind* the menu. The
  // fade is only drawn while something is actually below, so the last row is not
  // permanently dimmed once the list is scrolled to the end.
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [hasMoreBelow, setHasMoreBelow] = useState(false);

  const syncFade = useCallback(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    setHasMoreBelow(vp.scrollHeight - vp.clientHeight - vp.scrollTop > 4);
  }, []);

  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;

    syncFade();
    vp.addEventListener("scroll", syncFade, { passive: true });
    // The viewport keeps its size while its content grows (Load More, a slower
    // page arriving), so the content element has to be watched too.
    const observer = new ResizeObserver(syncFade);
    observer.observe(vp);
    if (vp.firstElementChild) observer.observe(vp.firstElementChild);

    return () => {
      vp.removeEventListener("scroll", syncFade);
      observer.disconnect();
    };
  }, [syncFade, flattened.length]);

  // Expose thread list revalidation to parent component
  // Use refs to create a stable callback that always calls the latest mutate function
  const onMutateReadyRef = useRef(onMutateReady);
  const mutateRef = useRef(threads.mutate);

  useEffect(() => {
    onMutateReadyRef.current = onMutateReady;
  }, [onMutateReady]);

  useEffect(() => {
    mutateRef.current = threads.mutate;
  }, [threads.mutate]);

  const mutateFn = useCallback(() => {
    mutateRef.current();
  }, []);

  useEffect(() => {
    onMutateReadyRef.current?.(mutateFn);
    // Only run once on mount to avoid infinite loops
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close popover when clicking outside
  useEffect(() => {
    if (!openPopoverId) return;

    const handleClickOutside = (e: MouseEvent) => {
      setOpenPopoverId(null);
      setPopoverPosition(null);
    };

    // Small delay to avoid immediate close
    const timeoutId = setTimeout(() => {
      document.addEventListener("click", handleClickOutside);
    }, 100);

    return () => {
      clearTimeout(timeoutId);
      document.removeEventListener("click", handleClickOutside);
    };
  }, [openPopoverId]);

  const handleContextMenu = useCallback(
    (thread: ThreadItem, e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setPopoverPosition({ x: e.clientX, y: e.clientY });
      setOpenPopoverId(thread.id);
    },
    []
  );

  const handleDeleteThread = useCallback(async (threadId: string) => {
    setOpenPopoverId(null);
    setPopoverPosition(null);
    setDeletingThreadId(threadId);
    
    try {
      await apiClient.deleteThread(threadId);
      
      // If the deleted thread is currently selected, navigate away
      if (currentThreadId === threadId) {
        await setThreadIdParam(null);
      }

      // Refresh the thread list
      threads.mutate();
    } catch (error) {
      console.error("Failed to delete thread:", error);
      alert("Failed to delete thread. Please try again.");
    } finally {
      setDeletingThreadId(null);
    }
  }, [apiClient, currentThreadId, setThreadIdParam, threads]);

  return (
    // No header of its own: a "Threads" label sat directly above the first
    // "Today" group header in the same style, saying nothing the groups and the
    // sidebar itself did not. The status filter it shared the row with moved to
    // the sidebar header, so the list starts at the top of the panel.
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Radix lays the viewport's wrapper out as a table, which sizes to
          content — long thread titles would widen the rows and clip instead of
          truncating. Forcing it to a block keeps rows at sidebar width. */}
      <div className="relative min-h-0 flex-1">
        <ScrollArea
          viewportRef={viewportRef}
          className="h-full [&_[data-slot=scroll-area-viewport]>div]:!block"
        >
          {threads.error && (
            <ErrorState
              message={threads.error.message}
              onRetry={() => void threads.mutate()}
            />
          )}

          {!threads.error && !threads.data && threads.isLoading && (
            <LoadingState />
          )}

          {!threads.error && !threads.isLoading && isEmpty && <EmptyState />}

          {!threads.error && !isEmpty && (
            <div className="box-border w-full max-w-full overflow-hidden px-2 pb-2 pt-1">
              {(
                Object.keys(GROUP_LABELS) as Array<keyof typeof GROUP_LABELS>
              ).map((group) => {
                const groupThreads = grouped[group];
                if (groupThreads.length === 0) return null;

                return (
                  <div
                    key={group}
                    className="mb-3"
                  >
                    <h4 className="m-0 px-2 pb-1 pt-2 caps-label-xs text-muted-foreground">
                      {GROUP_LABELS[group]}
                    </h4>
                    <div className="flex flex-col gap-0.5">
                      {groupThreads.map((thread) => (
                        <Popover
                          key={thread.id}
                          open={openPopoverId === thread.id}
                          onOpenChange={(open) => {
                            if (!open) {
                              setOpenPopoverId(null);
                              setPopoverPosition(null);
                            }
                          }}
                        >
                          <button
                            type="button"
                            onClick={() => openThread(thread.id)}
                            onContextMenu={(e) => handleContextMenu(thread, e)}
                            disabled={deletingThreadId === thread.id}
                            title={thread.title}
                            className={cn(
                              "flex w-full cursor-pointer flex-col rounded-md px-2 py-1.5 text-left transition-colors",
                              "hover:bg-accent",
                              deletingThreadId === thread.id &&
                                "cursor-not-allowed opacity-50",
                              currentThreadId === thread.id
                                ? "bg-primary-tint font-medium text-primary"
                                : "bg-transparent"
                            )}
                            aria-current={currentThreadId === thread.id}
                          >
                            {/* Title on one line: in a sidebar this narrow the
                                preview text truncated to near-uselessness anyway,
                                so the space goes to showing more threads. The agent
                                gets a second line — it is what tells two otherwise
                                similar conversations apart. */}
                            <span className="flex w-full items-center gap-2">
                              {deletingThreadId === thread.id ? (
                                <Loader2 className="h-3 w-3 flex-shrink-0 animate-spin text-muted-foreground" />
                              ) : (
                                // Idle is the resting state and needs no marker;
                                // a dot on every row would just be noise.
                                thread.status !== "idle" && (
                                  <span
                                    className={cn(
                                      "h-2 w-2 flex-shrink-0 rounded-full",
                                      getThreadColor(thread.status)
                                    )}
                                  />
                                )
                              )}
                              <span className="min-w-0 flex-1 truncate text-xs">
                                {thread.title}
                              </span>
                              <span className="flex-shrink-0 text-[10px] text-muted-foreground">
                                {formatTime(thread.updatedAt)}
                              </span>
                            </span>

                            {/* Which agent answered here. A label, not a grouping:
                                threads group by time because that is how people
                                look for a conversation, and adding an agent axis
                                would leave a near-empty group per agent. */}
                            {thread.agentName && (
                              <span className="mt-0.5 flex w-full items-center gap-1 text-[10px] text-muted-foreground">
                                <Bot className="size-2.5 flex-shrink-0" />
                                <span className="min-w-0 truncate">
                                  {thread.agentName}
                                </span>
                              </span>
                            )}
                          </button>
                          {popoverPosition && (
                            <PopoverContent
                              style={{
                                position: "fixed",
                                left: `${popoverPosition.x}px`,
                                top: `${popoverPosition.y}px`,
                                transform: "none",
                              }}
                              className="w-auto p-1"
                              onOpenAutoFocus={(e) => e.preventDefault()}
                            >
                              <div className="flex flex-col gap-0.5">
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="h-8 justify-start gap-2 px-2 text-sm"
                                  onClick={() => handleDeleteThread(thread.id)}
                                  disabled={deletingThreadId === thread.id}
                                >
                                  <Trash2 className="size-3.5 text-destructive" />
                                  <span>Delete</span>
                                </Button>
                              </div>
                            </PopoverContent>
                          )}
                        </Popover>
                      ))}
                    </div>
                  </div>
                );
              })}

            </div>
          )}
        </ScrollArea>
        <div
          aria-hidden
          className={cn(
            "pointer-events-none absolute inset-x-0 bottom-0 h-6",
            "bg-gradient-to-t from-sidebar to-transparent",
            "transition-opacity duration-150 ease-snap",
            hasMoreBelow ? "opacity-100" : "opacity-0"
          )}
        />
      </div>

      {/* Pinned, not the last row of the scroll content: inside the list it was
          only reachable after scrolling to the very bottom, which is exactly the
          position where a user has no idea more pages exist. */}
      {!threads.error && !isEmpty && !isReachingEnd && (
        <div className="flex flex-shrink-0 px-2 pb-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-full text-xs font-medium text-muted-foreground"
            onClick={() => threads.setSize(threads.size + 1)}
            disabled={isLoadingMore}
          >
            {isLoadingMore ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                Loading...
              </>
            ) : (
              "Load More"
            )}
          </Button>
        </div>
      )}
    </div>
  );
}
