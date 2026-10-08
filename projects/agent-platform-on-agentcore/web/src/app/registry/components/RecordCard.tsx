"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Check, MessagesSquare } from "lucide-react";
import { isChattable, type RegistryRecordSummary } from "@/lib/registry";
import { cn } from "@/lib/utils";
import { StatusBadge } from "./shared";
import { TypeMarker } from "./types";

interface RecordCardProps {
  record: RegistryRecordSummary;
  isSelected: boolean;
  onOpenDetail: (recordId: string) => void;
  onChat: (record: RegistryRecordSummary) => void;
  /** Index in the grid, used to stagger the entrance animation. */
  index?: number;
}

/**
 * A registry record.
 *
 * Laid out as header / meta / action at 1rem padding — the previous version used
 * CardHeader+CardContent at 1.5rem each, which spent roughly a third of the
 * card's height on padding alone and pushed the action button off-screen in a
 * three-column grid.
 */
export function RecordCard({
  record,
  isSelected,
  onOpenDetail,
  onChat,
  index = 0,
}: RecordCardProps) {
  const chattable = isChattable(record);
  return (
    <Card
      style={{ "--i": index } as React.CSSProperties}
      className={cn(
        "group animate-rise flex flex-col overflow-hidden",
        "transition-[border-color,background-color] duration-150 ease-snap",
        isSelected
          ? "border-primary/60 bg-primary-tint/50"
          : "hover:border-border-strong hover:bg-muted/40"
      )}
    >
      {/* The whole upper block is the hit target for the detail panel, so the
          title and description don't each need their own affordance. */}
      <button
        type="button"
        onClick={() => onOpenDetail(record.record_id)}
        className="flex flex-1 flex-col gap-1 p-4 text-left outline-none focus-visible:bg-accent/60"
      >
        <div className="flex items-start gap-2">
          <TypeMarker type={record.descriptor_type} className="mt-px" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight">
            {record.name}
          </span>
          {isSelected && (
            <span className="flex shrink-0 items-center gap-1 text-xxs font-semibold uppercase tracking-wide text-primary">
              <Check className="size-3" />
              선택됨
            </span>
          )}
        </div>
        {record.description && (
          <p className="line-clamp-2 text-xs leading-normal text-muted-foreground">
            {record.description}
          </p>
        )}
      </button>

      <div className="flex flex-col gap-2.5 px-4 pb-4">
        <div className="flex flex-wrap items-center gap-1">
          {record.descriptor_type && (
            <Badge shape="code" variant="outline">
              {record.descriptor_type}
            </Badge>
          )}
          {record.harness_arn && (
            <Badge shape="tag" variant="default">
              Harness
            </Badge>
          )}
          <StatusBadge status={record.status} />
          {record.version && (
            <Badge shape="count" variant="secondary" className="ml-auto">
              v{record.version}
            </Badge>
          )}
        </div>
        {chattable && (
          <Button
            size="sm"
            variant={isSelected ? "outline" : "brand"}
            className="w-full"
            onClick={() => onChat(record)}
          >
            <MessagesSquare className="size-3.5" />
            {isSelected ? "채팅 열기" : "이 에이전트와 채팅"}
          </Button>
        )}
      </div>
    </Card>
  );
}
