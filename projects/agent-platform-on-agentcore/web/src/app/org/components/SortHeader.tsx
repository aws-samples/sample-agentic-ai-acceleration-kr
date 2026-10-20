"use client";

import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { cn } from "@/lib/utils";
import type { UserSort, UserSortKey } from "./useUserSort";

/** A header cell that sorts. The faint ↕ on every sortable column says it can be clicked. */
export function SortTh({ label, sortKey, sort, onSort, right = false, className }: {
  label: string;
  sortKey: UserSortKey;
  sort: UserSort;
  onSort: (key: UserSortKey) => void;
  right?: boolean;
  className?: string;
}) {
  const active = sort.key === sortKey;
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th className={cn(right && "text-right", className)} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}>
      <button type="button" onClick={() => onSort(sortKey)}
        className={cn("inline-flex items-center gap-0.5 hover:text-foreground", active && "text-foreground")}>
        {label}<Icon className={cn("size-3", !active && "opacity-40")} />
      </button>
    </th>
  );
}
