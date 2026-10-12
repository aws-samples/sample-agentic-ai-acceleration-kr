"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DescriptorType, MetadataField } from "@/lib/registry";
// Shared with the per-card TypeMarker so a type is drawn the same way in the
// toolbar and in the grid.
import { TYPE_OPTIONS } from "./types";

/** Cap the registry's search API applies; there is no pagination past it. */
const SEARCH_LIMIT = 20;

/** Radix Select rejects an empty-string item value, so "clear" uses a sentinel. */
const CLEAR = "__clear__";

interface RecordSearchProps {
  query: string;
  onQueryChange: (query: string) => void;
  selectedTypes: DescriptorType[];
  onTypesChange: (types: DescriptorType[]) => void;
  /** Filterable metadata fields; only enum fields get a control. */
  metadataFields: MetadataField[];
  /** Active metadata filters, keyed by field name. A missing key means no filter. */
  metaFilters: Record<string, string>;
  onMetaFiltersChange: (filters: Record<string, string>) => void;
  mode: "browse" | "search";
  resultCount: number;
  onClearQuery: () => void;
  loading: boolean;
  lastError: string | null;
}

export function RecordSearch({
  query,
  onQueryChange,
  selectedTypes,
  onTypesChange,
  metadataFields,
  metaFilters,
  onMetaFiltersChange,
  mode,
  resultCount,
  onClearQuery,
  loading,
  lastError,
}: RecordSearchProps) {
  const toggle = (type: DescriptorType) => {
    onTypesChange(
      selectedTypes.includes(type)
        ? selectedTypes.filter((t) => t !== type)
        : [...selectedTypes, type]
    );
  };

  const setMetaFilter = (key: string, value: string | undefined) => {
    const next = { ...metaFilters };
    if (value) next[key] = value;
    else delete next[key];
    onMetaFiltersChange(next);
  };

  const enumFields = metadataFields.filter((field) => field.kind === "enum");
  const activeMeta = Object.values(metaFilters).filter(Boolean).length;

  return (
    <>
      {/* Search and filters share one row: they are one control surface, and
          stacking them cost a third of the vertical space above the results. */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[16rem] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder="무엇이 필요한지 자연어로 적어보세요 — 예: PDF에서 표를 뽑아내는 툴"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
          />
        </div>

        {/* Segmented toggles, not buttons. As `default`/`outline` Buttons these
            read as four actions competing with Register; a joined group reads as
            one multi-select filter. */}
        <div className="flex shrink-0 items-center rounded-md border border-border bg-muted p-0.5">
          {TYPE_OPTIONS.map(({ type, label, icon: Icon }) => {
            const active = selectedTypes.includes(type);
            return (
              <button
                key={type}
                type="button"
                onClick={() => toggle(type)}
                aria-pressed={active}
                className={cn(
                  "inline-flex h-7 items-center gap-1.5 rounded-sm px-2.5 text-xs font-medium",
                  "transition-colors duration-150 ease-snap",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                  active
                    ? "bg-primary text-primary-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                <Icon className="size-3.5" />
                {label}
              </button>
            );
          })}
        </div>

        {enumFields.map((field) => (
          <Select
            key={field.name}
            value={metaFilters[field.name] ?? ""}
            onValueChange={(v) => setMetaFilter(field.name, v === CLEAR ? undefined : v)}
          >
            <SelectTrigger
              aria-label={`${field.name} 필터`}
              className="h-7 w-auto min-w-[7rem] shrink-0 gap-1.5 px-2.5 text-xs"
            >
              <SelectValue placeholder={field.name} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={CLEAR}>— 선택 안 함 —</SelectItem>
              {field.options?.map((option) => (
                <SelectItem key={option} value={option}>
                  {option}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ))}

        {(selectedTypes.length > 0 || activeMeta > 0) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              onTypesChange([]);
              onMetaFiltersChange({});
            }}
          >
            필터 해제
          </Button>
        )}
      </div>

      {/*
        The two paths return genuinely different sets, so say which one this is.
        Suppress all status claims while loading or after failure to avoid false
        statements about result set contents.
      */}
      {!loading && !lastError && (
        <div className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border pb-2 text-xs text-muted-foreground">
          {mode === "search" ? (
            <>
              <Badge shape="tag" variant="default">
                관련도순
              </Badge>
              <span>
                승인(Approved)된 레코드만 · 최대 {SEARCH_LIMIT}개 · 승인 직후에는
                색인 반영이 몇 초~몇 분 지연될 수 있습니다
              </span>
              {activeMeta > 0 && <span>· 메타데이터 필터 {activeMeta}개</span>}
            </>
          ) : (
            <>
              <span className="font-medium text-foreground tabular">
                {resultCount}개
              </span>
              <span>· 모든 상태 포함</span>
            </>
          )}
        </div>
      )}

      {!loading && !lastError && mode === "search" && resultCount >= SEARCH_LIMIT && (
        <p className="mb-3 text-xs text-warning">
          결과가 {SEARCH_LIMIT}개 상한에 도달했습니다. 가려진 항목이 있을 수
          있으니 검색어를 더 좁혀보세요.
        </p>
      )}

      {!loading && !lastError && mode === "search" && resultCount === 0 && (
        <div className="mb-3 text-sm text-muted-foreground">
          <p>검색 결과가 없습니다.</p>
          <p className="mt-1 text-xs">
            승인 대기 중이거나 색인이 아직 반영되지 않았을 수 있습니다.{" "}
            <button
              type="button"
              className="underline hover:text-foreground"
              onClick={onClearQuery}
            >
              전체 보기
            </button>
          </p>
        </div>
      )}
    </>
  );
}
