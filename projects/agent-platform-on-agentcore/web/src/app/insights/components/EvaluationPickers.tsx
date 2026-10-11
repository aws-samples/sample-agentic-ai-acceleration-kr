"use client";

import { useMemo, useState } from "react";
import { Check, ChevronsUpDown, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { Evaluator, RecordThread } from "@/lib/insights";
import { formatThreadTime, shortThreadId } from "@/app/insights/threadRows.mjs";
import { cn } from "@/lib/utils";

/**
 * Grouped multi-selects for the evaluation launcher (Insights → 품질 평가).
 *
 * Both lists it needs — the evaluator catalogue and the agent's threads — are
 * thirty-odd rows that read as one block when laid flat, and both are chosen
 * from, not read. So both use the same control: a trigger that states the
 * count, a searchable popover folded into groups with a per-group "all" toggle,
 * and the choice restated outside as removable chips with a visible border, so
 * it stays legible with the popover closed.
 *
 * The popover is `modal`. Inside a Sheet or drawer the dialog's scroll lock
 * swallows wheel events outside its own content, and the popover is portalled
 * outside it — the wheel scrolled nothing while the scrollbar still dragged. A
 * modal popover brings its own lock, which admits its own content.
 */

/** Chips shown before the rest folds into a "+n" count. */
const CHIP_LIMIT = 8;

interface PickerItem {
  id: string;
  label: string;
  /** Small neutral tag at the row's end, e.g. the level an evaluator scores at. */
  tag?: string;
  /** Muted detail after the label, e.g. a thread's time and turn count. */
  meta?: string;
  /** Hover title; the raw id when the label is a rendering of it. */
  title?: string;
}

interface PickerGroup {
  key: string;
  label: string;
  items: PickerItem[];
}

function GroupedPicker({
  groups,
  total,
  selected,
  onChange,
  triggerLabel,
  placeholder,
  emptyHint,
}: {
  groups: PickerGroup[];
  total: number;
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  triggerLabel: string;
  placeholder: string;
  emptyHint: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const q = query.trim().toLowerCase();
  const visible = groups
    .map((group) => ({
      ...group,
      items: group.items.filter(
        (item) =>
          !q ||
          item.label.toLowerCase().includes(q) ||
          (item.meta ?? "").toLowerCase().includes(q) ||
          item.id.toLowerCase().includes(q),
      ),
    }))
    .filter((group) => group.items.length > 0);

  const byId = useMemo(
    () => new Map(groups.flatMap((group) => group.items).map((item) => [item.id, item])),
    [groups],
  );
  const chosen = Array.from(selected)
    .map((id) => byId.get(id))
    .filter((item): item is PickerItem => Boolean(item));

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };
  const setAll = (ids: string[], on: boolean) => {
    const next = new Set(selected);
    ids.forEach((id) => (on ? next.add(id) : next.delete(id)));
    onChange(next);
  };

  return (
    <div className="space-y-1.5">
      <Popover open={open} onOpenChange={setOpen} modal>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-expanded={open}
            className="w-full justify-between font-normal"
          >
            <span>
              {triggerLabel}
              <span className="ml-1.5 tabular-nums text-muted-foreground">
                {selected.size}/{total}
              </span>
            </span>
            <ChevronsUpDown className="size-3.5 text-muted-foreground" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-80 p-0">
          <div className="border-b border-border p-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={placeholder}
              className="h-8 text-xs"
              autoFocus
            />
          </div>
          <div className="max-h-72 overflow-y-auto p-1">
            {visible.length === 0 && (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">
                일치하는 항목이 없습니다.
              </p>
            )}
            {visible.map((group) => {
              const ids = group.items.map((item) => item.id);
              const picked = ids.filter((id) => selected.has(id)).length;
              const allOn = picked === ids.length;
              return (
                <div key={group.key} className="py-1">
                  <div className="flex items-center justify-between px-2 py-1">
                    <span className="text-xxs font-semibold uppercase tracking-wider text-muted-foreground">
                      {group.label}
                      <span className="ml-1.5 font-normal normal-case tracking-normal tabular-nums">
                        {picked}/{ids.length}
                      </span>
                    </span>
                    <button
                      type="button"
                      className="text-xxs text-primary hover:underline"
                      onClick={() => setAll(ids, !allOn)}
                    >
                      {allOn ? "모두 해제" : "모두 선택"}
                    </button>
                  </div>
                  <ul>
                    {group.items.map((item) => {
                      const on = selected.has(item.id);
                      return (
                        <li key={item.id}>
                          <button
                            type="button"
                            role="option"
                            aria-selected={on}
                            onClick={() => toggle(item.id)}
                            title={item.title}
                            className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-xs hover:bg-muted/60"
                          >
                            <span
                              className={cn(
                                "flex size-3.5 shrink-0 items-center justify-center rounded-xs border",
                                on
                                  ? "border-primary bg-primary text-primary-foreground"
                                  : "border-border-strong",
                              )}
                            >
                              {on && <Check className="size-2.5" strokeWidth={3} />}
                            </span>
                            <span className="min-w-0 flex-1 truncate">
                              {item.label}
                              {item.meta && (
                                <span className="ml-1.5 text-xxs tabular-nums text-muted-foreground">
                                  {item.meta}
                                </span>
                              )}
                            </span>
                            {item.tag && (
                              <Badge shape="tag" variant="secondary">
                                {item.tag}
                              </Badge>
                            )}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })}
          </div>
        </PopoverContent>
      </Popover>

      {/* Restating twenty chips for "all of them" says less than one chip does,
          and past eight the list is a wall rather than a summary. */}
      {chosen.length > 0 && chosen.length === total && total > CHIP_LIMIT ? (
        <ul className="flex flex-wrap gap-1">
          <li>
            <Badge shape="chip" variant="default">전체 {total}개 선택</Badge>
          </li>
        </ul>
      ) : chosen.length > 0 ? (
        <ul className="flex flex-wrap gap-1">
          {chosen.slice(0, CHIP_LIMIT).map((item) => (
            <li key={item.id}>
              <Badge shape="chip" variant="default" title={item.title}>
                <span className="max-w-48 truncate">{item.label}</span>
                {item.tag && <span className="text-primary/60">· {item.tag}</span>}
                <button
                  type="button"
                  aria-label={`${item.label} 선택 해제`}
                  className="-mr-0.5 rounded-xs hover:bg-primary/15"
                  onClick={() => toggle(item.id)}
                >
                  <X className="size-3" />
                </button>
              </Badge>
            </li>
          ))}
          {chosen.length > CHIP_LIMIT && (
            <li>
              <Badge shape="chip" variant="outline" title="나머지는 목록에서 봅니다">
                +{chosen.length - CHIP_LIMIT}개
              </Badge>
            </li>
          )}
        </ul>
      ) : (
        <p className="text-xxs text-muted-foreground">{emptyHint}</p>
      )}
    </div>
  );
}

// --- evaluators -------------------------------------------------------------
//
// The catalogue's ids are `Builtin.Correctness`, `ThirdParty.DeepEval.Toxicity`
// and so on. The provider prefix is what people scan by, so it is the group and
// the last segment is the label; the level each one scores at (turn / session /
// tool call) rides along as the tag, since that decides which scores can be read
// side by side.

const FAMILY_ORDER = ["Builtin", "DeepEval", "AutoEval", "커스텀"] as const;
const LEVEL_ORDER = ["TRACE", "SESSION", "TOOL_CALL"];
const LEVEL_SHORT: Record<string, string> = {
  TRACE: "턴",
  SESSION: "세션",
  TOOL_CALL: "툴 호출",
};

function familyOf(evaluator: Evaluator): (typeof FAMILY_ORDER)[number] {
  const id = evaluator.evaluator_id;
  if (id.startsWith("Builtin.")) return "Builtin";
  if (id.startsWith("ThirdParty.DeepEval.")) return "DeepEval";
  if (id.startsWith("ThirdParty.AutoEval.")) return "AutoEval";
  return "커스텀";
}

function shortName(evaluator: Evaluator): string {
  const source = evaluator.name || evaluator.evaluator_id;
  if (familyOf(evaluator) === "커스텀") return source;
  return source.slice(source.lastIndexOf(".") + 1);
}

export function EvaluatorPicker({
  evaluators,
  selected,
  onChange,
}: {
  evaluators: Evaluator[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const groups = useMemo<PickerGroup[]>(
    () =>
      FAMILY_ORDER.map((family) => ({
        key: family,
        label: family,
        items: evaluators
          .filter((evaluator) => familyOf(evaluator) === family)
          .sort(
            (left, right) =>
              LEVEL_ORDER.indexOf(left.level) - LEVEL_ORDER.indexOf(right.level) ||
              shortName(left).localeCompare(shortName(right)),
          )
          .map((evaluator) => ({
            id: evaluator.evaluator_id,
            label: shortName(evaluator),
            tag: LEVEL_SHORT[evaluator.level] ?? evaluator.level,
            title: evaluator.evaluator_id,
          })),
      })).filter((group) => group.items.length > 0),
    [evaluators],
  );
  return (
    <GroupedPicker
      groups={groups}
      total={evaluators.length}
      selected={selected}
      onChange={onChange}
      triggerLabel="평가자 선택"
      placeholder="평가자 검색"
      emptyHint="평가자를 하나 이상 고르세요."
    />
  );
}

// --- threads ----------------------------------------------------------------
//
// A thread is recognised by what was asked, not by its id, so the label is its
// opening question (the id only when it had no readable text) and the id is the
// hover title. Grouped by recency the way the sidebar groups them.

const DAY_MS = 24 * 60 * 60 * 1000;

function recencyBucket(iso: string | null, now: number): { key: string; label: string; order: number } {
  const t = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return { key: "older", label: "이전", order: 3 };
  const days = Math.floor((now - t) / DAY_MS);
  if (days <= 0) return { key: "today", label: "오늘", order: 0 };
  if (days === 1) return { key: "yesterday", label: "어제", order: 1 };
  if (days < 7) return { key: "week", label: "이번 주", order: 2 };
  return { key: "older", label: "이전", order: 3 };
}

export function ThreadPicker({
  threads,
  selected,
  onChange,
}: {
  threads: RecordThread[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const groups = useMemo<PickerGroup[]>(() => {
    const now = Date.now();
    const buckets = new Map<string, PickerGroup & { order: number }>();
    for (const thread of threads) {
      const bucket = recencyBucket(thread.updated_at, now);
      const group =
        buckets.get(bucket.key) ??
        buckets.set(bucket.key, { key: bucket.key, label: bucket.label, order: bucket.order, items: [] }).get(bucket.key)!;
      group.items.push({
        id: thread.thread_id,
        label: thread.title || shortThreadId(thread.thread_id),
        meta: `${formatThreadTime(thread.updated_at)} · 턴 ${thread.turns.toLocaleString("ko-KR")}`,
        title: thread.thread_id,
      });
    }
    return Array.from(buckets.values()).sort((left, right) => left.order - right.order);
  }, [threads]);
  return (
    <GroupedPicker
      groups={groups}
      total={threads.length}
      selected={selected}
      onChange={onChange}
      triggerLabel="스레드 선택"
      placeholder="질문 내용으로 검색"
      emptyHint="평가할 스레드를 하나 이상 고르세요."
    />
  );
}
